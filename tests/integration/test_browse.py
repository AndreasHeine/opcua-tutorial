"""Chapter 5 and Chapter 6 integration tests: browsing a real address space.

The demo server keeps its tags under ``ns=2;s=ProductionLine/Line1`` and puts the
read only tags in a separate ``Maintenance`` object, which is the shape a real
plant has: process values here, diagnostics there.
"""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_toolkit.browse.explorer import NamespaceExplorer
from opcua_toolkit.errors import BrowsePathError, NodeNotAccessibleError, format_node_id
from opcua_toolkit.limits import OperationLimits

pytestmark = pytest.mark.integration


async def test_the_namespace_array_names_the_server_default_and_the_demo_namespace(
    client, limits, demo_server
) -> None:
    namespaces = await NamespaceExplorer(client, limits=limits).namespaces()
    assert namespaces[0][0] == 0
    assert "opcfoundation.org" in namespaces[0][1]
    assert demo_server.namespace_uri in [uri for _, uri in namespaces]
    published = {uri: index for index, uri in namespaces}
    assert published[demo_server.namespace_uri] == demo_server.namespace_index


async def test_browsing_a_folder_returns_references_not_values(client, limits) -> None:
    children = await NamespaceExplorer(client, limits=limits).children("ns=2;s=ProductionLine")
    names = {child.name for child in children}
    assert {"Line1", "Maintenance"} <= names
    assert all(child.node_class is ua.NodeClass.Object for child in children)
    assert all(child.node_id_text.startswith("ns=2;s=") for child in children)


async def test_a_variable_filter_is_applied_by_the_server_not_by_the_client(client, limits) -> None:
    variables = await NamespaceExplorer(client, limits=limits).children(
        "ns=2;s=Line1", node_class=ua.NodeClass.Variable
    )
    names = {variable.name for variable in variables}
    assert "Temperature" in names
    assert "BatchTag001" in names
    assert all(variable.node_class is ua.NodeClass.Variable for variable in variables)
    # NodeClassMask filters what the *server* sends; it knows nothing about access
    # rights. Vibration is read only and still shows up, because "read only" is an
    # attribute of the variable, not a different kind of node.
    assert "Vibration" in names


async def test_a_server_side_class_filter_prunes_a_walk_but_a_flat_filter_does_not(
    client, limits
) -> None:
    explorer = NamespaceExplorer(client, limits=limits)
    # The ObjectsFolder's children are Objects, and the tags sit one level below them.
    # A server-side ResultNodeClass filter therefore never reaches the tags at all:
    # it shapes what the server sends, and what the server does not send cannot be walked.
    pruned, _ = await explorer.walk(
        ua.ObjectIds.ObjectsFolder,
        max_depth=-1,
        max_nodes=400,
        node_class=ua.NodeClass.Variable,
    )
    assert "Temperature" not in {ref.name for ref in pruned.walk()}

    # walk_flat browses every level and selects the class on the way home, which is
    # what a collector scanning an unknown namespace actually needs.
    collected, stats = await explorer.walk_flat(
        ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
    )
    names = {ref.name for ref in collected}
    assert {"Temperature", "BatchTag001", "Vibration"} <= names
    assert all(ref.node_class is ua.NodeClass.Variable for ref in collected)
    # The walk visited more nodes than the filter kept: the count is the cost of the
    # scan, the list is the answer, and pretending they are the same number is how a
    # collector underestimates what exploring a plant costs.
    assert stats.nodes_visited > len(collected)


async def test_a_walk_builds_the_tree_the_engineer_drew(client, limits) -> None:
    tree, stats = await NamespaceExplorer(client, limits=limits).walk(
        "ns=2;s=ProductionLine", max_depth=4
    )
    line = tree.find_by_name("Line1")
    assert line is not None
    temperature = line.find_by_name("Temperature")
    assert temperature is not None
    assert temperature.node is not None
    assert temperature.node.node_id_text == "ns=2;s=Temperature"
    assert stats.nodes_visited >= 10
    # Exactly one node has no reference pointing at it: the root we started from.
    # Everything else arrived through a Browse result, which is why a walk can name
    # a server's whole subtree without a single Read.
    assert stats.references_seen == stats.nodes_visited - 1
    assert "ProductionLine" in tree.render()


