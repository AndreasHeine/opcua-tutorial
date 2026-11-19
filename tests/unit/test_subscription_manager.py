"""Chapter 11 unit tests: what the dispatcher promises, proven without a server.

Some behaviours cannot be shown against a well behaved server at all. The client
side pause guard only proves itself against a server that keeps reporting while
publishing is off, and a "stray" notification only appears when a notification was
already on the wire while its monitored item was deleted.

So these tests run the manager against a fake subscription that yields exactly what
the test queues and ignores ``SetPublishingMode``. Everything the manager is driven
through is its public API: ``subscribe``, ``pause``, ``resume``, the waits and the
counters. The fake creates a new subscription per ``create_subscription`` call, as a
real client does, because a fake that reuses one object hides exactly the bug the
rebuild tests are there to catch.

The fake can also answer ``ModifySubscription`` two ways: apply the request, or answer
with the values it already had. The second one is what asyncua's own server does, and a
manager that believes "we asked" instead of reading the answer would pass every test
against the first fake and fail in the field against the second.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from asyncua import Client, Node, ua
from asyncua.common.session_interface import AbstractSession
from asyncua.common.subscription import (
    DataChangeEvent,
    DataChangeNotif,
    OverflowPolicy,
    StatusChangeEvent,
    SubscriptionItemData,
)
from asyncua.ua.uaerrors import BadSubscriptionIdInvalid

from opcua_toolkit.nodeids import to_nodeid
from opcua_toolkit.subscriptions import (
    DEFAULT_SUBSCRIPTION_KEY,
    DataChangeRecord,
    SubscriptionManager,
    SubscriptionOptions,
    SubscriptionRevision,
)

TEMPERATURE = "ns=2;s=Temperature"
PRESSURE = "ns=2;s=Pressure"


class FakeSubscription:
    """A subscription that reports exactly what the test queues, and nothing else.

    It deliberately ignores ``SetPublishingMode``: that is the non-compliant server
    the client side guard exists for, and it is the only way to show the guard does
    something.

    Whether it honours ``ModifySubscription`` is the switch the negotiation tests
    use. A server that applies the request and a server that answers with the values
    it already had (asyncua's own server) produce two different outcomes, and the
    manager must tell them apart.
    """

    def __init__(self, client: FakeClient, subscription_id: int) -> None:
        self.client = client
        self.subscription_id = subscription_id
        self.is_deleted = False
        # asyncua closes the notification queue when the subscription is gone, and its
        # next_event() turns that into StopAsyncIteration. Separate from is_deleted
        # because a server can close a queue without the client ever calling delete().
        self.closed = False
        self.publishing_modes: list[bool] = []
        self.monitoring_modes: list[ua.MonitoringMode] = []
        self.create_calls: list[int] = []
        self.delete_calls: list[list[int]] = []
        self.modifies: list[ua.ModifySubscriptionParameters] = []
        self._next_item_id = 101
        # What the client asked for, in the shape a real client keeps: asyncua stores
        # the create parameters here and overwrites them with whatever a later
        # ModifySubscription sent, which is why the manager keeps its own record.
        self.parameters = ua.CreateSubscriptionParameters(
            RequestedPublishingInterval=client.periods[-1],
            RequestedLifetimeCount=10_000,
            RequestedMaxKeepAliveCount=100,
            MaxNotificationsPerPublish=10_000,
            PublishingEnabled=client.publishing_flags[-1],
            Priority=0,
        )
        # What the server says it is *using*. A create echoes the interval and clamps
        # the two counts to its own caps, which is what an OPC UA server does.
        self.revised_publishing_interval = client.periods[-1]
        self.revised_lifetime_count = min(
            self.parameters.RequestedLifetimeCount, client.lifetime_cap
        )
        self.revised_keepalive_count = min(
            self.parameters.RequestedMaxKeepAliveCount, client.keepalive_cap
        )

    @property
    def events(self) -> list[DataChangeEvent]:
        """Notifications live on the client: a rebuilt subscription sees the backlog."""
        return self.client.events

    async def next_event(self, timeout: float | None = None) -> DataChangeEvent | None:
        if self.client.publish_error is not None:
            # A server that deleted the subscription answers the next publish with
            # BadSubscriptionIdInvalid instead of sending anything, which is the other
            # way a client finds out.
            raise self.client.publish_error
        if self.closed:
            # The sentinel: nothing will ever arrive on this subscription again.
            raise StopAsyncIteration
        if self.events:
            return self.events.pop(0)
        await asyncio.sleep(0.01)
        return None

    async def subscribe_data_change(
        self,
        *,
        nodes: Sequence[Node],
        attr: ua.AttributeIds,
        queuesize: int,
        monitoring: ua.MonitoringMode,
        sampling_interval: float,
    ) -> list[int]:
        self.create_calls.append(len(nodes))
        ids = [self._next_item_id + index for index in range(len(nodes))]
        self._next_item_id += len(ids)
        return ids

    async def unsubscribe(self, handles: Sequence[int]) -> None:
        self.delete_calls.append(list(handles))

    async def update(self, params: ua.ModifySubscriptionParameters) -> ua.ModifySubscriptionResult:
        """``ModifySubscription``, answered the way the server under test answers it."""
        self.modifies.append(params)
        if self.client.modify_error is not None:
            # The server's subscription table no longer has this id, and the session is
            # still open. Nothing else in the protocol tells the client that.
            raise self.client.modify_error
        if self.client.honours_modify:
            self.revised_publishing_interval = params.RequestedPublishingInterval
        # A server that has a cap never hands out more than the cap, whether or not it
        # applies the rest of the request: clamping and refusing are different things,
        # and a client must be able to tell them apart.
        self.revised_lifetime_count = min(params.RequestedLifetimeCount, self.client.lifetime_cap)
        self.revised_keepalive_count = min(
            params.RequestedMaxKeepAliveCount, self.client.keepalive_cap
        )
        result = ua.ModifySubscriptionResult()
        result.RevisedPublishingInterval = self.revised_publishing_interval
        result.RevisedLifetimeCount = self.revised_lifetime_count
        result.RevisedMaxKeepAliveCount = self.revised_keepalive_count
        return result

    async def set_publishing_mode(self, publishing: bool) -> None:
        self.publishing_modes.append(publishing)

    async def set_monitoring_mode(self, mode: ua.MonitoringMode) -> None:
        self.monitoring_modes.append(mode)

    async def delete(self) -> None:
        self.is_deleted = True


class FakeClient:
    """Enough of asyncua's ``Client`` for the manager to do its job.

    Each ``create_subscription`` makes a *new* fake, which is what a real client
    does. That detail matters: a fake that hands back the same object would let a
    manager bug (a rebuilt subscription reusing a deleted one, or an old consumer
    task left running) pass unnoticed.

    The caps mirror asyncua's own server defaults (lifetime 15 000, keepalive 5 000),
    so a test has to lower a cap on purpose to see a revision happen - which is the
    same situation a client meets in the field, just with the numbers reversed.
    """

    def __init__(
        self,
        *,
        honours_modify: bool = True,
        lifetime_cap: int = 15_000,
        keepalive_cap: int = 5_000,
    ) -> None:
        self.events: list[DataChangeEvent] = []
        self.subscriptions: list[FakeSubscription] = []
        self.publishing_flags: list[bool] = []
        self.periods: list[float] = []
        self.queue_sizes: list[int] = []
        self.overflow: OverflowPolicy | None = None
        self.honours_modify = honours_modify
        self.lifetime_cap = lifetime_cap
        self.keepalive_cap = keepalive_cap
        # Set this to make the server answer a publish with an error instead of a
        # notification: the way a client finds out the subscription is gone.
        self.publish_error: BaseException | None = None
        # Set this to make the server answer ModifySubscription with an error while the
        # session stays perfectly open. Silence proves nothing, so this is the answer a
        # client only ever gets when it asks.
        self.modify_error: BaseException | None = None
        # A create that never returns models what a rebuild does while it waits for a
        # transport to come back: the rebuild is in progress, and that is a fact the
        # supervision loop has to respect.
        self.create_gate: asyncio.Event | None = None

    @property
    def latest(self) -> FakeSubscription:
        """The subscription the manager is using now."""
        return self.subscriptions[-1]

    async def create_subscription(
        self,
        *,
        period: float,
        handler: Any = None,
        publishing: bool = True,
        queue_maxsize: int = 1_000,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> FakeSubscription:
        if self.create_gate is not None:
            await self.create_gate.wait()
        self.publishing_flags.append(publishing)
        self.periods.append(period)
        self.queue_sizes.append(queue_maxsize)
        self.overflow = overflow
        subscription = FakeSubscription(self, 77 + len(self.subscriptions))
        self.subscriptions.append(subscription)
        return subscription

    def get_node(self, nodeid: ua.NodeId) -> Node:
        return _node(nodeid)


class Recorder:
    """A sink that stores the records it is handed."""

    def __init__(self) -> None:
        self.records: list[DataChangeRecord] = []

    async def __call__(self, record: DataChangeRecord) -> None:
        self.records.append(record)


def make_manager(
    *, client_kwargs: dict[str, Any] | None = None, **kwargs: Any
) -> tuple[SubscriptionManager, FakeClient]:
    """A manager, the fake client it talks to, and the kind of server that answers.

    ``client_kwargs`` choose the *server's* behaviour (``honours_modify``, the
    subscription caps); ``kwargs`` configure the manager itself. Keeping them apart is
    what lets one test say "the server applied the request" and the next say "the
    server ignored it" while the manager is configured identically.
    """
    client = FakeClient(**(client_kwargs or {}))
    manager = SubscriptionManager(cast("Client", client), publishing_interval=100.0, **kwargs)
    return manager, client


def latest(client: FakeClient) -> FakeSubscription:
    """The subscription under test, which is not always ``subscriptions[0]``."""
    return client.latest


def current_revision(manager: SubscriptionManager) -> SubscriptionRevision | None:
    """The answer the manager holds right now, read through a call.

    Asking twice in one test - "there is an answer", then "there is none" - is the
    point, and going through the property each time is how the test says *right now*
    instead of reusing a value it captured earlier.
    """
    return manager.revision


def reported_status(manager: SubscriptionManager) -> ua.StatusCode | None:
    """What the server last said about the subscription, read through a call.

    A test wants to say both "nothing has been reported" and later "this has been
    reported", and reading an optional property directly lets the type checker hold on
    to the first answer for the whole function.
    """
    return manager.subscription_status


def _node(node_id: ua.NodeId) -> Node:
    """A ``Node`` that never talks to a server.

    asyncua's ``Node`` only needs a session to do IO, and these tests do none: the
    manager reads ``event.node.nodeid`` and nothing else.
    """
    return Node(cast("AbstractSession", object()), node_id)


def make_event(
    node: str,
    value: object,
    *,
    status: ua.StatusCode | None = None,
    client_handle: int = 41,
    replayed: bool = False,
    source_timestamp: datetime | None = None,
) -> DataChangeEvent:
    """Build the notification object asyncua would have built, by hand.

    The NodeId is parsed with the toolkit's own parser so that the node the event
    names is the same node ``subscribe()`` created: NodeId text is a contract here,
    and hand assembling it is how a test ends up asserting against a tag nobody
    subscribed to.
    """
    stamp = source_timestamp or datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC)
    data_value = ua.DataValue(
        Value=ua.Variant(value),
        StatusCode=status if status is not None else ua.StatusCode(0),
        SourceTimestamp=stamp,
        ServerTimestamp=stamp,
    )
    notification = ua.MonitoredItemNotification(ClientHandle=client_handle, Value=data_value)
    item_data = SubscriptionItemData()
    item_data.client_handle = client_handle
    item_data.node = _node(to_nodeid(node))
    return DataChangeEvent(
        node=_node(to_nodeid(node)),
        value=value,
        data=DataChangeNotif(item_data, notification),
        replayed=replayed,
    )


async def wait_until(
    predicate: Callable[[], bool], *, timeout: float = 2.0, what: str = "condition"
) -> None:
    """Poll a counter the consumer task updates.

    The manager exposes its counters precisely so that a test (or an operator) can
    watch them; polling them for a bounded time is honest, waiting on them forever
    is not. The manager's own waits (:meth:`SubscriptionManager.wait_for_value` and
    friends) are what application code should use, and they get their own tests.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def test_a_paused_manager_refuses_a_server_that_keeps_reporting() -> None:
    """The whole point of the client side guard, on a server that ignores the request."""
    manager, client = make_manager()
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        client.events.append(make_event(TEMPERATURE, 1.0))
        await manager.subscribe([TEMPERATURE])
        await wait_until(lambda: len(recorder.records) == 1, what="the first notification")

        await manager.pause()
        assert latest(client).publishing_modes == [False]

        # This is the non-compliant part: the server goes on reporting.
        client.events.extend([make_event(TEMPERATURE, 2.0), make_event(TEMPERATURE, 3.0)])
        await wait_until(lambda: manager.stats.suppressed == 2, what="two suppressed reports")

        assert [record.value for record in recorder.records] == [1.0]
        assert manager.stats.notifications == 1
        assert manager.stats.suppressed == 2
        # The cached value is untouched: a pause stops the flow, it does not erase
        # what the application already learned.
        assert manager.last_value(TEMPERATURE) is recorder.records[0]

        await manager.resume()
        assert latest(client).publishing_modes == [False, True]
        client.events.append(make_event(TEMPERATURE, 4.0))
        await wait_until(lambda: len(recorder.records) == 2, what="the resumed notification")
        assert recorder.records[1].value == 4.0
    finally:
        await manager.stop()


