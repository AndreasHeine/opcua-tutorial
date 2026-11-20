"""Chapter 15: queues, buffering, and the file that survives the outage.

Three queues sit between a tag and a historian, and only one of them can say what it threw away:

* the **monitored item queue** on the server: sized by ``queueSize``, trimmed by the server's own
  rule, drained by the next ``Publish``, and gone the moment the session is.
* **the client library's own notification queue**: asyncua keeps an
  ``asyncio.Queue(maxsize=queue_maxsize)`` per subscription with an ``OverflowPolicy`` of its own.
  It is real, bounded, and silent - a notification removed there never reaches a counter the
  application owns, because it never reached the application.
* **the application's buffer**, the only one of the three that reports its own losses, and the
  **spool** behind it, which is the only thing in this chapter that survives a restart.

Run it:

    uv run python examples/ch15_buffering.py --demo
    uv run python examples/ch15_buffering.py --endpoint opc.tcp://127.0.0.1:4840

With ``--demo`` the example writes every value it counts, so a missing notification can only have
come from a queue and never from a plant that changed something on its own. Point it at a real
plant and the write-driven sections say so out loud while the replay sections work off whatever
that server reported.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import tempfile
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from asyncua import Client, ua
from asyncua.common.subscription import OverflowPolicy

from _support import bullet, chapter_run, heading, plant_variables
from opcua_toolkit import (
    BufferDecision,
    BufferSettings,
    BulkReader,
    BulkWriter,
    DataChangeRecord,
    DropPolicy,
    JsonlSpool,
    NamespaceExplorer,
    NotificationBuffer,
    OperationLimits,
    SpoolFullPolicy,
    SpoolSettings,
    SubscriptionDefaults,
    SubscriptionManager,
    SubscriptionOptions,
    WriteItem,
    buffering_handler,
    format_milliseconds,
    format_node_id,
    format_number,
    format_seconds,
    offer_many,
)

#: Publishing interval for the section that measures the server's monitored item queue: long
#: enough, and started far enough from the burst, that the whole burst lands between two publishes.
QUEUE_PUBLISHING_MS = 2_000.0

#: Publishing interval for the two client-side sections: short enough that a burst arrives over
#: several publishes, which is how a slow consumer gets overtaken.
BURST_PUBLISHING_MS = 250.0

SAMPLING_MS = 100.0

#: Values written to one tag while the server was between publishes. This is the number the
#: monitored item queue is measured against.
BURST_VALUES = 8

#: Tags written once each. Each one owes exactly one notification, so a shortfall can only have
#: come from a queue.
BURST_TAGS = 20

#: Queue sizes asked for in the same subscription. ``0`` is the interesting one: the
#: specification gives a data item's request of 0 and 1 the same meaning - the server's default -
#: and the answer is the only place a client learns what that default was on this server.
ASKED_QUEUE_SIZES: tuple[int, ...] = (1, 5, 0)

#: The value each queue-size tag is written with, so its arrivals are recognisable in a mixed list.
QUEUE_BASES: tuple[int, ...] = (100, 200, 300)
LIBRARY_BASE = 2_000
BUFFER_BASE = 3_000

#: The library queue section 3 deliberately overflows. 2 is small enough to make the point with a
#: slow handler, and it is the same mechanism at the default 1_000.
SMALL_LIBRARY_QUEUE = 2

#: The queue size the toolkit's own default and asyncua's own default share, so section 1 can name
#: the number a subscription gets when nobody configures one.
DEFAULT_LIBRARY_QUEUE = 1_000

#: Seconds each section waits for the server to publish what it queued.
QUEUE_WINDOW_S = 2.6
BURST_WINDOW_S = 2.0

#: Seconds allowed for a subscription's first values to arrive before a section resets what it
#: collected, so a section measures only what it wrote.
SETTLE_S = 1.2

#: How long the simulated historian spends on every record. This is the historian's own latency,
#: and it is the thing that makes the library queue overflow.
HANDLER_LATENCY_S = 0.06

#: Buffer bounds, and the two budgets the wait policy is measured against: one with a consumer
#: draining the buffer, one with nobody draining it.
BUFFER_CAPACITY = 4
WAIT_BUDGET_MS = 500.0
TIMEOUT_BUDGET_MS = 150.0

#: Records a section needs before it can show a policy working at all.
MIN_RECORDS = 2

#: Records replayed through the three deciding policies, and the records spilled during the outage.
POLICY_RECORDS = 8
OUTAGE_RECORDS = 8

#: How many individual buffer and spool decisions a section prints before it switches to the
#: summary line. The point is the summary; the decisions are there to show what the summary counts.
SHOW_DECISIONS = 3

#: The spool bound, and the outage the sizing arithmetic is done for.
SPOOL_CAPACITY = 4
OUTAGE_S = 300.0


class Collector:
    """Collect every record one manager dispatches, in arrival order."""

    def __init__(self) -> None:
        self.records: list[DataChangeRecord] = []

    async def __call__(self, record: DataChangeRecord) -> None:
        """Store the record, which is all a collector does."""
        self.records.append(record)

    def reset(self) -> None:
        """Forget what was collected, so a section measures only what it wrote."""
        self.records.clear()

    def values_for(self, node_id_text: str) -> list[Any]:
        """The values that arrived for one node, in arrival order."""
        return [record.value for record in self.records if record.node_id_text == node_id_text]


class SlowCollector:
    """A collector that spends time on every record, the way a historian writing to a disk does."""

    def __init__(self, inner: Collector, latency_s: float) -> None:
        self._inner = inner
        self._latency_s = latency_s

    async def __call__(self, record: DataChangeRecord) -> None:
        """Spend time on the record, then hand it to the collector inside."""
        await asyncio.sleep(self._latency_s)
        await self._inner(record)


class Historian:
    """A downstream sink that can be up or down, and remembers what it accepted."""

    def __init__(self) -> None:
        self.accepted: list[Any] = []
        self.refused: list[Any] = []
        self.up = True

    async def send(self, value: Any) -> bool:
        """Accept the value if the historian is reachable, refuse it if it is not."""
        if not self.up:
            self.refused.append(value)
            return False
        self.accepted.append(value)
        return True

    def outage(self) -> None:
        """Take the historian down."""
        self.up = False

    def recover(self) -> None:
        """Bring the historian back."""
        self.up = True


class Elapsed:
    """Measure a block of work with the monotonic clock, and report it in seconds."""

    def __init__(self) -> None:
        self.seconds = 0.0
        self._started = 0.0

    def __enter__(self) -> Elapsed:
        """Start the clock."""
        self._started = time.monotonic()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Stop the clock, whether or not the block raised."""
        self.seconds = time.monotonic() - self._started

    @property
    def milliseconds(self) -> float:
        """The same measurement in milliseconds, which is what this toolkit reports durations in."""
        return self.seconds * 1000.0