async def test_a_flat_walk_finds_every_tag_the_server_advertises(
    client, limits, demo_server
) -> None:
    flat, stats = await NamespaceExplorer(client, limits=limits).walk_flat(
        "ns=2;s=ProductionLine", max_nodes=500
    )
    names = {ref.name for ref in flat}
    for tag in demo_server.tags.values():
        assert tag.Identifier in names
    assert stats.nodes_visited == len(flat)
    assert stats.stopped_reason is None


async def test_max_nodes_stops_a_walk_and_the_stats_say_why(client, limits) -> None:
    _, stats = await NamespaceExplorer(client, limits=limits).walk(
        "ns=2;s=ProductionLine", max_depth=-1, max_nodes=5
    )
    assert stats.nodes_visited <= 5
    assert stats.stopped_reason is not None
    assert "max_nodes=5" in stats.stopped_reason


async def test_max_depth_keeps_a_walk_shallow(client, limits) -> None:
    tree, stats = await NamespaceExplorer(client, limits=limits).walk(
        "ns=2;s=ProductionLine", max_depth=1
    )
    # max_depth counts levels *below* the root, and the root itself is a node the
    # caller owns: ProductionLine + Line1 + Maintenance.
    assert stats.nodes_visited == 3
    assert tree.node is not None
    assert tree.node.name == "ProductionLine"
    line = tree.find_by_name("Line1")
    assert line is not None
    assert line.find_by_name("Temperature") is None


async def test_browsing_is_batched_by_the_limit_the_client_declares(client, service_calls) -> None:
    wide = NamespaceExplorer(client, limits=OperationLimits(max_nodes_per_browse=10))
    service_calls["browse"] = 0
    wide_flat, wide_stats = await wide.walk_flat("ns=2;s=ProductionLine", max_nodes=500)
    assert service_calls["browse"] == wide_stats.browse_calls

    strict = NamespaceExplorer(client, limits=OperationLimits(max_nodes_per_browse=2))
    service_calls["browse"] = 0
    strict_flat, strict_stats = await strict.walk_flat("ns=2;s=ProductionLine", max_nodes=500)

    assert strict_stats.browse_calls > wide_stats.browse_calls
    # Chunking is an optimisation, not a filter: the same nodes must arrive either way.
    assert {ref.node_id_text for ref in strict_flat} == {ref.node_id_text for ref in wide_flat}


async def test_a_translate_call_reaches_a_known_path_in_one_request(
    client, limits, service_calls, demo_server
) -> None:
    explorer = NamespaceExplorer(client, limits=limits)
    service_calls["translate_browsepaths_to_nodeids"] = 0
    # A BrowseName is matched *with* its namespace index, so a plain name needs one:
    # ns=0:Temperature does not exist under an ns=2 folder even when the tag is there.
    resolved = await explorer.resolve_path_with_translate(
        ["ProductionLine", "Line1", "Temperature"],
        namespace_index=demo_server.namespace_index,
    )
    assert resolved.node_id_text == "ns=2;s=Temperature"
    assert resolved.node_class is ua.NodeClass.Variable
    assert service_calls["translate_browsepaths_to_nodeids"] == 1


async def test_translate_accepts_qualified_names_from_a_discovered_namespace(
    client, limits, demo_server
) -> None:
    # The Chapter 13 shape: the index came from the NamespaceArray, not from source.
    index = demo_server.namespace_index
    explorer = NamespaceExplorer(client, limits=limits)
    resolved = await explorer.resolve_path_with_translate(
        [
            ua.QualifiedName("ProductionLine", index),
            ua.QualifiedName("Line1", index),
            ua.QualifiedName("Temperature", index),
        ]
    )
    assert resolved.node_id_text == "ns=2;s=Temperature"
    assert resolved.browse_name.NamespaceIndex == index


