"""A bounded on-disk spool: the store in store-and-forward.

A buffer survives a slow consumer. Only a file survives a restart, a crash, and an outage that
lasts longer than the memory a gateway is allowed to spend. Chapter 15's other half is therefore
a file: records that the application could not hand to its historian are written down in order,
and taken back out in the same order when the historian comes back.

Three facts decide the design, and none of them are about JSON:

* **A spool has to be bounded, and bounding it means throwing something away.** An unbounded
  spool does not avoid the decision - it moves it to the operating system, which fills the disk
  and then fails every write, including the ones that would have worked. A bounded spool decides
  in advance which end it sacrifices and counts what it sacrificed. :attr:`SpoolFullPolicy` is
  that decision: :attr:`~SpoolFullPolicy.TRIM_OLDEST` keeps the freshest data and reports the
  age it destroyed, :attr:`~SpoolFullPolicy.REFUSE` keeps everything it already had and reports
  the data it never took.
* **The file is the truth, not an in-memory mirror of it.** After a crash the only thing left is
  the bytes on disk, so every read goes to the file and the counters that describe it are
  rebuilt from it. The in-memory counts exist to keep appends cheap, and they are re-derived
  from the file whenever the spool is opened again.
* **A spool is one process's file.** Two writers appending to the same JSONL file interleave
  lines and corrupt each other's line count. A gateway with two processes needs two spools, which
  is also the shape that makes it obvious which outage each one covers.

Durability is a separate choice from buffering. :attr:`SpoolSettings.flush_on_write` pushes the
bytes into the operating system after every record; :attr:`SpoolSettings.sync_on_write` asks the
device to confirm them, which is slower and is the only option that means something when the
power is the failure being designed for. Both are on by default for flush and off for fsync,
because a gateway that fsyncs every notification trades the outage it is defending against for a
throughput ceiling it will hit every day.

Every read here runs in a thread (:func:`asyncio.to_thread`): a file on a network mount is a
blocking call, and a blocking call inside the subscription's consumer task is how a slow disk
kills a subscription.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opcua_toolkit.errors import SpoolError
from opcua_toolkit.settings import ValueUnit, format_number
from opcua_toolkit.subscriptions.model import DataChangeRecord

_logger = logging.getLogger(__name__)

_SPOOL_RECORDS = ValueUnit("spooled records")
_BYTES = ValueUnit("bytes")


class SpoolFullPolicy(StrEnum):
    """What the spool does when it has no room left for the next record.

    Attributes:
        TRIM_OLDEST: Remove the oldest spooled records until the new one fits. The freshest
            data is the data that survives, and every record destroyed this way is counted,
            because "the disk was full" is not an excuse for losing the oldest evidence of a
            long outage.
        REFUSE: Keep what is already spooled and refuse the new record. Choose this when the
            records already written are more valuable than the ones still arriving - which is
            the usual answer once an outage is already in progress.
    """

    TRIM_OLDEST = "trim_oldest"
    REFUSE = "refuse"


class SpoolSettings(BaseModel):
    """Where a spool lives, how big it may get, and what it does when it is full.

    Attributes:
        path: The JSONL file. Its parent directories are created on first write.
        capacity: Records the spool holds at most.
        max_bytes: Bytes the file may grow to. Both bounds matter: a spool bounded only by
            record count is unbounded in size the moment the values are strings or arrays.
        full_policy: What happens to the next record when either bound is reached.
        flush_on_write: Push each written line into the operating system immediately. On by
            default: a spool that loses its last few records because the process died is a
            spool that did not do its one job.
        sync_on_write: Also ask the device to confirm the bytes (``os.fsync``). Off by default
            because it is the slowest thing a gateway can do on every record, and it is what
            an outage that lasts minutes costs a system designed for thousands of records a
            second.
    """

    model_config = ConfigDict(frozen=True)

    path: Path
    capacity: Annotated[int, _SPOOL_RECORDS] = Field(default=10_000, gt=0)
    max_bytes: Annotated[int, _BYTES] = Field(default=8 * 1024 * 1024, gt=0)
    full_policy: SpoolFullPolicy = SpoolFullPolicy.TRIM_OLDEST
    flush_on_write: bool = True
    sync_on_write: bool = False

    @field_validator("path")
    @classmethod
    def _require_a_file(cls, value: Path) -> Path:
        """Refuse a path that names a directory or nothing at all.

        A spool pointed at a directory fails at the first write, several seconds into a run that
        already looked healthy. Refusing it when the settings are built is what makes the
        failure say "this setting" instead of "this traceback".
        """
        if value.is_dir():
            raise ValueError(f"spool path '{value}' is a directory, not a file")
        if not value.name:
            raise ValueError(f"spool path '{value}' names no file: give it a file name")
        return value

    def summary(self) -> str:
        """One line naming the file, both bounds, and the full policy."""
        return (
            f"spool '{self.path}' capacity={self.capacity} {_SPOOL_RECORDS.name} "
            f"max_bytes={format_number(self.max_bytes)} {_BYTES.name} "
            f"full={self.full_policy.value} flush={self.flush_on_write} sync={self.sync_on_write}"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SpoolEntry:
    """One record as it is stored on disk.

    A spool entry is not a :class:`~opcua_toolkit.DataChangeRecord` in disguise. A record is what
    a subscription delivered; an entry is what a gateway wrote down so that a value is not lost
    because a downstream system was unreachable, and the two have different lifetimes. Reading a
    spool back therefore produces entries, and replaying them is the application's decision about
    what to do with a value that arrived minutes or hours ago.

    Attributes:
        node_id: Canonical NodeId text of the tag the value belongs to.
        tag: The application's name for the tag, when it had one.
        value: The value, as JSON when JSON could hold it. Anything a JSON writer cannot express
            is stored as text with :attr:`value_as_text` set, because a spool that silently
            changed a byte string into something else would be replaying a lie.
        status: The status code name that came with the value.
        source_timestamp / server_timestamp: When the source produced the value and when the
            server sampled it, as aware datetimes.
        arrived_at: When this gateway received it, which is the timestamp a replay is judged by.
        sequence: The arrival sequence the subscription manager gave the record, or 0 when the
            entry was not produced by one. Replay order is file order, never this number.
        replayed: True when the record itself arrived through Republish.
        variant_type: The wire type name the server reported, kept so a replay can say what the
            value used to be.
        value_as_text: True when :attr:`value` had to be stored as text.
    """

    node_id: str
    value: Any
    status: str = "Good"
    tag: str | None = None
    source_timestamp: datetime | None = None
    server_timestamp: datetime | None = None
    arrived_at: datetime | None = None
    sequence: int = 0
    replayed: bool = False
    variant_type: str | None = None
    value_as_text: bool = False

    @classmethod
    def from_record(
        cls, record: DataChangeRecord, *, tag: str | None = None, arrived_at: datetime | None = None
    ) -> SpoolEntry:
        """Build an entry from a record a subscription delivered."""
        value, as_text = _json_safe(record.value)
        return cls(
            node_id=record.node_id_text,
            value=value,
            status=record.status_code.name,
            tag=tag if tag is not None else (record.key or None),
            source_timestamp=record.source_timestamp,
            server_timestamp=record.server_timestamp,
            arrived_at=arrived_at if arrived_at is not None else datetime.now(UTC),
            sequence=record.sequence,
            replayed=record.replayed,
            variant_type=record.variant_type.name if record.variant_type else None,
            value_as_text=as_text,
        )

    def as_line(self) -> str:
        """The one JSON line this entry occupies in the file."""
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation.

        Timestamps become ISO 8601 text with their offset preserved, because a spool read back
        after a restart has to be able to tell "the plant's time" from "this machine's time",
        and a naive timestamp cannot.
        """
        return {
            "node_id": self.node_id,
            "tag": self.tag,
            "value": self.value,
            "value_as_text": self.value_as_text,
            "status": self.status,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
            "server_timestamp": (
                self.server_timestamp.isoformat() if self.server_timestamp else None
            ),
            "arrived_at": self.arrived_at.isoformat() if self.arrived_at else None,
            "sequence": self.sequence,
            "replayed": self.replayed,
            "variant_type": self.variant_type,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpoolEntry:
        """Read an entry back out of a parsed line.

        Raises:
            SpoolError: The payload is not a spool entry. A torn last line from a crash is a
                normal thing to find in a spool file, so a caller that asks for entries gets an
                error naming the line rather than a half-built entry.
        """
        node_id = payload.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise SpoolError(f"spool line has no node_id: {payload!r}")
        return cls(
            node_id=node_id,
            value=payload.get("value"),
            status=str(payload.get("status") or "Good"),
            tag=_optional_text(payload.get("tag")),
            source_timestamp=_parse_timestamp(payload.get("source_timestamp")),
            server_timestamp=_parse_timestamp(payload.get("server_timestamp")),
            arrived_at=_parse_timestamp(payload.get("arrived_at")),
            sequence=int(payload.get("sequence") or 0),
            replayed=bool(payload.get("replayed") or False),
            variant_type=_optional_text(payload.get("variant_type")),
            value_as_text=bool(payload.get("value_as_text") or False),
        )

    @classmethod
    def from_line(cls, line: str) -> SpoolEntry:
        """Parse one line of the spool file.

        Raises:
            SpoolError: The line is not valid JSON, or is valid JSON and not a spool entry.
        """
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SpoolError(f"spool line is not JSON: {exc.msg} at position {exc.pos}") from exc
        if not isinstance(payload, dict):
            raise SpoolError(f"spool line is a {type(payload).__name__}, not an entry")
        return cls.from_dict(payload)

    def describe(self) -> str:
        """One line naming what this entry is and when it arrived."""
        tag = self.tag or self.node_id
        arrived = (
            self.arrived_at.isoformat(sep=" ", timespec="milliseconds")
            if self.arrived_at
            else "no arrival timestamp"
        )
        stored = "stored as text" if self.value_as_text else "stored as JSON"
        return f"{tag}={self.value!r} [{self.status}] {arrived} ({stored})"


@dataclasses.dataclass(frozen=True, slots=True)
class SpoolWrite:
    """What the spool decided about one record.

    Attributes:
        accepted: Whether the record is on disk now.
        entry: The entry that was offered.
        reason: One sentence naming the outcome, including which bound was reached.
        records_after: Records on disk after this write.
        bytes_after: Bytes the file occupies after this write.
        trimmed: Entries removed to make room, which is the list of data this gateway destroyed
            while trying to keep data.
        unreadable_dropped: Lines removed to make room that could not be read as entries at all -
            the torn halves a crash left behind. They are counted apart because "removed a record"
            and "removed damage" are different sentences to have to say.
    """

    accepted: bool
    entry: SpoolEntry
    reason: str
    records_after: int
    bytes_after: int
    trimmed: tuple[SpoolEntry, ...] = ()
    unreadable_dropped: int = 0

    @property
    def trimmed_count(self) -> int:
        """How many spooled records this write destroyed to make room."""
        return len(self.trimmed)

    def describe(self) -> str:
        """One line saying what landed, what was removed, and the size the file ended at."""
        outcome = "spooled" if self.accepted else "refused"
        line = (
            f"{outcome} {self.entry.node_id}: {self.reason} "
            f"[{self.records_after} spooled records, {format_number(self.bytes_after)} bytes]"
        )
        if self.trimmed:
            oldest = self.trimmed[0]
            arrived = oldest.arrived_at.isoformat() if oldest.arrived_at else "unknown"
            extra = (
                f", plus {self.unreadable_dropped} unreadable line(s)"
                if self.unreadable_dropped
                else ""
            )
            line += (
                f" (made room by removing {len(self.trimmed)} spooled record(s), oldest "
                f"{oldest.node_id} arrived {arrived}{extra})"
            )
        elif self.unreadable_dropped:
            line += f" (made room by removing {self.unreadable_dropped} unreadable line(s))"
        return line

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "accepted": self.accepted,
            "node_id": self.entry.node_id,
            "reason": self.reason,
            "records_after": self.records_after,
            "bytes_after": self.bytes_after,
            "trimmed": self.trimmed_count,
            "unreadable_dropped": self.unreadable_dropped,
        }