@dataclasses.dataclass(frozen=True, slots=True)
class QueueAnswer:
    """What one monitored item asked for, and what the server answered."""

    node_id_text: str
    requested: int
    revised: int


class AnswerRecorder:
    """Capture the answers a high level client creates and then throws away.

    Deliberate instrumentation, never production code. asyncua's high level create path hands back
    the ``MonitoredItemId`` and drops the rest of the ``CreateMonitoredItems`` response, so the
    only place ``revisedQueueSize`` exists is that response. ``CreateSubscription`` is wrapped for
    the same reason: section 2 measures a burst against the publishing interval the server *agreed*
    to, and the number that decides whether a burst fits in one publish window is the revised one,
    not the requested one. The wrap sits on the client's session, which is the object the
    subscription actually calls, and it keeps the original behaviour intact.
    """

    def __init__(self) -> None:
        self.answers: list[QueueAnswer] = []
        self.publishing: list[tuple[float, float]] = []
        self.requests = 0

    def install(self, client: Client) -> None:
        """Wrap the two session calls this chapter reads answers from."""
        session = client.uaclient.session
        self._wrap(session, "create_monitored_items", self._record_items)
        self._wrap(session, "create_subscription", self._record_subscription)

    def _wrap(self, session: Any, name: str, record: Any) -> None:
        original = getattr(session, name)

        async def counted(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            record(args[0] if args else kwargs.get("parameters"), result)
            return result

        setattr(session, name, counted)

    def _record_items(self, params: Any, results: Any) -> None:
        self.requests += 1
        for request, result in zip(params.ItemsToCreate, results, strict=True):
            self.answers.append(
                QueueAnswer(
                    node_id_text=format_node_id(request.ItemToMonitor.NodeId),
                    requested=int(request.RequestedParameters.QueueSize),
                    revised=int(result.RevisedQueueSize),
                )
            )

    def _record_subscription(self, params: Any, result: Any) -> None:
        self.publishing.append(
            (float(params.RequestedPublishingInterval), float(result.RevisedPublishingInterval))
        )

    def clear(self) -> None:
        """Forget the answers so a section reports only the items it created."""
        self.answers.clear()
        self.publishing.clear()
        self.requests = 0


@contextlib.asynccontextmanager
async def scratch_directory() -> AsyncIterator[Path]:
    """A directory for this run's spool files, removed when the example ends.

    A real gateway names its spool directory on purpose: it is the thing that survives, and it
    has to live somewhere a restart finds again. This example is allowed to use a temporary one
    because it is demonstrating the spool, not operating one.
    """
    with tempfile.TemporaryDirectory(prefix="opcua-ch15-") as tmpdir:
        yield Path(tmpdir)


async def collect_records(
    client: Client,
    limits: OperationLimits,
    nodes: Sequence[ua.NodeId],
    *,
    publishing_interval: float,
    window_s: float,
) -> tuple[list[DataChangeRecord], float]:
    """Subscribe to ``nodes`` and report what the server produced during ``window_s``.

    Returns:
        The records that arrived, and the seconds they were collected over, measured with the
        same clock the toolkit reports durations with instead of assumed from the window the
        example asked for.
    """
    manager = SubscriptionManager(
        client,
        limits=limits,
        defaults=SubscriptionDefaults(
            publishing_interval=publishing_interval,
            sampling_interval=SAMPLING_MS,
            queue_size=BURST_VALUES,
        ),
    )
    collector = Collector()
    manager.register_handler(collector)
    try:
        await manager.subscribe(nodes, options=SubscriptionOptions(queue_size=BURST_VALUES))
        # A plant that is quiet for a second is a normal plant, so a first value is worth waiting
        # for and not worth failing over.
        with contextlib.suppress(TimeoutError):
            await manager.wait_for_notifications(1, max_wait=SETTLE_S)
        with Elapsed() as window:
            await asyncio.sleep(window_s)
    finally:
        await manager.stop()
    return collector.records, window.seconds


async def main() -> int:
    """Measure what each queue keeps, and what each one admits to losing."""
    async with (
        chapter_run(
            "find out what each queue between a tag and a historian keeps, and what it drops",
            session_name="Chapter 15 - queues and buffering",
            simulate=False,
        ) as run,
        run.open_client() as client,
        scratch_directory() as tmpdir,
    ):
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)
        reader = BulkReader(client, limits=limits)
        writer = BulkWriter(client, limits=limits)
        recorder = AnswerRecorder()
        recorder.install(client)
        found, _ = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        plant = plant_variables(found)
        names = {format_node_id(ref.node_id): ref.name for ref in plant}
        batch_tags: list[ua.NodeId] = run.demo_server.tag_ids() if run.uses_demo_server else []
        if run.uses_demo_server:
            names.update(
                {format_node_id(node_id): tag for tag, node_id in run.demo_server.tags.items()}
            )

        heading("1. three queues, three owners")
        bullet(f"endpoint: {run.endpoint}")
        bullet(
            "queue 1, the server's monitored item queue: per monitored item, drained by the next "
            "publish, sized by the client's request and the server's own cap"
        )
        if run.uses_demo_server:
            caps = run.demo_server.subscription_caps
            queue_cap = caps["max_monitored_item_queue_size"]
            bullet(
                "   this server answers a queue request of 0 with "
                f"{format_number(queue_cap)} notifications, and publishes no such limit anywhere"
            )
        bullet(
            f"queue 2, asyncua's notification queue: one asyncio.Queue per subscription, "
            f"maxsize={DEFAULT_LIBRARY_QUEUE} unless configured otherwise (section 3 uses "
            f"{SMALL_LIBRARY_QUEUE}), overflow={OverflowPolicy.DROP_OLDEST.name}"
        )
        bullet(
            f"queue 3, the application buffer: {BufferSettings(capacity=BUFFER_CAPACITY).summary()}"
        )
        bullet(
            "only queue 3 can answer 'what is missing?', and only a file answers it after a restart"
        )

        heading("2. what the server's queue keeps between two publishes")
        if len(batch_tags) >= len(ASKED_QUEUE_SIZES):
            recorder.clear()
            queue_tags = batch_tags[: len(ASKED_QUEUE_SIZES)]
            bases = {
                format_node_id(tag): base for tag, base in zip(queue_tags, QUEUE_BASES, strict=True)
            }
            read_first, read_stats = await reader.read_values(queue_tags)
            started_at = {res.node_id_text: int(res.value) for res in read_first}
            before_text = ", ".join(
                f"{res.node_id_text}={started_at[res.node_id_text]}" for res in read_first
            )
            manager = SubscriptionManager(
                client,
                limits=limits,
                defaults=SubscriptionDefaults(
                    publishing_interval=QUEUE_PUBLISHING_MS,
                    sampling_interval=SAMPLING_MS,
                    queue_size=BURST_VALUES,
                ),
            )
            collector = Collector()
            manager.register_handler(collector)
            try:
                for asked, tag in zip(ASKED_QUEUE_SIZES, queue_tags, strict=True):
                    await manager.subscribe([tag], options=SubscriptionOptions(queue_size=asked))
                requested_ms, revised_ms = recorder.publishing[-1]
                asked_sizes = ", ".join(str(size) for size in ASKED_QUEUE_SIZES)
                bullet(
                    f"one subscription, {len(queue_tags)} monitored items, queueSize asked: "
                    f"{asked_sizes}"
                )
                bullet(
                    f"publishing asked {format_milliseconds(requested_ms)}, answered "
                    f"{format_milliseconds(revised_ms)}"
                )
                bullet(
                    f"read before subscribing ({read_stats.requests} Read request): {before_text}"
                )
                # Creating a monitored item queues the value it has at that moment as its first
                # notification, so every queue below was offered that value plus this burst.
                await asyncio.sleep(SETTLE_S)
                collector.reset()
                items = [
                    WriteItem(tag, base + step + 1)
                    for step in range(BURST_VALUES)
                    for base, tag in zip(QUEUE_BASES, queue_tags, strict=True)
                ]
                with Elapsed() as burst:
                    report = await writer.write(items)
                burst_ms = format_milliseconds(burst.milliseconds)
                window_ms = format_milliseconds(revised_ms)
                inside = "fits inside" if burst.milliseconds < revised_ms else "spans"
                bullet(
                    f"{report.succeeded} of {len(items)} values written in {burst_ms} across "
                    f"{report.requests} Write request(s): the burst {inside} one {window_ms} "
                    "publishing interval"
                )
                await asyncio.sleep(QUEUE_WINDOW_S)
                for answer in recorder.answers:
                    base = bases[answer.node_id_text]
                    arrived = [int(value) for value in collector.values_for(answer.node_id_text)]
                    written = list(range(base + 1, base + BURST_VALUES + 1))
                    offered_to_queue = [started_at[answer.node_id_text], *written]
                    missing = sorted(set(written) - set(arrived))
                    queued = len(offered_to_queue)
                    bullet(
                        f"{answer.node_id_text}: asked queueSize={answer.requested}, answered "
                        f"revisedQueueSize={answer.revised}; {queued} values offered to the queue, "
                        f"{len(arrived)} arrived"
                    )
                    bullet(f"   offered: {offered_to_queue}")
                    bullet(f"   arrived: {arrived}")
                    if missing:
                        bullet(f"   never arrived: {missing}")
                    bullet(
                        f"   kept {len(arrived)} of {queued}: a queue of {answer.revised} keeps "
                        "the newest that many"
                    )
                bullet(
                    "each item was offered the value it had when it was created plus this burst: a "
                    "monitored item queues its current value the moment the server creates it"
                )
                bullet(
                    "this server queues a data change when the value is written, not on a sampling "
                    "timer that re-reads it - and it answers every item's sampling interval as the "
                    "subscription's publishing interval"
                )
                bullet(
                    "a queue of one is not a small queue: 5.13.1.5 describes it as a buffer that "
                    "always contains the newest notification, and says the discard policy is not "
                    "used for it"
                )
                # Part 4 7.21 gives a data monitored item's request of 0 and 1 the same meaning -
                # the server's default - and says that default shall be 1. asyncua's server answers
                # a request of 0 with its own cap instead (its _revise_queue_size returns
                # max_queue_size), so this example prints the asked/answered pairs and lets the
                # divergence be visible instead of describing it.
                bullet(
                    "asked for 0, this server answered its own cap: 'the server decides' means the "
                    "server says, and revisedQueueSize is the only place a client can hear it"
                )
                bullet(
                    "asyncua's server trims a full queue by removing the oldest entry and never "
                    "reads discardOldest, and asyncua's client always sends that field as True: "
                    "neither side of this pair is using it"
                )
                bullet(
                    "and nothing in the notification says it happened: the Overflow bit the "
                    "specification defines for a trimmed queue is never set by this server either"
                )
            finally:
                await manager.stop()
        else:
            bullet("this section writes values, so it needs demo tags to write to")

        heading("3. the client library's queue drops without telling anybody")
        if len(batch_tags) >= BURST_TAGS:
            tags = batch_tags[:BURST_TAGS]
            manager = SubscriptionManager(
                client,
                limits=limits,
                defaults=SubscriptionDefaults(
                    publishing_interval=BURST_PUBLISHING_MS,
                    sampling_interval=SAMPLING_MS,
                    queue_size=BURST_VALUES,
                ),
                queue_maxsize=SMALL_LIBRARY_QUEUE,
                overflow=OverflowPolicy.DROP_OLDEST,
            )
            collector = Collector()
            manager.register_handler(SlowCollector(collector, HANDLER_LATENCY_S))
            try:
                await manager.subscribe(tags, options=SubscriptionOptions(queue_size=BURST_VALUES))
                # Nothing changes on its own in this run: the settle sleep is only there to let
                # this subscription's first publish pass before the burst is written.
                await asyncio.sleep(SETTLE_S)
                collector.reset()
                written = [LIBRARY_BASE + index for index in range(len(tags))]
                await writer.write(
                    [WriteItem(tag, value) for tag, value in zip(tags, written, strict=True)]
                )
                with Elapsed() as window:
                    await asyncio.sleep(BURST_WINDOW_S)
                seen = [
                    int(record.value) for record in collector.records if record.value is not None
                ]
                mine = sorted(value for value in seen if value in set(written))
                missing = sorted(set(written) - set(mine))
                bullet(
                    f"{BURST_TAGS} tags: each item owes one notification the moment the server "
                    f"creates it and one for the value written here, so {2 * len(tags)} were owed"
                )
                bullet(f"the server's queue for each item: {BURST_VALUES} notifications")
                bullet(
                    f"this subscription's notification queue: maxsize={SMALL_LIBRARY_QUEUE}, "
                    f"overflow={OverflowPolicy.DROP_OLDEST.name}, where asyncua's default is "
                    f"{DEFAULT_LIBRARY_QUEUE}"
                )
                bullet(f"the handler spends {format_seconds(HANDLER_LATENCY_S)} on every record")
                bullet(f"records that reached a handler at all: {len(seen)}")
                bullet(f"of the {len(written)} values written, the handler saw {len(mine)}: {mine}")
                bullet(f"the values that vanished between the server and the handler: {missing}")
                bullet(f"what the manager's own counters say: {manager.stats.summary()}")
                bullet(
                    "nothing in that report mentions the missing ones: a notification removed by "
                    "the library's overflow policy never reaches any counter the application owns, "
                    "and DROP_OLDEST does not even log"
                )
                bullet(
                    "the queue filled because the consumer was slow, not because the plant was "
                    "fast: a historian that spends time on every record is every historian"
                )
            finally:
                await manager.stop()
        else:
            bullet("this section needs demo tags to count what was written")

        heading("4. a run of records this chapter can decide about")
        plant_records: list[DataChangeRecord] = []
        observation_s = 0.0
        if len(batch_tags) >= BURST_TAGS:
            tags = batch_tags[:BURST_TAGS]
            lost: list[BufferDecision] = []

            async def note(decision: BufferDecision) -> None:
                lost.append(decision)

            manager = SubscriptionManager(
                client,
                limits=limits,
                defaults=SubscriptionDefaults(
                    publishing_interval=BURST_PUBLISHING_MS,
                    sampling_interval=SAMPLING_MS,
                    queue_size=BURST_VALUES,
                ),
            )
            collector = Collector()
            manager.register_handler(collector)
            try:
                await manager.subscribe(tags, options=SubscriptionOptions(queue_size=BURST_VALUES))
                # Nothing changes on its own in this run: the settle sleep lets the values these
                # items were created with arrive and be forgotten, so the buffer's report describes
                # this burst and nothing else.
                await asyncio.sleep(SETTLE_S)
                collector.reset()
                buffer = NotificationBuffer(
                    BufferSettings(capacity=BUFFER_CAPACITY, policy=DropPolicy.KEEP_LATEST),
                    name="plant",
                )
                manager.register_handler(buffering_handler(buffer, on_lost=note))
                written = [BUFFER_BASE + index for index in range(len(tags))]
                await writer.write(
                    [WriteItem(tag, value) for tag, value in zip(tags, written, strict=True)]
                )
                with Elapsed() as window:
                    await asyncio.sleep(BURST_WINDOW_S)
                observation_s = window.seconds
                offered = collector.records
                delivered = {int(record.value) for record in offered if record.value is not None}
                missing = sorted(set(written) - delivered)
                bullet(
                    f"the same {BURST_TAGS} values, with the library queue back at "
                    f"{DEFAULT_LIBRARY_QUEUE} and the buffer as the bounded point:"
                )
                for line in buffer.describe():
                    bullet(f"   {line}")
                for decision in lost[:SHOW_DECISIONS]:
                    bullet(f"   {decision.describe()}")
                if len(lost) > SHOW_DECISIONS:
                    extra = len(lost) - SHOW_DECISIONS
                    bullet(f"   ...and {extra} more decisions in the same words")
                bullet(
                    f"records handed to the buffer: {len(offered)} of {len(written)} written; "
                    f"values unaccounted for: {missing or 'none'}"
                )
                bullet(
                    "every record is accounted for here, kept or dropped, and the dropped ones are "
                    "named by sequence: that is the difference between a bounded buffer and a "
                    "queue that drops quietly"
                )
                plant_records = list(offered)
            finally:
                await manager.stop()
        elif passive_tags := plant[:POLICY_RECORDS]:
            plant_records, observation_s = await collect_records(
                client,
                limits,
                [ref.node_id for ref in passive_tags],
                publishing_interval=BURST_PUBLISHING_MS,
                window_s=BURST_WINDOW_S,
            )
            bullet(
                f"this server changes values on its own, so these {len(plant_records)} records are "
                f"the plant's, collected from {len(passive_tags)} tags over "
                f"{format_seconds(observation_s)}: nothing was written here"
            )

        if len(plant_records) < MIN_RECORDS:
            bullet("no records arrived to replay: the buffer sections need a live subscription")

        heading("5. back-pressure, and the wrong place to use it")
        if len(plant_records) >= MIN_RECORDS:
            first, second = plant_records[0], plant_records[1]
            draining = NotificationBuffer(
                BufferSettings(capacity=1, policy=DropPolicy.WAIT, max_wait=WAIT_BUDGET_MS),
                name="to-historian",
            )
            await draining.offer(first)
            waiter = asyncio.create_task(draining.offer(second))
            await asyncio.sleep(HANDLER_LATENCY_S)
            taken = draining.drain(1)
            decision = await waiter
            bullet(f"offered seq={first.sequence}, then seq={second.sequence}, to a buffer of one")
            bullet(f"a consumer took {len(taken)} record {format_seconds(HANDLER_LATENCY_S)} later")
            bullet(decision.describe())
            bullet(
                "the offer parked instead of deciding: nothing was thrown away, and the waiting "
                "became a cost the application paid instead of a loss the plant suffered"
            )
            stuck = NotificationBuffer(
                BufferSettings(capacity=1, policy=DropPolicy.WAIT, max_wait=TIMEOUT_BUDGET_MS),
                name="stuck",
            )
            await stuck.offer(first)
            refused = await stuck.offer(second)
            bullet(refused.describe())
            for line in stuck.describe():
                bullet(f"   {line}")
            bullet(
                "the wait is bounded on purpose: a producer that waits forever has stopped "
                "collecting, and a refusal after a wait is counted in the same report as the "
                "records it dropped"
            )
            bullet(
                "the warning that comes with it: a handler that waits stops draining the "
                "library's queue, which puts the silent loss of section 3 back where it came from. "
                "Wait between two application tasks, never on the publish path"
            )

        heading("6. three policies over the same run of records")
        replay = plant_records[:POLICY_RECORDS]
        if replay:
            bullet(f"replaying {len(replay)} records into a buffer of {BUFFER_CAPACITY}")
            for policy in (DropPolicy.KEEP_LATEST, DropPolicy.KEEP_WINDOW, DropPolicy.REJECT_NEW):
                buffer = NotificationBuffer(
                    BufferSettings(capacity=BUFFER_CAPACITY, policy=policy), name=policy.value
                )
                decisions = await offer_many(buffer, replay)
                kept = [
                    int(record.value) for record in buffer.records() if record.value is not None
                ]
                discarded = [
                    decision.discarded for decision in decisions if decision.discarded is not None
                ]
                destroyed = [int(item.value) for item in discarded if item.value is not None]
                counts = buffer.stats
                bullet(f"policy {policy.value}: kept {kept}")
                bullet(
                    f"   accepted {counts.accepted} refused {counts.refused} "
                    f"discarded_oldest {counts.discarded_oldest} collapsed {counts.collapsed} "
                    f"contiguous={str(counts.contiguous).lower()}"
                )
                if destroyed:
                    bullet(f"   values destroyed to make room: {destroyed}")
            bullet(
                "keep_window is the policy that leaves a hole in the middle of a run of values, "
                "and the report says so with contiguous=false instead of leaving the reader to do "
                "the arithmetic"
            )

        heading("7. the spool: the only thing here that survives a restart")
        working = Path(tmpdir)
        bounded_path = working / "outage.jsonl"
        bounded = JsonlSpool(
            SpoolSettings(
                path=bounded_path, capacity=SPOOL_CAPACITY, full_policy=SpoolFullPolicy.TRIM_OLDEST
            )
        )
        offered = plant_records[: SPOOL_CAPACITY + 4]
        if offered:
            writes = [
                await bounded.append(record, tag=names.get(record.node_id_text))
                for record in offered
            ]
            lines = bounded_path.read_text(encoding="utf-8").splitlines()
            bounded_stats = await bounded.stats()
            bullet(f"{bounded.describe()[0]}")
            bullet(f"appended {len(offered)} records to it")
            bullet(writes[-1].describe())
            for line in bounded_stats.describe():
                bullet(f"   {line}")
            used_bytes = format_number(bounded_stats.bytes_used)
            bullet(f"the file holds {len(lines)} lines in {used_bytes} bytes")
            bullet(f"first line on disk: {lines[0]}")
            read_back = await bounded.read()
            bullet(f"read back oldest first: {[entry.value for entry in read_back]}")

            byte_bound = bounded_stats.bytes_used
            tight_path = working / "tight.jsonl"
            tight = JsonlSpool(
                SpoolSettings(
                    path=tight_path,
                    capacity=10_000,
                    max_bytes=byte_bound,
                    full_policy=SpoolFullPolicy.TRIM_OLDEST,
                )
            )
            for record in offered:
                await tight.append(record, tag=names.get(record.node_id_text))
            tight_stats = await tight.stats()
            surviving = format_number(tight_stats.bytes_used)
            bullet(
                f"the same records against a byte bound of {format_number(byte_bound)} bytes: "
                f"{tight_stats.records} records survive in {surviving} bytes, "
                f"{tight_stats.trimmed} trimmed"
            )

            refused_path = working / "refused.jsonl"
            refusing = JsonlSpool(
                SpoolSettings(path=refused_path, capacity=2, full_policy=SpoolFullPolicy.REFUSE)
            )
            refusals = []
            for record in offered:
                write = await refusing.append(record, tag=names.get(record.node_id_text))
                if not write.accepted:
                    refusals.append(write)
            before = refused_path.read_bytes()
            last = offered[-1]
            again = await refusing.append(last, tag=names.get(last.node_id_text))
            refusing_stats = await refusing.stats()
            unchanged = refused_path.read_bytes() == before
            bullet(
                f"policy 'refuse' turned down {len(refusals) + 1} of {len(offered) + 1} writes and "
                f"removed {refusing_stats.trimmed} records that were already on disk: the file is "
                f"unchanged ({'yes' if unchanged else 'no'})"
            )
            if refusals:
                bullet(refusals[0].describe())
            bullet(f"the write it refused last: {last.value} -> {again.reason}")

        heading("8. store and forward: a historian that was down")
        batch = plant_records[:OUTAGE_RECORDS]
        if batch:
            forward_path = working / "forward.jsonl"
            spool = JsonlSpool(SpoolSettings(path=forward_path, capacity=1_000))
            historian = Historian()
            spill_buffer = NotificationBuffer(
                BufferSettings(capacity=2, policy=DropPolicy.KEEP_LATEST), name="during-outage"
            )
            spilled: list[Any] = []

            async def spill(decision: BufferDecision) -> None:
                record = decision.discarded if decision.discarded is not None else decision.record
                entry = await spool.append(record, tag=names.get(record.node_id_text))
                spilled.append(entry.entry.value)

            handler = buffering_handler(spill_buffer, on_lost=spill)
            historian.outage()
            with Elapsed() as spilled_span:
                for record in batch:
                    await handler(record)
            for record in spill_buffer.drain():
                if not await historian.send(record.value):
                    await spool.append(record, tag=names.get(record.node_id_text))
            bullet(f"historian down while {len(batch)} records arrived, into a buffer of 2")
            for line in spill_buffer.describe():
                bullet(f"   buffer: {line}")
            for line in (await spool.stats()).describe():
                bullet(f"   {line}")
            historian.recover()
            replayed = await spool.take(1_000)
            for entry in replayed:
                await historian.send(entry.value)
            wanted = [int(record.value) for record in batch if record.value is not None]
            got = [int(value) for value in historian.accepted if value is not None]
            bullet(f"records spilled straight to the file while the historian was down: {spilled}")
            bullet(f"the historian refused {len(historian.refused)} while it was down")
            bullet(f"after it came back, take() handed over {len(replayed)} spooled records")
            bullet(f"the historian accepted: {got}")
            bullet(f"every value that arrived during the outage, in arrival order: {got == wanted}")
            for line in (await spool.stats()).describe()[-1:]:
                bullet(f"   after the replay: {line}")
            after_bytes = forward_path.stat().st_size
            bullet(f"the spool file after the replay: {format_number(after_bytes)} bytes")
            spilled_ms = format_milliseconds(spilled_span.milliseconds)
            bullet(
                f"the same {len(batch)} records took {spilled_ms} to handle while the handler was "
                "also writing a file"
            )
            bullet(
                "and the caveat that comes with it: on_lost runs on the subscription's consumer "
                "task, so a spool write there is an await on a thread - safe for the event loop, "
                "still paid for by the consumer. A gateway that spills at rate hands the decision "
                "to its own drain task instead"
            )

        heading("9. sizing the queues for an outage you can name")
        if plant_records and observation_s > 0.0:
            spooled = await bounded.stats()
            per_item_s = len(plant_records) / observation_s / max(1, BURST_TAGS)
            whole_s = len(plant_records) / observation_s
            needed = math.ceil(whole_s * OUTAGE_S)
            bullet(
                f"{len(plant_records)} notifications over {format_seconds(observation_s)} on "
                f"{BURST_TAGS} tags is {format_number(round(whole_s, 2))} records/s in total and "
                f"{format_number(round(per_item_s, 3))} records/s per tag"
            )
            bullet(
                "that rate is this example's own write burst - every tag written once - not a "
                "plant's rate. The arithmetic is only as honest as the window it was measured "
                "from, which is why it is measured rather than typed in"
            )
            if spooled.records:
                bytes_per_record = spooled.bytes_used / spooled.records
                per_record = format_number(round(bytes_per_record, 1))
                total_bytes = format_number(round(needed * bytes_per_record / 1_024))
                bullet(
                    f"one spooled record measured {per_record} bytes, so a "
                    f"{format_seconds(OUTAGE_S)} outage at this rate needs "
                    f"{format_number(needed)} records and about {total_bytes} KiB of file"
                )
            if run.uses_demo_server:
                cap = run.demo_server.subscription_caps["max_monitored_item_queue_size"]
                covered = format_seconds(cap / per_item_s)
                bullet(
                    f"the server's own queue holds {format_number(cap)} notifications per item, "
                    f"which at {format_number(round(per_item_s, 3))} records/s per tag is "
                    f"{covered} of this outage - and it dies with the session"
                )
            bullet(
                "so the sizing splits in three: the server's queue covers a client that stopped "
                "publishing, the application buffer covers a sink that is slow, and only the file "
                "covers a sink that is gone"
            )

        heading("10. what to take from this chapter")
        bullet(
            f"NotificationBuffer({BufferSettings(capacity=BUFFER_CAPACITY).summary()}) and "
            "buffering_handler() are the pair: a handler that never blocks, and a bounded buffer "
            "that names every record it could not keep"
        )
        bullet(
            "JsonlSpool(settings) is the store in store-and-forward: bounded by records and by "
            "bytes, trimming or refusing by policy, and read back oldest first with take() as the "
            "acknowledgement that a replay was accepted"
        )
        bullet(
            "read revisedQueueSize out of the answer instead of trusting the request, keep the "
            "handler on the publish path non-blocking, and put a number on every record that did "
            "not make it"
        )
        bullet(
            "next: Part 5 turns to the server side, where these limits are set in the first place"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