async def test_a_translate_call_in_the_wrong_namespace_finds_nothing(
    client, limits, demo_server
) -> None:
    # Same names, wrong namespace: the server answers BadBrowsePathInvalid/NoMatch
    # and the toolkit turns that into a typed error naming the path.
    explorer = NamespaceExplorer(client, limits=limits)
    with pytest.raises(BrowsePathError, match="Temperature"):
        await explorer.resolve_path_with_translate(
            ["ProductionLine", "Line1", "Temperature"], namespace_index=0
        )
    assert demo_server.namespace_index != 0


async def test_a_translate_call_reports_the_path_it_could_not_resolve(client, limits) -> None:
    explorer = NamespaceExplorer(client, limits=limits)
    with pytest.raises(BrowsePathError, match="TranslateBrowsePathsToNodeIds"):
        await explorer.resolve_path_with_translate(
            ["ProductionLine", "GhostLine", "Temperature"], namespace_index=2
        )


async def test_a_browse_walk_resolves_the_same_node_without_namespace_indexes(
    client, limits
) -> None:
    resolved = await NamespaceExplorer(client, limits=limits).find_by_browse_path(
        ["ProductionLine", "Line1", "Temperature"]
    )
    assert resolved.node_id_text == "ns=2;s=Temperature"


async def test_a_browse_walk_reports_the_segment_that_broke_the_path(client, limits) -> None:
    with pytest.raises(BrowsePathError) as caught:
        await NamespaceExplorer(client, limits=limits).find_by_browse_path(
            ["ProductionLine", "GhostLine"]
        )
    assert caught.value.path
    assert "GhostLine" in caught.value.path


async def test_a_path_may_be_written_from_the_node_it_starts_at(client, limits) -> None:
    """``Objects/ProductionLine`` is how the address space is drawn, so it is accepted.

    OPC 10000-4 5.9.4 makes a browse path *relative to its starting node*, which is why
    ``ProductionLine/Line1`` works from the Objects folder. People write the drawn path,
    which names the starting node itself, and a tool that rejects the natural spelling
    teaches the reader that the address space is harder than it is.
    """
    explorer = NamespaceExplorer(client, limits=limits)
    relative = await explorer.find_by_browse_path(["ProductionLine", "Line1"])
    drawn = await explorer.find_by_browse_path(["Objects", "ProductionLine", "Line1"])
    assert drawn.node_id_text == relative.node_id_text == "ns=2;s=Line1"

    # A path that says nothing but the starting node's own name is that node.
    folder = await explorer.find_by_browse_path(["Objects"])
    assert folder.node_id_text == "ns=0;i=85"
    assert folder.node_class is ua.NodeClass.Object


async def test_a_browse_path_starts_where_it_was_told(client, limits) -> None:
    """``--node`` is not decoration: it is where the first segment is looked for."""
    explorer = NamespaceExplorer(client, limits=limits)
    rooted = await explorer.find_by_browse_path(
        ["Line1", "Temperature"], root="ns=2;s=ProductionLine"
    )
    assert rooted.node_id_text == "ns=2;s=Temperature"

    with pytest.raises(BrowsePathError, match="under ns=2;s=Line1"):
        await explorer.find_by_browse_path(["ProductionLine", "Line1"], root="ns=2;s=Line1")


async def test_describe_node_identifies_a_node_from_one_read(client, limits, service_calls) -> None:
    service_calls.clear()
    described = await NamespaceExplorer(client, limits=limits).describe_node("ns=2;s=Temperature")
    assert described.node_class is ua.NodeClass.Variable
    assert described.browse_name.Name == "Temperature"
    assert described.browse_name.NamespaceIndex == 2
    assert described.display_name == "Temperature"
    # Three attributes, one Read request: Read carries (node, attribute) pairs, so a
    # "simple lookup" does not have to cost three roundtrips.
    assert service_calls["read"] == 1
    assert service_calls.get("read_attributes", 0) == 0


async def test_describe_node_fails_loudly_for_a_node_that_is_not_there(client, limits) -> None:
    # Not "the path broke": the node itself is unknown, which is a different problem
    # from a browse path with a segment in the wrong namespace.
    with pytest.raises(NodeNotAccessibleError, match="BadNodeIdUnknown") as caught:
        await NamespaceExplorer(client, limits=limits).describe_node("ns=2;s=NoSuchNode")
    assert format_node_id(caught.value.node_id) == "ns=2;s=NoSuchNode"