@dataclasses.dataclass(slots=True)
class SpoolStats:
    """What a spool holds, and what it already admitted to losing.

    Attributes:
        path: The file these numbers describe.
        records: Records on disk.
        bytes_used: Bytes the file occupies.
        capacity: The record bound being enforced.
        max_bytes: The byte bound being enforced.
        appended: Records successfully written.
        refused: Writes the spool turned down.
        trimmed: Records destroyed to make room.
        corrupt_lines: Lines that could not be read as entries, which is what a crash mid-write
            leaves behind and what a replay must not be allowed to mistake for data.
        taken: Records read out and acknowledged by :meth:`JsonlSpool.take`.
        cleared: Records removed on purpose by :meth:`JsonlSpool.clear`.
        oldest / newest: The ends of the file, so a report can say how old the backlog is
            without reading the whole file.
    """

    path: Path
    records: int = 0
    bytes_used: int = 0
    capacity: int = 0
    max_bytes: int = 0
    appended: int = 0
    refused: int = 0
    trimmed: int = 0
    corrupt_lines: int = 0
    taken: int = 0
    cleared: int = 0
    oldest: SpoolEntry | None = None
    newest: SpoolEntry | None = None

    @property
    def lost(self) -> int:
        """Records this spool refused, destroyed, or could not read."""
        return self.refused + self.trimmed + self.corrupt_lines

    @property
    def is_full(self) -> bool:
        """Whether either bound has been reached."""
        return self.records >= self.capacity or self.bytes_used >= self.max_bytes

    def backlog_age_ms(self) -> float | None:
        """Milliseconds since the oldest spooled record arrived, or ``None`` when empty.

        This is the number an operator cares about more than the file size: a spool holding four
        hours of backlog is not "buffering", it is an outage with a paper trail.
        """
        if self.oldest is None or self.oldest.arrived_at is None:
            return None
        return (datetime.now(UTC) - self.oldest.arrived_at).total_seconds() * 1000.0

    def describe(self) -> tuple[str, ...]:
        """Report lines, every number carrying the unit it counts."""
        age = self.backlog_age_ms()
        lines = [
            f"spool file '{self.path}'",
            (
                f"holds {self.records} of {self.capacity} {_SPOOL_RECORDS.name} in "
                f"{format_number(self.bytes_used)} of {format_number(self.max_bytes)} {_BYTES.name}"
            ),
            (
                f"appended {self.appended} taken {self.taken} cleared {self.cleared} "
                f"refused {self.refused} trimmed {self.trimmed} "
                f"unreadable lines {self.corrupt_lines}"
            ),
        ]
        if self.records:
            oldest = self.oldest.arrived_at if self.oldest else None
            oldest_text = (
                oldest.isoformat(sep=" ", timespec="milliseconds") if oldest else "unknown"
            )
            lines.append(f"oldest spooled record arrived {oldest_text}")
            if age is not None:
                lines.append(f"the backlog is {format_number(age / 1000.0)} s old")
        else:
            lines.append("the spool holds nothing")
        lines.append(
            f"this spool has admitted losing {self.lost} record(s): "
            f"{self.refused} refused, {self.trimmed} trimmed, {self.corrupt_lines} unreadable"
        )
        return tuple(lines)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "path": str(self.path),
            "records": self.records,
            "bytes_used": self.bytes_used,
            "capacity": self.capacity,
            "max_bytes": self.max_bytes,
            "appended": self.appended,
            "taken": self.taken,
            "cleared": self.cleared,
            "refused": self.refused,
            "trimmed": self.trimmed,
            "corrupt_lines": self.corrupt_lines,
            "lost": self.lost,
            "is_full": self.is_full,
            "oldest_arrived_at": (
                self.oldest.arrived_at.isoformat()
                if self.oldest and self.oldest.arrived_at
                else None
            ),
            "backlog_age_ms": self.backlog_age_ms(),
        }


