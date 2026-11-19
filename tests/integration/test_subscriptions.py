"""Chapter 10 and Chapter 11 integration tests: monitored items that behave.

Two rules shape this file.

**Waits, never sleeps.** Every "did the notification arrive?" question is answered
by :meth:`SubscriptionManager.wait_for_value` or
:meth:`SubscriptionManager.wait_for_notifications`, which wake up on a real
dispatch. A test that sleeps is a test that passes on a fast machine and fails in
CI on a loaded one.

**Silence is proven the same way.** "Nothing arrived" is not the absence of a
sleep: it is a wait that must time out. Those tests assert the timeout, so a
subscription that wrongly reports cannot hide behind a short pause.

The demo server runs with its simulator off (see ``conftest.py``), so every value
asserted here is a value the test wrote. Every test stops its subscription in a
``finally`` block: a leaked subscription is the classic source of flaky OPC UA
test suites.
"""

from __future__ import annotations

import asyncio
import math

import pytest
from asyncua import ua

from opcua_toolkit.errors import NotConnectedError
from opcua_toolkit.subscriptions import (
    DEFAULT_SUBSCRIPTION_KEY,
    DataChangeRecord,
    SubscriptionManager,
    SubscriptionOptions,
    SubscriptionRevision,
)

pytestmark = pytest.mark.integration

PUBLISHING_MS = 100.0
#: Long enough for ten publish cycles: a value that is coming arrives well inside
#: this, and a value that is not coming stays away.
PRESENT_WAIT_S = 5.0
ABSENT_WAIT_S = 1.0

TEMPERATURE = "ns=2;s=Temperature"
RECIPE = "ns=2;s=RecipeName"


class Recorder:
    """Collect notifications the way a Chapter 11 dispatcher would."""

    def __init__(self) -> None:
        self.records: list[DataChangeRecord] = []

    async def __call__(self, record: DataChangeRecord) -> None:
        # The record is frozen and already unwrapped, so storing it as it is cannot
        # be invalidated by the next notification. That is why the toolkit flattens
        # asyncua's mutable notification object in the first place.
        self.records.append(record)

    @property
    def nodes(self) -> set[str]:
        return {record.node_id_text for record in self.records}

    @property
    def values(self) -> list[object]:
        return [record.value for record in self.records]

    @property
    def statuses(self) -> list[str]:
        return [record.status_code.name for record in self.records]

    @property
    def keys(self) -> list[str]:
        return [record.key for record in self.records]


async def expect_value(
    manager: SubscriptionManager, node: str, value: object, *, max_wait: float = PRESENT_WAIT_S
) -> DataChangeRecord:
    """Wait until the newest record for ``node`` carries exactly ``value``.

    Built on :meth:`SubscriptionManager.wait_for_value` with the previous record as
    a watermark: each iteration blocks on a real dispatch instead of a sleep, and a
    notification that arrives while the test is still writing is consumed by the
    loop instead of being mistaken for the answer.
    """
    seen: DataChangeRecord | None = None
    try:
        async with asyncio.timeout(max_wait):
            while True:
                record = await manager.wait_for_value(node, newer_than=seen, max_wait=max_wait)
                if record.value == value:
                    return record
                seen = record
    except TimeoutError:
        raise AssertionError(f"never saw {value!r} for {node}; last record was {seen!r}") from None


async def expect_silence(manager: SubscriptionManager, *, max_wait: float = ABSENT_WAIT_S) -> int:
    """Prove that no notification arrives within ``max_wait`` seconds."""
    baseline = manager.stats.notifications
    with pytest.raises(TimeoutError):
        await manager.wait_for_notifications(baseline + 1, max_wait=max_wait)
    assert manager.stats.notifications == baseline
    return baseline


def new_manager(client, limits) -> SubscriptionManager:
    return SubscriptionManager(client, limits=limits, publishing_interval=PUBLISHING_MS)