async def test_a_paused_wait_reports_a_timeout_instead_of_a_none() -> None:
    """A paused subscription has no value to hand out, and saying so is the API's job.

    A method that returns ``None`` when the caller asked it to wait for data turns a
    deliberate pause into a mystery outage downstream.
    """
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        client.events.append(make_event(TEMPERATURE, 1.0))
        await manager.subscribe([TEMPERATURE])
        initial = await manager.wait_for_value(TEMPERATURE, max_wait=2.0)
        assert initial.value == 1.0

        await manager.pause()
        client.events.append(make_event(TEMPERATURE, 2.0))
        with pytest.raises(TimeoutError) as caught:
            await manager.wait_for_value(TEMPERATURE, newer_than=initial, max_wait=0.4)
        assert TEMPERATURE in str(caught.value)

        # The refused notification is not lost information: it is a counter.
        assert manager.stats.suppressed == 1
        assert manager.stats.notifications == 1
    finally:
        await manager.stop()


async def test_every_handler_under_one_key_runs_in_registration_order() -> None:
    """Registering a second handler must not replace the first one.

    Three sinks on the same notification is the normal shape of a real application:
    a historian, a dashboard and an alarm rule. A dispatcher that kept one handler
    per key would silently drop two of them, and nothing in the application would
    ever notice - the counters would still look healthy.
    """
    manager, client = make_manager()
    order: list[tuple[str, object]] = []

    async def historian(record: DataChangeRecord) -> None:
        order.append(("historian", record.value))

    async def broken_dashboard(record: DataChangeRecord) -> None:
        order.append(("dashboard", record.value))
        raise RuntimeError("dashboard is down")

    async def alarm_rule(record: DataChangeRecord) -> None:
        order.append(("alarm", record.value))

    manager.register_handler(historian)
    manager.register_handler(broken_dashboard)
    manager.register_handler(alarm_rule)
    try:
        await manager.subscribe([TEMPERATURE])
        client.events.append(make_event(TEMPERATURE, 1.0))
        await wait_until(lambda: len(order) == 3, what="all three handlers to run")
        assert order == [("historian", 1.0), ("dashboard", 1.0), ("alarm", 1.0)]
        # The middle one failed, the last one still got the record, and the failure is
        # a counter rather than a mystery.
        assert manager.stats.errors == 1
        assert manager.stats.notifications == 1
        # One notification produced one record and one sequence number, shared by all
        # three handlers instead of copied per handler.
        cached = manager.last_value(TEMPERATURE)
        assert cached is not None
        assert cached.sequence == 1
    finally:
        await manager.stop()


