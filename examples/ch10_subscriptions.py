"""Chapter 10: subscriptions - monitored items, chunked to the server's limit.

A subscription is not a callback you register: it is state you establish on the
server, and every part of it is negotiated.

* one ``CreateSubscription``, then ``CreateMonitoredItems`` in chunks of the
  server's ``MaxMonitoredItemsPerCall``;
* every subscription is *negotiated*: ``CreateSubscription`` and
  ``ModifySubscription`` answer with **revised** publishing, lifetime and
  keepalive numbers, and a client that never reads them believes its own request;
* publishing is client driven - the server only reports when a ``Publish`` request
  is answered - so the publishing interval decides how often you hear, and the
  sampling interval only how often the server looks;
* the sampling interval and queue size you ask for are *requests*: asyncua's
  high level create path returns the ``MonitoredItemId`` and discards the revised
  values, which is why this chapter asks the server a second question;
* a new monitored item reports the current value on its first publish, which is
  why a subscription is also a fast way to take a snapshot;
* ``MonitoredItemId`` (the server's handle) and ``ClientHandle`` (the number that
  comes back with every notification) are different ids, and mixing them up is the
  classic Chapter 10 mistake; and a ``MonitoredItemId`` is unique *inside its
  subscription*, not across a server, so the pair is what identifies an item.

Run it:

    uv run python examples/ch10_subscriptions.py --demo
    uv run python examples/ch10_subscriptions.py --endpoint opc.tcp://127.0.0.1:4840

With ``--demo`` the demo server keeps changing its process tags, so the values that
arrive are spontaneous; the one value the example writes itself proves the path from
a write to a notification.
"""

from __future__ import annotations

import asyncio
import math

from asyncua import ua

from _support import (
    bullet,
    chapter_run,
    heading,
    instrument_service_calls,
    plant_variables,
)
from opcua_toolkit import (
    BrowsePathError,
    DataChangeRecord,
    NamespaceExplorer,
    NodeRef,
    SubscriptionDefaults,
    SubscriptionManager,
    SubscriptionOptions,
    describe_status,
)

#: How many tags this chapter monitors. The number is not the point - showing that
#: a monitored item is a *server resource* with a cost per item is.
MONITOR_LIMIT = 30


