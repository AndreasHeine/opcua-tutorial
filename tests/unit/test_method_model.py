"""Chapter 9 tests: method descriptors and call results.

A generic application can only call a method it discovered, so the descriptors
produced by :class:`~opcua_toolkit.MethodCatalog` have to be precise about
names, types and per-argument verdicts.
"""

from __future__ import annotations

import json

from asyncua import ua

from opcua_toolkit.methods import ArgumentSpec, CallResult, MethodDescriptor


def spec(name: str, type_name: str, *, value_rank: int = -1) -> ArgumentSpec:
    return ArgumentSpec(
        name=name,
        data_type=ua.NodeId(ua.ObjectIds.UInt32, 0),
        data_type_name=type_name,
        value_rank=value_rank,
        array_dimensions=(),
    )


def descriptor(
    *,
    browse_name: str = "StartLine",
    inputs: tuple[ArgumentSpec, ...] = (spec("Mode", "UInt32"),),
    outputs: tuple[ArgumentSpec, ...] = (spec("Accepted", "Boolean"),),
    executable: bool = True,
    user_executable: bool = True,
) -> MethodDescriptor:
    return MethodDescriptor(
        node_id=ua.NodeId("StartLine", 2),
        parent_node_id=ua.NodeId("Line1", 2),
        browse_name=browse_name,
        display_name=browse_name,
        inputs=inputs,
        outputs=outputs,
        executable=executable,
        user_executable=user_executable,
    )


def test_scalar_arguments_are_not_arrays_and_declared_arrays_are() -> None:
    assert not spec("Mode", "UInt32").is_array
    assert spec("Values", "UInt32", value_rank=0).is_array
    assert spec("Matrix", "UInt32", value_rank=1).is_array
    # -2 means "any rank", which is not a declared array either.
    assert not spec("Any", "UInt32", value_rank=-2).is_array


def test_argument_signature_shows_arrays_as_brackets() -> None:
    assert spec("speed", "UInt32").signature() == "speed: UInt32"
    assert spec("values", "UInt32", value_rank=0).signature() == "values: UInt32[]"


def test_method_signature_reads_like_the_server_declared_it() -> None:
    assert descriptor().signature() == "StartLine(Mode: UInt32) -> Accepted: Boolean"
    silent = descriptor(browse_name="StopLine", inputs=(), outputs=())
    assert silent.signature() == "StopLine() -> None"
    adding = descriptor(
        browse_name="AddValues",
        inputs=(spec("First", "Int32"), spec("Second", "Int32")),
        outputs=(spec("Sum", "Int32"),),
    )
    assert adding.signature() == "AddValues(First: Int32, Second: Int32) -> Sum: Int32"


def test_a_method_is_callable_only_when_both_executability_flags_are_set() -> None:
    assert descriptor().callable_by_user
    assert not descriptor(user_executable=False).callable_by_user
    assert not descriptor(executable=False).callable_by_user


def test_descriptor_as_dict_carries_the_signature_information_a_tool_needs() -> None:
    payload = descriptor().as_dict()
    assert payload["node_id"] == "ns=2;s=StartLine"
    assert payload["parent"] == "ns=2;s=Line1"
    assert payload["inputs"] == [
        {
            "name": "Mode",
            "data_type": "UInt32",
            "value_rank": -1,
            "array_dimensions": [],
            "description": "",
        }
    ]
    assert payload["outputs"][0]["name"] == "Accepted"


def build_result(
    *,
    method: MethodDescriptor | None = None,
    status: ua.StatusCode | None = None,
    outputs: dict[str, object] | None = None,
    argument_results: tuple[ua.StatusCode, ...] = (),
) -> CallResult:
    return CallResult(
        method=method if method is not None else descriptor(),
        status=status if status is not None else ua.StatusCode(ua.StatusCodes.Good),
        outputs=outputs if outputs is not None else {"Accepted": True},
        input_argument_results=argument_results,
        elapsed_ms=12.5,
    )


def test_a_good_call_exposes_its_outputs_by_declared_name() -> None:
    result = build_result()
    assert result.ok
    assert result.output_of("Accepted") is True
    assert result.output_of("Missing", default="fallback") == "fallback"


def test_a_bad_method_status_is_never_reported_as_success() -> None:
    # A rejected call can still carry outputs; "not ok" is what the caller must trust.
    result = build_result(
        status=ua.StatusCode(ua.StatusCodes.BadInvalidArgument),
        outputs={},
        argument_results=(ua.StatusCode(ua.StatusCodes.BadInvalidArgument),),
    )
    assert not result.ok
    assert result.output_of("Accepted") is None
    assert result.rejected_arguments == ["Mode"]


def test_rejected_arguments_are_named_from_the_declared_input_order() -> None:
    two_inputs = descriptor(
        inputs=(spec("First", "Int32"), spec("Second", "Int32")),
    )
    result = build_result(
        method=two_inputs,
        status=ua.StatusCode(ua.StatusCodes.BadInvalidArgument),
        outputs={},
        argument_results=(
            ua.StatusCode(ua.StatusCodes.Good),
            ua.StatusCode(ua.StatusCodes.BadTypeMismatch),
        ),
    )
    assert result.rejected_arguments == ["Second"]


def test_argument_results_without_matching_inputs_reject_nothing() -> None:
    # A sloppy server can return argument results for a method that declared none.
    result = build_result(
        method=descriptor(inputs=()),
        argument_results=(ua.StatusCode(ua.StatusCodes.BadTypeMismatch),),
    )
    assert result.rejected_arguments == []


def test_call_result_as_dict_is_json_safe_and_names_the_problems() -> None:
    payload = build_result(
        status=ua.StatusCode(ua.StatusCodes.BadInvalidArgument),
        outputs={"Accepted": False},
        argument_results=(ua.StatusCode(ua.StatusCodes.BadTypeMismatch),),
    ).as_dict()
    encoded = json.dumps(payload)
    assert "BadTypeMismatch" in encoded
    assert payload["rejected_arguments"] == ["Mode"]
    assert payload["outputs"]["Accepted"] == "False"