async def test_a_key_routes_and_only_an_unmatched_key_falls_back() -> None:
    """A key is a routing decision, and the fallback belongs to the default key only."""
    manager, client = make_manager()
    seen: list[tuple[str, str]] = []

    async def default_handler(record: DataChangeRecord) -> None:
        seen.append(("default", record.key))

    async def process_handler(record: DataChangeRecord) -> None:
        seen.append(("process", record.key))

    manager.register_handler(default_handler)
    manager.register_handler(process_handler, key="process")
    try:
        await manager.subscribe([TEMPERATURE], key="process")
        await manager.subscribe([PRESSURE])
        client.events.extend([make_event(TEMPERATURE, 1.0), make_event(PRESSURE, 2.0)])
        await wait_until(lambda: len(seen) == 2, what="both records to be routed")

        # The keyed item went to the keyed handler and *not* to the default one: an
        # application that grouped its tags by key must not have the ungrouped
        # handlers reading them as well.
        assert seen == [("process", "process"), ("default", DEFAULT_SUBSCRIPTION_KEY)]
    finally:
        await manager.stop()


async def test_a_notification_for_an_item_that_is_already_gone_is_counted_not_dispatched() -> None:
    """A straggler on the wire must not write a tag the application stopped monitoring."""
    manager, client = make_manager()
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        handles = await manager.subscribe([TEMPERATURE, PRESSURE])
        client.events.append(make_event(TEMPERATURE, 1.0))
        await wait_until(lambda: len(recorder.records) == 1, what="the first notification")

        temperature_handle = next(h for h in handles if h.node_id_text == TEMPERATURE)
        await manager.unsubscribe([temperature_handle])
        assert latest(client).delete_calls == [[temperature_handle.monitored_item_id]]
        assert manager.stats.monitored_items == 1

        # Already in flight when DeleteMonitoredItems ran.
        client.events.append(make_event(TEMPERATURE, 2.0))
        await wait_until(lambda: manager.stats.stray_notifications == 1, what="the stray counter")

        assert [record.value for record in recorder.records] == [1.0]
        cached = manager.last_value(TEMPERATURE)
        assert cached is not None
        # The last known value stays known: what stopped is the flow, not the history.
        assert cached.value == 1.0
    finally:
        await manager.stop()