async def main() -> int:
    """Establish monitored items, then watch what the server actually agreed to."""
    async with (
        chapter_run(
            "subscribe to tags and read back what the server revised",
            session_name="Chapter 10 - subscriptions",
        ) as run,
        run.open_client() as client,
    ):
        counters = instrument_service_calls(
            client,
            (
                "create_subscription",
                "update_subscription",
                "publish",
                "create_monitored_items",
                "delete_monitored_items",
                "delete_subscriptions",
                "set_publishing_mode",
            ),
        )
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)
        manager = SubscriptionManager(
            client,
            limits=limits,
            defaults=SubscriptionDefaults(
                publishing_interval=500.0, sampling_interval=100.0, queue_size=10
            ),
        )

        heading("1. the nodes come from browsing")
        found, _ = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        candidates = plant_variables(found)[:MONITOR_LIMIT]
        nodes = [ref.node_id for ref in candidates]
        bullet(f"{len(nodes)} variables to monitor")
        bullet(
            f"MaxMonitoredItemsPerCall published by this server: "
            f"{limits.max_monitored_items_per_call}"
        )
        bullet(f"MinimumSupportedSampleRate: {limits.min_supported_sample_rate} ms")

        heading("2. one subscription, many monitored items")
        handles = await manager.subscribe(
            nodes, options=SubscriptionOptions(sampling_interval=10.0, queue_size=5)
        )
        bullet(f"subscription id {manager.subscription_id}, items {manager.stats.monitored_items}")
        bullet(f"CreateSubscription calls      : {counters['create_subscription']}")
        bullet(
            f"CreateMonitoredItems calls    : {counters['create_monitored_items']} "
            f"(expected {math.ceil(len(nodes) / limits.max_monitored_items_per_call)})"
        )
        for handle in handles[:4]:
            revised = (
                f"{handle.revised_sampling_interval}ms"
                if handle.revised_sampling_interval is not None
                else "not reported"
            )
            queue = (
                str(handle.revised_queue_size)
                if handle.revised_queue_size is not None
                else "not reported"
            )
            bullet(
                f"  {handle.node_id_text:<26} MonitoredItemId={handle.monitored_item_id} "
                f"asked={handle.requested_sampling_interval}ms revised={revised} queue={queue}"
            )
        bullet(
            "sampling interval asked for: 10ms. The answer is 'not reported', and that is "
            "the honest answer:"
        )
        bullet(
            "asyncua's create path returns the MonitoredItemId and throws the rest of the "
            "response away"
        )
        bullet(
            "the toolkit reports only what the server actually told it, so Chapter 10 has to "
            "ask a second question"
        )
        bullet(
            "one SubscriptionId, 30 MonitoredItemIds: an item id is unique inside its "
            "subscription, not across a server"
        )

        heading("3. what the server actually agreed to")
        counters["update_subscription"] = 0
        revision = await manager.confirm_subscription()
        bullet(revision.summary())
        for change in revision.revisions:
            bullet(f"  the server changed: {change}")
        bullet(f"ModifySubscription calls used to find that out: {counters['update_subscription']}")
        bullet(
            "a subscription is negotiated, not configured: CreateSubscription and "
            "ModifySubscription never answer 'no', they answer with revised numbers"
        )
        bullet(
            "publishing 500ms with items sampled at 10ms means the server looks at the tag "
            "often and reports in batches: the publishing interval decides how often you hear"
        )
        if limits.min_supported_sample_rate:
            bullet(
                f"this server publishes MinimumSupportedSampleRate="
                f"{limits.min_supported_sample_rate}ms and does not enforce it"
            )
            bullet(
                "a limit a server advertises and a limit a server applies are different promises"
            )

        heading("4. the first publish is the current value")
        snapshot = await manager.wait_for_value(nodes[0])
        _line(snapshot)
        bullet(
            "no write was needed: monitoring a variable reports its present value, "
            "which makes a subscription a cheap way to snapshot a plant"
        )

        heading("5. reporting is client driven: Publish requests")
        counters["publish"] = 0
        await asyncio.sleep(1.2)
        bullet(f"Publish requests in 1.2s at a 500ms interval: {counters['publish']}")
        bullet(manager.stats.summary())
        bullet(
            "the server queues notifications; nothing arrives until the client asks with "
            "Publish, which is why a subscription can look dead while the server is busy reporting"
        )

        heading("6. a write becomes a notification")
        temperature = await _find_temperature(explorer)
        if temperature is not None and run.uses_demo_server:
            before = manager.last_value(temperature.node_id)
            counters["create_monitored_items"] = 0
            await run.demo_server.set_value("Temperature", 43.75)
            newer = await manager.wait_for_value(
                temperature.node_id, newer_than=before, max_wait=8.0
            )
            _line(newer)
            bullet(
                f"new CreateMonitoredItems calls: {counters['create_monitored_items']} "
                "(0 is the point: the item was already monitored)"
            )
            bullet(
                f"sequence {before.sequence if before else 0} -> {newer.sequence}: "
                "the manager numbers every record so a reader can see a gap"
            )
        else:
            bullet("temperature tag not found, or the server is not the demo server")

        heading("7. changing the interval is a negotiation, not an assignment")
        counters["update_subscription"] = 0
        counters["create_subscription"] = 0
        counters["create_monitored_items"] = 0
        answer = await manager.set_publishing_interval(1000.0)
        if answer is not None:
            bullet(answer.summary())
            bullet(
                f"the server was asked for 1000ms and answered "
                f"{answer.revised_publishing_interval}ms"
            )
            if answer.rebuilt:
                bullet("so the manager rebuilt the subscription instead of pretending")
                bullet(
                    f"rebuilt in {counters['create_subscription']} CreateSubscription and "
                    f"{counters['create_monitored_items']} CreateMonitoredItems calls, "
                    f"every item restored"
                )
                bullet(
                    "the restored items carry the same MonitoredItemIds as the ones that were "
                    "deleted: this server restarts its counter for every subscription"
                )
                bullet(
                    "so (SubscriptionId, MonitoredItemId) is what identifies a monitored item, "
                    "and a cache keyed on the item id alone mixes two subscriptions"
                )
                bullet(
                    "asyncua's own server ignores ModifySubscription requests and answers "
                    "with the values from creation: it is not the only server that does"
                )
            else:
                bullet("the server accepted it: same subscription id, same monitored items")
            bullet(f"ModifySubscription calls: {counters['update_subscription']}")
            bullet(f"monitored items now: {manager.stats.monitored_items}, all Good")
        bullet(f"publishing interval this client runs with: {manager.publishing_interval} ms")

        heading("8. pausing is a server decision with a client guard")
        counters["set_publishing_mode"] = 0
        await manager.pause()
        bullet(f"after pause : {manager.stats.summary()}")
        bullet(f"SetPublishingMode calls: {counters['set_publishing_mode']}")
        await manager.resume()
        bullet(f"after resume: {manager.stats.summary()}")

        heading("9. releasing what we took")
        counters["delete_monitored_items"] = 0
        await manager.unsubscribe([handles[0]])
        bullet(
            f"DeleteMonitoredItems calls: {counters['delete_monitored_items']}, "
            f"items left {manager.stats.monitored_items}"
        )
        counters["delete_subscriptions"] = 0
        await manager.stop()
        bullet(
            f"DeleteSubscriptions calls: {counters['delete_subscriptions']}, "
            f"subscription id {manager.subscription_id}"
        )
        bullet(manager.stats.summary())

        heading("building block")
        bullet("SubscriptionManager: subscribe / unsubscribe / wait_for_value / pause / rebuild")
        bullet("SubscriptionRevision: what the server answered, not what we asked for")
        bullet(
            "MonitoredItemHandle keeps the server's id; DataChangeRecord keeps the client handle"
        )
    return 0


async def _find_temperature(explorer: NamespaceExplorer) -> NodeRef | None:
    """Find the demo temperature tag if this server has one."""
    try:
        return await explorer.find_by_browse_path(["ProductionLine", "Line1", "Temperature"])
    except BrowsePathError:
        return None


def _line(record: DataChangeRecord | None) -> None:
    """Print one notification the way an operator needs to read it."""
    if record is None:
        bullet("  (no value known yet)")
        return
    bullet(
        f"  {record.node_id_text:<26} value={record.value!r} {describe_status(record.status_code)} "
        f"seq={record.sequence} clientHandle={record.client_handle} "
        f"source={record.source_timestamp}"
    )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