class JsonlSpool:
    """A bounded append-only JSONL file that a gateway can write to and replay from.

    Args:
        settings: Where the file is, how big it may get, and what it does when it is full.
    """

    def __init__(self, settings: SpoolSettings) -> None:
        self._settings = settings
        self._stats = SpoolStats(
            path=settings.path, capacity=settings.capacity, max_bytes=settings.max_bytes
        )
        self._loaded = False
        # One lock for the whole file: append, take and clear all rewrite it, and two of them
        # running at once would corrupt the line count the bounds are measured against.
        self._lock = asyncio.Lock()

    @property
    def settings(self) -> SpoolSettings:
        """The settings this spool enforces."""
        return self._settings

    @property
    def path(self) -> Path:
        """The file this spool writes to."""
        return self._settings.path

    def exists(self) -> bool:
        """Whether the spool file is on disk. Cheap, and a fact worth printing before a replay."""
        return self._settings.path.is_file()

    async def open(self) -> SpoolStats:
        """Read the file's own state back, and report what was found in it.

        A spool is the thing that survives the restart, so the first thing a restarted gateway
        does is ask the file what it holds: how many records, how many bytes, and how many lines
        the crash tore in half. Those corrupt lines are counted and skipped rather than turned
        into entries with missing fields.
        """
        async with self._lock:
            return await self._load_locked()

    async def append(
        self, entry: SpoolEntry | DataChangeRecord, *, tag: str | None = None
    ) -> SpoolWrite:
        """Write one record to the spool and report what it cost.

        Args:
            entry: A :class:`SpoolEntry`, or a live :class:`~opcua_toolkit.DataChangeRecord` to
                turn into one.
            tag: The application's name for the record's tag.

        Returns:
            The write: whether it landed, the size the file ended at, and every record that had
            to be destroyed to make room for it.
        """
        offered = entry if isinstance(entry, SpoolEntry) else SpoolEntry.from_record(entry, tag=tag)
        record = _json_safe_entry(offered)
        line = record.as_line()
        async with self._lock:
            await self._load_locked()
            lines, bytes_used = await asyncio.to_thread(self._read_lines)
            room_needed = len(f"{line}\n".encode())
            fits = (
                self._stats.records + 1 <= self._settings.capacity
                and bytes_used + room_needed <= self._settings.max_bytes
            )
            trimmed: list[SpoolEntry] = []
            unreadable_dropped = 0
            kept: list[str] = lines
            if not fits:
                if room_needed > self._settings.max_bytes:
                    self._stats.refused += 1
                    reason = (
                        f"this record alone is {format_number(room_needed)} bytes and the whole "
                        f"spool is bounded at {format_number(self._settings.max_bytes)} "
                        f"{_BYTES.name}: trimming to make room for it would destroy the backlog "
                        "and still not hold it"
                    )
                    _logger.warning("spool %s: %s", self.path, reason)
                    return SpoolWrite(
                        accepted=False,
                        entry=record,
                        reason=reason,
                        records_after=self._stats.records,
                        bytes_after=bytes_used,
                    )
                if self._settings.full_policy is SpoolFullPolicy.REFUSE:
                    self._stats.refused += 1
                    reason = (
                        f"the spool is at its bound ({self._stats.records} of "
                        f"{self._settings.capacity} {_SPOOL_RECORDS.name}, "
                        f"{format_number(bytes_used)} of "
                        f"{format_number(self._settings.max_bytes)} {_BYTES.name}) and "
                        "policy 'refuse' does not remove anything already spooled"
                    )
                    _logger.warning("spool %s: %s", self.path, reason)
                    return SpoolWrite(
                        accepted=False,
                        entry=record,
                        reason=reason,
                        records_after=self._stats.records,
                        bytes_after=bytes_used,
                    )
                kept, trimmed, unreadable_dropped = _trim_to_fit(
                    lines, self._settings.capacity, self._settings.max_bytes, room_needed
                )
                await asyncio.to_thread(self._write_lines, kept)
                self._stats.trimmed += len(trimmed)
            bytes_after = await asyncio.to_thread(self._append_line, line)
            self._stats.records = len(self._entries_in(kept)) + 1
            self._stats.appended += 1
            self._stats.bytes_used = bytes_after
            self._stats.newest = record
            self._stats.oldest = _first_entry(kept) or record
            write = SpoolWrite(
                accepted=True,
                entry=record,
                reason=(
                    "made room by removing the oldest spooled records"
                    if trimmed or unreadable_dropped
                    else "the spool had room"
                ),
                records_after=self._stats.records,
                bytes_after=bytes_after,
                trimmed=tuple(trimmed),
                unreadable_dropped=unreadable_dropped,
            )
            if trimmed or unreadable_dropped:
                _logger.warning("spool %s: %s", self.path, write.describe())
            return write

    async def read(self, limit: int | None = None) -> tuple[SpoolEntry, ...]:
        """Read spooled entries oldest first, without acknowledging them.

        Args:
            limit: How many to read. ``None`` reads all of them.
        """
        async with self._lock:
            await self._load_locked()
            lines, _ = await asyncio.to_thread(self._read_lines)
            entries = self._entries_in(lines)
            return tuple(entries if limit is None else entries[:limit])

    async def take(self, limit: int) -> tuple[SpoolEntry, ...]:
        """Read the oldest entries **and remove them**: replay, then acknowledge.

        A spool that is only ever read is a spool that replays the same records forever. A
        gateway sends a batch, and only after the downstream system accepted it calls this
        method to agree those records are no longer its problem. Taking them before the send
        succeeds is the mistake that turns an outage into data loss.

        Args:
            limit: How many of the oldest entries to remove.

        Returns:
            The removed entries, which is fewer than ``limit`` when the spool held fewer.
        """
        async with self._lock:
            await self._load_locked()
            lines, _ = await asyncio.to_thread(self._read_lines)
            parsed, _corrupt = self._parse_lines(lines, count_corrupt=False)
            consumed = 0
            batch: list[SpoolEntry] = []
            for index, (_, entry) in enumerate(parsed):
                if len(batch) >= limit:
                    break
                if entry is not None:
                    batch.append(entry)
                    consumed = index + 1
            if not batch:
                return ()
            remainder = [line for line, _ in parsed[consumed:]]
            bytes_used = await asyncio.to_thread(self._write_lines, remainder)
            self._stats.records = len(self._entries_in(remainder))
            self._stats.bytes_used = bytes_used
            self._stats.taken += len(batch)
            self._stats.oldest = _first_entry(remainder)
            self._stats.newest = _last_entry(remainder)
            if self._stats.records == 0:
                self._stats.oldest = None
                self._stats.newest = None
            return tuple(batch)

    async def clear(self) -> int:
        """Delete the whole spool file, on purpose, and count what was in it.

        Returns:
            How many spooled records were discarded. Deliberate, so it is kept apart from the
            losses a replay report counts.
        """
        async with self._lock:
            await self._load_locked()
            lines, _ = await asyncio.to_thread(self._read_lines)
            removed = len(self._entries_in(lines))
            await asyncio.to_thread(self._write_lines, [])
            self._stats.records = 0
            self._stats.bytes_used = 0
            self._stats.cleared += removed
            self._stats.oldest = None
            self._stats.newest = None
            return removed

    async def stats(self) -> SpoolStats:
        """This spool's counters, refreshed from the file's own size."""
        async with self._lock:
            await self._load_locked()
            self._stats.bytes_used = await asyncio.to_thread(self._file_bytes)
            lines, _ = await asyncio.to_thread(self._read_lines)
            self._stats.oldest = _first_entry(lines)
            self._stats.newest = _last_entry(lines)
            self._stats.records = len(self._entries_in(lines))
            return self._stats

    async def _load_locked(self) -> SpoolStats:
        """Read the file once per process and make its contents the counters."""
        if self._loaded:
            return self._stats
        lines, bytes_used = await asyncio.to_thread(self._read_lines)
        parsed, _corrupt = self._parse_lines(lines, count_corrupt=True)
        entries = [entry for _, entry in parsed if entry is not None]
        self._stats.records = len(entries)
        self._stats.bytes_used = bytes_used
        self._stats.oldest = entries[0] if entries else None
        self._stats.newest = entries[-1] if entries else None
        self._loaded = True
        return self._stats

    def _entries_in(self, lines: list[str]) -> list[SpoolEntry]:
        """Readable entries in file order, with damage left to the count that already has it."""
        parsed, _corrupt = self._parse_lines(lines, count_corrupt=False)
        return [entry for _, entry in parsed if entry is not None]

    def _parse_lines(
        self, lines: list[str], *, count_corrupt: bool
    ) -> tuple[list[tuple[str, SpoolEntry | None]], int]:
        """Pair every non-empty line with the entry it parses to, or ``None`` when it is torn.

        The pairing keeps the file's own order, which is the order a replay has to follow, and it
        keeps unreadable lines visible instead of quietly dropping them from a rewrite.
        :attr:`count_corrupt` is set only when the file is read back: a torn line is one fact, and
        counting it again on every read would make a single crash look like a growing problem.
        """
        parsed: list[tuple[str, SpoolEntry | None]] = []
        corrupt = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                parsed.append((line, SpoolEntry.from_line(line)))
            except SpoolError as exc:
                corrupt += 1
                parsed.append((line, None))
                if count_corrupt:
                    _logger.warning("spool %s: unreadable line: %s", self.path, exc)
        if count_corrupt:
            self._stats.corrupt_lines += corrupt
        return parsed, corrupt

    def _read_lines(self) -> tuple[list[str], int]:
        """The file's lines and its byte size. Runs in a thread: this is the blocking part."""
        path = self._settings.path
        if not path.is_file():
            return [], 0
        data = path.read_bytes()
        return data.decode("utf-8").splitlines(), len(data)

    def _write_lines(self, lines: list[str]) -> int:
        """Rewrite the file from scratch and return its new size."""
        path = self._settings.path
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{line}\n" for line in lines if line.strip())
        with path.open("wb") as handle:
            handle.write(body.encode("utf-8"))
            handle.flush()
            if self._settings.sync_on_write:
                os.fsync(handle.fileno())
        return len(body.encode("utf-8"))

    def _append_line(self, line: str) -> int:
        """Append one line and return the file's new size.

        The line is written in a single call on purpose: a record split across two writes is how
        a crash produces the torn line that :meth:`_load_locked` then has to count.
        """
        path = self._settings.path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = f"{line}\n".encode()
        with path.open("ab") as handle:
            handle.write(payload)
            if self._settings.flush_on_write:
                handle.flush()
            if self._settings.sync_on_write:
                os.fsync(handle.fileno())
            return handle.seek(0, os.SEEK_END)

    def _file_bytes(self) -> int:
        """Bytes the file occupies, or 0 when it does not exist."""
        path = self._settings.path
        if not path.is_file():
            return 0
        return path.stat().st_size

    def describe(self) -> tuple[str, ...]:
        """The settings line for this spool; call :meth:`stats` for the file's own report."""
        return (self._settings.summary(),)


