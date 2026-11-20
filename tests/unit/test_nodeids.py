"""Chapter 4 tests: NodeId conversion at the toolkit boundary."""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_toolkit.errors import ConfigurationError, format_node_id
from opcua_toolkit.nodeids import node_ids_equal, to_nodeid, to_nodeid_list


def test_to_nodeid_parses_the_forms_a_human_types() -> None:
    assert to_nodeid("ns=2;s=Temperature") == ua.NodeId("Temperature", 2)
    guid = to_nodeid("ns=1;g=08a9a1a4-2f4c-4f9c-9a4d-6a6b1c1d1e1f")
    assert guid.NamespaceIndex == 1
    assert guid.NodeIdType is ua.NodeIdType.Guid
    assert to_nodeid("i=2259") == ua.NodeId(2259, 0)
    assert to_nodeid("2259") == ua.NodeId(2259, 0)


def test_to_nodeid_accepts_ints_and_nodeids_unchanged() -> None:
    assert to_nodeid(2259) == ua.NodeId(2259, 0)
    already = ua.NodeId("Setpoint", 4)
    assert to_nodeid(already) is already


def test_to_nodeid_strips_accidental_whitespace() -> None:
    assert to_nodeid("  ns=3;s=Motor.Rpm  ") == ua.NodeId("Motor.Rpm", 3)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "Temperature", "ns=2;Temperature", "ns=x;s=Temperature", None, 3.5, ["i=1"]],
)
def test_to_nodeid_rejects_what_is_not_a_node_id(value: object) -> None:
    # No type checker can catch these: NodeIdLike deliberately accepts whatever a
    # config file or a JSON payload might contain, so the toolkit's own guard is the
    # check under test.
    with pytest.raises(ConfigurationError):
        to_nodeid(value)


def test_to_nodeid_list_keeps_the_callers_order() -> None:
    converted = to_nodeid_list(["ns=2;s=B", "i=85", 7, ua.NodeId("A", 2)])
    assert [format_node_id(node) for node in converted] == [
        "ns=2;s=B",
        "ns=0;i=85",
        "ns=0;i=7",
        "ns=2;s=A",
    ]


def test_node_ids_equal_understands_that_i_2259_and_2259_are_the_same_node() -> None:
    assert node_ids_equal("i=2259", 2259)
    assert node_ids_equal("ns=0;i=2259", "i=2259")
    assert not node_ids_equal("ns=2;s=Temperature", "ns=3;s=Temperature")
    assert node_ids_equal("ns=2;s=Temperature", ua.NodeId("Temperature", 2))
