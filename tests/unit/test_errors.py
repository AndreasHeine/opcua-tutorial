"""Toolkit error contract tests: every error carries something actionable."""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_toolkit.errors import (
    BrowsePathError,
    MethodCallError,
    NodeNotAccessibleError,
    OpcuaToolkitError,
    PartialFailureError,
    ServerUnhealthyError,
    describe_status,
    format_node_id,
)


def test_every_toolkit_error_derives_from_one_base() -> None:
    for error in (
        BrowsePathError,
        MethodCallError,
        NodeNotAccessibleError,
        PartialFailureError,
        ServerUnhealthyError,
    ):
        assert issubclass(error, OpcuaToolkitError)


def test_describe_status_shows_the_name_and_the_wire_value() -> None:
    described = describe_status(ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown))
    assert described.startswith("BadNodeIdUnknown")
    assert described.endswith("(0x80340000)")
    assert describe_status(None) == "Unknown"


def test_format_node_id_handles_numeric_string_and_missing_node_ids() -> None:
    assert format_node_id(ua.NodeId(2259, 0)) == "ns=0;i=2259"
    assert format_node_id(ua.NodeId("Temperature", 2)) == "ns=2;s=Temperature"
    assert format_node_id("ns=3;s=Machine1") == "ns=3;s=Machine1"
    assert format_node_id(None) == "<none>"


def test_browse_path_error_keeps_the_path_that_failed() -> None:
    error = BrowsePathError("missing segment", path=("ProductionLine", "Nope"))
    assert error.path == ("ProductionLine", "Nope")
    assert "missing segment" in str(error)


def test_node_not_accessible_error_keeps_the_node() -> None:
    node_id = ua.NodeId("Vibration", 2)
    error = NodeNotAccessibleError("read only", node_id=node_id)
    assert error.node_id == node_id


def test_method_call_error_points_at_the_bad_arguments() -> None:
    error = MethodCallError(
        "bad argument",
        status_code=ua.StatusCode(ua.StatusCodes.BadInvalidArgument),
        input_argument_results=[
            ua.StatusCode(ua.StatusCodes.BadTypeMismatch),
            ua.StatusCode(ua.StatusCodes.Good),
            ua.StatusCode(ua.StatusCodes.BadInvalidArgument),
        ],
    )
    assert error.bad_arguments == [0, 2]
    assert error.status_code is not None
    assert error.status_code.name == "BadInvalidArgument"


def test_partial_failure_error_hands_back_the_failed_items() -> None:
    error = PartialFailureError("2 of 24 failed", failures=["a", "b"])
    assert len(error.failures) == 2
    with pytest.raises(PartialFailureError):
        raise error