def _trim_to_fit(
    lines: list[str], capacity: int, max_bytes: int, incoming_bytes: int
) -> tuple[list[str], list[SpoolEntry], int]:
    """Remove lines from the front until the incoming record fits.

    Both bounds are measured against the file's *lines*, not against readable entries: a torn line
    takes up room too, and a spool that counted only the readable ones would blow through its byte
    bound on a file full of crash damage.

    Returns:
        The lines to keep, the entries that were destroyed (in the order they were stored, which is
        the order a report should name them in), and how many of the removed lines could not be
        read as entries at all. Those are reported on the write instead of being invented into
        entries, and they are not added to the corrupt-line count again: the read that found them
        already counted them once.
    """
    kept = [line for line in lines if line.strip()]
    removed: list[SpoolEntry] = []
    torn = 0
    while kept and (len(kept) + 1 > capacity or _total_bytes(kept) + incoming_bytes > max_bytes):
        dropped = kept.pop(0)
        try:
            removed.append(SpoolEntry.from_line(dropped))
        except SpoolError:
            torn += 1
    return kept, removed, torn


def _total_bytes(lines: list[str]) -> int:
    """The size a file holding these lines would occupy."""
    return sum(len(f"{line}\n".encode()) for line in lines if line.strip())


def _first_entry(lines: list[str]) -> SpoolEntry | None:
    """Parse the first readable line, or ``None`` when the file is empty or unreadable."""
    for line in lines:
        if not line.strip():
            continue
        try:
            return SpoolEntry.from_line(line)
        except SpoolError:
            continue
    return None


