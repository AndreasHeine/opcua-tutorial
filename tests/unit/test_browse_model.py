"""Chapter 5 and 6 tests: the browse-side models.

Browsing produces references, and a reference already answers most questions
about a node. These models are what Chapter 13's generic applications rely on.
"""

from __future__ import annotations

from asyncua import ua

from opcua_toolkit.browse.model import (
    BrowseStats,
    BrowseTree,
    NodeRef,
    to_plain_node_id,
)


def make_reference(
    *,
    identifier: str | int = "Temperature",
    namespace: int = 2,
    name: str = "Temperature",
    node_class: ua.NodeClass = ua.NodeClass.Variable,
    type_definition: ua.NodeId | None = None,
) -> ua.ReferenceDescription:
    """One browse result, exactly as a server would send it."""
    description = ua.ReferenceDescription()
    description.NodeId = ua.ExpandedNodeId(identifier, namespace)
    description.BrowseName = ua.QualifiedName(name, namespace)
    description.DisplayName = ua.LocalizedText(Text=name)
    description.NodeClass = node_class
    description.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasProperty, 0)
    description.IsForward = True
    description.TypeDefinition = (
        type_definition
        if type_definition is not None
        else ua.NodeId(ua.ObjectIds.BaseDataVariableType, 0)
    )
    return description


def ref_for(
    name: str,
    node_class: ua.NodeClass,
    type_definition: ua.NodeId | None = None,
) -> NodeRef:
    """One browse entry for a node of that name and NodeClass."""
    return NodeRef.from_description(
        make_reference(
            identifier=name,
            name=name,
            node_class=node_class,
            type_definition=type_definition,
        )
    )


def test_node_ref_carries_everything_a_client_needs_before_reading() -> None:
    ref = NodeRef.from_description(make_reference())
    assert ref.name == "Temperature"
    assert ref.node_id_text == "ns=2;s=Temperature"
    assert ref.node_class is ua.NodeClass.Variable
    assert ref.display_name == "Temperature"
    assert ref.is_forward
    assert ref.reference_type == ua.NodeId(ua.ObjectIds.HasProperty, 0)


def test_node_ref_flattens_an_expanded_node_id_to_the_target_server() -> None:
    expanded = ua.ExpandedNodeId("Temperature", 2, NamespaceUri="urn:other", ServerIndex=7)
    plain = to_plain_node_id(expanded)
    assert not isinstance(plain, ua.ExpandedNodeId)
    assert plain == ua.NodeId("Temperature", 2)


def test_node_ref_as_dict_is_json_safe_and_stable() -> None:
    ref = NodeRef.from_description(
        make_reference(identifier="Line1", name="Line1", node_class=ua.NodeClass.Object)
    )
    payload = ref.as_dict()
    assert payload == {
        "node_id": "ns=2;s=Line1",
        "browse_name": "2:Line1",
        "display_name": "Line1",
        "node_class": "Object",
        "type_definition": f"ns=0;i={ua.ObjectIds.BaseDataVariableType}",
        "reference_type": f"ns=0;i={ua.ObjectIds.HasProperty}",
    }


def test_node_ref_tolerates_a_server_that_omits_the_type_definition() -> None:
    description = make_reference()
    description.TypeDefinition = ua.NodeId(0, 0)
    description.DisplayName = ua.LocalizedText()
    ref = NodeRef.from_description(description)
    assert ref.display_name == ""
    assert ref.type_definition is None or ref.type_definition == ua.NodeId(0, 0)


def build_tree() -> BrowseTree:
    """ProductionLine -> Line1 -> Temperature / Setpoint, as a captured subtree."""
    temperature = BrowseTree(node=NodeRef.from_description(make_reference()))
    setpoint = BrowseTree(
        node=NodeRef.from_description(make_reference(identifier="Setpoint", name="Setpoint"))
    )
    line = BrowseTree(
        node=NodeRef.from_description(
            make_reference(identifier="Line1", name="Line1", node_class=ua.NodeClass.Object)
        ),
        children=[temperature, setpoint],
    )
    return BrowseTree(
        node=NodeRef.from_description(
            make_reference(
                identifier="ProductionLine", name="ProductionLine", node_class=ua.NodeClass.Object
            )
        ),
        children=[line],
    )


