"""Chapter 5, 6 and 7 integration tests: reading in batches is a measurable win.

The counters wrap the client's service layer, so "3 requests instead of 24" is a
fact the test observes on the wire, not a number the implementation prints.
"""

from __future__ import annotations

import math

import pytest
from asyncua import ua

from opcua_toolkit.errors import format_node_id
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.read import BulkReader

pytestmark = pytest.mark.integration


async def test_a_bulk_read_sends_one_request_per_chunk(
    client, limits, demo_server, service_calls
) -> None:
    nodes = demo_server.tag_ids()
    expected_requests = math.ceil(len(nodes) / limits.max_nodes_per_read)

    service_calls["read_attributes"] = 0
    _results, stats = await BulkReader(client, limits=limits).read_values(nodes)

    assert stats.requests == expected_requests
    assert service_calls["read_attributes"] == expected_requests
    assert stats.requested == len(nodes)
    assert stats.succeeded == len(nodes)
    assert stats.failed == 0
    assert stats.chunk_size <= limits.max_nodes_per_read


async def test_bulk_reading_is_measurably_cheaper_than_one_read_per_tag(
    client, limits, demo_server, service_calls
) -> None:
    nodes = demo_server.tag_ids()
    reader = BulkReader(client, limits=limits)

    _, batched = await reader.read_values(nodes)
    service_calls.clear()
    _, baseline = await reader.read_one_by_one(nodes)

    # A tag at a time means one Read request per tag - but it goes through
    # Node.read_data_value, i.e. the session's read(), not read_attributes().
    # Counting only one of them is how a measurement quietly reports 0.
    total_reads = service_calls["read"] + service_calls["read_attributes"]
    assert total_reads == len(nodes)
    assert baseline.requests == len(nodes)
    assert batched.requests < baseline.requests
    assert batched.nodes_per_request > 1.0
    assert baseline.nodes_per_request == 1.0


async def test_results_come_back_in_the_order_the_caller_asks_for(
    client, limits, demo_server
) -> None:
    nodes = demo_server.tag_ids()
    results, _ = await BulkReader(client, limits=limits).read_values(nodes)
    assert [result.node_id_text for result in results] == [
        format_node_id(node_id) for node_id in nodes
    ]
    assert len({result.node_id_text for result in results}) == len(nodes)


async def test_values_are_unwrapped_and_typed(client, limits, demo_server) -> None:
    results, _ = await BulkReader(client, limits=limits).read_values(
        ["ns=2;s=Temperature", "ns=2;s=RecipeName", "ns=2;s=MotorRpm"]
    )
    by_name = {result.node_id_text: result for result in results}
    assert isinstance(by_name["ns=2;s=Temperature"].value, float)
    assert isinstance(by_name["ns=2;s=RecipeName"].value, str)
    assert isinstance(by_name["ns=2;s=MotorRpm"].value, int)
    assert all(result.ok for result in results)


async def test_an_array_value_arrives_as_a_list(client, limits) -> None:
    results, _ = await BulkReader(client, limits=limits).read_values(["ns=2;s=SampleArray"])
    assert results[0].ok
    assert isinstance(results[0].value, list)
    assert len(results[0].value) > 1


async def test_a_missing_node_never_raises_and_never_disappears(client, limits) -> None:
    results, stats = await BulkReader(client, limits=limits).read_values(
        ["ns=2;s=Temperature", "ns=2;s=NoSuchTag"]
    )
    assert len(results) == 2
    assert stats.succeeded == 1
    assert stats.failed == 1
    missing = results[1]
    assert missing.status.name == "BadNodeIdUnknown"
    assert missing.value is None
    assert missing.node_id_text == "ns=2;s=NoSuchTag"


async def test_timestamps_arrive_with_the_value(client, limits, demo_server) -> None:
    results, _ = await BulkReader(client, limits=limits).read_values(["ns=2;s=Temperature"])
    assert results[0].server_timestamp is not None
    assert results[0].source_timestamp is not None


async def test_reading_an_attribute_other_than_value_works_the_same_way(
    client, limits, demo_server
) -> None:
    nodes = demo_server.tag_ids()
    results, stats = await BulkReader(client, limits=limits).read_attribute(
        nodes, ua.AttributeIds.DataType
    )
    assert stats.failed == 0
    assert all(result.status.is_good() for result in results)
    assert {result.value.Identifier for result in results} <= {
        ua.ObjectIds.Double,
        ua.ObjectIds.Float,
        ua.ObjectIds.Int32,
        ua.ObjectIds.UInt32,
        ua.ObjectIds.String,
        ua.ObjectIds.Boolean,
        ua.ObjectIds.Byte,
    }


async def test_values_of_returns_only_the_nodes_that_actually_read(
    client, limits, demo_server
) -> None:
    values = await BulkReader(client, limits=limits).values_of(
        [*demo_server.tag_ids(), "ns=2;s=NoSuchTag"]
    )
    assert len(values) == len(demo_server.tag_ids())
    assert "ns=2;s=NoSuchTag" not in values


async def test_chunking_follows_the_limit_the_caller_declared(
    client, demo_server, service_calls
) -> None:
    nodes = demo_server.tag_ids()
    strict = OperationLimits(max_nodes_per_read=7)
    service_calls["read_attributes"] = 0
    _, stats = await BulkReader(client, limits=strict).read_values(nodes)
    assert stats.requests == math.ceil(len(nodes) / 7)
    assert service_calls["read_attributes"] == stats.requests
