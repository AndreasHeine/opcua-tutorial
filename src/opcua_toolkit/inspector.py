"""Reusable node inspector.

Chapter 6's deliverable: one component that answers "what is this node?" for
any server - identity, data type, access rights, sampling and historizing
metadata. Every later chapter uses it: Chapter 8 checks AccessLevel before
writing, Chapter 9 reads method arguments, Chapter 14 validates collected
variables, Chapter 23 verifies that a companion specification instance has the
shape the specification promises.

Design rules:

* one Read call per *attribute*, not per node;
* one Browse call per batch for TypeDefinition;
* a bad status never raises - it is reported per node.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterable, Sequence
from typing import Any

from asyncua import ua
from asyncua.client.client import Client

from opcua_toolkit.errors import NodeNotAccessibleError, describe_status, format_node_id
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.values import (
    as_node_class,
    as_qualified_name,
    data_type_name,
    format_value,
    unwrap,
    variant_type_for_data_type,
)

#: Attributes that identify a node. Reading these is what "understanding a node"
#: means in practice, and it is the exact set UaExpert shows in its Attribute panel.
IDENTITY_ATTRIBUTES: tuple[ua.AttributeIds, ...] = (
    ua.AttributeIds.NodeClass,
    ua.AttributeIds.BrowseName,
    ua.AttributeIds.DisplayName,
    ua.AttributeIds.Description,
    ua.AttributeIds.DataType,
    ua.AttributeIds.ValueRank,
    ua.AttributeIds.ArrayDimensions,
    ua.AttributeIds.AccessLevel,
    ua.AttributeIds.UserAccessLevel,
    ua.AttributeIds.MinimumSamplingInterval,
    ua.AttributeIds.Historizing,
)

#: asyncua's enum mirrors the AccessLevelType table in OPC 10000-3: its members are bit
#: positions, not masks. Bit 7 is `Reserved` there and has no member here, because the
#: specification says it shall always be zero, so there is nothing to decode it into.
_ACCESS_BIT_POSITIONS: dict[str, int] = {
    access.name: int(access.value) for access in ua.AccessLevel
}


@dataclasses.dataclass(frozen=True, slots=True)
class AccessRights:
    """Decoded AccessLevel / UserAccessLevel bit mask.

    OPC 10000-3 gives both attributes the data type ``AccessLevelType``: a subtype of
    ``Byte`` with an ``OptionSetValues`` property, which is the specification's way of
    saying "one byte whose every bit has a defined name". All eight bits are named
    there, and a decoder that knows only the first two is the classic beginner bug -
    ``access & 0x02`` by hand looks right until it silently tests the wrong bit.

    ``Reserved`` (bit 7) is deliberately not modelled. The specification says it shall
    always be zero, so a server that sets it is out of specification and there is no
    honest name to report for it.
    """

    bits: int = 0

    @property
    def current_read(self) -> bool:
        """``CurrentRead``: the current value is readable, and is available at all."""
        return self._has("CurrentRead")

    @property
    def current_write(self) -> bool:
        """``CurrentWrite``: the current value is writeable, and is available at all."""
        return self._has("CurrentWrite")

    @property
    def history_read(self) -> bool:
        """``HistoryRead``: this server has history for the node and it may be read (Chapter 20)."""
        return self._has("HistoryRead")

    @property
    def history_write(self) -> bool:
        """``HistoryWrite``: history may be written to the node (Chapter 20)."""
        return self._has("HistoryWrite")

    @property
    def timestamp_write(self) -> bool:
        """``TimestampWrite``: any ``SourceTimestamp`` may be written, not only a null one."""
        return self._has("TimestampWrite")

    @property
    def status_write(self) -> bool:
        """``StatusWrite``: any ``StatusCode`` may be written, not only ``Good``."""
        return self._has("StatusWrite")

    @property
    def semantic_change(self) -> bool:
        """``SemanticChange``: this node is a Property that carries meaning for its parent.

        OPC 10000-3 sets the bit on Properties that define a semantic aspect of their
        parent Node, and whose value - so that semantic aspect - may change while the
        server runs. It belongs on type metadata, not on a plant tag.
        """
        return self._has("SemanticChange")

    def _has(self, name: str) -> bool:
        return bool(self.bits & (1 << _ACCESS_BIT_POSITIONS[name]))

    def describe(self) -> str:
        """Short human readable form, e.g. ``CurrentRead|CurrentWrite``.

        Names come from the specification in table order, so the reserved bit is simply
        absent rather than renamed.
        """
        names = [name for name in _ACCESS_BIT_POSITIONS if self._has(name)]
        return "|".join(names) if names else "none"

    def as_dict(self) -> dict[str, bool]:
        """JSON-safe representation, keyed by the specification's names in snake case."""
        return {_snake_case(name): self._has(name) for name in _ACCESS_BIT_POSITIONS}