async def test_a_handler_that_raises_is_counted_and_the_consumer_carries_on() -> None:
    """One dead sink must never take the subscription down with it."""
    manager, client = make_manager()
    seen: list[object] = []

    async def broken_handler(record: DataChangeRecord) -> None:
        seen.append(record.value)
        raise RuntimeError("downstream sink is down")

    manager.register_handler(broken_handler)
    try:
        client.events.extend([make_event(TEMPERATURE, 1.0), make_event(TEMPERATURE, 2.0)])
        await manager.subscribe([TEMPERATURE])
        await wait_until(lambda: manager.stats.errors == 2, what="two handler failures")

        assert seen == [1.0, 2.0]
        # The records were still counted and cached: the sink failed, the subscription
        # did not, and the consumer is still running.
        assert manager.stats.notifications == 2
        cached = manager.last_value(TEMPERATURE)
        assert cached is not None
        assert cached.value == 2.0
        assert manager.subscription_id == 77
        assert manager._consumer is not None
        assert not manager._consumer.done()
    finally:
        await manager.stop()


async def test_statuses_are_counted_as_the_server_sent_them() -> None:
    """Good, Uncertain and Bad are three facts, not one boolean."""
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        client.events.extend(
            [
                make_event(TEMPERATURE, 1.0),
                make_event(
                    TEMPERATURE, 2.0, status=ua.StatusCode(ua.StatusCodes.UncertainLastUsableValue)
                ),
                make_event(TEMPERATURE, 3.0, status=ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown)),
            ]
        )
        await manager.subscribe([TEMPERATURE])
        await wait_until(lambda: manager.stats.notifications == 3, what="three notifications")

        assert (manager.stats.good_values, manager.stats.uncertain_values) == (1, 1)
        assert manager.stats.bad_values == 1
        last = manager.last_value(TEMPERATURE)
        assert last is not None
        # A Bad status with a plausible number in it is the case a historian must not
        # present as a measurement.
        assert not last.is_good
        assert last.status_code.name == "BadNodeIdUnknown"
        assert last.value == 3.0
    finally:
        await manager.stop()


async def test_a_status_change_is_not_mistaken_for_a_value() -> None:
    """The server may report on the subscription itself, and that is not process data.

    A server that has dropped our subscription says so with a subscription status
    notification carrying ``BadSubscriptionIdInvalid`` - the fact Chapter 12's
    reconnect supervisor acts on. Treating it as a value change is how an
    application ends up publishing a StatusCode as if it were a temperature.
    """
    manager, client = make_manager()
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        await manager.subscribe([TEMPERATURE])
        client.events.append(
            StatusChangeEvent(
                notification=ua.StatusChangeNotification(
                    Status=ua.StatusCode(ua.StatusCodes.BadSubscriptionIdInvalid)
                )
            )
        )
        # The consumer took it off the queue - and nothing was dispatched, which is
        # the whole point: a wait for process data must not be satisfied by a
        # notification about the subscription.
        with pytest.raises(TimeoutError):
            await manager.wait_for_notifications(1, max_wait=0.4)
        assert client.events == []
        assert recorder.records == []
        assert manager.stats.notifications == 0
        assert manager.stats.errors == 0
        assert manager.stats.suppressed == 0
        assert manager.last_value(TEMPERATURE) is None
    finally:
        await manager.stop()


async def test_a_server_that_says_the_subscription_is_gone_is_believed() -> None:
    """The report is recorded as a fact the supervisor can act on.

    ``BadSubscriptionIdInvalid`` in a ``SubscriptionStatus`` is the server saying it
    deleted this subscription while leaving the session alone. A client that logs it
    and forgets it waits for values from something that no longer exists, and the
    counters look healthy the whole time.
    """
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        subscription_id = manager.subscription_id
        assert manager.rebuild_reason() is None

        client.events.append(
            StatusChangeEvent(
                notification=ua.StatusChangeNotification(
                    Status=ua.StatusCode(ua.StatusCodes.BadSubscriptionIdInvalid)
                )
            )
        )
        await wait_until(lambda: manager.rebuild_reason() is not None, what="a rebuild reason")

        assert manager.subscription_status is not None
        assert manager.subscription_status.name == "BadSubscriptionIdInvalid"
        # One fact, one sentence: however the client learned that the subscription is
        # gone, the reason it reports is the same.
        assert manager.rebuild_reason() == "the server no longer has this subscription"
        assert manager.stats.status_changes == 1
        assert manager.stats.bad_status_changes == 1
        assert manager.stats.last_status_change == "BadSubscriptionIdInvalid"
        # The manager reports and does not repair: a rebuild costs a new
        # SubscriptionId, new MonitoredItemIds and a gap in the values, and that is a
        # decision for the supervisor (Chapter 12), not a reflex.
        assert manager.stats.recreations == 0
        assert manager.subscription_id == subscription_id
    finally:
        await manager.stop()


