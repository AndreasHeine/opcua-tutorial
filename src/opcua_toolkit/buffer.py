"""Application-side buffering: what a gateway keeps, what it drops, and what it admits to.

Chapter 10 established that a *server* queues values per monitored item, and Chapter 12
established that a session can die while the application keeps running. Those two facts meet
here. A monitored item's queue is the server's memory, sized by the server's cap, and it dies
with the subscription: when the session goes, so does every value that had not been published
yet. Nothing in OPC UA standardises the other side - what a client keeps after the publish
arrived - which is why a gateway that must survive an outage has to build that part itself.

Three queues sit between a tag and a historian, and each one has a different owner:

* the **MonitoredItem queue**, sized by ``queueSize`` and drained by each publish
  (OPC 10000-4, 5.13.1.5 *Queue parameters*). For a data monitored item, Table 140 in 7.21
  gives the values ``0`` and ``1`` a special meaning - the server answers with
  ``revisedQueueSize`` 1, "effectively disabling queuing" - and 5.13.1.5 describes a queue of
  one as "a buffer that always contains the newest Notification", with the discard policy
  ignored. ``discardOldest`` decides what is thrown away when the queue is full: ``TRUE``
  removes the oldest entry and appends the new one, ``FALSE`` replaces the last entry added
  with the new one. When a queued value is discarded and the queue is larger than one, the
  Overflow bit in the ``InfoBits`` portion of the ``DataValue`` status code is set on the value
  that survives (5.13.1.5 and 7.25.2).
* the **client library's own queue**, which is where a slow consumer gets dealt with. asyncua's
  iterator-mode subscription keeps an ``asyncio.Queue(maxsize=queue_maxsize)`` and applies its
  own ``OverflowPolicy`` when that queue is full - ``DROP_OLDEST``, ``DROP_NEWEST``, ``WARN``
  or ``DISCONNECT``. This queue is real, bounded, and silent: a record removed here never
  reaches an application counter, because it never reached the application.
* the **application's buffer**, this module. It is the only one of the three that can say what
  it lost.

The policies below are the client-side form of the choice the specification makes, plus the one
a client must not take silently:

* :attr:`DropPolicy.KEEP_LATEST` is ``discardOldest=TRUE``: the oldest stored record is removed
  to make room, so the buffer always ends with the newest records.
* :attr:`DropPolicy.KEEP_WINDOW` is ``discardOldest=FALSE``: the newest stored record is
  replaced by the incoming one, so the older part of the window survives.
* :attr:`DropPolicy.REJECT_NEW` refuses the incoming record. It is the policy for a buffer a
  human or a downstream system reads as *a run of consecutive values*: a record that arrives
  while the window is full is not inserted into a window it does not belong to.
* :attr:`DropPolicy.WAIT` applies back-pressure instead of deciding. It is also the policy that
  moves the decision somewhere else when it is used in the wrong place: waiting inside the
  subscription's consumer task lets the library's queue fill up and *its* overflow policy
  applies, which is the silent loss this module exists to prevent. Use it between two
  application tasks, never inside a handler that runs on the publish path.

One rule runs through all four: **every removal is reported**. A buffer that discards silently
is a buffer that lies, and an application that cannot say how many values it threw away cannot
answer the only question anyone asks about a historian - "what is missing?".
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opcua_toolkit.settings import (
    MillisecondsOrNone,
    ValueUnit,
    format_milliseconds,
)
from opcua_toolkit.subscriptions.model import DataChangeRecord, NotificationCallback

_logger = logging.getLogger(__name__)

_RECORDS = ValueUnit("records")


class DropPolicy(StrEnum):
    """What a full buffer does with the next record.

    Attributes:
        KEEP_LATEST: Discard the oldest stored record to make room. The buffer always ends with
            the most recent records, which is what a value cache or an HMI mirror needs.
        KEEP_WINDOW: Replace the newest stored record with the incoming one. The start of the
            window stays and the tail collapses, which is what a bounded last-N-samples view
            wants - and the only policy that leaves a hole in the middle of a sequence.
        REJECT_NEW: Refuse the incoming record. What is stored stays one run of consecutive
            arrivals, and the buffer stops accepting anything while it is full.
        WAIT: Wait for room instead of deciding, up to :attr:`BufferSettings.max_wait`, and
            refuse afterwards. A producer that waits forever is a producer that stopped
            collecting, so the wait is bounded and the refusal is counted.
    """

    KEEP_LATEST = "keep_latest"
    KEEP_WINDOW = "keep_window"
    REJECT_NEW = "reject_new"
    WAIT = "wait"


class BufferSettings(BaseModel):
    """How much one buffering point keeps, and what it does when it is full.

    Attributes:
        capacity: Records this buffer holds at most.
        policy: What happens to the next record when that many are already stored.
        max_wait: How long a :attr:`DropPolicy.WAIT` offer may wait for room, in milliseconds.
            Required for that policy and refused for every other one: a setting no policy can
            read is a setting nobody will ever notice is wrong.
    """

    model_config = ConfigDict(frozen=True)

    capacity: Annotated[int, _RECORDS] = Field(
        default=100, gt=0, description="records held at most"
    )
    policy: DropPolicy = DropPolicy.KEEP_LATEST
    max_wait: MillisecondsOrNone = Field(
        default=None, description="how long a wait-policy offer may wait for room"
    )

    @model_validator(mode="after")
    def _check_the_wait_budget(self) -> BufferSettings:
        """Refuse the two shapes that make ``max_wait`` a lie."""
        if self.policy is DropPolicy.WAIT and self.max_wait is None:
            raise ValueError(
                f"policy {self.policy.value} must say how long it may wait: max_wait is required"
            )
        if self.policy is not DropPolicy.WAIT and self.max_wait is not None:
            raise ValueError(
                f"max_wait is not used by policy {self.policy.value}: remove it or choose 'wait'"
            )
        return self

    @property
    def waits(self) -> bool:
        """Whether this policy applies back-pressure instead of deciding immediately."""
        return self.policy is DropPolicy.WAIT

    def summary(self) -> str:
        """One line naming the capacity, the policy, and the wait budget when there is one."""
        line = f"capacity={self.capacity} {_RECORDS.name} policy={self.policy.value}"
        if self.max_wait is not None:
            line += f" max_wait={format_milliseconds(self.max_wait)}"
        return line


@dataclasses.dataclass(frozen=True, slots=True)
class BufferDecision:
    """What a buffer decided about one record, including what the decision cost.

    Attributes:
        accepted: Whether the record is stored now.
        policy: The policy that decided, carried so a log line explains itself.
        record: The record that was offered.
        discarded: A record that was already stored and had to be removed to make room, or
            ``None``. This is what makes a loss report honest: the record an application will
            never see again is named here instead of only counted in a total.
        reason: One sentence saying why, in words an operator can act on.
        depth_after: How many records the buffer holds after the decision.
        waited_ms: How long this offer waited for room, for :attr:`DropPolicy.WAIT`.
        timed_out: True when a wait ended in a refusal instead of in room.
    """

    accepted: bool
    policy: DropPolicy
    record: DataChangeRecord
    discarded: DataChangeRecord | None
    reason: str
    depth_after: int
    waited_ms: float = 0.0
    timed_out: bool = False

    @property
    def lost(self) -> bool:
        """Whether this decision cost a record: the offered one, a stored one, or both."""
        return not self.accepted or self.discarded is not None

    def describe(self) -> str:
        """One line naming the decision, its cost, and the buffer's depth afterwards."""
        outcome = "kept" if self.accepted else "refused"
        line = f"{outcome} seq={self.record.sequence}: {self.reason}"
        if self.discarded is not None:
            line += f" (made room by removing seq={self.discarded.sequence})"
        if self.waited_ms > 0.0:
            line += f" after waiting {format_milliseconds(self.waited_ms)}"
        return f"{line} [{self.depth_after} {_RECORDS.name} stored]"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "accepted": self.accepted,
            "policy": self.policy.value,
            "sequence": self.record.sequence,
            "discarded_sequence": (self.discarded.sequence if self.discarded is not None else None),
            "reason": self.reason,
            "depth_after": self.depth_after,
            "waited_ms": self.waited_ms,
            "timed_out": self.timed_out,
        }


