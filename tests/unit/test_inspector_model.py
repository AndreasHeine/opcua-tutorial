"""Chapter 8 and Chapter 15 tests: access level masks and node descriptors.

AccessLevel is a bit mask on a Byte. The beginner bug is testing ``access & 2``
by hand and getting the wrong bit. These tests pin the decoding so that
"can this node be written?" is one question with one answer.
"""

from __future__ import annotations

import json

from asyncua import ua

from opcua_toolkit.inspector import AccessRights, NodeDescriptor
from opcua_toolkit.values import format_value

READ = 1 << int(ua.AccessLevel.CurrentRead.value)
WRITE = 1 << int(ua.AccessLevel.CurrentWrite.value)
HISTORY_READ = 1 << int(ua.AccessLevel.HistoryRead.value)
HISTORY_WRITE = 1 << int(ua.AccessLevel.HistoryWrite.value)


def test_read_only_access_has_exactly_the_read_bit() -> None:
    access = AccessRights(READ)
    assert access.current_read
    assert not access.current_write
    assert not access.history_read
    assert access.describe() == "CurrentRead"


def test_a_writable_historized_variable_carries_three_bits() -> None:
    access = AccessRights(READ | WRITE | HISTORY_READ)
    assert access.current_read
    assert access.current_write
    assert access.history_read
    assert not access.history_write
    assert access.describe() == "CurrentRead|CurrentWrite|HistoryRead"


def test_no_bits_means_no_access_at_all() -> None:
    access = AccessRights()
    assert access.describe() == "none"
    assert not access.current_read
    assert not access.current_write
    assert not access.timestamp_write
    assert not access.status_write
    assert not access.semantic_change


def test_the_rarer_bits_are_decoded_as_their_own_flags() -> None:
    access = AccessRights(HISTORY_WRITE | (1 << int(ua.AccessLevel.StatusWrite.value)))
    assert access.history_write
    assert access.status_write
    assert not access.current_write
    assert not access.timestamp_write


def test_access_levels_are_bit_positions_not_bit_masks() -> None:
    # This is the trap the tutorial warns about: enum values are positions (0,1,2...).
    assert ua.AccessLevel.CurrentRead.value == 0
    assert ua.AccessLevel.CurrentWrite.value == 1
    assert ua.AccessLevel.CurrentWrite.mask == WRITE
    assert WRITE != READ
    assert (READ & WRITE) == 0


def test_access_as_dict_is_json_safe() -> None:
    payload = AccessRights(READ | WRITE).as_dict()
    json.dumps(payload)
    assert payload["currentread"] is True
    assert payload["historyread"] is False


def make_descriptor(
    *,
    identifier: str = "Temperature",
    access: int = READ | WRITE,
    user_access: int | None = None,
    data_type: ua.NodeId | None = None,
    value_rank: int = -1,
    resolved_data_type: str | None = None,
    value: object = None,
) -> NodeDescriptor:
    return NodeDescriptor(
        node_id=ua.NodeId(identifier, 2),
        node_class=ua.NodeClass.Variable,
        browse_name=ua.QualifiedName(identifier, 2),
        display_name=identifier,
        description="",
        data_type=data_type if data_type is not None else ua.NodeId(ua.ObjectIds.Double, 0),
        value_rank=value_rank,
        array_dimensions=(),
        access_level=AccessRights(access),
        user_access_level=AccessRights(user_access if user_access is not None else access),
        minimum_sampling_interval=250.0,
        historizing=False,
        status=None,
        type_definition="BaseDataVariableType",
        resolved_data_type=resolved_data_type,
        value=value,
    )


def test_writable_requires_both_the_server_bit_and_the_user_bit() -> None:
    writable = make_descriptor()
    assert writable.writable
    assert writable.readable

    # A server may allow writing while the logged in user may not.
    user_blocked = make_descriptor(user_access=READ)
    assert user_blocked.access_level.current_write
    assert not user_blocked.writable

    server_blocked = make_descriptor(access=READ, user_access=READ | WRITE)
    assert not server_blocked.writable


def test_array_variables_are_recognised_from_value_rank() -> None:
    assert make_descriptor(value_rank=0).is_array
    assert make_descriptor(value_rank=1).is_array
    assert not make_descriptor(value_rank=-1).is_array
    assert not make_descriptor(value_rank=-2).is_array


def test_data_type_text_prefers_the_resolved_name() -> None:
    assert make_descriptor().data_type_text == "Double"
    assert make_descriptor(resolved_data_type="MachineTemperatureType").data_type_text == (
        "MachineTemperatureType"
    )
    assert make_descriptor(data_type=ua.NodeId(4213, 3)).data_type_text == "ns=3;i=4213"


def test_descriptor_as_dict_is_json_safe_and_explains_itself() -> None:
    payload = make_descriptor(value=[1.0, 2.0], value_rank=0).as_dict()
    encoded = json.dumps(payload)
    assert "ns=2;s=Temperature" in encoded
    assert payload["access_level"] == "CurrentRead|CurrentWrite"
    assert payload["status"] == "Good"
    assert payload["value"].startswith("[1.0, 2.0]")
    assert payload["value"] == format_value([1.0, 2.0])


def test_summary_line_names_the_node_class_and_the_user_access() -> None:
    line = make_descriptor(access=READ, user_access=READ | HISTORY_READ).summary_line()
    assert "ns=2;s=Temperature" in line
    assert "Variable" in line
    assert "CurrentRead|HistoryRead" in line