async def test_a_publish_the_server_refuses_is_the_same_fact_as_a_status_change() -> None:
    """A client also learns it from an exception, and it must learn it the same way.

    A server that has deleted the subscription does not always send a status
    notification first: sometimes the next publish simply comes back
    ``BadSubscriptionIdInvalid``. Treating that as a transient error and retrying the
    same subscription id forever is how a gateway stops collecting without ever
    reporting that it stopped.
    """
    manager, client = make_manager()
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        client.publish_error = BadSubscriptionIdInvalid()
        await manager.subscribe([TEMPERATURE])
        await wait_until(lambda: manager.rebuild_reason() is not None, what="the refused publish")

        assert manager.subscription_status is not None
        assert manager.subscription_status.name == "BadSubscriptionIdInvalid"
        assert manager.stats.bad_status_changes == 1
        assert recorder.records == []

        # The consumer survives the refusal, so the same manager still delivers once
        # the subscription is rebuilt and the server answers again.
        client.publish_error = None
        client.events.append(make_event(TEMPERATURE, 7.0))
        await wait_until(lambda: len(recorder.records) == 1, what="a value after the refusal")
        assert recorder.records[0].value == 7.0
    finally:
        await manager.stop()


async def test_the_last_thing_the_server_said_is_what_decides() -> None:
    """A later Good report means the subscription is usable again - right now."""
    manager, client = make_manager()
    try:
        await manager.subscribe([TEMPERATURE])
        client.events.append(
            StatusChangeEvent(
                notification=ua.StatusChangeNotification(
                    Status=ua.StatusCode(ua.StatusCodes.BadSubscriptionIdInvalid)
                )
            )
        )
        await wait_until(lambda: manager.rebuild_reason() is not None, what="the damage")

        client.events.append(
            StatusChangeEvent(notification=ua.StatusChangeNotification(Status=ua.StatusCode(0)))
        )
        await wait_until(lambda: manager.rebuild_reason() is None, what="the all-clear")

        assert manager.stats.status_changes == 2
        # The bad one is still counted: what happened happened, even though the
        # subscription is fine at the moment of the assertion.
        assert manager.stats.bad_status_changes == 1
        assert manager.stats.last_status_change == "Good"
    finally:
        await manager.stop()


async def test_a_rebuilt_subscription_no_longer_reports_the_old_subscription() -> None:
    """After a rebuild the manager has no news about a subscription that is gone."""
    manager, client = make_manager()
    try:
        await manager.subscribe([TEMPERATURE])
        client.events.append(
            StatusChangeEvent(
                notification=ua.StatusChangeNotification(
                    Status=ua.StatusCode(ua.StatusCodes.BadSubscriptionIdInvalid)
                )
            )
        )
        await wait_until(lambda: manager.rebuild_reason() is not None, what="the damage")

        restored = await manager.rebuild()
        assert len(restored) == 1
        assert manager.subscription_status is None
        assert manager.rebuild_reason() is None
        assert manager.stats.last_status_change == "none"
        assert manager.stats.recreations == 1
    finally:
        await manager.stop()


async def test_a_manager_that_was_never_started_reports_nothing_to_rebuild() -> None:
    """``None`` means "there is no damage to repair", including for an empty manager.

    A supervisor that rebuilt managers nobody started would create subscriptions on
    a loop, and the application would never see a single value it asked for.
    """
    manager, _client = make_manager()
    assert manager.subscription_status is None
    assert manager.rebuild_reason() is None
    assert manager.stats.last_status_change == "none"

    await manager.subscribe([TEMPERATURE])
    await manager.stop()
    assert manager.rebuild_reason() is None


async def test_a_subscription_whose_queue_was_closed_reports_it_once_not_erroring() -> None:
    """The end of a subscription is a fact, not an exception storm.

    asyncua closes the notification queue when a subscription is gone and hands the
    consumer a ``StopAsyncIteration``. A client that treats that as a retryable error
    either spins on a dead subscription or dies, and in both cases the application
    keeps believing it is collecting data.
    """
    manager, client = make_manager()
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        await manager.subscribe([TEMPERATURE])
        client.events.append(make_event(TEMPERATURE, 1.0))
        await wait_until(lambda: len(recorder.records) == 1, what="the first value")

        latest(client).closed = True
        await wait_until(lambda: manager.rebuild_reason() is not None, what="the closed queue")

        assert "queue is closed" in (manager.rebuild_reason() or "")
        # The subscription ended, it did not fail. An error counter that grows here
        # is what turns a repairable outage into an alert that means nothing.
        assert manager.stats.errors == 0
        assert manager.stats.status_changes == 0
        assert manager.stats.recreations == 0

        # The consumer stopped instead of pulling on a closed queue.
        before = manager.stats.notifications
        await asyncio.sleep(0.1)
        assert manager.stats.notifications == before

        restored = await manager.rebuild()
        assert len(restored) == 1
        assert manager.rebuild_reason() is None
        client.events.append(make_event(TEMPERATURE, 2.0))
        await wait_until(lambda: len(recorder.records) == 2, what="a value after the rebuild")
        assert [record.value for record in recorder.records] == [1.0, 2.0]
    finally:
        await manager.stop()


async def test_a_subscription_the_manager_closed_is_not_reported_as_damage() -> None:
    """Closing it yourself is a decision, and the supervisor must not fight it."""
    manager, client = make_manager()
    try:
        await manager.subscribe([TEMPERATURE])
        latest(client).closed = True
        await manager.stop()

        assert manager.rebuild_reason() is None
        assert manager.stats.errors == 0
        assert manager.subscription_status is None
    finally:
        await manager.stop()