async def test_a_subscription_reports_values_as_they_change(client, limits, demo_server) -> None:
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe([TEMPERATURE, RECIPE])
        assert len(handles) == 2
        assert manager.subscription_id is not None
        assert manager.stats.monitored_items == 2

        # The first thing a monitored item reports is the value it already has.
        await manager.wait_for_value(TEMPERATURE)

        for value in (20.5, 21.5, 22.5):
            await demo_server.set_value("Temperature", value)
            await expect_value(manager, TEMPERATURE, value)
        await demo_server.set_value("RecipeName", "recipe-a")
        await expect_value(manager, RECIPE, "recipe-a")

        assert {TEMPERATURE, RECIPE} <= recorder.nodes
        assert manager.stats.notifications == len(recorder.records)
        assert all(status == "Good" for status in recorder.statuses)
        assert manager.stats.good_values == manager.stats.notifications
        assert manager.stats.uncertain_values == 0
        assert manager.stats.bad_values == 0
    finally:
        await manager.stop()


async def test_the_first_notification_carries_the_current_value_not_a_change(
    client, limits, demo_server
) -> None:
    """A monitored item reports what is true now, not only what changes next.

    Writing before subscribing is the honest way to see it: the value the tag
    already had arrives as the first notification, with the same StatusCode and
    SourceTimestamp as a change would. A client that treats "a notification" as
    "a change" will double count this one.
    """
    await demo_server.set_value("RecipeName", "already-set")
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe([RECIPE])
        first = await manager.wait_for_value(RECIPE)
        assert first.value == "already-set"
        assert first.status_code.is_good()
        assert first.source_timestamp is not None
        assert first.server_timestamp is not None
        assert first.node_id_text == RECIPE
        assert first.key == DEFAULT_SUBSCRIPTION_KEY
        # The ClientHandle the notification carries is not the MonitoredItemId the
        # subscribe call returned: a monitored item has two ids.
        assert first.client_handle > 0
        assert first.sequence == 1  # the first record this manager dispatched
        # The MonitoredItemId subscribe() returned is the server's number for the
        # item, and it is a different number from the ClientHandle above.
        assert handles[0].monitored_item_id > 0
        assert recorder.records == [first]
    finally:
        await manager.stop()


async def test_a_write_arrives_as_a_notification_with_its_timestamps(
    client, limits, demo_server
) -> None:
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        await manager.subscribe([RECIPE])
        initial = await manager.wait_for_value(RECIPE)

        await demo_server.set_value("RecipeName", "written-value")
        record = await expect_value(manager, RECIPE, "written-value")

        assert record.status_code.is_good()
        assert record.variant_type in (ua.VariantType.String, ua.VariantType.LocalizedText)
        assert record.source_timestamp is not None
        assert initial.source_timestamp is not None
        assert record.source_timestamp >= initial.source_timestamp
        assert record.sequence > initial.sequence
        assert manager.last_value(RECIPE) is record
        assert manager.latest_values[RECIPE] is record
    finally:
        await manager.stop()


async def test_monitored_items_are_chunked_to_max_monitored_items_per_call(
    client, limits, demo_server, service_calls
) -> None:
    nodes = demo_server.tag_ids()
    manager = new_manager(client, limits)
    try:
        service_calls.clear()
        handles = await manager.subscribe(nodes)
        expected = math.ceil(len(nodes) / limits.max_monitored_items_per_call)
        assert service_calls["create_monitored_items"] == expected
        assert len(handles) == len(nodes)
        assert manager.stats.monitored_items == len(nodes)
        assert manager.stats.failed_items == 0
        # One CreateMonitoredItems request per chunk, and every item in it got its
        # own server assigned id.
        assert len({handle.monitored_item_id for handle in handles}) == len(handles)
    finally:
        await manager.stop()


async def test_a_handler_is_dispatched_by_the_key_it_was_registered_under(
    client, limits, demo_server
) -> None:
    process = Recorder()
    quality = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(process, key="process")
    manager.register_handler(quality, key="quality")
    try:
        await manager.subscribe([TEMPERATURE], key="process")
        await manager.subscribe([RECIPE], key="quality")

        await demo_server.set_value("Temperature", 19.5)
        await demo_server.set_value("RecipeName", "dispatch-check")
        await expect_value(manager, TEMPERATURE, 19.5)
        await expect_value(manager, RECIPE, "dispatch-check")

        assert process.nodes == {TEMPERATURE}
        assert quality.nodes == {RECIPE}
        assert set(process.keys) == {"process"}
        assert set(quality.keys) == {"quality"}
    finally:
        await manager.stop()


