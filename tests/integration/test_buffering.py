"""Chapter 15 integration tests: queues that trim, queues that drop, and a file that remembers.

The unit tests for the buffer and the spool decide over invented records. These tests do not get to
invent anything: every record asserted here arrived from a subscription to the demo server, with the
simulator off so every value in the file is a value the test wrote.

Three rules hold here.

**Waits, never sleeps.** A number of notifications is waited for, and silence is proven by a wait
that must time out. The only ``asyncio.sleep`` in this file is inside a deliberately slow handler,
which is the historian's own latency and the thing being measured.

**Settle on a fact, not on a pause.** Creating a monitored item queues the value it has at that
moment, so every subscription here first dispatches one notification per item. :func:`settle` waits
for that round to end on a proven silence, which is how a burst is measured against the values it
wrote and nothing else.

**Subscriptions are stopped in a ``finally``.** A leaked subscription is how an OPC UA test suite
becomes flaky.

What a server does to a monitored item queue, and what the client library does to its own, is
asserted as behaviour: how many values arrived, and which ones. The revised queue size is not
readable from asyncua's high level create path, which is the point Chapter 15 makes in its
verification section.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from asyncua import ua
from asyncua.common.subscription import OverflowPolicy

from opcua_toolkit import (
    BufferDecision,
    BufferSettings,
    BulkReader,
    BulkWriter,
    DataChangeRecord,
    DropPolicy,
    JsonlSpool,
    NotificationBuffer,
    OperationLimits,
    SpoolFullPolicy,
    SpoolSettings,
    SubscriptionManager,
    SubscriptionOptions,
    WriteItem,
    buffering_handler,
    format_node_id,
    offer_many,
)

pytestmark = pytest.mark.integration

#: Publishing interval for the monitored item queue test: long enough, and the burst is written
# long after the subscription's first publish, that everything written lands in one publish.
QUEUE_PUBLISHING_MS = 2_000.0

#: Publishing interval for the client-side tests: short enough that a burst arrives at once, which
#: is how a slow consumer gets overtaken.
BURST_PUBLISHING_MS = 100.0

#: How long a value that is coming is given, and how long a subscription must stay quiet before
#: "nothing more is coming" counts as a fact.
PRESENT_WAIT_S = 10.0
QUIET_S = 1.5

#: Values written to each tag in the queue test. Distinct, and above every batch tag's initial
#: value, so a value in an arrival list can only have come from this test.
BURST_VALUES = (11, 12, 13, 14, 15)
QUEUE_BASES = (100, 200, 300)

#: The queue sizes asked for in one subscription, matched to :data:`QUEUE_BASES`. ``0`` is the
#: interesting one: the specification gives a data item's request of 0 and 1 the same meaning -
#: the server's default - and only the arrivals show what this server used for it.
ASKED_QUEUES = (1, 3, 0)

#: Tags written once each in the sections that replay records through a buffer or a file.
BURST_TAGS = 8

#: The value each of those tags is written with.
TAG_BASE = 4_000

#: The latency the slow handler spends on every record: the historian's own cost, and the reason
#: the bounded queue in front of it overflows.
HANDLER_LATENCY_S = 0.05

#: The queue the library test binds on purpose. asyncua's own default is one thousand, which no
#: test here needs: at the default nothing is dropped, and two tests assert that.
SMALL_LIBRARY_QUEUE = 1

#: How many records a buffer section replays, and the capacity it replays them into.
REPLAY_CAPACITY = 4


class Recorder:
    """Collect every record one manager dispatches, in arrival order."""

    def __init__(self) -> None:
        self.records: list[DataChangeRecord] = []

    async def __call__(self, record: DataChangeRecord) -> None:
        self.records.append(record)

    def reset(self) -> None:
        """Forget what was collected, so a test measures only what it wrote."""
        self.records.clear()

    def values(self) -> list[Any]:
        """Every value collected, in arrival order."""
        return [record.value for record in self.records]

    def values_for(self, node_id_text: str) -> list[Any]:
        """The values that arrived for one node, in arrival order."""
        return [record.value for record in self.records if record.node_id_text == node_id_text]


class SlowRecorder:
    """A handler that spends time on every record, the way a historian writing to a disk does."""

    def __init__(self, inner: Recorder, latency_s: float) -> None:
        self._inner = inner
        self._latency_s = latency_s

    async def __call__(self, record: DataChangeRecord) -> None:
        # This is the measured thing, not a test pause: a handler with a cost is what turns the
        # bounded queue in front of it into a queue that drops.
        await asyncio.sleep(self._latency_s)
        await self._inner(record)


class Historian:
    """A downstream sink that can be up or down, and remembers what it accepted."""

    def __init__(self) -> None:
        self.accepted: list[Any] = []
        self.refused: list[Any] = []
        self.available = True

    async def send(self, value: Any) -> bool:
        """Accept the value if the historian is reachable, refuse it if it is not."""
        if not self.available:
            self.refused.append(value)
            return False
        self.accepted.append(value)
        return True

    def outage(self) -> None:
        """Take the historian down."""
        self.available = False

    def recover(self) -> None:
        """Bring the historian back."""
        self.available = True


async def settle(manager: SubscriptionManager, *, quiet_for: float = QUIET_S) -> int:
    """Wait until the subscription has dispatched everything it is going to dispatch.

    Every iteration blocks on a real dispatch. The loop ends on a wait that timed out, which is the
    proof that nothing is still on its way: a test that paused instead would pass with a
    notification sitting in the queue about to be counted.
    """
    seen = manager.stats.notifications
    while True:
        try:
            await manager.wait_for_notifications(seen + 1, max_wait=quiet_for)
        except TimeoutError:
            return manager.stats.notifications
        seen = manager.stats.notifications


async def expect_newest(
    manager: SubscriptionManager, node_id_text: str, value: object, *, label: str = ""
) -> DataChangeRecord:
    """Wait until the newest record for one node carries exactly ``value``."""
    seen: DataChangeRecord | None = None
    name = label or node_id_text
    try:
        async with asyncio.timeout(PRESENT_WAIT_S):
            while True:
                record = await manager.wait_for_value(
                    node_id_text, newer_than=seen, max_wait=PRESENT_WAIT_S
                )
                if record.value == value:
                    return record
                seen = record
    except TimeoutError:
        raise AssertionError(f"{name}: never saw {value!r}; newest record was {seen!r}") from None


def tag_values(count: int) -> list[int]:
    """The value each of ``count`` tags will be written with: distinct and recognisable."""
    return [TAG_BASE + 100 * index for index in range(count)]


async def written_records(
    client: Any,
    limits: OperationLimits,
    tags: Sequence[ua.NodeId],
    *,
    publishing_interval: float = BURST_PUBLISHING_MS,
) -> tuple[list[DataChangeRecord], SubscriptionManager]:
    """Subscribe, settle, write one value per tag, and hand back the records that arrived.

    The manager is returned still running: the caller decides what the records are for and stops it
    in its own ``finally``.
    """
    recorder = Recorder()
    manager = SubscriptionManager(client, limits=limits, publishing_interval=publishing_interval)
    manager.register_handler(recorder)
    await manager.subscribe(tags, options=SubscriptionOptions(queue_size=len(BURST_VALUES)))
    # One notification per item arrives before anything is written: the value each monitored item
    # was created with. Waiting for that round is what keeps a burst's numbers honest.
    await manager.wait_for_notifications(len(tags), max_wait=PRESENT_WAIT_S)
    await settle(manager)
    recorder.reset()

    values = tag_values(len(tags))
    report = await BulkWriter(client, limits=limits).write(
        [WriteItem(tag, value) for tag, value in zip(tags, values, strict=True)]
    )
    assert report.failed == 0, report.summary()
    for tag, value in zip(tags, values, strict=True):
        await expect_newest(manager, format_node_id(tag), value)
    await settle(manager)
    return list(recorder.records), manager


async def test_a_queue_of_one_delivers_only_the_newest_value(client, limits, demo_server) -> None:
    """The queue behind a monitored item trims from the front and says nothing when it does."""
    tags = demo_server.tag_ids(0, len(ASKED_QUEUES))
    texts = [format_node_id(tag) for tag in tags]
    read_first, _ = await BulkReader(client, limits=limits).read_values(tags)
    started_at = {text: int(result.value) for text, result in zip(texts, read_first, strict=True)}

    recorder = Recorder()
    manager = SubscriptionManager(client, limits=limits, publishing_interval=QUEUE_PUBLISHING_MS)
    manager.register_handler(recorder)
    try:
        for queue_size, tag in zip(ASKED_QUEUES, tags, strict=True):
            await manager.subscribe([tag], options=SubscriptionOptions(queue_size=queue_size))
        # The first publish carries the value each item was created with: one notification per
        # item, before this test has written anything. Waiting for it is also what puts this burst
        # in the next publish window instead of across one.
        await manager.wait_for_notifications(len(tags), max_wait=PRESENT_WAIT_S)
        for text in texts:
            assert recorder.values_for(text) == [started_at[text]], (
                f"{text}: a new monitored item reports the value it has when it is created"
            )
        recorder.reset()

        written = {
            format_node_id(tag): [base + offset for offset in BURST_VALUES]
            for tag, base in zip(tags, QUEUE_BASES, strict=True)
        }
        items = [
            WriteItem(tag, value)
            for tag, base in zip(tags, QUEUE_BASES, strict=True)
            for value in written[format_node_id(tag)]
        ]
        report = await BulkWriter(client, limits=limits).write(items)
        assert report.failed == 0, report.summary()

        for text, base in zip(texts, QUEUE_BASES, strict=True):
            await expect_newest(manager, text, base + BURST_VALUES[-1])
        await settle(manager)

        cap = demo_server.subscription_caps["max_monitored_item_queue_size"]
        for text, queue_size in zip(texts, ASKED_QUEUES, strict=True):
            offered = written[text]
            limit = cap if queue_size == 0 else queue_size
            kept = offered[-min(len(offered), limit) :]
            arrived = [int(value) for value in recorder.values_for(text)]
            assert arrived == kept, f"{text}: arrived {arrived}, expected {kept}"
    finally:
        await manager.stop()


async def test_the_libraries_own_queue_drops_without_telling_the_application(
    client, limits, demo_server
) -> None:
    """A notification removed by the library never reaches a counter the application owns."""
    tags = demo_server.tag_ids(0, BURST_TAGS)
    values = tag_values(len(tags))
    written = {int(value) for value in values}

    recorder = Recorder()
    manager = SubscriptionManager(
        client,
        limits=limits,
        publishing_interval=BURST_PUBLISHING_MS,
        queue_maxsize=SMALL_LIBRARY_QUEUE,
        overflow=OverflowPolicy.DROP_OLDEST,
    )
    manager.register_handler(SlowRecorder(recorder, HANDLER_LATENCY_S))
    try:
        await manager.subscribe(tags, options=SubscriptionOptions(queue_size=len(BURST_VALUES)))
        await manager.wait_for_notifications(1, max_wait=PRESENT_WAIT_S)
        # Whatever the first round kept is dispatched here; the rest of that round is already gone,
        # because the queue in front of the handler holds one record.
        await settle(manager)
        recorder.reset()

        report = await BulkWriter(client, limits=limits).write(
            [WriteItem(tag, value) for tag, value in zip(tags, values, strict=True)]
        )
        assert report.failed == 0, report.summary()
        newest = int(values[-1])
        await expect_newest(manager, format_node_id(tags[-1]), newest)
        await settle(manager)

        arrived = {int(value) for value in recorder.values() if value is not None}
        missing = sorted(written - arrived)
        assert newest in arrived, "DROP_OLDEST keeps the newest notification, so it must arrive"
        assert missing, f"nothing was dropped, so this test measured the wrong queue: {arrived}"
        owed = 2 * len(tags)
        assert manager.stats.notifications < owed, (
            f"a dropped notification never reaches these counters, so they must understate what "
            f"was owed: {manager.stats.summary()}"
        )
        assert not hasattr(manager.stats, "dropped"), (
            "the toolkit has no counter for notifications the library removed, which is the "
            "point: nothing downstream could have reported them"
        )
    finally:
        await manager.stop()


async def test_a_buffer_replays_the_same_records_three_ways(client, limits, demo_server) -> None:
    """One run of real records, three policies, and a report that names every decision."""
    tags = demo_server.tag_ids(0, BURST_TAGS)
    records, manager = await written_records(client, limits, tags)
    try:
        values = [int(record.value) for record in records]
        assert len(values) == BURST_TAGS
        # The library queue is at asyncua's default here, and every notification this subscription
        # owed arrived: the bounded point in this test is the buffer, not the queue in front of it.
        assert manager.stats.notifications == 2 * BURST_TAGS

        keep_latest = NotificationBuffer(
            BufferSettings(capacity=REPLAY_CAPACITY, policy=DropPolicy.KEEP_LATEST),
            name="keep-latest",
        )
        await offer_many(keep_latest, records)
        assert [int(record.value) for record in keep_latest.records()] == values[-REPLAY_CAPACITY:]
        assert keep_latest.stats.discarded_oldest == len(values) - REPLAY_CAPACITY
        assert keep_latest.stats.collapsed == 0
        assert keep_latest.stats.contiguous

        keep_window = NotificationBuffer(
            BufferSettings(capacity=REPLAY_CAPACITY, policy=DropPolicy.KEEP_WINDOW),
            name="keep-window",
        )
        await offer_many(keep_window, records)
        assert [int(record.value) for record in keep_window.records()] == [
            *values[: REPLAY_CAPACITY - 1],
            values[-1],
        ]
        assert keep_window.stats.collapsed == len(values) - REPLAY_CAPACITY
        assert keep_window.stats.discarded_oldest == 0
        assert not keep_window.stats.contiguous, "replacing the tail leaves a hole on purpose"

        reject_new = NotificationBuffer(
            BufferSettings(capacity=REPLAY_CAPACITY, policy=DropPolicy.REJECT_NEW),
            name="reject-new",
        )
        decisions = await offer_many(reject_new, records)
        assert [int(record.value) for record in reject_new.records()] == values[:REPLAY_CAPACITY]
        assert reject_new.stats.refused == len(values) - REPLAY_CAPACITY
        refused = [int(decision.record.value) for decision in decisions if not decision.accepted]
        assert refused == values[REPLAY_CAPACITY:]
    finally:
        await manager.stop()


async def test_a_spool_holds_what_a_replay_needs_and_remembers_its_order(
    client, limits, demo_server, tmp_path: Path
) -> None:
    """The file is the truth: capacity trims it, ``read`` gives it back oldest first, ``take`` is
    the acknowledgement that a replay was accepted."""
    tags = demo_server.tag_ids(0, BURST_TAGS)
    capacity = 5
    records, manager = await written_records(client, limits, tags)
    try:
        values = [int(record.value) for record in records]
        path = tmp_path / "outage.jsonl"
        spool = JsonlSpool(
            SpoolSettings(path=path, capacity=capacity, full_policy=SpoolFullPolicy.TRIM_OLDEST)
        )
        writes = [await spool.append(record) for record in records]
        assert all(write.accepted for write in writes), [write.reason for write in writes]

        stats = await spool.stats()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert stats.records == capacity == len(lines), "the counters must match the file"
        assert stats.trimmed == len(values) - capacity
        assert [int(entry.value) for entry in await spool.read()] == values[-capacity:]

        taken = [int(entry.value) for entry in await spool.take(2)]
        assert taken == values[-capacity : -capacity + 2]
        after = await spool.stats()
        assert after.taken == 2
        assert after.records == capacity - 2
        assert after.records == len(path.read_text(encoding="utf-8").splitlines())
    finally:
        await manager.stop()


async def test_store_and_forward_replays_a_down_historian_in_arrival_order(
    client, limits, demo_server, tmp_path: Path
) -> None:
    """Records a refused sink could not take end up in the file, oldest first."""
    tags = demo_server.tag_ids(0, BURST_TAGS)
    buffer_capacity = 2

    historian = Historian()
    historian.outage()
    buffer = NotificationBuffer(
        BufferSettings(capacity=buffer_capacity, policy=DropPolicy.KEEP_LATEST),
        name="during-outage",
    )
    path = tmp_path / "forward.jsonl"
    spool = JsonlSpool(SpoolSettings(path=path, capacity=1_000))
    spilled: list[BufferDecision] = []

    async def spill(decision: BufferDecision) -> None:
        """Spill the record a policy destroyed straight to the file."""
        record = decision.discarded if decision.discarded is not None else decision.record
        await spool.append(record)
        spilled.append(decision)

    recorder = Recorder()
    manager = SubscriptionManager(client, limits=limits, publishing_interval=BURST_PUBLISHING_MS)
    manager.register_handler(recorder)
    try:
        await manager.subscribe(tags, options=SubscriptionOptions(queue_size=len(BURST_VALUES)))
        await manager.wait_for_notifications(len(tags), max_wait=PRESENT_WAIT_S)
        await settle(manager)
        recorder.reset()
        # The buffer joins the dispatch path only now, so its report describes this burst.
        manager.register_handler(buffering_handler(buffer, on_lost=spill))

        values = tag_values(len(tags))
        report = await BulkWriter(client, limits=limits).write(
            [WriteItem(tag, value) for tag, value in zip(tags, values, strict=True)]
        )
        assert report.failed == 0, report.summary()
        for tag, value in zip(tags, values, strict=True):
            await expect_newest(manager, format_node_id(tag), value, label="during the outage")
        await settle(manager)

        order = [int(record.value) for record in recorder.records]
        assert len(order) == BURST_TAGS
        assert buffer.stats.discarded_oldest == len(order) - buffer_capacity
        assert buffer.stats.lost == len(spilled)
        assert (await spool.stats()).records == len(order) - buffer_capacity

        # The records the buffer still held were refused as well, so they join the file instead of
        # vanishing when the drain finds nobody waiting for them.
        for record in buffer.drain():
            if not await historian.send(record.value):
                await spool.append(record)
        assert historian.accepted == []
        assert len(historian.refused) == buffer_capacity
        assert (await spool.stats()).records == len(order)

        historian.recover()
        for entry in await spool.take(1_000):
            assert await historian.send(entry.value)
        assert [int(value) for value in historian.accepted] == order
        assert (await spool.stats()).records == 0
        assert path.stat().st_size == 0, "take() acknowledged every replayed record"
    finally:
        await manager.stop()