async def test_a_subscription_the_server_forgot_is_found_by_asking_not_by_guessing() -> None:
    """A server-side delete leaves the client nothing to notice except quiet.

    The client's own objects are untouched, no notification ever arrives again, no error
    is raised anywhere, and every counter simply stops moving. ``ModifySubscription`` with
    the numbers already in use is the only service that answers the question, and it is a
    question the client has to decide to ask.
    """
    manager, client = make_manager()
    recorder = Recorder()
    manager.register_handler(recorder)
    try:
        await manager.subscribe([TEMPERATURE])
        client.events.append(make_event(TEMPERATURE, 1.0))
        await wait_until(lambda: len(recorder.records) == 1, what="the first value")
        old_id = manager.subscription_id

        # The server forgets the subscription: publishes stop, and the client has no idea.
        client.modify_error = BadSubscriptionIdInvalid()
        assert manager.rebuild_reason() is None
        assert reported_status(manager) is None

        reason = await manager.probe_subscription()
        assert reason == "the server no longer has this subscription"
        status = reported_status(manager)
        assert status is not None
        assert status.name == "BadSubscriptionIdInvalid"
        assert manager.stats.status_changes == 1
        assert manager.stats.bad_status_changes == 1
        # Asking is not failing: a probe the server refused is a report, not an error.
        assert manager.stats.errors == 0

        restored = await manager.rebuild()
        assert len(restored) == 1
        assert manager.subscription_id not in (None, old_id)
        assert manager.rebuild_reason() is None
        # The verdict belonged to the subscription that got deleted, not to this one.
        assert manager.subscription_status is None
        # The subscription that got deleted is retired, and the one under test now is not.
        assert client.subscriptions[0].is_deleted is True
        assert latest(client).is_deleted is False

        client.modify_error = None
        client.events.append(make_event(TEMPERATURE, 2.0))
        await wait_until(lambda: len(recorder.records) == 2, what="a value after the rebuild")
        assert await manager.probe_subscription() is None
        assert manager.seconds_since_probe() is not None
    finally:
        await manager.stop()


async def test_a_probe_that_cannot_reach_the_server_says_nothing_about_the_subscription() -> None:
    """A transport that is down answers every question the same way, so it answers none.

    Treating "the connection failed" as "the subscription is gone" makes a client rebuild
    subscriptions it does not have on a link that is not there, which is how reconnect
    loops turn into subscription storms.
    """
    manager, client = make_manager()
    try:
        await manager.subscribe([TEMPERATURE])
        client.modify_error = ConnectionRefusedError("connection closed")

        assert await manager.probe_subscription() is None
        assert manager.subscription_status is None
        assert manager.rebuild_reason() is None
        assert manager.stats.errors == 0
        assert manager.stats.recreations == 0
        assert manager.subscription_id is not None
    finally:
        await manager.stop()


async def test_silence_is_measured_and_never_taken_for_damage() -> None:
    """A plant at steady state reports nothing, and that is not an outage.

    What the manager is allowed to say is how long it has been quiet, which is a number a
    supervisor can act on, and never a conclusion about why.
    """
    manager, client = make_manager()
    try:
        await manager.subscribe([TEMPERATURE])
        assert manager.seconds_since_probe() is None

        first = manager.seconds_since_report()
        assert first is not None
        await asyncio.sleep(0.1)
        later = manager.seconds_since_report()
        assert later is not None
        assert later > first
        assert manager.rebuild_reason() is None
        assert manager.stats.recreations == 0
        assert manager.stats.notifications == 0
        assert latest(client).is_deleted is False
    finally:
        await manager.stop()


async def test_a_rebuild_that_is_waiting_for_a_link_is_not_a_new_outage() -> None:
    """A rebuild can outlive several supervision polls, and polls must respect that.

    Without the flag a watchdog that polls quickly stacks rebuilds on the same outage and
    ends up holding subscriptions the application never asked for.
    """
    manager, client = make_manager()
    try:
        await manager.subscribe([TEMPERATURE])
        latest(client).closed = True
        await wait_until(lambda: manager.rebuild_reason() is not None, what="the closed queue")

        # The create that a rebuild needs blocks, which is what a rebuild waiting for a
        # transport to come back looks like from the outside.
        client.create_gate = asyncio.Event()
        rebuilding = asyncio.create_task(manager.rebuild())
        await wait_until(lambda: manager.rebuild_in_progress, what="the rebuild to start")
        # While a repair is running there is no second repair to decide on.
        assert manager.rebuild_reason() is None

        client.create_gate.set()
        restored = await rebuilding
        assert len(restored) == 1
        assert manager.rebuild_in_progress is False
        assert manager.stats.recreations == 1
    finally:
        await manager.stop()


async def test_a_handler_can_read_the_record_it_is_being_handed() -> None:
    """The cache is filled before the callback runs, so a sink can check its own order."""
    manager, client = make_manager()
    seen: list[tuple[object, object]] = []

    async def handler(record: DataChangeRecord) -> None:
        cached = manager.last_value(record.node_id)
        assert cached is not None
        seen.append((record.value, cached.value))

    manager.register_handler(handler)
    try:
        client.events.append(make_event(TEMPERATURE, 1.0))
        await manager.subscribe([TEMPERATURE])
        await wait_until(lambda: len(seen) == 1, what="the handler to run")
        assert seen == [(1.0, 1.0)]
    finally:
        await manager.stop()


async def test_wait_for_value_waits_for_a_newer_record_instead_of_the_cached_one() -> None:
    """The watermark rule that keeps Chapter 10 tests from racing their own writes."""
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        client.events.append(make_event(TEMPERATURE, 1.0))
        await manager.subscribe([TEMPERATURE])
        first = await manager.wait_for_value(TEMPERATURE, max_wait=2.0)
        assert first.value == 1.0
        assert first.sequence == 1

        # Nothing newer exists: the wait must time out instead of returning the
        # stale record, which is the bug a "get the latest value" API hides.
        with pytest.raises(TimeoutError):
            await manager.wait_for_value(TEMPERATURE, newer_than=first, max_wait=0.3)

        client.events.append(make_event(TEMPERATURE, 2.0))
        newer = await manager.wait_for_value(TEMPERATURE, newer_than=first, max_wait=2.0)
        assert newer.value == 2.0
        assert newer.sequence > first.sequence

        # A suppressed (paused) notification never consumes a sequence number, so a
        # watermark stays valid across a pause/resume cycle.
        await manager.pause()
        client.events.append(make_event(TEMPERATURE, 3.0))
        await wait_until(lambda: manager.stats.suppressed == 1, what="the suppressed report")
        await manager.resume()
        client.events.append(make_event(TEMPERATURE, 4.0))
        after = await manager.wait_for_value(TEMPERATURE, newer_than=newer, max_wait=2.0)
        assert after.value == 4.0
    finally:
        await manager.stop()


