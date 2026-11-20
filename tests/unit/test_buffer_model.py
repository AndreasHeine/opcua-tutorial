"""Chapter 15 unit tests: what a buffer keeps, what it drops, and what it admits to.

No server and no transport here: the four policies are pure decisions, and a decision is only
worth having if it can be read back afterwards. Every test therefore asserts on the decisions a
buffer produced and on the counters it reports, not on the values sitting inside it - the values
are the easy part, and the report is the part a gateway is judged on.

The two waiting tests are the only ones that touch the event loop, and they wait on a real drain
instead of a sleep: a buffer that wakes up for room is a fact about the class, and a buffer that
runs out of waiting budget is a fact about the configuration.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from asyncua import ua
from pydantic import ValidationError

from opcua_toolkit.buffer import (
    BufferSettings,
    DropPolicy,
    NotificationBuffer,
    buffering_handler,
    offer_many,
)
from opcua_toolkit.subscriptions.model import DEFAULT_SUBSCRIPTION_KEY, DataChangeRecord

BASE = datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)


def make_record(sequence: int, **overrides: object) -> DataChangeRecord:
    """One record with a distinct sequence, the only field a buffer policy cares about."""
    fields: dict[str, object] = {
        "node_id": ua.NodeId(f"Tag{sequence}", 2),
        "value": float(sequence),
        "status_code": ua.StatusCode(0),
        "source_timestamp": BASE + timedelta(seconds=sequence),
        "server_timestamp": BASE + timedelta(seconds=sequence),
        "client_handle": 200,
        "key": DEFAULT_SUBSCRIPTION_KEY,
        "sequence": sequence,
    }
    fields.update(overrides)
    return DataChangeRecord(**fields)  # type: ignore[arg-type]


def sequences_in(buffer: NotificationBuffer) -> tuple[int, ...]:
    """The arrival order the buffer is currently holding."""
    return tuple(record.sequence for record in buffer.records())


def test_settings_refuse_a_capacity_of_zero() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        BufferSettings(capacity=0)


def test_a_wait_policy_without_a_budget_is_refused() -> None:
    """An unbounded wait is not a policy, it is a gateway that stopped collecting."""
    with pytest.raises(ValidationError, match="max_wait is required"):
        BufferSettings(policy=DropPolicy.WAIT)


def test_a_wait_budget_for_a_policy_that_never_waits_is_refused() -> None:
    """A setting no policy can read is a setting nobody will notice is wrong."""
    with pytest.raises(ValidationError, match="not used by policy"):
        BufferSettings(policy=DropPolicy.KEEP_LATEST, max_wait=250.0)


def test_summary_prints_the_unit_next_to_every_number() -> None:
    settings = BufferSettings(capacity=3, policy=DropPolicy.WAIT, max_wait=250.0)
    assert settings.summary() == "capacity=3 records policy=wait max_wait=250 ms"


async def test_keep_latest_discards_the_oldest_and_always_ends_with_the_newest() -> None:
    """The client-side form of the specification's ``discardOldest=TRUE``."""
    buffer = NotificationBuffer(BufferSettings(capacity=3, policy=DropPolicy.KEEP_LATEST))
    decisions = await offer_many(buffer, [make_record(n) for n in range(1, 6)])

    assert sequences_in(buffer) == (3, 4, 5)
    assert [decision.accepted for decision in decisions] == [True] * 5
    assert [decision.discarded is None for decision in decisions] == [
        True,
        True,
        True,
        False,
        False,
    ]
    dropped = [
        decision.discarded.sequence if decision.discarded is not None else 0
        for decision in decisions
    ]
    assert dropped == [0, 0, 0, 1, 2]
    stats = buffer.stats
    assert stats.accepted == 5
    assert stats.discarded_oldest == 2
    assert stats.refused == 0
    assert stats.lost == 2
    assert stats.peak_depth == 3
    assert stats.contiguous is True


async def test_keep_window_replaces_the_newest_and_leaves_a_hole() -> None:
    """The client-side form of ``discardOldest=FALSE``, and the report that admits the hole.

    What is stored is the oldest part of the window plus the newest arrival. That is a useful
    shape and a misleading one at the same time, which is why the stats carry
    ``contiguous == False`` instead of leaving the reader to do the arithmetic.
    """
    buffer = NotificationBuffer(BufferSettings(capacity=3, policy=DropPolicy.KEEP_WINDOW))
    decisions = await offer_many(buffer, [make_record(n) for n in range(1, 6)])

    assert sequences_in(buffer) == (1, 2, 5)
    assert decisions[3].discarded is not None
    assert decisions[3].discarded.sequence == 3
    stats = buffer.stats
    assert stats.collapsed == 2
    assert stats.discarded_oldest == 0
    assert stats.lost == 2
    assert stats.contiguous is False
    assert stats.newest_sequence == 5
    assert stats.oldest_sequence == 1