def _last_entry(lines: list[str]) -> SpoolEntry | None:
    """Parse the last readable line, or ``None`` when the file is empty or unreadable."""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            return SpoolEntry.from_line(line)
        except SpoolError:
            continue
    return None


def _json_safe(value: Any) -> tuple[Any, bool]:
    """Return a value JSON can write, and whether it had to be turned into text.

    A spool stores what a gateway knew, not what a JSON encoder found convenient. Byte strings,
    datetimes and NodeIds are real OPC UA values, so they are kept as text with the flag set
    instead of being dropped or quietly rewritten: a replay then says "this value came back as
    text" rather than pretending it is still a number.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value), True
    return value, False


def _json_safe_entry(entry: SpoolEntry) -> SpoolEntry:
    """Return the entry with a value this file format can actually hold.

    Entries built from a live record are already normalised. Entries written by hand are not, and
    a gateway that crashed on the first byte string it tried to spool would be a spool that lost
    the outage it was there to survive.
    """
    value, as_text = _json_safe(entry.value)
    if as_text and not entry.value_as_text:
        return dataclasses.replace(entry, value=value, value_as_text=True)
    if as_text:
        return dataclasses.replace(entry, value=value)
    return entry


def _optional_text(value: Any) -> str | None:
    """A string field or ``None``, without turning a missing field into the text ``"None"``."""
    return value if isinstance(value, str) else None


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp, and refuse one that lost its offset.

    A naive timestamp in a spool file is not a missing fact, it is a wrong one: it reads as
    local time on whatever machine replays it. A spool written by this toolkit always carries an
    offset, so anything that arrives without one is reported instead of trusted.

    Raises:
        SpoolError: The text is not an ISO 8601 timestamp, or it has no offset.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise SpoolError(f"spool timestamp {value!r} is not ISO 8601 text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SpoolError(f"spool timestamp {value!r} is not ISO 8601 text: {exc}") from exc
    if parsed.tzinfo is None:
        raise SpoolError(
            f"spool timestamp {value!r} has no UTC offset: a naive timestamp would be "
            "replayed as this machine's local time"
        )
    return parsed


__all__ = [
    "JsonlSpool",
    "SpoolEntry",
    "SpoolFullPolicy",
    "SpoolSettings",
    "SpoolStats",
    "SpoolWrite",
]