async def test_wait_for_exact_waits_for_my_value_not_for_any_newer_record() -> None:
    """The write-verification rule Chapter 11 states, as an API.

    ``wait_for_value`` answers "has anything newer arrived?". On a server that writes the
    tag by itself - which is every server that owns the tag - that question has a different
    answer from "did my write come back", and a client that uses the first when it means the
    second reports somebody else's value as its own.
    """
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        client.events.append(make_event(TEMPERATURE, 1.0))
        await manager.subscribe([TEMPERATURE])

        # A value that is already the newest one answers at once: no watermark, no waiting.
        mine = await manager.wait_for_exact(TEMPERATURE, 1.0, max_wait=2.0)
        assert mine.value == 1.0

        # The server's own writer then wins. wait_for_value would hand this record to an
        # application that was waiting for its own 1.0, which is the exact bug.
        client.events.append(make_event(TEMPERATURE, 2.0))
        server_wrote = await manager.wait_for_value(TEMPERATURE, newer_than=mine, max_wait=2.0)
        assert server_wrote.value == 2.0

        with pytest.raises(TimeoutError) as refused:
            await manager.wait_for_exact(TEMPERATURE, 1.0, max_wait=0.4)
        # "mine never arrived" and "something else overwrote it" are different incidents,
        # and the message has to carry the evidence that tells them apart.
        assert "never saw 1.0" in str(refused.value)
        assert "2.0" in str(refused.value)

        client.events.append(make_event(TEMPERATURE, 9.9))
        arrived = await manager.wait_for_exact(TEMPERATURE, 9.9, max_wait=2.0)
        assert arrived.value == 9.9

        # With a watermark, a cached match is not an answer: the value has to arrive after
        # the record the caller read before it wrote. This is what keeps a verification from
        # passing while the write itself is broken.
        before = manager.last_value(TEMPERATURE)
        assert before is not None
        with pytest.raises(TimeoutError):
            await manager.wait_for_exact(TEMPERATURE, 9.9, newer_than=before, max_wait=0.3)
    finally:
        await manager.stop()


async def test_the_publishing_interval_and_overflow_policy_reach_the_server() -> None:
    """The queue an application never sees is the one that decides what it misses."""
    manager, client = make_manager(queue_maxsize=10, overflow=OverflowPolicy.DROP_NEWEST)
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        assert client.periods == [100.0]
        assert client.queue_sizes == [10]
        assert client.overflow is OverflowPolicy.DROP_NEWEST
    finally:
        await manager.stop()


async def test_a_rebuild_keeps_the_publishing_state_the_application_had_chosen() -> None:
    """A reconnect must not turn the data flow back on behind the application's back."""
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        assert client.publishing_flags == [True]

        await manager.pause()
        await manager.rebuild()
        # Created paused, not created reporting and then quieted down: the difference
        # is whether any value is delivered at all in the window between the two.
        assert client.publishing_flags == [True, False]
        assert manager.stats.paused
        assert manager.stats.recreations == 1
        assert manager.stats.monitored_items == 1
        assert client.subscriptions[0].is_deleted is True
        assert latest(client).is_deleted is False
        assert manager.subscription_id == latest(client).subscription_id == 78
    finally:
        await manager.stop()


async def test_rebuilding_replaces_the_consumer_instead_of_piling_another_one_up() -> None:
    """A rebuild that leaks its old consumer leaks one live subscription each time."""
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        old_consumer = manager._consumer
        assert old_consumer is not None

        await manager.rebuild()
        assert manager._consumer is not old_consumer
        assert old_consumer.done()

        await manager.rebuild()
        assert old_consumer.done()
        assert manager._consumer is not None
        assert not manager._consumer.done()
        assert manager.stats.monitored_items == 1
        assert len(client.subscriptions) == 3
        # Items are restored in groups, one CreateMonitoredItems per chunk, and only
        # on the subscription that is actually alive.
        assert [subscription.create_calls for subscription in client.subscriptions] == [
            [1],
            [1],
            [1],
        ]
        assert client.subscriptions[0].is_deleted
        assert client.subscriptions[1].is_deleted
        assert not client.subscriptions[2].is_deleted
    finally:
        await manager.stop()


async def test_a_rebuilt_subscription_keeps_the_key_and_options_it_was_created_with() -> None:
    """Otherwise a reconnect silently changes how the tags are monitored."""
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        handles = await manager.subscribe(
            [TEMPERATURE, PRESSURE],
            key="process",
            options=SubscriptionOptions(monitoring_mode=ua.MonitoringMode.Sampling, queue_size=3),
        )
        assert {handle.key for handle in handles} == {"process"}
        assert handles[0].options.queue_size == 3

        restored = await manager.rebuild()
        assert [handle.node_id_text for handle in restored] == [TEMPERATURE, PRESSURE]
        assert {handle.key for handle in restored} == {"process"}
        assert restored[0].options.monitoring_mode is ua.MonitoringMode.Sampling
        assert restored[0].options.queue_size == 3
        # Restored as one group, not one CreateMonitoredItems per remembered item.
        assert latest(client).create_calls == [2]

        await manager.set_monitoring_mode(ua.MonitoringMode.Reporting)
        assert latest(client).monitoring_modes == [ua.MonitoringMode.Reporting]
        # The key survives as the name the application chose, not as the internal
        # default: a rebuilt item that lost its key would be dispatched to the wrong
        # handler without ever raising anything.
        assert manager.items[0].key == "process"
        assert manager.items[0].key != DEFAULT_SUBSCRIPTION_KEY
    finally:
        await manager.stop()


