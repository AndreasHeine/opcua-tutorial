"""Chapter 11: dispatch - one ordered consumer, many handlers.

A subscription is only useful if every part of your application receives the same
facts in the same order. The manager therefore runs **one** consumer task over the
subscription's event iterator and turns each event into a frozen
:class:`~opcua_toolkit.DataChangeRecord`:

* handlers run in registration order, one at a time: predictable before it is fast,
  and Chapter 15 is where a handler hands the value to its own queue instead;
* a handler that raises is counted and skipped - one broken consumer may not stop
  the others, and must not kill the subscription;
* ``key`` groups monitored items so one callback can serve several groups;
* every record carries a monotonic ``sequence``, which is how you prove nothing was
  dropped between the server and your code;
* a notification for an item you no longer monitor is counted as a stray and dropped
  instead of being dispatched into an application that has moved on.

Run it:

    uv run python examples/ch11_dispatch.py --demo
    uv run python examples/ch11_dispatch.py --endpoint opc.tcp://127.0.0.1:4840

The demo server keeps writing its process tags, so this example also drives one value
by hand to show the difference between "something arrived" and "the value I wrote
arrived".
"""

from __future__ import annotations

import asyncio
from itertools import pairwise

from asyncua import ua

from _support import (
    bullet,
    chapter_run,
    heading,
    plant_variables,
)
from opcua_toolkit import (
    BrowsePathError,
    DataChangeRecord,
    NamespaceExplorer,
    NodeRef,
    SubscriptionManager,
    describe_status,
)

#: Tags this chapter drives by hand. Missing ones are reported, not assumed.
DRIVEN_TAGS: tuple[str, ...] = ("Temperature", "Pressure", "RecipeName")


class Recorder:
    """A handler that keeps what it saw, so the example can show the order."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.records: list[DataChangeRecord] = []

    async def __call__(self, record: DataChangeRecord) -> None:
        """Store one notification, in the order the consumer delivered it."""
        self.records.append(record)

    @property
    def sequences(self) -> list[int]:
        """The record numbers this handler received."""
        return [record.sequence for record in self.records]


#: The value this example writes to a tag the demo server also writes by itself.
WRITTEN_VALUE = 99.25


def _as_number(value: object) -> float | None:
    """What the dashboard handler below can actually plot."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


class BrokenHandler:
    """A handler that assumes every record is a number, and fails when one is not.

    This is the realistic shape of a broken sink: it works for most of a plant's tags
    and throws on the one tag whose type nobody considered. The point is what happens
    to the *other* handlers when it does.
    """

    def __init__(self) -> None:
        self.failures = 0

    async def __call__(self, record: DataChangeRecord) -> None:
        """Fail on purpose, and count how often the dispatcher let it through."""
        if _as_number(record.value) is None:
            self.failures += 1
            raise RuntimeError(f"the dashboard expected a number, got {record.value!r}")


