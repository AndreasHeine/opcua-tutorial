"""Server operation limits and request chunking.

OPC UA servers announce how many items a single service call may contain
(OPC 10000-5, 6.3.11 *OperationLimitsType*). Servers are entitled to reject an
oversized request with ``BadTooManyOperations``. Production clients never send
"all nodes at once": they read the limits once per session and split requests
into chunks that fit. This is where the bulk read/write/subscribe chapters get
their efficiency and their robustness.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from typing import Any

from asyncua import ua
from asyncua.client.client import Client

from opcua_toolkit.errors import OperationLimitError
from opcua_toolkit.values import unwrap

#: Fallbacks used when a server does not report a limit. OPC 10000-5 makes every
#: OperationLimits property optional, so "no value" must never mean "unlimited".
DEFAULT_MAX_NODES_PER_CALL = 1000
DEFAULT_MAX_NODES_PER_BROWSE = 100
DEFAULT_MAX_NODES_PER_METHOD_CALL = 100

_PROPERTY_NODES: tuple[tuple[str, int], ...] = (
    ("max_nodes_per_read", ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead),
    (
        "max_nodes_per_write",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerWrite,
    ),
    (
        "max_nodes_per_method_call",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerMethodCall,
    ),
    (
        "max_nodes_per_browse",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerBrowse,
    ),
    (
        "max_nodes_per_register_nodes",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRegisterNodes,
    ),
    (
        "max_nodes_per_translate_browse_paths",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerTranslateBrowsePathsToNodeIds,
    ),
    (
        "max_nodes_per_node_management",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerNodeManagement,
    ),
    (
        "max_monitored_items_per_call",
        ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxMonitoredItemsPerCall,
    ),
)

#: Public mapping of limit name -> NodeId of the property the server publishes.
#: Used by the demo server (which publishes limits) and by the discovery chapter.
LIMIT_PROPERTY_IDS: dict[str, int] = dict(_PROPERTY_NODES)

_DEFAULTS: dict[str, int] = {
    "max_nodes_per_read": DEFAULT_MAX_NODES_PER_CALL,
    "max_nodes_per_write": DEFAULT_MAX_NODES_PER_CALL,
    "max_nodes_per_method_call": DEFAULT_MAX_NODES_PER_METHOD_CALL,
    "max_nodes_per_browse": DEFAULT_MAX_NODES_PER_BROWSE,
    "max_nodes_per_register_nodes": DEFAULT_MAX_NODES_PER_CALL,
    "max_nodes_per_translate_browse_paths": DEFAULT_MAX_NODES_PER_CALL,
    "max_nodes_per_node_management": DEFAULT_MAX_NODES_PER_CALL,
    "max_monitored_items_per_call": DEFAULT_MAX_NODES_PER_CALL,
}


@dataclasses.dataclass(frozen=True, slots=True)
class OperationLimits:
    """Chunking budgets that a client must respect for one server session.

    Attributes:
        reported: Names of limits the server actually published. Anything not
            reported falls back to a conservative default and is *not* in this
            set, which is how callers detect an incomplete server.
    """

    max_nodes_per_read: int = DEFAULT_MAX_NODES_PER_CALL
    max_nodes_per_write: int = DEFAULT_MAX_NODES_PER_CALL
    max_nodes_per_method_call: int = DEFAULT_MAX_NODES_PER_METHOD_CALL
    max_nodes_per_browse: int = DEFAULT_MAX_NODES_PER_BROWSE
    max_nodes_per_register_nodes: int = DEFAULT_MAX_NODES_PER_CALL
    max_nodes_per_translate_browse_paths: int = DEFAULT_MAX_NODES_PER_CALL
    max_nodes_per_node_management: int = DEFAULT_MAX_NODES_PER_CALL
    max_monitored_items_per_call: int = DEFAULT_MAX_NODES_PER_CALL
    min_supported_sample_rate: float | None = None
    max_browse_continuation_points: int | None = None
    reported: frozenset[str] = frozenset()

    @classmethod
    async def read(cls, client: Client) -> OperationLimits:
        """Read all operation limits with a single Read service call.

        One call for all limits instead of one call per limit is the first
        efficiency lesson of the tutorial: 8 properties, 1 roundtrip.
        """
        node_ids = [ua.NodeId(identifier, 0) for _, identifier in _PROPERTY_NODES]
        values = await client.uaclient.read_attributes(node_ids, ua.AttributeIds.Value)

        kwargs: dict[str, int] = {}
        reported: list[str] = []
        for (name, _), data_value in zip(_PROPERTY_NODES, values, strict=True):
            status = data_value.StatusCode
            if status is None or status.is_bad():
                continue
            raw = unwrap(data_value.Value)
            if raw is None or int(raw) <= 0:
                continue
            kwargs[name] = int(raw)
            reported.append(name)

        sample_rate: float | None = None
        rate_results = await client.uaclient.read_attributes(
            [ua.NodeId(ua.ObjectIds.Server_ServerCapabilities_MinSupportedSampleRate, 0)],
            ua.AttributeIds.Value,
        )
        rate = rate_results[0]
        rate_status = rate.StatusCode
        if rate_status is None or rate_status.is_good():
            raw_rate = unwrap(rate.Value)
            if raw_rate is not None:
                sample_rate = float(raw_rate)

        continuation = await client.uaclient.read_attributes(
            [ua.NodeId(ua.ObjectIds.Server_ServerCapabilities_MaxBrowseContinuationPoints, 0)],
            ua.AttributeIds.Value,
        )
        continuation_value = continuation[0]
        continuation_status = continuation_value.StatusCode
        raw_continuation = unwrap(continuation_value.Value)
        max_continuation = (
            int(raw_continuation)
            if (continuation_status is None or continuation_status.is_good())
            and raw_continuation is not None
            else None
        )

        merged = {**_DEFAULTS, **kwargs}
        return cls(
            **merged,
            min_supported_sample_rate=sample_rate,
            max_browse_continuation_points=max_continuation,
            reported=frozenset(reported),
        )

    def with_overrides(self, overrides: dict[str, int | None]) -> OperationLimits:
        """Apply user supplied overrides, ignoring empty or invalid values."""
        changes = {
            name: value
            for name, value in overrides.items()
            if value is not None and value > 0 and hasattr(self, name)
        }
        if not changes:
            return self
        current = dataclasses.asdict(self)
        current.update(changes)
        return OperationLimits(**current)

    def unresolved(self) -> tuple[str, ...]:
        """Limits the server did not publish, sorted for stable output."""
        known = {name for name, _ in _PROPERTY_NODES}
        return tuple(sorted(known - self.reported))

    def describe(self) -> str:
        """Multi line summary used by ``opcua-toolkit discover``."""
        lines = [f"{name}: {getattr(self, name)}" for name in sorted(_DEFAULTS)]
        if self.min_supported_sample_rate is not None:
            lines.append(f"min_supported_sample_rate: {self.min_supported_sample_rate} ms")
        missing = self.unresolved()
        if missing:
            lines.append(f"not published by server (assumed): {', '.join(missing)}")
        return "\n".join(lines)

    def status_note(self) -> str:
        """Human readable hint about how complete the server's limits are."""
        missing = self.unresolved()
        if not missing:
            return "server published all operation limits"
        return f"server omitted {len(missing)} limit(s); conservative defaults applied"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation for ``opcua-toolkit discover --json``.

        ``reported`` is a frozenset, which :mod:`json` refuses to encode, so it
        becomes a sorted list. Sorting is not cosmetic: a CI diff of two runs of
        the same discovery should be empty.
        """
        payload = dataclasses.asdict(self)
        payload["reported"] = sorted(self.reported)
        payload["unresolved"] = list(self.unresolved())
        return payload


def chunked[T](items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Split a sequence into consecutive chunks of at most ``size`` items.

    Args:
        items: The items to split.
        size: Maximum chunk size, typically an operation limit.

    Yields:
        Consecutive slices of ``items``.

    Raises:
        OperationLimitError: ``size`` is not a positive integer.
    """
    if size <= 0:
        raise OperationLimitError(f"Chunk size must be > 0, got {size}")
    if not items:
        return
    for start in range(0, len(items), size):
        yield items[start : start + size]


__all__ = [
    "DEFAULT_MAX_NODES_PER_BROWSE",
    "DEFAULT_MAX_NODES_PER_CALL",
    "DEFAULT_MAX_NODES_PER_METHOD_CALL",
    "LIMIT_PROPERTY_IDS",
    "OperationLimits",
    "chunked",
]