async def test_a_rebuild_without_items_does_not_open_a_subscription_nobody_asked_for() -> None:
    """A supervisor that rebuilds a stopped manager must not resurrect it."""
    manager, client = make_manager()
    await manager.subscribe([TEMPERATURE])
    await manager.stop()
    assert client.subscriptions[0].is_deleted

    assert await manager.rebuild() == []
    assert len(client.subscriptions) == 1
    assert manager.subscription_id is None
    assert manager.stats.recreations == 0


async def test_an_answer_is_not_claimed_before_the_server_has_been_asked() -> None:
    """``revision`` is empty until a ModifySubscription has actually come back.

    A manager that reported the interval it *wanted* as the interval it *got* would
    make every later negotiation test pass for the wrong reason.
    """
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        assert manager.publishing_interval == 100.0
        assert current_revision(manager) is None
        assert latest(client).modifies == []

        answer = await manager.confirm_subscription()
        assert answer.service == "ModifySubscription"
        assert len(latest(client).modifies) == 1
        assert current_revision(manager) is answer
    finally:
        await manager.stop()


async def test_the_revised_numbers_are_the_servers_not_the_requested_ones() -> None:
    """A server clamps a subscription, and a client that cannot see it is guessing.

    The client asked for a 10 000 publishing interval lifetime; this server's cap is
    1 000, so the subscription that runs is the short one. ``requested_*`` and
    ``revised_*`` are kept apart precisely so that the difference can be logged.
    """
    manager, _client = make_manager(client_kwargs={"lifetime_cap": 1_000})
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        answer = await manager.confirm_subscription()
        assert answer.requested_lifetime_count == 10_000
        assert answer.revised_lifetime_count == 1_000
        assert answer.publishing_interval_accepted
        assert answer.rebuilt is False
        assert answer.revised is True
        assert "lifetime count 10000 -> 1000" in answer.revisions
        # The summary is the line an operator sees in a log: it carries both numbers,
        # the one that was asked for and the one the server is actually using.
        summary = answer.summary()
        assert summary.startswith("ModifySubscription:")
        assert "lifetime=10000" in summary  # what this client asked for
        assert "lifetime=1000 keepalive" in summary  # what this server is using
        assert "[rebuilt]" not in summary
        # The clamped number is a timeout, not a formality: a lifetime count is
        # measured in publishing intervals, so 1000 intervals of 100 ms is 100 seconds
        # a subscription may survive without a Publish request before the server
        # deletes it. That is the number Chapter 12's supervisor reasons about.
    finally:
        await manager.stop()


async def test_an_interval_the_server_applied_keeps_the_subscription_it_has() -> None:
    """ModifySubscription is the right service, so nothing is rebuilt."""
    manager, client = make_manager()
    manager.register_handler(Recorder())
    try:
        handles = await manager.subscribe([TEMPERATURE, PRESSURE])
        answer = await manager.set_publishing_interval(300.0)
        assert answer is not None
        assert answer.publishing_interval_accepted
        assert answer.rebuilt is False
        assert answer.revised_publishing_interval == 300.0
        assert manager.publishing_interval == 300.0
        assert "publishing=300" in manager.stats.summary()
        # Nothing was thrown away: same subscription, same monitored items, same ids,
        # and the sequence numbers of what has already been reported stay valid.
        assert len(client.subscriptions) == 1
        assert manager.subscription_id == 77
        assert manager.stats.recreations == 0
        assert latest(client).modifies[0].RequestedPublishingInterval == 300.0
        assert [item.monitored_item_id for item in manager.items] == [
            handle.monitored_item_id for handle in handles
        ]
        assert current_revision(manager) is answer
    finally:
        await manager.stop()


async def test_an_interval_a_server_ignored_is_not_reported_as_an_accepted_change() -> None:
    """asyncua's own server answers ModifySubscription with its old values.

    "We asked" is not "we got". The manager sees an answer that did not move, says so
    in the log, and rebuilds so that the subscription it reports on really is the one
    that publishes at the interval the application is asking for.
    """
    manager, client = make_manager(client_kwargs={"honours_modify": False})
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE, PRESSURE])
        answer = await manager.set_publishing_interval(300.0)
        assert answer is not None
        assert answer.rebuilt is True
        assert answer.revised_publishing_interval == 300.0
        assert manager.publishing_interval == 300.0
        # The first create used 100ms, the rebuild used 300ms: an application reading
        # only "publishing_interval" cannot tell those two situations apart, which is
        # why the answer carries ``rebuilt``.
        assert client.periods == [100.0, 300.0]
        assert len(client.subscriptions) == 2
        assert client.subscriptions[0].is_deleted
        assert client.subscriptions[0].modifies[0].RequestedPublishingInterval == 300.0
        assert latest(client).create_calls == [2]
        assert manager.stats.monitored_items == 2
        assert manager.stats.recreations == 1
        assert manager.subscription_id == 78
    finally:
        await manager.stop()


async def test_a_rebuild_forgets_an_answer_about_a_subscription_that_no_longer_exists() -> None:
    """A stale answer about a deleted subscription is worse than no answer.

    Without this, a gateway could log "lifetime 1000" for a subscription the server
    has already replaced with a different one.
    """
    manager, _client = make_manager(client_kwargs={"lifetime_cap": 1_000})
    manager.register_handler(Recorder())
    try:
        await manager.subscribe([TEMPERATURE])
        first = await manager.confirm_subscription()
        assert first.revised_lifetime_count == 1_000
        assert manager.revision is first

        await manager.rebuild()
        assert current_revision(manager) is None
        again = await manager.confirm_subscription()
        assert again.revised_lifetime_count == 1_000
    finally:
        await manager.stop()