async def main() -> int:
    """Subscribe, register handlers that disagree, and keep the records in order."""
    async with (
        chapter_run(
            "dispatch notifications to several handlers without losing their order",
            session_name="Chapter 11 - dispatch",
        ) as run,
        run.open_client() as client,
    ):
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)
        manager = SubscriptionManager(client, limits=limits, publishing_interval=250.0)

        recorder = Recorder("recorder")
        second = Recorder("second consumer")
        broken = BrokenHandler()
        manager.register_handler(recorder)
        manager.register_handler(broken)
        manager.register_handler(second)

        heading("1. the tags come from browse paths")
        targets: list[NodeRef] = []
        for name in DRIVEN_TAGS:
            try:
                targets.append(
                    await explorer.find_by_browse_path(["ProductionLine", "Line1", name])
                )
            except BrowsePathError:
                bullet(f"this server has no {name} tag")
        if not targets:
            found, _ = await explorer.walk_flat(
                ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
            )
            targets = plant_variables(found)[:3]
        if not targets:
            bullet("this server exposes no variables to monitor - the chapter stops here")
            return 0
        handles = await manager.subscribe([node.node_id for node in targets])
        bullet(f"{len(handles)} monitored items, subscription {manager.subscription_id}")

        heading("2. the first publish fills every handler")
        await manager.wait_for_notifications(len(targets), max_wait=10.0)
        bullet(f"recorder saw {len(recorder.records)} records: {recorder.sequences}")
        bullet(f"second consumer saw {len(second.records)} records")
        bullet(
            f"broken handler failed {broken.failures} time(s), "
            f"subscription errors={manager.stats.errors}"
        )
        bullet(
            "a handler that raises is a bug in that handler, not a reason to stop the "
            "subscription or to skip the handlers that come after it"
        )

        heading("3. order is a promise, not a side effect")
        if run.uses_demo_server:
            known_tags = run.demo_server.tags
            written = 0
            for node in targets:
                if node.name not in known_tags:
                    continue
                if node.name == "RecipeName":
                    await run.demo_server.set_value(node.name, "dispatch-recipe")
                else:
                    await run.demo_server.set_value(node.name, 11.5)
                written += 1
            # ``count`` is a total, not "count more": asking for one record when three
            # have already arrived returns at once, which is how a subscription test
            # ends up always green and never meaning anything.
            target = manager.stats.notifications + written
            try:
                await manager.wait_for_notifications(target, max_wait=10.0)
            except TimeoutError:
                bullet(f"only {manager.stats.notifications} of {target} records had arrived")
        sequences = recorder.sequences
        bullet(f"recorder sequences: {sequences}")
        bullet(f"strictly increasing: {all(b > a for a, b in pairwise(sequences))}")
        bullet(f"no gaps from the first record: {sequences == list(range(1, len(sequences) + 1))}")
        bullet(
            f"this handler saw {len(sequences)} of {manager.stats.notifications} records: "
            "every item is unkeyed so far, so a gap here would mean a lost notification"
        )
        bullet(
            "a handler's sequences are a subsequence of the manager's numbering: a key that "
            "routes some items elsewhere shows up as gaps, so the check has to know what "
            "this handler is supposed to see"
        )
        for record in recorder.records[-3:]:
            bullet(
                f"  seq={record.sequence:<4} {record.node_id_text:<26} "
                f"value={record.value!r} {describe_status(record.status_code)}"
            )

        heading("4. waiting for the value I wrote, not for any value")
        first = targets[0]
        # Only the demo server has a tag this example can write without competing with
        # the simulator, and only when the example is the one that wrote the value it
        # then waits for.
        quiet_tag = (
            next((node for node in targets if node.name == "RecipeName"), None)
            if run.uses_demo_server
            else None
        )
        if run.uses_demo_server:
            known = manager.last_value(first.node_id)
            await run.demo_server.set_value(first.name, WRITTEN_VALUE)
            raced = await manager.wait_for_value(first.node_id, newer_than=known, max_wait=8.0)
            bullet(f"the example wrote {WRITTEN_VALUE} on {first.node_id_text}")
            bullet(f"the first record newer than the one it waited for says {raced.value!r}")
            if raced.value == WRITTEN_VALUE:
                bullet("this time the example's own write won the race, and no client may rely on")
                bullet("that: this server writes the same tag by itself, and a monitored item")
                bullet("reports whatever the tag held at its own sampling instant")
            else:
                bullet("the server's own simulator won this race: 'newer' is not 'mine'")
        if quiet_tag is not None:
            before = manager.last_value(quiet_tag.node_id)
            arrived = await manager.wait_for_exact(
                quiet_tag.node_id, "dispatch-recipe", max_wait=8.0
            )
            bullet(f"the value this example wrote: {arrived.node_id_text} = {arrived.value!r}")
            bullet(f"sequence {(before or arrived).sequence} -> {arrived.sequence}")
            bullet(
                "wait_for_value answers 'anything newer'; wait_for_exact turns that into "
                "'the value I wrote', which is what an application usually means"
            )
        else:
            arrived = await manager.wait_for_value(first.node_id, max_wait=8.0)
            bullet(f"wait_for_value: {arrived.node_id_text} = {arrived.value!r}")

        heading("5. keys group items so one callback can serve several sources")
        grouped = Recorder("grouped")
        manager.register_handler(grouped, key="quiet")
        quiet, _ = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        monitored = {item.node_id for item in targets}
        spare = [node for node in plant_variables(quiet) if node.node_id not in monitored][:2]
        spare_nodes = [node.node_id for node in spare]
        await manager.subscribe(spare_nodes, key="quiet")
        if spare_nodes:
            # Wait for one of the new items specifically. wait_for_notifications counts
            # every notification the subscription has dispatched, so the tags this
            # example already monitors satisfy it and the keyed items prove nothing.
            keyed = await manager.wait_for_value(spare_nodes[0], max_wait=10.0)
            bullet(f"keyed record: {keyed.node_id_text} key={keyed.key!r} seq={keyed.sequence}")
        bullet(f"key 'quiet' handler saw: {grouped.sequences}")
        leaked = [record for record in recorder.records if record.key == "quiet"]
        bullet(f"records with key 'quiet' that reached the default handler: {len(leaked)}")
        bullet("a key routes: the default handlers do not also read grouped items")
        bullet(f"manager knows {manager.stats.monitored_items} items in total")

        heading("6. an absence is a TimeoutError, never a None")
        await manager.pause()
        # The watermark is taken *after* the pause: a record that arrived before it is
        # not a value that arrived during it, and comparing against an old one makes an
        # absence look like a success.
        watermark = manager.last_value(first.node_id)
        bullet(f"paused: {manager.stats.summary()}")
        if run.uses_demo_server:
            await run.demo_server.set_value(first.name, 1.5)
        try:
            awaited = await manager.wait_for_value(
                first.node_id, newer_than=watermark, max_wait=1.0
            )
        except TimeoutError:
            bullet("wait_for_value raised TimeoutError: the value did not arrive, and the")
            bullet("example knows that instead of reading a None it has to guess about")
        else:  # pragma: no cover - only if this server publishes while paused
            bullet(f"unexpected value while paused: {awaited.value!r}")
        await manager.resume()
        bullet(f"resumed: {manager.stats.summary()}")
        restored = await manager.wait_for_value(first.node_id, max_wait=8.0)
        bullet(f"the cached value survived the pause: {restored.node_id_text} = {restored.value!r}")

        heading("7. what the manager kept track of")
        bullet(manager.stats.summary())
        bullet(f"stray notifications dropped: {manager.stats.stray_notifications}")
        bullet(f"latest cached values: {len(manager.latest_values)} nodes")
        await manager.stop()

        heading("building block")
        bullet("SubscriptionManager.register_handler / wait_for_* / last_value")
        bullet("DataChangeRecord is the frozen contract between the protocol and your code")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