@dataclasses.dataclass(slots=True)
class BufferStats:
    """Counters that let a buffer answer "what did you lose?" in one report.

    Attributes:
        capacity: The limit this buffer is enforcing.
        policy: The policy that produced the numbers below.
        depth: Records stored right now.
        peak_depth: The most it ever held, which is what says whether a capacity was chosen or
            merely happened to be enough.
        accepted: Offers that stored the record.
        refused: Offers that did not.
        discarded_oldest: Records removed from the front by :attr:`DropPolicy.KEEP_LATEST`.
        collapsed: Records replaced at the back by :attr:`DropPolicy.KEEP_WINDOW`.
        drained: Records a consumer took out. Not a loss: they left because somebody asked.
        drains: Drain calls that removed at least one record.
        cleared: Records removed on purpose. A decision, not an accident, so it is kept apart
            from the losses instead of inflating them.
        waits: Offers that had to wait under :attr:`DropPolicy.WAIT`.
        waited_ms: Total time those offers waited, which is the price of back-pressure.
        timeouts: Waits that ended in a refusal.
        oldest_sequence / newest_sequence: Arrival sequence numbers of the stored records,
            which is what makes :attr:`contiguous` computable instead of assumed.
    """

    capacity: int
    policy: DropPolicy
    depth: int = 0
    peak_depth: int = 0
    accepted: int = 0
    refused: int = 0
    discarded_oldest: int = 0
    collapsed: int = 0
    drained: int = 0
    drains: int = 0
    cleared: int = 0
    waits: int = 0
    waited_ms: float = 0.0
    timeouts: int = 0
    oldest_sequence: int | None = None
    newest_sequence: int | None = None

    @property
    def lost(self) -> int:
        """Every record that left this buffer without a consumer taking it."""
        return self.refused + self.discarded_oldest + self.collapsed

    @property
    def contiguous(self) -> bool:
        """Whether what is stored is one unbroken run of arrival sequence numbers.

        ``KEEP_WINDOW`` breaks this on purpose: replacing the tail leaves a hole where the
        replaced record used to be. A reader who assumes a buffer holds consecutive values has
        to be told otherwise, and this is the sentence that does it.
        """
        if self.depth <= 1:
            return True
        if self.oldest_sequence is None or self.newest_sequence is None:
            return False
        return self.newest_sequence - self.oldest_sequence + 1 == self.depth

    @property
    def is_full(self) -> bool:
        """Whether the buffer is at its capacity right now."""
        return self.depth >= self.capacity

    @property
    def ever_hit_the_limit(self) -> bool:
        """Whether this buffer was ever the thing that decided, instead of just holding."""
        return self.peak_depth >= self.capacity or self.lost > 0

    def describe(self) -> tuple[str, ...]:
        """Report lines, every number in them carrying the unit it counts."""
        held = "holds nothing"
        if self.depth:
            span = f"seq={self.oldest_sequence}..{self.newest_sequence}"
            gap = "" if self.contiguous else " with a gap"
            held = f"holds {self.depth} of {self.capacity} {_RECORDS.name} ({span}{gap})"
        return (
            f"policy={self.policy.value} capacity={self.capacity} {_RECORDS.name}",
            held,
            (
                f"accepted {self.accepted} refused {self.refused} "
                f"discarded_oldest {self.discarded_oldest} collapsed {self.collapsed}"
            ),
            (
                f"drained {self.drained} {_RECORDS.name} in {self.drains} drain(s) "
                f"cleared {self.cleared} peak_depth {self.peak_depth}"
            ),
            (
                f"waited {format_milliseconds(self.waited_ms)} across {self.waits} wait(s), "
                f"{self.timeouts} ended in a refusal"
            ),
            f"this buffer has admitted losing {self.lost} {_RECORDS.name}",
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "capacity": self.capacity,
            "policy": self.policy.value,
            "depth": self.depth,
            "peak_depth": self.peak_depth,
            "accepted": self.accepted,
            "refused": self.refused,
            "discarded_oldest": self.discarded_oldest,
            "collapsed": self.collapsed,
            "drained": self.drained,
            "drains": self.drains,
            "cleared": self.cleared,
            "waits": self.waits,
            "waited_ms": self.waited_ms,
            "timeouts": self.timeouts,
            "lost": self.lost,
            "contiguous": self.contiguous,
            "oldest_sequence": self.oldest_sequence,
            "newest_sequence": self.newest_sequence,
        }