@dataclasses.dataclass(frozen=True, slots=True)
class NodeDescriptor:
    """Everything a client needs to know before touching a node."""

    node_id: ua.NodeId
    node_class: ua.NodeClass
    browse_name: ua.QualifiedName
    display_name: str
    description: str
    data_type: ua.NodeId | None
    value_rank: int
    array_dimensions: tuple[int, ...]
    access_level: AccessRights
    user_access_level: AccessRights
    minimum_sampling_interval: float | None
    historizing: bool
    status: ua.StatusCode | None
    type_definition: str | None
    resolved_data_type: str | None = None
    value: Any = None

    @property
    def readable(self) -> bool:
        """Server side read permission for the current user."""
        return self.user_access_level.current_read

    @property
    def writable(self) -> bool:
        """Server side write permission for the current user."""
        return self.access_level.current_write and self.user_access_level.current_write

    @property
    def is_array(self) -> bool:
        """True for one or two dimensional arrays."""
        return self.value_rank >= 0

    @property
    def node_id_text(self) -> str:
        """Canonical NodeId text."""
        return format_node_id(self.node_id)

    @property
    def data_type_text(self) -> str:
        """Resolved data type name, e.g. ``Double`` or a custom type's BrowseName."""
        return self.resolved_data_type or data_type_name(self.data_type)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation used by the CLI and tests."""
        return {
            "node_id": self.node_id_text,
            "node_class": self.node_class.name,
            "browse_name": f"{self.browse_name.NamespaceIndex}:{self.browse_name.Name}",
            "display_name": self.display_name,
            "description": self.description,
            "data_type": self.data_type_text,
            "value_rank": self.value_rank,
            "array_dimensions": list(self.array_dimensions),
            "access_level": self.access_level.describe(),
            "user_access_level": self.user_access_level.describe(),
            "minimum_sampling_interval": self.minimum_sampling_interval,
            "historizing": self.historizing,
            "type_definition": self.type_definition,
            "status": self.status.name if self.status else "Good",
            "value": format_value(self.value),
        }

    def summary_line(self) -> str:
        """Single line for terminal output."""
        return (
            f"{self.node_id_text:<28} {self.node_class.name:<9} "
            f"{self.browse_name.Name:<24} {self.data_type_text:<12} "
            f"rank={self.value_rank:<3} access={self.user_access_level.describe()}"
        )


class NodeInspector:
    """Batched node inspection for one or thousands of nodes."""

    def __init__(
        self,
        client: Client,
        *,
        limits: OperationLimits | None = None,
        include_value: bool = False,
        resolve_type_definition: bool = True,
    ) -> None:
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()
        self._include_value = include_value
        self._resolve_type_definition = resolve_type_definition

    async def inspect(self, node: NodeIdLike) -> NodeDescriptor:
        """Inspect a single node.

        Raises:
            NodeNotAccessibleError: The server does not know the node, or will not
                tell the current user anything about it. A bulk inspection reports
                per node; a single one is an explicit question and gets an explicit
                answer.
        """
        results = await self.inspect_many([node])
        if not results:
            raise NodeNotAccessibleError(f"Node {node} could not be inspected")
        descriptor = results[0]
        if descriptor.node_class is ua.NodeClass.Unspecified:
            raise NodeNotAccessibleError(
                f"Node {format_node_id(descriptor.node_id)}: {describe_status(descriptor.status)}",
                node_id=descriptor.node_id,
            )
        return descriptor

    async def inspect_many(self, nodes: Iterable[NodeIdLike]) -> list[NodeDescriptor]:
        """Inspect many nodes with one Read call per attribute plus one Browse batch.

        For 11 attributes and 1000 nodes this means 11 requests (plus chunking),
        not 11000 - the central efficiency lesson of the tutorial.
        """
        node_ids = [to_nodeid(node) for node in nodes]
        if not node_ids:
            return []

        columns: dict[ua.AttributeIds, list[ua.DataValue]] = {}
        for attribute in IDENTITY_ATTRIBUTES:
            columns[attribute] = await self._read_attribute(node_ids, attribute)
        if self._include_value:
            columns[ua.AttributeIds.Value] = await self._read_attribute(
                node_ids, ua.AttributeIds.Value
            )

        type_definitions = (
            await self._read_type_definitions(node_ids) if self._resolve_type_definition else {}
        )
        data_type_names = await self._resolve_data_type_names(columns[ua.AttributeIds.DataType])

        descriptors: list[NodeDescriptor] = []
        for index, node_id in enumerate(node_ids):
            descriptors.append(
                _build_descriptor(
                    node_id=node_id,
                    columns=columns,
                    index=index,
                    type_definition=type_definitions.get(index),
                    data_type_names=data_type_names,
                )
            )
        return descriptors

    async def _read_attribute(
        self, node_ids: Sequence[ua.NodeId], attribute: ua.AttributeIds
    ) -> list[ua.DataValue]:
        """Read one attribute for all nodes, chunked to the server's limit."""
        collected: list[ua.DataValue] = []
        for chunk in chunked(list(node_ids), self._limits.max_nodes_per_read):
            collected.extend(await self._client.uaclient.read_attributes(list(chunk), attribute))
        return collected

    async def _read_type_definitions(self, node_ids: Sequence[ua.NodeId]) -> dict[int, str]:
        """Browse the HasTypeDefinition reference for every node in batches."""
        found: dict[int, str] = {}
        indices = list(range(len(node_ids)))
        for chunk in chunked(indices, self._limits.max_nodes_per_browse):
            descriptions = []
            for index in chunk:
                description = ua.BrowseDescription()
                description.NodeId = node_ids[index]
                description.BrowseDirection = ua.BrowseDirection.Forward
                description.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasTypeDefinition, 0)
                description.IncludeSubtypes = True
                description.NodeClassMask = ua.NodeClass.Unspecified
                description.ResultMask = ua.BrowseResultMask.BrowseName
                descriptions.append(description)
            params = ua.BrowseParameters()
            params.View = ua.ViewDescription()
            params.RequestedMaxReferencesPerNode = 1
            params.NodesToBrowse = descriptions
            results = await self._client.uaclient.browse(params)
            for position, result in enumerate(results):
                if result.StatusCode.is_bad() or not result.References:
                    continue
                found[chunk[position]] = str(result.References[0].BrowseName.Name)
        return found

    async def _resolve_data_type_names(self, column: list[ua.DataValue]) -> dict[str, str]:
        """Name the data types the specification does not already name.

        The 25 built-in types have fixed NodeIds in every OPC UA server: asking the
        server for the BrowseName of ``ns=0;i=11`` is a roundtrip that answers
        "Double", which the client already knows. Only custom or vendor types are
        worth a Read - and one Read for all of them together.
        """
        unique: dict[str, ua.NodeId] = {}
        for data_value in column:
            raw = unwrap(data_value.Value)
            if isinstance(raw, ua.NodeId) and variant_type_for_data_type(raw) is None:
                unique[format_node_id(raw)] = raw
        if not unique:
            return {}
        node_ids = list(unique.values())
        names: dict[str, str] = {}
        for chunk in chunked(node_ids, self._limits.max_nodes_per_read):
            results = await self._client.uaclient.read_attributes(
                list(chunk), ua.AttributeIds.BrowseName
            )
            for node_id, data_value in zip(chunk, results, strict=True):
                raw = unwrap(data_value.Value)
                text = getattr(raw, "Name", None) or data_type_name(node_id)
                names[format_node_id(node_id)] = str(text)
        return names