def test_walk_visits_depth_first_including_the_root() -> None:
    names = [ref.name for ref in build_tree().walk()]
    assert names == ["ProductionLine", "Line1", "Temperature", "Setpoint"]


def test_find_by_name_searches_children_not_the_whole_subtree() -> None:
    tree = build_tree()
    line = tree.find_by_name("Line1")
    assert line is not None
    assert line.node is not None
    assert line.node.node_id_text == "ns=2;s=Line1"
    assert tree.find_by_name("Temperature") is None
    assert line.find_by_name("Setpoint") is not None


def test_render_shows_indentation_and_node_kind() -> None:
    rendered = build_tree().render()
    lines = rendered.splitlines()
    assert lines[0].startswith("ProductionLine [obje] ns=2;s=ProductionLine")
    assert "  Line1 [obje]" in rendered
    assert "    Temperature [vari]" in rendered


def test_a_type_and_an_instance_are_not_printed_with_the_same_label() -> None:
    """Chapter 16 is exactly this distinction, so a browse tree has to print it.

    ``ua.NodeClass.ObjectType.name[:4]`` and ``ua.NodeClass.Object.name[:4]`` are both ``obje``,
    and ``ua.NodeClass.VariableType.name[:4]`` is ``vari`` - the label a Variable already has.
    """
    tree = BrowseTree(
        node=ref_for("MachineType", ua.NodeClass.ObjectType, ua.NodeId(0, 0)),
        children=[
            BrowseTree(node=ref_for("PumpType", ua.NodeClass.ObjectType, ua.NodeId(0, 0))),
            BrowseTree(node=ref_for("Pump_01", ua.NodeClass.Object, ua.NodeId("PumpType", 2))),
            BrowseTree(node=ref_for("MachineSetPointType", ua.NodeClass.VariableType)),
        ],
    )
    rendered = tree.render()
    assert "MachineType [objt] ns=2;s=MachineType" in rendered
    assert "  PumpType [objt] ns=2;s=PumpType" in rendered
    assert "  Pump_01 [obje] ns=2;s=Pump_01 : ns=2;s=PumpType" in rendered
    assert "  MachineSetPointType [virt] ns=2;s=MachineSetPointType" in rendered
    assert "ns=0;i=0" not in rendered


def test_an_empty_type_definition_is_reported_as_none() -> None:
    """The demo server reports namespace 0 identifier 0 for a type node, which is not a node."""
    ref = ref_for("PumpType", ua.NodeClass.ObjectType, ua.NodeId(0, 0))
    assert ref.type_definition is None
    assert ref.as_dict()["type_definition"] is None

    typed = ref_for("SetPoint", ua.NodeClass.Variable, ua.NodeId("MachineSetPointType", 2))
    assert typed.type_definition == ua.NodeId("MachineSetPointType", 2)
    assert typed.as_dict()["type_definition"] == "ns=2;s=MachineSetPointType"


def test_render_truncates_instead_of_printing_a_whole_factory() -> None:
    rendered = build_tree().render(max_rows=2)
    assert len(rendered.splitlines()) <= 3
    assert "truncated" in rendered


def test_browse_stats_count_requests_as_browse_plus_browse_next() -> None:
    stats = BrowseStats(nodes_visited=5, references_seen=9, browse_calls=2, browse_next_calls=3)
    assert stats.requests == 5
    assert "nodes=5" in stats.summary()
    assert "browse_next_calls=3" in stats.summary()


def test_browse_stats_merge_two_explorations_of_one_server() -> None:
    first = BrowseStats(nodes_visited=10, references_seen=20, browse_calls=3, errors=1)
    second = BrowseStats(nodes_visited=5, references_seen=7, browse_calls=2, continuation_points=1)
    merged = first.merge(second)
    assert merged.nodes_visited == 15
    assert merged.references_seen == 27
    assert merged.browse_calls == 5
    assert merged.errors == 1
    assert merged.continuation_points == 1
    assert merged.stopped_reason is None


def test_browse_stats_merge_prefers_the_last_reason_that_stopped_a_walk() -> None:
    first = BrowseStats(stopped_reason="max_nodes=100 reached")
    second = BrowseStats(stopped_reason="max_depth=2 reached")
    assert first.merge(second).stopped_reason == "max_depth=2 reached"
    assert second.merge(BrowseStats()).stopped_reason == "max_depth=2 reached"