async def test_pausing_keeps_the_items_and_stops_the_reports(
    client, limits, demo_server, service_calls
) -> None:
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe([TEMPERATURE])
        before = await manager.wait_for_value(TEMPERATURE)
        count = len(recorder.records)

        await manager.pause()
        assert "mode=paused" in manager.stats.summary()
        assert manager.stats.monitored_items == 1

        await demo_server.set_value("Temperature", 40.5)
        await expect_silence(manager)
        assert len(recorder.records) == count
        # The cached record is still the last known value: a pause hides nothing,
        # it only stops the flow.
        assert manager.last_value(TEMPERATURE) is before

        await manager.resume()
        assert "mode=reporting" in manager.stats.summary()
        service_calls.clear()
        await demo_server.set_value("Temperature", 50.5)
        record = await expect_value(manager, TEMPERATURE, 50.5)
        # Nothing had to be re-monitored: the same MonitoredItemId is still in
        # place, so a pause costs one SetPublishingMode per direction and no
        # CreateMonitoredItems at all.
        assert manager.items[0].monitored_item_id == handles[0].monitored_item_id
        assert service_calls["create_monitored_items"] == 0
        assert service_calls["set_publishing_mode"] == 0
        assert record.client_handle > 0
        assert len(recorder.records) > count
    finally:
        await manager.stop()


async def test_disabled_mode_reports_nothing_at_all(client, limits, demo_server) -> None:
    """``Disabled`` stops monitoring: not even the current value is reported."""
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe(
            [TEMPERATURE], options=SubscriptionOptions(monitoring_mode=ua.MonitoringMode.Disabled)
        )
        assert handles[0].options.monitoring_mode is ua.MonitoringMode.Disabled

        await demo_server.set_value("Temperature", 60.5)
        await expect_silence(manager)
        assert recorder.records == []
        assert manager.last_value(TEMPERATURE) is None

        await manager.set_monitoring_mode(ua.MonitoringMode.Reporting)
        await demo_server.set_value("Temperature", 61.5)
        record = await expect_value(manager, TEMPERATURE, 61.5)
        assert record.status_code.is_good()
        assert recorder.records
    finally:
        await manager.stop()


async def test_sampling_mode_reports_on_this_server_so_pause_is_the_switch_you_control(
    client, limits, demo_server
) -> None:
    """MonitoringMode.Sampling is specified as "sample, do not report".

    asyncua's server does not implement it as a separate behaviour: a Sampling item
    keeps reporting. That is a lesson, not a bug to route around - a client that
    needs a subscription muted must use the mechanism it controls, SetPublishingMode
    plus the manager's own guard, and not a mode it merely hopes the server honours.
    """
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        await manager.subscribe(
            [TEMPERATURE], options=SubscriptionOptions(monitoring_mode=ua.MonitoringMode.Sampling)
        )
        # The surprising fact first: Sampling reports, on this server.
        await manager.wait_for_value(TEMPERATURE)
        assert recorder.records

        await demo_server.set_value("Temperature", 62.5)
        await expect_value(manager, TEMPERATURE, 62.5)

        await manager.pause()
        await demo_server.set_value("Temperature", 63.5)
        await expect_silence(manager)
        assert manager.stats.paused
        # On a server that honours SetPublishingMode the client guard has nothing
        # to drop, so the counter stays 0. On one that ignores it, this is the
        # number that shows the guard worked: see the unit test that drives a
        # non-compliant server.
        assert manager.stats.suppressed == 0
        assert 63.5 not in recorder.values

        await manager.set_monitoring_mode(ua.MonitoringMode.Reporting)
        await manager.resume()
        await demo_server.set_value("Temperature", 64.5)
        await expect_value(manager, TEMPERATURE, 64.5)
    finally:
        await manager.stop()


