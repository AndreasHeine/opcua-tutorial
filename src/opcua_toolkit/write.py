"""Efficient, honest writing.

Chapter 8 exists because "the write call did not raise" is not the same thing
as "the value arrived". OPC UA returns a StatusCode *per node*, so a bulk write
that returned successfully can still contain failed tags. This module:

* checks AccessLevel and UserAccessLevel before sending anything;
* matches the VariantType to the node's DataType (``float`` -> ``Single`` is the
  bug that burns every beginner);
* keeps every StatusCode and reports partial failure loudly;
* shrinks the chunk instead of failing the batch when the server answers
  ``BadTooManyOperations``;
* retries only genuinely retryable statuses, with backoff.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from asyncua import ua
from asyncua.client.client import Client
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from opcua_toolkit.errors import ConfigurationError, PartialFailureError, format_node_id
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.settings import RetryPolicy
from opcua_toolkit.values import make_variant, unwrap, variant_type_for_data_type

_logger = logging.getLogger(__name__)

#: Status codes that mean "ask again later", not "your request is wrong".
RETRYABLE_STATUSES: frozenset[int] = frozenset(
    {
        ua.StatusCodes.BadTooManyOperations,
        ua.StatusCodes.BadServerTooBusy,
        ua.StatusCodes.BadServerHalted,
        ua.StatusCodes.BadSessionNotActivated,
        ua.StatusCodes.BadCommunicationError,
    }
)

#: Status codes that mean "your chunk is too big", which we answer by shrinking.
SHRINK_STATUSES: frozenset[int] = frozenset({ua.StatusCodes.BadTooManyOperations})

_MAX_SHRINK_DEPTH = 5
_MAX_WRITE_ATTEMPTS = 20


@dataclasses.dataclass(frozen=True, slots=True)
class WriteItem:
    """One value to write, optionally pinned to an explicit OPC UA type.

    The node may be NodeId text (``"ns=2;s=Setpoint"``) or an asyncua NodeId: a
    Chapter 7 caller builds these from a browse result or a config file, so
    :attr:`node_id` is normalised on construction and everything downstream can rely
    on one canonical form and one canonical text.
    """

    node_id: NodeIdLike
    value: Any
    variant_type: ua.VariantType | None = None

    def __post_init__(self) -> None:
        """Normalise the node reference to one canonical NodeId."""
        object.__setattr__(self, "node_id", to_nodeid(self.node_id))

    @property
    def node_id_text(self) -> str:
        """Canonical NodeId text."""
        return format_node_id(self.node_id)


@dataclasses.dataclass(frozen=True, slots=True)
class WriteOutcome:
    """What happened to one item, including the server's verdict."""

    node_id: ua.NodeId
    status: ua.StatusCode
    value: Any = None
    attempts: int = 1
    note: str = ""

    @property
    def ok(self) -> bool:
        """True for Good and Uncertain statuses."""
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
            "attempts": self.attempts,
            "note": self.note,
        }


@dataclasses.dataclass(slots=True)
class WriteReport:
    """Result of a bulk write, always including the failed items."""

    outcomes: list[WriteOutcome] = dataclasses.field(default_factory=list)
    requested: int = 0
    requests: int = 0
    retried: int = 0
    skipped: int = 0
    shrinks: int = 0
    elapsed_ms: float = 0.0

    @property
    def succeeded(self) -> int:
        """Number of items the server accepted."""
        return sum(1 for outcome in self.outcomes if outcome.ok)

    @property
    def failed(self) -> int:
        """Number of items the server rejected."""
        return sum(1 for outcome in self.outcomes if not outcome.ok)

    def failures(self) -> list[WriteOutcome]:
        """The failed outcomes, in the order they were requested."""
        return [outcome for outcome in self.outcomes if not outcome.ok]

    @property
    def unaccounted(self) -> int:
        """Items the caller asked for that have no outcome.

        A writer that "forgot" an item is the worst kind of bug in a collector: the
        value never arrives and nothing is logged. The toolkit reports the invariant
        instead of hiding it, and Chapter 8 tests it.
        """
        return self.requested - len(self.outcomes)

    def summary(self) -> str:
        """One line summary for terminal and CI output."""
        suffix = "" if self.unaccounted == 0 else f" MISSING_OUTCOMES={self.unaccounted}"
        return (
            f"items={self.requested} outcomes={len(self.outcomes)} ok={self.succeeded} "
            f"failed={self.failed} skipped={self.skipped} requests={self.requests} "
            f"retried={self.retried} shrinks={self.shrinks} elapsed={self.elapsed_ms:.1f} ms"
            f"{suffix}"
        )

    def raise_for_failures(self) -> WriteReport:
        """Raise :class:`PartialFailureError` if anything failed.

        Useful in collectors that must not continue with a half written batch.
        """
        failures = self.failures()
        if failures:
            detail = ", ".join(
                f"{outcome.node_id_text}={outcome.status.name}" for outcome in failures[:10]
            )
            raise PartialFailureError(
                f"bulk write failed for {len(failures)} of {len(self.outcomes)} items: {detail}",
                failures=failures,
            )
        return self