async def test_reject_new_keeps_one_unbroken_run_and_refuses_the_rest() -> None:
    """A window that a reader may trust as consecutive values costs every record after it."""
    buffer = NotificationBuffer(BufferSettings(capacity=2, policy=DropPolicy.REJECT_NEW))
    decisions = await offer_many(buffer, [make_record(n) for n in range(1, 5)])

    assert sequences_in(buffer) == (1, 2)
    assert [decision.accepted for decision in decisions] == [True, True, False, False]
    assert [decision.discarded for decision in decisions] == [None, None, None, None]
    stats = buffer.stats
    assert stats.refused == 2
    assert stats.lost == 2
    assert stats.contiguous is True
    assert "adds nothing to a full buffer" in decisions[2].reason


async def test_every_decision_names_the_record_it_cost() -> None:
    """A total is a statistic; a named record is evidence."""
    buffer = NotificationBuffer(BufferSettings(capacity=1, policy=DropPolicy.KEEP_LATEST))
    decisions = await offer_many(buffer, [make_record(10), make_record(11)])

    assert decisions[1].discarded is not None
    assert decisions[1].discarded.sequence == 10
    assert decisions[1].lost is True
    line = decisions[1].describe()
    assert "kept seq=11" in line
    assert "made room by removing seq=10" in line
    assert "1 records stored" in line


async def test_a_drain_is_a_delivery_and_not_a_loss() -> None:
    buffer = NotificationBuffer(BufferSettings(capacity=2, policy=DropPolicy.KEEP_LATEST))
    await offer_many(buffer, [make_record(1), make_record(2)])
    taken = buffer.drain(1)

    assert tuple(record.sequence for record in taken) == (1,)
    stats = buffer.stats
    assert stats.drained == 1
    assert stats.drains == 1
    assert stats.lost == 0
    assert stats.depth == 1


async def test_draining_the_whole_buffer_empties_it_and_counts_nothing_as_lost() -> None:
    buffer = NotificationBuffer(BufferSettings(capacity=3))
    await offer_many(buffer, [make_record(n) for n in range(1, 4)])
    taken = buffer.drain()

    assert len(taken) == 3
    assert len(buffer) == 0
    assert buffer.stats.lost == 0
    assert buffer.peek_oldest() is None
    assert buffer.peek_newest() is None


async def test_clear_is_a_decision_and_is_counted_apart_from_the_losses() -> None:
    """A gateway that throws data away on purpose must not hide it in the loss column."""
    buffer = NotificationBuffer(BufferSettings(capacity=2, policy=DropPolicy.KEEP_LATEST))
    await offer_many(buffer, [make_record(n) for n in range(1, 5)])
    removed = buffer.clear()

    assert removed == 2
    stats = buffer.stats
    assert stats.cleared == 2
    assert stats.lost == 2
    assert stats.depth == 0


async def test_a_wait_policy_waits_for_a_drain_instead_of_dropping() -> None:
    """Back-pressure only works if the offer actually parks instead of deciding."""
    buffer = NotificationBuffer(
        BufferSettings(capacity=1, policy=DropPolicy.WAIT, max_wait=1_000.0), name="live"
    )
    await buffer.offer(make_record(1))

    waiter = asyncio.create_task(buffer.offer(make_record(2)))
    await asyncio.sleep(0.02)
    assert waiter.done() is False, "a full wait-policy buffer must not have decided yet"

    assert len(buffer.drain(1)) == 1
    decision = await waiter

    assert decision.accepted is True
    assert decision.waited_ms > 0.0
    stats = buffer.stats
    assert stats.waits == 1
    assert stats.timeouts == 0
    assert stats.waited_ms >= decision.waited_ms


async def test_a_wait_that_runs_out_is_a_counted_refusal() -> None:
    """The bounded half of back-pressure: waiting is allowed to end in a number."""
    buffer = NotificationBuffer(
        BufferSettings(capacity=1, policy=DropPolicy.WAIT, max_wait=50.0), name="live"
    )
    await buffer.offer(make_record(1))
    decision = await buffer.offer(make_record(2))

    assert decision.accepted is False
    assert decision.timed_out is True
    assert decision.waited_ms >= 50.0
    assert decision.discarded is None
    stats = buffer.stats
    assert stats.refused == 1
    assert stats.timeouts == 1
    assert stats.lost == 1


