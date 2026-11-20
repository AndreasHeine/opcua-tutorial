"""Efficient reading.

Chapter 7 turns "read a value" into a measurable engineering decision. One Read
service call per tag is fine for 10 tags and catastrophic for 10,000. The
:class:`BulkReader` batches per server limit, keeps the per-node StatusCode, and
reports its own cost so you can prove the improvement instead of believing it.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from opcua_toolkit.errors import format_node_id
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.values import format_value, unwrap

_GOOD = ua.StatusCode(ua.StatusCodes.Good)
_logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class ReadResult:
    """Result of reading one node, including the status the server reported."""

    node_id: ua.NodeId
    status: ua.StatusCode
    value: Any = None
    source_timestamp: datetime | None = None
    server_timestamp: datetime | None = None

    @property
    def ok(self) -> bool:
        """True when the server returned a Good or Uncertain status."""
        return not self.status.is_bad()

    @property
    def node_id_text(self) -> str:
        """Canonical NodeId text."""
        return format_node_id(self.node_id)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "node_id": self.node_id_text,
            "status": self.status.name,
            "value": format_value(self.value),
            "source_timestamp": self.source_timestamp.isoformat()
            if self.source_timestamp
            else None,
            "server_timestamp": self.server_timestamp.isoformat()
            if self.server_timestamp
            else None,
        }

    @classmethod
    def failure(cls, node_id: ua.NodeId, status: ua.StatusCode) -> ReadResult:
        """Build a failed result for a node whose chunk never arrived."""
        return cls(node_id=node_id, status=status)


@dataclasses.dataclass(slots=True)
class BulkReadStats:
    """Cost of one bulk read, the number Chapter 7 optimises."""

    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    requests: int = 0
    chunk_size: int = 0
    elapsed_ms: float = 0.0

    @property
    def nodes_per_request(self) -> float:
        """Average nodes carried by one service call - the efficiency metric."""
        return self.requested / self.requests if self.requests else 0.0

    def summary(self) -> str:
        """One line summary for terminal and CI output."""
        return (
            f"nodes={self.requested} ok={self.succeeded} failed={self.failed} "
            f"requests={self.requests} nodes/request={self.nodes_per_request:.1f} "
            f"elapsed={self.elapsed_ms:.1f} ms"
        )


class BulkReader:
    """Batched reading that respects the server's operation limits."""

    def __init__(
        self,
        client: Client,
        *,
        limits: OperationLimits | None = None,
        include_timestamps: bool = True,
    ) -> None:
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()
        self._include_timestamps = include_timestamps

    @property
    def limits(self) -> OperationLimits:
        """Limits used for chunking."""
        return self._limits

    async def read_values(
        self, nodes: Iterable[NodeIdLike]
    ) -> tuple[list[ReadResult], BulkReadStats]:
        """Read the Value attribute of many nodes.

        Returns:
            Results in the same order as the input, and the cost of the operation.
            A node that failed never raises: inspect :attr:`ReadResult.status`.
        """
        return await self.read_attribute(nodes, ua.AttributeIds.Value)

    async def read_attribute(
        self, nodes: Iterable[NodeIdLike], attribute: ua.AttributeIds
    ) -> tuple[list[ReadResult], BulkReadStats]:
        """Read any attribute of many nodes, chunked to the server's limit."""
        node_ids = [to_nodeid(node) for node in nodes]
        stats = BulkReadStats(requested=len(node_ids))
        started = time.monotonic()
        results: list[ReadResult] = []

        for chunk in chunked(node_ids, self._limits.max_nodes_per_read):
            chunk_items = list(chunk)
            stats.chunk_size = len(chunk_items)
            try:
                values = await self._client.uaclient.read_attributes(chunk_items, attribute)
            except (UaStatusCodeError, UaError) as exc:
                status = _status_from_exception(exc)
                _log_chunk_failure(chunk_items, attribute, status)
                results.extend(ReadResult.failure(node_id, status) for node_id in chunk_items)
                stats.requests += 1
                stats.failed += len(chunk_items)
                continue
            stats.requests += 1
            if len(values) != len(chunk_items):
                # A server that answers fewer results than asked is a spec violation,
                # but a client that crashes on it is worse: record the gap instead.
                _logger.warning(
                    "server returned %d results for %d requested nodes",
                    len(values),
                    len(chunk_items),
                )
            for node_id, data_value in zip(chunk_items, values, strict=False):
                result = _to_result(
                    node_id, data_value, include_timestamps=self._include_timestamps
                )
                results.append(result)
                if result.ok:
                    stats.succeeded += 1
                else:
                    stats.failed += 1
            missing = chunk_items[len(values) :]
            if missing:
                results.extend(
                    ReadResult.failure(node_id, ua.StatusCode(ua.StatusCodes.BadCommunicationError))
                    for node_id in missing
                )
                stats.failed += len(missing)

        stats.elapsed_ms = (time.monotonic() - started) * 1000.0
        return results, stats

    async def read_one_by_one(
        self, nodes: Iterable[NodeIdLike]
    ) -> tuple[list[ReadResult], BulkReadStats]:
        """Unbatched baseline used to *measure* the cost of a bad pattern.

        Never use this in production code. It exists so Chapter 7 can show the
        difference instead of asserting it.
        """
        node_ids = [to_nodeid(node) for node in nodes]
        stats = BulkReadStats(requested=len(node_ids), chunk_size=1)
        started = time.monotonic()
        results: list[ReadResult] = []
        for node_id in node_ids:
            stats.requests += 1
            try:
                data_value = await self._client.get_node(node_id).read_data_value(
                    raise_on_bad_status=False
                )
            except (UaStatusCodeError, UaError) as exc:
                results.append(ReadResult.failure(node_id, _status_from_exception(exc)))
                stats.failed += 1
                continue
            result = _to_result(node_id, data_value, include_timestamps=self._include_timestamps)
            results.append(result)
            if result.ok:
                stats.succeeded += 1
            else:
                stats.failed += 1
        stats.elapsed_ms = (time.monotonic() - started) * 1000.0
        return results, stats

    async def values_of(self, nodes: Iterable[NodeIdLike]) -> dict[str, Any]:
        """Convenience: NodeId text -> value for nodes that read successfully."""
        results, _ = await self.read_values(nodes)
        return {result.node_id_text: result.value for result in results if result.ok}


def _to_result(
    node_id: ua.NodeId, data_value: ua.DataValue, *, include_timestamps: bool
) -> ReadResult:
    """Convert one DataValue into a ReadResult, preserving per-node status."""
    status = data_value.StatusCode or _GOOD
    return ReadResult(
        node_id=node_id,
        status=status,
        value=unwrap(data_value.Value),
        source_timestamp=data_value.SourceTimestamp if include_timestamps else None,
        server_timestamp=data_value.ServerTimestamp if include_timestamps else None,
    )


def _status_from_exception(exc: BaseException) -> ua.StatusCode:
    """Best effort StatusCode for a request level failure."""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return ua.StatusCode(code)
    return ua.StatusCode(ua.StatusCodes.BadCommunicationError)


def _log_chunk_failure(
    chunk: list[ua.NodeId], attribute: ua.AttributeIds, status: ua.StatusCode
) -> None:
    _logger.warning(
        "read of %d nodes attribute %s failed with %s",
        len(chunk),
        attribute.name,
        status.name,
    )


__all__ = ["BulkReadStats", "BulkReader", "ReadResult"]