class BulkWriter:
    """Batched writing with access checks, type coercion and bounded retries."""

    def __init__(
        self,
        client: Client,
        *,
        limits: OperationLimits | None = None,
        retry: RetryPolicy | None = None,
        check_access: bool = True,
        coerce_types: bool = True,
        use_source_timestamp: bool = False,
    ) -> None:
        """Configure how much the writer checks before it sends anything.

        Args:
            client: A connected asyncua client.
            limits: Server operation limits, used to chunk Write requests.
            retry: Policy applied to retryable status codes such as
                ``BadTooManyOperations``. ``None`` means three bounded attempts.
            check_access: Read AccessLevel and UserAccessLevel first and refuse
                locally. Left ``True``, a batch never half-writes and a rejected item
                costs nothing. Left ``False``, the writer sends the write and reports
                what the server said - and that is the honest risk: ``AccessLevel`` is
                something a server *publishes*, not something every server *enforces*,
                so a permissive server will happily accept a write to a node it calls
                read only.
            coerce_types: Convert values that fit the node's data type (an ``int``
                written to a ``Single`` node) instead of failing them.
            use_source_timestamp: Ask the server to record the client's timestamp.
        """
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()
        self._retry = retry if retry is not None else RetryPolicy(max_attempts=3)
        self._check_access = check_access
        self._coerce_types = coerce_types
        self._use_source_timestamp = use_source_timestamp

    async def write(self, items: Iterable[WriteItem | tuple[NodeIdLike, Any]]) -> WriteReport:
        """Write many values and report every StatusCode.

        Args:
            items: :class:`WriteItem` objects or ``(node_id, value)`` pairs.

        Returns:
            A :class:`WriteReport` in the same order as the input.
        """
        prepared = _normalise(items)
        report = WriteReport()
        if not prepared:
            return report
        report.requested = len(prepared)
        started = time.monotonic()

        targets = await self._resolve_targets(prepared)
        writable: list[tuple[int, WriteItem]] = []
        for index, item in enumerate(prepared):
            target = targets[index]
            if target is None:
                report.outcomes.append(
                    WriteOutcome(
                        node_id=item.node_id,
                        status=ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown),
                        value=item.value,
                        note="node not readable, access level unknown",
                    )
                )
                report.skipped += 1
                continue
            if target.blocked_by is not None:
                report.outcomes.append(
                    WriteOutcome(
                        node_id=item.node_id,
                        status=target.blocked_by,
                        value=item.value,
                        note=target.note,
                    )
                )
                report.skipped += 1
                continue
            writable.append((index, item))

        await self._write_batches(writable, targets, self._limits.max_nodes_per_write, report, 0)
        self._account_for_missing(prepared, report)
        report.outcomes.sort(key=_outcome_order_key(prepared))
        report.elapsed_ms = (time.monotonic() - started) * 1000.0
        return report

    @staticmethod
    def _account_for_missing(items: Sequence[WriteItem], report: WriteReport) -> None:
        """Guarantee one outcome per requested item.

        A silent gap is worse than an ugly status code: the value never arrives and
        nothing in the log says so. Turning a gap into ``BadCommunicationError`` keeps
        the report honest.
        """
        seen = {format_node_id(outcome.node_id) for outcome in report.outcomes}
        for item in items:
            text = format_node_id(item.node_id)
            if text in seen:
                continue
            _logger.warning("no write outcome recorded for %s", text)
            report.outcomes.append(
                WriteOutcome(
                    node_id=item.node_id,
                    status=ua.StatusCode(ua.StatusCodes.BadCommunicationError),
                    value=item.value,
                    note="no status was returned for this item",
                )
            )
            seen.add(text)

    async def _write_batches(
        self,
        writable: Sequence[tuple[int, WriteItem]],
        targets: Sequence[_WriteTarget | None],
        chunk_size: int,
        report: WriteReport,
        depth: int,
    ) -> None:
        """Write every chunk. One chunk's trouble must never cancel the others."""
        for chunk in chunked(list(writable), max(1, chunk_size)):
            await self._write_chunk(list(chunk), targets, report, depth)

    async def _write_chunk(
        self,
        pending: list[tuple[int, WriteItem]],
        targets: Sequence[_WriteTarget | None],
        report: WriteReport,
        depth: int,
    ) -> None:
        """Write one chunk, retrying or shrinking it when the server asks us to."""
        attempt = 1
        while pending:
            datavalues: list[ua.DataValue] = []
            accepted: list[tuple[int, WriteItem]] = []
            for index, item in pending:
                prepared = _build_datavalue(
                    item,
                    targets[index],
                    coerce_types=self._coerce_types,
                    use_source_timestamp=self._use_source_timestamp,
                )
                if prepared.failure is not None:
                    report.outcomes.append(prepared.failure)
                elif prepared.pending is not None:
                    datavalues.append(prepared.pending)
                    accepted.append((index, item))
            if not accepted:
                return

            report.requests += 1
            try:
                statuses = await self._client.uaclient.write_attributes(
                    [item.node_id for _, item in accepted],
                    datavalues,
                    ua.AttributeIds.Value,
                )
            except (UaStatusCodeError, UaError) as exc:
                status = _status_from_exception(exc)
                if self._should_retry(status, attempt):
                    report.retried += len(pending)
                    await asyncio.sleep(self._retry.delay_before(attempt))
                    attempt += 1
                    continue
                self._record(
                    pending, status, report, attempt, f"write request failed: {status.name}"
                )
                return

            retryable: list[tuple[int, WriteItem]] = []
            shrink_seen = False
            for (index, item), status in zip(accepted, statuses, strict=True):
                if not status.is_bad():
                    report.outcomes.append(
                        WriteOutcome(
                            node_id=item.node_id,
                            status=status,
                            value=item.value,
                            attempts=attempt,
                        )
                    )
                elif self._should_retry(status, attempt):
                    retryable.append((index, item))
                    shrink_seen = shrink_seen or status.value in SHRINK_STATUSES
                else:
                    report.outcomes.append(
                        WriteOutcome(
                            node_id=item.node_id,
                            status=status,
                            value=item.value,
                            attempts=attempt,
                            note=f"{status.name} is not retryable",
                        )
                    )

            if not retryable:
                return
            report.retried += len(retryable)
            if shrink_seen and len(retryable) > 1 and depth < _MAX_SHRINK_DEPTH:
                report.shrinks += 1
                await self._write_batches(
                    retryable, targets, max(1, len(retryable) // 2), report, depth + 1
                )
                return
            if attempt >= _MAX_WRITE_ATTEMPTS:
                self._record(
                    retryable,
                    ua.StatusCode(ua.StatusCodes.BadCommunicationError),
                    report,
                    attempt,
                    "gave up after the retry limit",
                )
                return
            await asyncio.sleep(self._retry.delay_before(attempt))
            attempt += 1
            pending = retryable

    @staticmethod
    def _record(
        pending: Sequence[tuple[int, WriteItem]],
        status: ua.StatusCode,
        report: WriteReport,
        attempts: int,
        note: str,
    ) -> None:
        """Give every item in a doomed chunk its own verdict."""
        report.outcomes.extend(
            WriteOutcome(
                node_id=item.node_id,
                status=status,
                value=item.value,
                attempts=attempts,
                note=note,
            )
            for _, item in pending
        )

    async def _resolve_targets(self, items: Sequence[WriteItem]) -> list[_WriteTarget | None]:
        """Read what a write needs *before* anything is sent.

        With ``check_access`` the writer reads AccessLevel, UserAccessLevel and DataType
        and refuses locally. Without it, only DataType is read - because the type
        coercion still has to know the type - and the write is sent. What comes back is
        the server's own judgement, which for a read-only node is either the
        ``BadNotWritable`` the specification expects or a ``Good`` nobody promised you:
        ``AccessLevel`` is a declaration, and not every server enforces declarations.
        """
        attributes: tuple[ua.AttributeIds, ...] = (ua.AttributeIds.DataType,)
        if self._check_access:
            attributes = (
                ua.AttributeIds.AccessLevel,
                ua.AttributeIds.UserAccessLevel,
                ua.AttributeIds.DataType,
            )
        columns: list[list[ua.DataValue]] = []
        for attribute in attributes:
            collected: list[ua.DataValue] = []
            for chunk in chunked([item.node_id for item in items], self._limits.max_nodes_per_read):
                collected.extend(
                    await self._client.uaclient.read_attributes(list(chunk), attribute)
                )
            columns.append(collected)
        data_type_column = len(columns) - 1

        targets: list[_WriteTarget | None] = []
        for index in range(len(items)):
            if any(index >= len(column) for column in columns):
                targets.append(None)
                continue
            probed = tuple(column[index] for column in columns)
            if all(_is_bad(data_value) for data_value in probed):
                # Every read the preflight made failed: the node is not there at all.
                # Reporting "not writable" for a node that does not exist sends the
                # operator looking in the wrong place, so the real reason goes in the report.
                targets.append(None)
                continue
            # DataValue.Value is a Variant, not the raw value: unwrapping here is what
            # makes the type coercion later actually happen.
            data_type_raw = unwrap(probed[data_type_column].Value)
            data_type = data_type_raw if isinstance(data_type_raw, ua.NodeId) else None
            blocked: ua.StatusCode | None = None
            note = ""
            if self._check_access:
                access = _bits(probed[0])
                user_access = _bits(probed[1])
                write_bit = 1 << int(ua.AccessLevel.CurrentWrite.value)
                if not access & write_bit:
                    blocked = ua.StatusCode(ua.StatusCodes.BadNotWritable)
                    note = f"AccessLevel does not allow CurrentWrite (bits=0x{access:02X})"
                elif not user_access & write_bit:
                    blocked = ua.StatusCode(ua.StatusCodes.BadUserAccessDenied)
                    note = f"UserAccessLevel does not allow CurrentWrite (bits=0x{user_access:02X})"
            targets.append(_WriteTarget(data_type=data_type, blocked_by=blocked, note=note))
        return targets

    def _should_retry(self, status: ua.StatusCode, attempt: int) -> bool:
        """Whether a status code justifies another attempt."""
        return status.value in RETRYABLE_STATUSES and self._retry.allows_another(attempt)


@dataclasses.dataclass(frozen=True, slots=True)
class _WriteTarget:
    data_type: ua.NodeId | None
    blocked_by: ua.StatusCode | None
    note: str


@dataclasses.dataclass(frozen=True, slots=True)
class _PendingWrite:
    pending: ua.DataValue | None
    failure: WriteOutcome | None


def _normalise(items: Iterable[WriteItem | tuple[NodeIdLike, Any]]) -> list[WriteItem]:
    """Convert every input to a :class:`WriteItem` with a real ``ua.NodeId``.

    Callers pass strings, tuples and items interchangeably. Serialising a plain
    string where a NodeId is expected is exactly how ``AttributeError`` shows up
    deep inside the protocol stack, so this is where the boundary is cleaned.
    """
    prepared: list[WriteItem] = []
    for item in items:
        if isinstance(item, WriteItem):
            prepared.append(dataclasses.replace(item, node_id=to_nodeid(item.node_id)))
        else:
            node, value = item
            prepared.append(WriteItem(node_id=to_nodeid(node), value=value))
    return prepared


def _build_datavalue(
    item: WriteItem,
    target: _WriteTarget | None,
    *,
    coerce_types: bool,
    use_source_timestamp: bool,
) -> _PendingWrite:
    """Coerce a value to the node's DataType and wrap it in a DataValue."""
    variant_type = item.variant_type
    if variant_type is None and coerce_types and target is not None:
        variant_type = variant_type_for_data_type(target.data_type)
    try:
        variant = make_variant(item.value, variant_type=variant_type)
    except ConfigurationError as exc:
        return _PendingWrite(
            pending=None,
            failure=WriteOutcome(
                node_id=item.node_id,
                status=ua.StatusCode(ua.StatusCodes.BadTypeMismatch),
                value=item.value,
                note=str(exc),
            ),
        )
    data_value = ua.DataValue(variant)
    if use_source_timestamp:
        data_value.SourceTimestamp = datetime.now(UTC)
    return _PendingWrite(pending=data_value, failure=None)


def _bits(data_value: ua.DataValue) -> int:
    raw = data_value.Value
    if raw is None:
        return 0
    if isinstance(raw, ua.Variant):
        raw = raw.Value
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _is_bad(data_value: ua.DataValue) -> bool:
    """Whether one probed attribute came back with a bad status."""
    status = data_value.StatusCode
    return status is not None and status.is_bad()


def _status_from_exception(exc: BaseException) -> ua.StatusCode:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return ua.StatusCode(code)
    return ua.StatusCode(ua.StatusCodes.BadCommunicationError)


def _outcome_order_key(prepared: Sequence[WriteItem]) -> Callable[[WriteOutcome], int]:
    """Sort outcomes back into the order the caller asked for."""
    wanted = {format_node_id(item.node_id): index for index, item in enumerate(prepared)}
    return lambda outcome: wanted.get(outcome.node_id_text, len(prepared))


__all__ = [
    "RETRYABLE_STATUSES",
    "SHRINK_STATUSES",
    "BulkWriter",
    "WriteItem",
    "WriteOutcome",
    "WriteReport",
]