class NotificationBuffer:
    """A bounded queue of records that reports every record it cannot keep.

    This is deliberately not a queue abstraction with a strategy object plugged into it: the
    four policies are four sentences each, and the point of reading them is knowing which one
    is hiding something from you. It is filled the way a :class:`~opcua_toolkit.SubscriptionManager`
    hands records to something - see :func:`buffering_handler` - and emptied by the
    application's own drain task.

    Args:
        settings: What to keep and what to do when it is full. Defaults to 100 records, keeping
            the latest.
        name: Labels this buffer in its own reports and log lines, because a gateway has more
            than one buffer and a log line saying "buffer full" is not a fact about anything.
    """

    def __init__(self, settings: BufferSettings | None = None, *, name: str = "buffer") -> None:
        self._settings = settings if settings is not None else BufferSettings()
        self._name = name
        self._records: collections.deque[DataChangeRecord] = collections.deque()
        self._stats = BufferStats(capacity=self._settings.capacity, policy=self._settings.policy)
        # Level-triggered on purpose. An offer clears the event and re-checks the depth before
        # it waits, so a drain that landed just before the check is seen instead of being
        # waited for, and a drain that lands during the wait wakes it. That ordering is the
        # whole race this class has, and it is settled by checking twice.
        self._space = asyncio.Event()
        self._space.set()

    @property
    def name(self) -> str:
        """The label this buffer reports under."""
        return self._name

    @property
    def settings(self) -> BufferSettings:
        """The settings this buffer enforces."""
        return self._settings

    @property
    def capacity(self) -> int:
        """Records this buffer holds at most."""
        return self._settings.capacity

    @property
    def policy(self) -> DropPolicy:
        """What the buffer does when it is full."""
        return self._settings.policy

    @property
    def stats(self) -> BufferStats:
        """Live counters, refreshed with the current depth and sequence span."""
        self._stats.depth = len(self._records)
        self._stats.oldest_sequence = self._records[0].sequence if self._records else None
        self._stats.newest_sequence = self._records[-1].sequence if self._records else None
        return self._stats

    def __len__(self) -> int:
        """Records stored right now."""
        return len(self._records)

    def records(self) -> tuple[DataChangeRecord, ...]:
        """What is stored, oldest first, as a snapshot a caller may keep."""
        return tuple(self._records)

    def peek_oldest(self) -> DataChangeRecord | None:
        """The record a drain would hand over first, or ``None`` when empty."""
        return self._records[0] if self._records else None

    def peek_newest(self) -> DataChangeRecord | None:
        """The newest record stored, which every policy keeps except ``REJECT_NEW``."""
        return self._records[-1] if self._records else None

    def oldest_age_ms(self) -> float | None:
        """Milliseconds since the oldest stored record's timestamps, or ``None`` when empty.

        A buffer that is not full can still be worthless: what it holds may be from before the
        outage. "How old is the oldest thing I am still holding" is the other half of the loss
        report, and it is the half that decides whether a replay is worth sending at all.
        """
        oldest = self.peek_oldest()
        if oldest is None:
            return None
        clock = oldest.source_timestamp or oldest.server_timestamp
        if clock is None:
            return None
        return (datetime.now(UTC) - clock).total_seconds() * 1000.0

    async def offer(self, record: DataChangeRecord) -> BufferDecision:
        """Offer one record to the buffer and report what happened to it.

        For the three deciding policies this never yields to the event loop. Only
        :attr:`DropPolicy.WAIT` awaits, and only when the buffer is already full.

        Args:
            record: The record to store.

        Returns:
            The decision, naming the record offered, the record removed to make room if any,
            and the reason this buffer gave itself.
        """
        if self._settings.waits and len(self._records) >= self.capacity:
            return await self._offer_waiting(record)
        return self._decide(record, waited_ms=0.0)

    def drain(self, max_records: int | None = None) -> tuple[DataChangeRecord, ...]:
        """Take records out oldest first, and count them as delivered rather than lost.

        Args:
            max_records: How many to take. ``None`` takes everything.
        """
        if not self._records:
            return ()
        wanted = len(self._records) if max_records is None else min(max_records, len(self._records))
        taken = tuple(self._records.popleft() for _ in range(wanted))
        self._stats.drained += len(taken)
        self._stats.drains += 1
        self._space.set()
        return taken

    def clear(self) -> int:
        """Throw away everything stored, on purpose, and count it as a decision.

        Returns:
            How many records were removed. These are not losses: the application decided, and
            the number is kept apart so that a loss report stays a loss report.
        """
        removed = len(self._records)
        self._records.clear()
        self._stats.cleared += removed
        self._space.set()
        return removed

    def describe(self) -> tuple[str, ...]:
        """This buffer's own report lines, labelled with its name."""
        return (f"buffer '{self._name}': {self._settings.summary()}", *self.stats.describe())

    async def _offer_waiting(self, record: DataChangeRecord) -> BufferDecision:
        """Wait for room under :attr:`DropPolicy.WAIT`, then decide.

        The wait is bounded by :attr:`BufferSettings.max_wait`, and a wait that ends without
        room is reported as a refusal with the waiting already paid for. That is the policy's
        whole point: back-pressure is a cost the application agrees to pay, and a counted
        refusal is what it looks like when the cost was not enough.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        budget_ms = float(self._settings.max_wait or 0.0)
        budget_s = budget_ms / 1000.0
        self._stats.waits += 1
        room = False
        while True:
            if len(self._records) < self.capacity:
                room = True
                break
            remaining = budget_s - (loop.time() - started)
            if remaining <= 0.0:
                break
            self._space.clear()
            try:
                # ``asyncio.timeout`` counts seconds; this setting counts milliseconds. Mixing
                # them up is a wait 1000 times longer than the operator asked for.
                async with asyncio.timeout(remaining):
                    await self._space.wait()
            except TimeoutError:
                break
        waited = (loop.time() - started) * 1000.0
        self._stats.waited_ms += waited
        if room:
            return self._decide(record, waited_ms=waited)

        self._stats.refused += 1
        self._stats.timeouts += 1
        _logger.warning(
            "buffer %s refused seq=%d: %d of %d records stored and no room appeared in %s",
            self._name,
            record.sequence,
            len(self._records),
            self.capacity,
            format_milliseconds(waited),
        )
        return BufferDecision(
            accepted=False,
            policy=self.policy,
            record=record,
            discarded=None,
            reason=(
                f"no room appeared in {format_milliseconds(waited)} of waiting: the consumer "
                f"took nothing while {self.capacity} {_RECORDS.name} were stored"
            ),
            depth_after=len(self._records),
            waited_ms=waited,
            timed_out=True,
        )

    def _decide(self, record: DataChangeRecord, *, waited_ms: float) -> BufferDecision:
        """Store one record, or apply the policy to it, and report the result either way."""
        discarded: DataChangeRecord | None = None
        accepted = True
        if len(self._records) < self.capacity:
            reason = "room was available"
        elif self.policy is DropPolicy.REJECT_NEW:
            accepted = False
            reason = (
                f"policy '{self.policy.value}' adds nothing to a full buffer: all "
                f"{self.capacity} stored records are older than this one"
            )
        elif self.policy is DropPolicy.KEEP_WINDOW:
            discarded = self._records.pop()
            self._stats.collapsed += 1
            reason = (
                "policy 'keep_window' replaced the newest stored record so that the older "
                "part of the window stays intact"
            )
        else:
            discarded = self._records.popleft()
            self._stats.discarded_oldest += 1
            reason = "policy 'keep_latest' removed the oldest stored record to make room"

        if accepted:
            self._records.append(record)
            self._stats.accepted += 1
        else:
            self._stats.refused += 1
        self._stats.peak_depth = max(self._stats.peak_depth, len(self._records))

        decision = BufferDecision(
            accepted=accepted,
            policy=self.policy,
            record=record,
            discarded=discarded,
            reason=reason,
            depth_after=len(self._records),
            waited_ms=waited_ms,
            timed_out=False,
        )
        if decision.lost:
            _logger.warning("buffer %s: %s", self._name, decision.describe())
        return decision


def buffering_handler(
    buffer: NotificationBuffer,
    *,
    on_lost: Callable[[BufferDecision], Awaitable[None]] | None = None,
) -> NotificationCallback:
    """Build a Chapter 11 handler that hands every record to a buffer.

    The handler never raises, and it never waits for a consumer except where the buffer's own
    policy says so. A handler that blocks is how a slow historian turns into a dead
    subscription, so the buffering point is a handler plus a drain task - not a handler that
    writes to a database.

    Args:
        buffer: The buffer every record is offered to.
        on_lost: Called for every decision that cost a record - a refusal, or a stored record
            removed to make room. This is where a gateway spills to a spool, bumps a metric or
            raises an alarm. It runs on the subscription's consumer task, so it must not block
            either.
    """

    async def handler(record: DataChangeRecord) -> None:
        decision = await buffer.offer(record)
        if decision.lost and on_lost is not None:
            await on_lost(decision)

    return handler


async def offer_many(
    buffer: NotificationBuffer, records: Iterable[DataChangeRecord]
) -> tuple[BufferDecision, ...]:
    """Offer a run of records to a buffer and return every decision, in order.

    A helper for tests, examples and replays. A buffer's behaviour is only understandable by
    reading its decisions one after another, and a caller that offers a list and then looks at
    the counters has already thrown away the interesting part.
    """
    return tuple([await buffer.offer(record) for record in records])


__all__ = [
    "BufferDecision",
    "BufferSettings",
    "BufferStats",
    "DropPolicy",
    "NotificationBuffer",
    "buffering_handler",
    "offer_many",
]
