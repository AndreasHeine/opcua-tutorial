"""Data models produced while browsing.

Browsing an unknown server produces *references*, not nodes. A
:class:`ua.ReferenceDescription` already carries the target NodeId, BrowseName,
DisplayName, NodeClass and TypeDefinition, so a well written explorer can build
a useful picture of a server without ever calling Read. That is the point of
Chapter 5 and Chapter 6.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any

from asyncua import ua

from opcua_toolkit.errors import format_node_id


def _to_plain_node_id(value: Any) -> ua.NodeId:
    """Normalise ExpandedNodeId/NodeId to a plain NodeId in the target server."""
    if isinstance(value, ua.NodeId) and not isinstance(value, ua.ExpandedNodeId):
        return value
    identifier = getattr(value, "Identifier", value)
    namespace = getattr(value, "NamespaceIndex", 0) or 0
    return ua.NodeId(identifier, int(namespace))


to_plain_node_id = _to_plain_node_id

#: Four letters are enough to read a browse tree, but two node classes share them:
#: ``ua.NodeClass.Object.name[:4]`` and ``ua.NodeClass.ObjectType.name[:4]`` are both ``obje``,
#: and "this one is the type, that one is the instance" is exactly what a reader needs to see
#: when a server authors its own types. So the two classes that would collide are named apart.
_NODE_CLASS_LABELS: dict[ua.NodeClass, str] = {
    ua.NodeClass.Object: "obje",
    ua.NodeClass.Variable: "vari",
    ua.NodeClass.Method: "meth",
    ua.NodeClass.ObjectType: "objt",
    ua.NodeClass.VariableType: "virt",
    ua.NodeClass.ReferenceType: "reft",
    ua.NodeClass.DataType: "data",
    ua.NodeClass.View: "view",
}


def _type_definition_or_none(value: Any) -> ua.NodeId | None:
    """Treat the empty NodeId as "no TypeDefinition was reported".

    asyncua's server fills ``ReferenceDescription.TypeDefinition`` for the node a browse result
    points at, and for a node that is itself a type it fills it with namespace 0, identifier 0 -
    measured on this repository's server: browsing ``MachineType`` through ``HasSubtype`` reports
    ``ns=0;i=0`` for ``PumpType``, while the same browse reports ``ns=0;i=63`` for the
    ``Temperature`` declaration and ``ns=2;s=MachineSetPointType`` for ``Pump_01.SetPoint``.
    Printed as though it were a NodeId, that makes a type look like it has one - which it has,
    as its supertype through ``HasSubtype``, and a type definition is not what that is.
    """
    if value is None:
        return None
    node_id = _to_plain_node_id(value)
    if node_id.NamespaceIndex == 0 and node_id.Identifier == 0:
        return None
    return node_id


@dataclasses.dataclass(frozen=True, slots=True)
class NodeRef:
    """One node as seen through the reference that points at it."""

    node_id: ua.NodeId
    browse_name: ua.QualifiedName
    display_name: str
    node_class: ua.NodeClass
    type_definition: ua.NodeId | None
    reference_type: ua.NodeId | None
    is_forward: bool = True

    @classmethod
    def from_description(cls, description: ua.ReferenceDescription) -> NodeRef:
        """Build a NodeRef from a browse result entry."""
        return cls(
            node_id=_to_plain_node_id(description.NodeId),
            browse_name=description.BrowseName,
            # A server may send an empty LocalizedText, whose .Text is None, not "".
            display_name=str(getattr(description.DisplayName, "Text", "") or ""),
            node_class=description.NodeClass,
            type_definition=_type_definition_or_none(description.TypeDefinition),
            reference_type=description.ReferenceTypeId,
            is_forward=description.IsForward,
        )

    @property
    def name(self) -> str:
        """The BrowseName without namespace prefix, e.g. ``Temperature``."""
        return str(self.browse_name.Name)

    @property
    def node_id_text(self) -> str:
        """Canonical ``ns=...;...`` text used in all tool output."""
        return format_node_id(self.node_id)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form for logs, JSON output and tests."""
        return {
            "node_id": self.node_id_text,
            "browse_name": f"{self.browse_name.NamespaceIndex}:{self.browse_name.Name}",
            "display_name": self.display_name,
            "node_class": self.node_class.name,
            "type_definition": format_node_id(self.type_definition)
            if self.type_definition
            else None,
            "reference_type": format_node_id(self.reference_type) if self.reference_type else None,
        }


@dataclasses.dataclass(slots=True)
class BrowseStats:
    """What it cost to explore the server - the basis of Chapter 7's tuning work."""

    nodes_visited: int = 0
    references_seen: int = 0
    browse_calls: int = 0
    browse_next_calls: int = 0
    continuation_points: int = 0
    errors: int = 0
    stopped_reason: str | None = None

    def merge(self, other: BrowseStats) -> BrowseStats:
        """Combine two explorations, e.g. several roots of one server."""
        return BrowseStats(
            nodes_visited=self.nodes_visited + other.nodes_visited,
            references_seen=self.references_seen + other.references_seen,
            browse_calls=self.browse_calls + other.browse_calls,
            browse_next_calls=self.browse_next_calls + other.browse_next_calls,
            continuation_points=self.continuation_points + other.continuation_points,
            errors=self.errors + other.errors,
            stopped_reason=other.stopped_reason or self.stopped_reason,
        )

    @property
    def requests(self) -> int:
        """Total service calls issued."""
        return self.browse_calls + self.browse_next_calls

    def summary(self) -> str:
        """One line summary for terminal output."""
        return (
            f"nodes={self.nodes_visited} references={self.references_seen} "
            f"browse_calls={self.browse_calls} browse_next_calls={self.browse_next_calls} "
            f"errors={self.errors}"
            + (f" stopped={self.stopped_reason}" if self.stopped_reason else "")
        )


@dataclasses.dataclass(slots=True)
class BrowseTree:
    """A subtree captured during exploration."""

    node: NodeRef | None
    children: list[BrowseTree] = dataclasses.field(default_factory=list)

    def walk(self) -> Iterator[NodeRef]:
        """Depth first iteration over every captured node."""
        if self.node is not None:
            yield self.node
        for child in self.children:
            yield from child.walk()

    def find_by_name(self, name: str) -> BrowseTree | None:
        """Return the first child subtree whose BrowseName matches."""
        for child in self.children:
            if child.node is not None and child.node.name == name:
                return child
        return None

    def render(self, *, max_rows: int = 200) -> str:
        """Render the tree as indented text, truncated to ``max_rows`` lines."""
        lines: list[str] = []

        def visit(branch: BrowseTree, depth: int) -> None:
            if len(lines) >= max_rows:
                return
            if branch.node is not None:
                kind = _NODE_CLASS_LABELS.get(
                    branch.node.node_class, branch.node.node_class.name[:4].lower()
                )
                type_hint = (
                    f" : {format_node_id(branch.node.type_definition)}"
                    if branch.node.type_definition
                    else ""
                )
                lines.append(
                    f"{'  ' * depth}{branch.node.name} [{kind}] {branch.node.node_id_text}"
                    f"{type_hint}"
                )
            for child in branch.children:
                if len(lines) >= max_rows:
                    lines.append(f"{'  ' * (depth + 1)}... truncated")
                    return
                visit(child, depth + 1)

        visit(self, 0)
        return "\n".join(lines)


__all__ = ["BrowseStats", "BrowseTree", "NodeRef", "to_plain_node_id"]
