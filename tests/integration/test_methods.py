"""Chapter 9 integration tests: discovering and calling methods generically.

The rule this chapter teaches is that a client must not know a method's NodeId or
its argument list in advance. Everything below is read from the server first,
then called by argument name.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest

from opcua_toolkit.errors import MethodNotFoundError
from opcua_toolkit.methods import MethodCatalog, MethodDescriptor, MethodInvoker

pytestmark = pytest.mark.integration

DEMO_METHODS = {"StartLine", "StopLine", "AddValues", "GetLineStatus", "AcknowledgeAlarm"}
PARENT = "ns=2;s=ProductionLine"


async def discover(client, limits):
    return await MethodCatalog(client, limits=limits).discover_under(PARENT, max_depth=3)


async def test_discovery_finds_the_methods_the_server_declared(client, limits) -> None:
    methods = await discover(client, limits)
    names = {method.browse_name for method in methods}
    assert names >= DEMO_METHODS
    assert all(method.callable_by_user for method in methods)
    assert all(method.executable and method.user_executable for method in methods)


async def test_discovery_reports_the_parent_object_that_owns_each_method(client, limits) -> None:
    methods = await discover(client, limits)
    assert {method.parent_node_id_text for method in methods} == {"ns=2;s=Line1"}
    assert all(method.node_id_text.startswith("ns=2;s=") for method in methods)


async def test_signatures_are_read_from_the_server_not_hardcoded(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    assert (
        by_name["AddValues"].signature() == "AddValues(First: Int32, Second: Int32) -> Sum: Int32"
    )
    assert by_name["StartLine"].signature() == "StartLine(Mode: UInt32) -> Accepted: Boolean"
    assert by_name["StopLine"].signature() == "StopLine() -> Accepted: Boolean"
    assert by_name["GetLineStatus"].signature() == "GetLineStatus() -> Status: String"
    assert (
        by_name["AcknowledgeAlarm"].signature()
        == "AcknowledgeAlarm(AlarmId: String) -> Accepted: Boolean"
    )


async def test_arguments_have_names_and_types_a_generic_client_can_use(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    start = by_name["StartLine"]
    assert [spec.name for spec in start.inputs] == ["Mode"]
    assert [spec.data_type_name for spec in start.inputs] == ["UInt32"]
    assert [spec.name for spec in start.outputs] == ["Accepted"]
    assert all(spec.data_type.Identifier for method in by_name.values() for spec in method.inputs)
    assert all(not spec.is_array for method in by_name.values() for spec in method.inputs)


async def test_a_method_is_called_with_named_arguments(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    invoker = MethodInvoker(client, limits=limits)

    started = await invoker.call(by_name["StartLine"], {"Mode": 4})
    assert started.ok
    assert started.output_of("Accepted") is True
    assert not started.rejected_arguments

    status = await invoker.call(by_name["GetLineStatus"], {})
    assert status.ok
    assert isinstance(status.output_of("Status"), str)


async def test_positional_arguments_follow_the_declared_order(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    result = await MethodInvoker(client, limits=limits).call(by_name["AddValues"], [20, 22])
    assert result.ok
    assert result.output_of("Sum") == 42


async def test_a_method_returns_what_the_server_computed(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    invoker = MethodInvoker(client, limits=limits)

    total = await invoker.call(by_name["AddValues"], {"First": -7, "Second": 49})
    assert total.output_of("Sum") == 42

    accepted = await invoker.call(by_name["AcknowledgeAlarm"], {"AlarmId": "A1"})
    assert accepted.output_of("Accepted") is True

    unknown = await invoker.call(by_name["AcknowledgeAlarm"], {"AlarmId": "not-an-alarm"})
    assert unknown.ok
    assert unknown.output_of("Accepted") is False


async def test_a_method_can_change_the_state_a_later_call_reads(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    invoker = MethodInvoker(client, limits=limits)

    await invoker.call(by_name["StartLine"], {"Mode": 2})
    running = await invoker.call(by_name["GetLineStatus"], {})
    assert "mode=2" in running.output_of("Status")

    await invoker.call(by_name["StopLine"], {})
    stopped = await invoker.call(by_name["GetLineStatus"], {})
    assert "mode=0" in stopped.output_of("Status")


async def test_argument_descriptions_arrive_with_the_declaration(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    assert by_name["StartLine"].inputs[0].description == "run mode to start with"
    assert by_name["AcknowledgeAlarm"].inputs[0].description.startswith("alarm instance")
    # A declared description is what lets a generic client explain an argument
    # instead of guessing from its name. One word names are the warning sign of a
    # server that never described anything.
    assert [spec.description for spec in by_name["AddValues"].inputs] == [
        "first addend",
        "second addend",
    ]
    assert by_name["AddValues"].outputs[0].description == "sum of the two addends"


async def test_arguments_are_sent_with_the_type_the_server_declared(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    # Mode is UInt32: a Python int would otherwise become Int64 and be rejected.
    result = await MethodInvoker(client, limits=limits).call(by_name["StartLine"], {"Mode": 65_535})
    assert result.ok
    assert result.output_of("Accepted") is True


async def test_a_missing_argument_is_refused_before_anything_is_sent(
    client, limits, service_calls
) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    service_calls["call"] = 0
    with pytest.raises(MethodNotFoundError, match="missing argument 'Second'"):
        await MethodInvoker(client, limits=limits).call(by_name["AddValues"], {"First": 1})
    assert service_calls["call"] == 0


async def test_an_argument_name_the_method_does_not_have_is_refused(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    with pytest.raises(MethodNotFoundError, match="does not accept"):
        await MethodInvoker(client, limits=limits).call(
            by_name["AddValues"], {"speed": 1, "Second": 2}
        )


async def test_a_value_that_cannot_be_converted_is_reported_before_the_call(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    with pytest.raises(MethodNotFoundError, match="not an integer"):
        await MethodInvoker(client, limits=limits).call(
            by_name["AddValues"], {"First": "hot", "Second": 2}
        )


async def test_the_wrong_number_of_positional_arguments_is_refused(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    with pytest.raises(MethodNotFoundError, match="expects 2 positional arguments, got 3"):
        await MethodInvoker(client, limits=limits).call(by_name["AddValues"], [1, 2, 3])


async def test_calls_are_chunked_to_max_nodes_per_method_call(
    client, limits, service_calls
) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    invoker = MethodInvoker(client, limits=limits)
    calls: list[tuple[MethodDescriptor, dict[str, Any]]] = [
        (by_name["GetLineStatus"], {}) for _ in range(7)
    ]

    service_calls["call"] = 0
    results = await invoker.call_many(calls)

    expected = math.ceil(len(calls) / limits.max_nodes_per_method_call)
    assert service_calls["call"] == expected
    assert len(results) == len(calls)
    assert all(result.ok for result in results)


async def test_a_bad_call_can_be_returned_instead_of_raising(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    invoker = MethodInvoker(client, limits=limits, raise_on_error=False)
    result = await invoker.call(by_name["GetLineStatus"], {})
    assert result.ok
    assert result.elapsed_ms >= 0.0
    assert isinstance(result.input_argument_results, tuple)


async def test_call_results_are_json_ready(client, limits) -> None:
    by_name = {method.browse_name: method for method in await discover(client, limits)}
    result = await MethodInvoker(client, limits=limits).call(
        by_name["AddValues"], {"First": 1, "Second": 2}
    )
    payload = json.dumps(result.as_dict())
    assert payload
    assert result.as_dict()["method"] == "AddValues"
    assert "Good" in result.as_dict()["status"]


async def test_discovery_finds_nothing_under_a_leaf_variable(client, limits) -> None:
    methods = await MethodCatalog(client, limits=limits).discover("ns=2;s=Temperature")
    assert methods == []