async def test_unsubscribing_removes_the_item_and_its_notifications(
    client, limits, demo_server, service_calls
) -> None:
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe([TEMPERATURE, RECIPE])
        await manager.wait_for_value(TEMPERATURE)
        await manager.wait_for_value(RECIPE)

        await manager.unsubscribe([handles[1]])
        assert manager.stats.monitored_items == 1
        assert [item.node_id_text for item in manager.items] == [TEMPERATURE]

        service_calls.clear()
        await manager.unsubscribe([TEMPERATURE])
        assert service_calls["delete_monitored_items"] == 1
        assert manager.items == []

        # The node still exists and still changes - it is just not monitored.
        await demo_server.set_value("Temperature", 80.5)
        await expect_silence(manager)
        assert 80.5 not in recorder.values
    finally:
        await manager.stop()


async def test_rebuilding_recreates_every_monitored_item_on_the_new_subscription(
    client, limits, demo_server
) -> None:
    recorder = Recorder()
    manager = new_manager(client, limits)
    manager.register_handler(recorder)
    try:
        await manager.subscribe([TEMPERATURE, RECIPE])
        old_id = manager.subscription_id
        await manager.wait_for_value(TEMPERATURE)
        await manager.wait_for_value(RECIPE)

        handles = await manager.rebuild()
        assert len(handles) == 2
        assert manager.stats.recreations == 1
        assert manager.stats.monitored_items == 2
        assert manager.subscription_id not in (None, old_id)

        # A rebuilt subscription reports the current values again: the items are
        # new, so their first notification is an initial value, not a change.
        await manager.wait_for_value(TEMPERATURE)

        await demo_server.set_value("RecipeName", "after-rebuild")
        await expect_value(manager, RECIPE, "after-rebuild")
        assert RECIPE in recorder.nodes
    finally:
        await manager.stop()


async def test_a_rebuilt_subscription_comes_back_paused_if_it_was_paused(
    client, limits, demo_server
) -> None:
    """A reconnect must not turn the data flow back on behind the application's back."""
    manager = new_manager(client, limits)
    try:
        await manager.subscribe([TEMPERATURE])
        await manager.wait_for_value(TEMPERATURE)

        await manager.pause()
        await manager.rebuild()
        assert manager.stats.paused
        assert manager.stats.monitored_items == 1

        await demo_server.set_value("Temperature", 90.5)
        await expect_silence(manager)

        await manager.resume()
        await demo_server.set_value("Temperature", 91.5)
        await expect_value(manager, TEMPERATURE, 91.5)
    finally:
        await manager.stop()


async def test_a_handler_that_raises_never_kills_the_subscription(
    client, limits, demo_server
) -> None:
    """A dead sink is an application bug, not a reason to lose the subscription."""
    broken_seen: list[str] = []
    healthy = Recorder()

    async def broken_handler(record: DataChangeRecord) -> None:
        broken_seen.append(record.node_id_text)
        raise RuntimeError("downstream sink is down")

    manager = new_manager(client, limits)
    manager.register_handler(broken_handler, key="process")
    manager.register_handler(healthy, key="quality")
    try:
        await manager.subscribe([TEMPERATURE], key="process")
        await manager.subscribe([RECIPE], key="quality")
        await manager.wait_for_value(TEMPERATURE)

        for value in (95.5, 96.5, 97.5):
            await demo_server.set_value("Temperature", value)
            await expect_value(manager, TEMPERATURE, value)

        assert len(broken_seen) >= 3
        assert manager.stats.errors >= 1

        # The consumer survived, and the other key still works.
        await demo_server.set_value("RecipeName", "still-delivering")
        await expect_value(manager, RECIPE, "still-delivering")
        assert healthy.values[-1] == "still-delivering"
        assert manager.subscription_id is not None
    finally:
        await manager.stop()