async def test_one_room_is_given_to_the_offer_that_asked_first() -> None:
    """Two offers waiting on one drain: one gets the room, the other keeps waiting."""
    buffer = NotificationBuffer(
        BufferSettings(capacity=1, policy=DropPolicy.WAIT, max_wait=150.0), name="live"
    )
    await buffer.offer(make_record(1))
    first = asyncio.create_task(buffer.offer(make_record(2)))
    await asyncio.sleep(0.02)
    second = asyncio.create_task(buffer.offer(make_record(3)))
    await asyncio.sleep(0.02)

    assert len(buffer.drain(1)) == 1
    first_decision = await first
    second_decision = await second

    assert first_decision.accepted is True
    assert second_decision.timed_out is True
    assert sequences_in(buffer) == (2,)


async def test_waiting_is_reported_as_time_and_not_only_as_events() -> None:
    """Back-pressure has a cost, and the report is where it becomes visible to an operator."""
    buffer = NotificationBuffer(
        BufferSettings(capacity=1, policy=DropPolicy.WAIT, max_wait=60.0), name="live"
    )
    await buffer.offer(make_record(1))
    await buffer.offer(make_record(2))
    lines = buffer.describe()

    joined = " ".join(lines)
    assert "policy=wait capacity=1 records" in joined
    assert "ms" in joined
    assert "ended in a refusal" in joined


async def test_a_buffer_that_never_hit_its_limit_says_so() -> None:
    buffer = NotificationBuffer(BufferSettings(capacity=10), name="live")
    await offer_many(buffer, [make_record(n) for n in range(1, 4)])
    stats = buffer.stats

    assert stats.ever_hit_the_limit is False
    assert stats.peak_depth == 3
    assert stats.lost == 0
    assert "admitted losing 0 records" in " ".join(buffer.describe())


async def test_an_empty_buffer_has_no_age_and_a_stale_one_does() -> None:
    buffer = NotificationBuffer(BufferSettings(capacity=2))
    assert buffer.oldest_age_ms() is None

    old = BASE - timedelta(minutes=30)
    await buffer.offer(make_record(1, source_timestamp=old))
    age = buffer.oldest_age_ms()

    assert age is not None
    assert age > 30 * 60 * 1000.0


async def test_a_handler_that_buffers_reports_every_loss_it_accepted() -> None:
    """The Chapter 11 dispatcher hands records to a buffer; the report is the other half."""
    buffer = NotificationBuffer(BufferSettings(capacity=2, policy=DropPolicy.KEEP_LATEST))
    lost = []

    async def note(decision: object) -> None:
        lost.append(decision)

    handler = buffering_handler(buffer, on_lost=note)
    for sequence in range(1, 5):
        await handler(make_record(sequence))

    assert sequences_in(buffer) == (3, 4)
    assert len(lost) == 2
    assert buffer.stats.lost == 2


async def test_a_handler_without_a_reporter_still_never_raises() -> None:
    """A gateway that does not care about the losses yet must not lose the subscription."""
    buffer = NotificationBuffer(BufferSettings(capacity=1, policy=DropPolicy.REJECT_NEW))
    handler = buffering_handler(buffer)
    await handler(make_record(1))
    await handler(make_record(2))

    assert sequences_in(buffer) == (1,)
    assert buffer.stats.refused == 1


async def test_the_stats_report_is_json_safe() -> None:
    buffer = NotificationBuffer(BufferSettings(capacity=1, policy=DropPolicy.KEEP_LATEST))
    await offer_many(buffer, [make_record(1), make_record(2)])
    payload = json.dumps(buffer.stats.as_dict())

    assert '"lost": 1' in payload
    assert '"peak_depth": 1' in payload


async def test_a_decision_serialises_for_a_log_without_the_record_it_wraps() -> None:
    buffer = NotificationBuffer(BufferSettings(capacity=1, policy=DropPolicy.KEEP_WINDOW))
    decisions = await offer_many(buffer, [make_record(1), make_record(2)])
    payload = json.dumps(decisions[1].as_dict())

    assert '"discarded_sequence": 1' in payload
    assert '"depth_after": 1' in payload
