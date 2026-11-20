"""Chapter 15 integration tests: inspecting a node is reading its metadata.

``AccessLevel`` and ``UserAccessLevel`` decide what a client may do, ``DataType``
and ``ValueRank`` decide what a value has to look like, and
``MinimumSamplingInterval`` decides how fast a subscription can be. Getting these
right is what makes Chapter 8's writes and Chapter 10's subscriptions work
without guessing.
"""

from __future__ import annotations

import json
import math

import pytest
from asyncua import ua

from opcua_toolkit.errors import NodeNotAccessibleError
from opcua_toolkit.inspector import IDENTITY_ATTRIBUTES, NodeInspector

pytestmark = pytest.mark.integration


async def test_inspecting_one_node_reads_the_attributes_a_human_would_ask_for(
    client, limits
) -> None:
    described = await NodeInspector(client, limits=limits, include_value=True).inspect(
        "ns=2;s=Temperature"
    )
    assert described.node_id_text == "ns=2;s=Temperature"
    assert described.node_class is ua.NodeClass.Variable
    assert described.browse_name.Name == "Temperature"
    assert described.data_type_text == "Double"
    assert described.value_rank == -1
    assert not described.is_array
    assert isinstance(described.value, float)
    assert described.description
    assert described.minimum_sampling_interval is not None


async def test_inspection_is_the_answer_to_can_i_write_this_node(client, limits) -> None:
    writable = await NodeInspector(client, limits=limits).inspect("ns=2;s=Temperature")
    readonly = await NodeInspector(client, limits=limits).inspect("ns=2;s=Vibration")

    assert writable.writable
    assert writable.access_level.current_write
    assert not readonly.writable
    assert not readonly.access_level.current_write
    assert readonly.access_level.describe() == "CurrentRead"
    # Read only is not the same as unreachable: the tag is still readable.
    assert readonly.readable


async def test_array_variables_are_described_as_arrays(client, limits) -> None:
    described = await NodeInspector(client, limits=limits, include_value=True).inspect(
        "ns=2;s=SampleArray"
    )
    assert described.is_array
    # ValueRank 1 means "one dimensional array": the shape, not a guessed list.
    assert described.value_rank == 1
    assert isinstance(described.value, list)
    assert len(described.value) > 1


async def test_the_data_type_of_a_tag_matches_what_a_write_must_provide(
    client, limits, demo_server
) -> None:
    inspector = NodeInspector(client, limits=limits)
    names = ["Setpoint", "MotorRpm", "BatchTag001", "RecipeName", "RunState"]
    descriptors = {
        descriptor.browse_name.Name: descriptor
        for descriptor in await inspector.inspect_many([f"ns=2;s={name}" for name in names])
    }
    assert descriptors["Setpoint"].data_type_text == "Float"
    assert descriptors["MotorRpm"].data_type_text == "UInt32"
    assert descriptors["BatchTag001"].data_type_text == "Int32"
    assert descriptors["RecipeName"].data_type_text == "String"
    assert descriptors["RunState"].data_type_text == "Boolean"
    assert all(
        demo_server.tags[name] == descriptor.node_id for name, descriptor in descriptors.items()
    )


async def test_inspecting_many_nodes_costs_one_read_per_attribute_not_one_per_node(
    client, limits, demo_server, service_calls
) -> None:
    nodes = demo_server.tag_ids()
    inspector = NodeInspector(
        client,
        limits=limits,
        include_value=False,
        resolve_type_definition=False,
    )
    service_calls.clear()
    descriptors = await inspector.inspect_many(nodes)
    # Every attribute is one chunked Read: 11 attributes x ceil(24 nodes / 10 per read).
    expected = len(IDENTITY_ATTRIBUTES) * math.ceil(len(nodes) / limits.max_nodes_per_read)
    assert service_calls["read_attributes"] == expected
    assert len(descriptors) == len(nodes)
    # One read per node per attribute would have been this many calls instead.
    assert expected < len(IDENTITY_ATTRIBUTES) * len(nodes)
    # Naming the data types costs nothing when they are built in: the specification
    # already maps ns=0;i=11 to Double, so no request is sent for it. A vendor type
    # would need exactly one batched Read, not one per node.
    assert service_calls.get("read", 0) == 0


async def test_every_demo_tag_can_be_inspected_in_one_batch(client, limits, demo_server) -> None:
    nodes = list(demo_server.tags.values())
    descriptors = await NodeInspector(client, limits=limits).inspect_many(nodes)
    assert len(descriptors) == len(nodes)
    assert all(descriptor.node_class is ua.NodeClass.Variable for descriptor in descriptors)
    assert {descriptor.data_type_text for descriptor in descriptors} >= {
        "Double",
        "Float",
        "Int32",
        "UInt32",
        "String",
        "Boolean",
    }


async def test_a_writable_historized_tag_is_reported_as_such(client, limits, demo_server) -> None:
    if not demo_server.config.historize:
        pytest.skip("historization is not enabled in this fixture")
    described = await NodeInspector(client, limits=limits).inspect("ns=2;s=Temperature")
    assert described.historizing
    assert described.access_level.history_read


async def test_the_type_definition_is_resolved_to_a_name_a_human_can_read(client, limits) -> None:
    described = await NodeInspector(client, limits=limits).inspect("ns=2;s=Temperature")
    assert described.type_definition is not None
    assert "BaseDataVariableType" in described.type_definition


async def test_inspecting_a_node_that_is_not_there_raises_with_the_node_in_the_message(
    client, limits
) -> None:
    with pytest.raises(NodeNotAccessibleError, match="ns=2;s=NoSuchTag"):
        await NodeInspector(client, limits=limits).inspect("ns=2;s=NoSuchTag")


async def test_descriptors_are_json_ready_for_a_dashboard(client, limits) -> None:
    descriptors = await NodeInspector(client, limits=limits, include_value=True).inspect_many(
        ["ns=2;s=Temperature", "ns=2;s=Vibration"]
    )
    payload = json.dumps([descriptor.as_dict() for descriptor in descriptors])
    assert "CurrentRead" in payload
    assert "Double" in payload


async def test_minimum_sampling_interval_is_the_floor_for_a_sampling_interval(
    client, limits, demo_server
) -> None:
    described = await NodeInspector(client, limits=limits).inspect("ns=2;s=Temperature")
    assert described.minimum_sampling_interval is not None
    assert described.minimum_sampling_interval <= demo_server.config.min_supported_sample_rate * 10