def _snake_case(name: str) -> str:
    """``CurrentRead`` -> ``current_read``: the specification's own names, spellable in JSON."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def _build_descriptor(
    *,
    node_id: ua.NodeId,
    columns: dict[ua.AttributeIds, list[ua.DataValue]],
    index: int,
    type_definition: str | None,
    data_type_names: dict[str, str],
) -> NodeDescriptor:
    """Assemble one descriptor out of the per-attribute columns."""

    def value_of(attribute: ua.AttributeIds) -> Any:
        column = columns.get(attribute)
        if column is None or index >= len(column):
            return None
        return unwrap(column[index].Value)

    def status_of(attribute: ua.AttributeIds) -> ua.StatusCode | None:
        column = columns.get(attribute)
        if column is None or index >= len(column):
            return None
        return column[index].StatusCode

    node_class_raw = value_of(ua.AttributeIds.NodeClass)
    browse_raw = value_of(ua.AttributeIds.BrowseName)
    display_raw = value_of(ua.AttributeIds.DisplayName)
    description_raw = value_of(ua.AttributeIds.Description)
    data_type_raw = value_of(ua.AttributeIds.DataType)
    value_rank_raw = value_of(ua.AttributeIds.ValueRank)
    array_raw = value_of(ua.AttributeIds.ArrayDimensions)
    access_raw = value_of(ua.AttributeIds.AccessLevel)
    user_access_raw = value_of(ua.AttributeIds.UserAccessLevel)
    sampling_raw = value_of(ua.AttributeIds.MinimumSamplingInterval)
    historizing_raw = value_of(ua.AttributeIds.Historizing)

    data_type = data_type_raw if isinstance(data_type_raw, ua.NodeId) else None
    resolved_name = data_type_names.get(format_node_id(data_type)) if data_type else None
    return NodeDescriptor(
        node_id=node_id,
        node_class=as_node_class(node_class_raw),
        browse_name=as_qualified_name(browse_raw, default_namespace=node_id.NamespaceIndex),
        display_name=str(getattr(display_raw, "Text", display_raw or "")),
        description=str(getattr(description_raw, "Text", description_raw or "")),
        data_type=data_type,
        resolved_data_type=resolved_name,
        value_rank=int(value_rank_raw) if value_rank_raw is not None else -1,
        array_dimensions=tuple(int(size) for size in (array_raw or ())),
        access_level=AccessRights(bits=int(access_raw or 0)),
        user_access_level=AccessRights(bits=int(user_access_raw or 0)),
        minimum_sampling_interval=(float(sampling_raw) if sampling_raw is not None else None),
        historizing=bool(historizing_raw or False),
        status=status_of(ua.AttributeIds.Value) or status_of(ua.AttributeIds.NodeClass),
        type_definition=type_definition,
        value=value_of(ua.AttributeIds.Value),
    )


def format_table(descriptors: Sequence[NodeDescriptor]) -> str:
    """Render descriptors as an aligned table for terminals and CI logs."""
    if not descriptors:
        return "no nodes"
    headers = ("NodeId", "Class", "BrowseName", "DataType", "Rank", "Access", "Historizing")
    rows = [
        (
            descriptor.node_id_text,
            descriptor.node_class.name,
            descriptor.browse_name.Name,
            descriptor.data_type_text,
            str(descriptor.value_rank),
            descriptor.user_access_level.describe(),
            "yes" if descriptor.historizing else "no",
        )
        for descriptor in descriptors
    ]
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)
    ]
    lines = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    lines.extend(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)) for row in rows
    )
    return "\n".join(lines)


__all__ = [
    "IDENTITY_ATTRIBUTES",
    "AccessRights",
    "NodeDescriptor",
    "NodeInspector",
    "format_table",
]