async def test_a_node_the_server_refuses_to_monitor_is_counted_not_hidden(client, limits) -> None:
    manager = new_manager(client, limits)
    try:
        handles = await manager.subscribe(["ns=2;s=NoSuchTag"])
        assert handles == []
        assert manager.stats.failed_items == 1
        assert manager.items == []
    finally:
        await manager.stop()


async def test_queue_size_and_sampling_interval_are_the_ones_the_caller_asked_for(
    client, limits
) -> None:
    manager = SubscriptionManager(client, limits=limits, publishing_interval=250.0)
    try:
        handles = await manager.subscribe(
            [TEMPERATURE],
            options=SubscriptionOptions(sampling_interval=100.0, queue_size=5),
        )
        assert handles[0].requested_sampling_interval == 100.0
        assert handles[0].options.queue_size == 5
        assert manager.publishing_interval == 250.0
        assert "publishing=250" in manager.stats.summary()
    finally:
        await manager.stop()


def revision_of(manager: SubscriptionManager) -> SubscriptionRevision | None:
    """Read the answer the manager currently holds, without caching it.

    Asking twice in one test - "nothing has been asked yet", then "here is the
    answer" - is the point, so the property is read through a call each time.
    """
    return manager.revision


async def test_confirm_subscription_reports_the_numbers_this_server_clamped(
    client, limits, demo_server, service_calls
) -> None:
    """A subscription is negotiated, and the revised numbers are the real ones.

    The client asked for asyncua's default 10 000 publishing intervals of lifetime.
    The demo server has a 1 000 cap it never advertises anywhere in the address
    space, so the only way to learn the subscription that actually runs is to send a
    ``ModifySubscription`` and read what comes back. That is what
    :meth:`SubscriptionManager.confirm_subscription` does, and it is one service call.
    """
    manager = new_manager(client, limits)
    try:
        await manager.subscribe([TEMPERATURE])
        # Nothing has been asked yet, so nothing is claimed.
        assert revision_of(manager) is None

        service_calls.clear()
        answer = await manager.confirm_subscription()
        assert service_calls["update_subscription"] == 1
        assert answer.service == "ModifySubscription"
        assert answer.rebuilt is False

        caps = demo_server.subscription_caps
        assert answer.requested_publishing_interval == PUBLISHING_MS
        # This server echoes the publishing interval it was asked for, and clamps the
        # two counts to caps it publishes nowhere.
        assert answer.publishing_interval_accepted
        assert answer.revised_publishing_interval == pytest.approx(PUBLISHING_MS)
        assert answer.requested_lifetime_count > caps["max_lifetime_count"]
        assert answer.revised_lifetime_count == caps["max_lifetime_count"]
        assert answer.revised is True
        assert any("lifetime count" in line for line in answer.revisions)
        assert revision_of(manager) is not None
        assert revision_of(manager) is answer
    finally:
        await manager.stop()


async def test_an_interval_this_server_ignores_is_answered_with_a_rebuild(
    client, limits, demo_server, service_calls
) -> None:
    """asyncua's server ignores ``ModifySubscription``: it answers with its own values.

    ``subscription_service.modify_subscription`` returns the numbers the subscription
    was created with, whatever was requested. A client that treats "we sent a
    request" as "we got a new interval" ends up believing it publishes every 1000 ms
    while the server publishes every 100 ms, forever and silently. The manager
    compares the answer with the request and, when the server did not move, rebuilds
    - which costs a DeleteSubscriptions, a CreateSubscription and a fresh
    CreateMonitoredItems, and is why knowing the cost matters.
    """
    manager = new_manager(client, limits)
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe([TEMPERATURE, RECIPE])
        old_subscription = manager.subscription_id
        old_items = [handle.monitored_item_id for handle in handles]
        await manager.wait_for_value(TEMPERATURE)

        service_calls.clear()
        answer = await manager.set_publishing_interval(300.0)
        assert answer is not None
        # The answer is the *final* subscription's, and it did move - but only because
        # the manager rebuilt. ``rebuilt`` is what tells those two cases apart.
        assert answer.rebuilt is True
        assert answer.revised_publishing_interval == pytest.approx(300.0)
        assert answer.publishing_interval_accepted
        assert manager.publishing_interval == 300.0
        assert "publishing=300" in manager.stats.summary()

        # The cost of a server that will not modify: one subscription replaced, items
        # recreated, and two ModifySubscription calls (the refusal and the confirmation).
        assert service_calls["update_subscription"] == 2
        assert service_calls["delete_subscriptions"] == 1
        assert service_calls["create_subscription"] == 1
        assert service_calls["create_monitored_items"] == 1
        assert manager.stats.recreations == 1
        assert manager.stats.monitored_items == 2

        # A new subscription, and - the part almost everyone gets wrong - possibly the
        # same MonitoredItemIds. A MonitoredItemId is unique inside one subscription,
        # not across a server: this one starts every subscription's counter at 112, so
        # the rebuilt items land on the numbers the deleted ones used. The pair
        # (SubscriptionId, MonitoredItemId) is what identifies a monitored item, and a
        # cache keyed on the item id alone will mix two subscriptions.
        assert manager.subscription_id not in (None, old_subscription)
        new_items = [item.monitored_item_id for item in manager.items]
        assert len(new_items) == 2
        assert set(new_items) == set(old_items)
        assert old_subscription != manager.subscription_id

        # And the new subscription does what the application asked: it reports.
        await demo_server.set_value("Temperature", 31.5)
        await expect_value(manager, TEMPERATURE, 31.5)
    finally:
        await manager.stop()


async def _ignore(record: DataChangeRecord) -> None:
    """A handler that exists only so the manager dispatches normally."""


async def test_latest_values_is_the_snapshot_an_application_can_serve(
    client, limits, demo_server
) -> None:
    """The cache a gateway answers from, and the trap that comes with it."""
    manager = new_manager(client, limits)
    manager.register_handler(_ignore)
    try:
        handles = await manager.subscribe([TEMPERATURE, RECIPE])
        await manager.wait_for_value(TEMPERATURE)
        await manager.wait_for_value(RECIPE)

        snapshot = manager.latest_values
        assert set(snapshot) == {TEMPERATURE, RECIPE}
        assert all(record.status_code.is_good() for record in snapshot.values())
        assert all(record.source_timestamp is not None for record in snapshot.values())

        # Unsubscribing does not delete what was already learned: the last known
        # value stays, and only SourceTimestamp tells you how old it is.
        await manager.unsubscribe([handles[0]])
        assert manager.last_value(TEMPERATURE) is not None
        assert TEMPERATURE in manager.latest_values
    finally:
        await manager.stop()


async def test_a_wait_that_never_completes_names_what_it_was_waiting_for(
    client, limits, demo_server
) -> None:
    """A timeout that does not say what it waited for is useless at 03:00."""
    manager = new_manager(client, limits)
    try:
        await manager.subscribe(
            [TEMPERATURE], options=SubscriptionOptions(monitoring_mode=ua.MonitoringMode.Disabled)
        )
        with pytest.raises(TimeoutError) as caught:
            await manager.wait_for_value(TEMPERATURE, max_wait=0.4)
        message = str(caught.value)
        assert TEMPERATURE in message
        assert "0.4s" in message
    finally:
        await manager.stop()


async def test_stopping_a_subscription_releases_it_and_reports_it(
    client, limits, service_calls
) -> None:
    manager = new_manager(client, limits)
    await manager.subscribe([TEMPERATURE])
    subscription_id = manager.subscription_id
    assert subscription_id is not None

    service_calls.clear()
    await manager.stop()
    # Not only a new local: the manager's own view of the subscription is gone, and
    # the DeleteSubscriptions request proves the server was told.
    assert manager.subscription_id is None
    assert service_calls["delete_subscriptions"] == 1
    assert manager.items == []


async def test_using_a_stopped_manager_says_so_instead_of_sending_anything(client, limits) -> None:
    manager = new_manager(client, limits)
    await manager.start()
    await manager.stop()
    with pytest.raises(NotConnectedError):
        await manager.pause()
