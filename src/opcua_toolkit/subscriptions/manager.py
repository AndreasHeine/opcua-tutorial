"""Reusable subscription manager.

Built in Chapter 10 (the subscription and its monitored items) and Chapter 11
(dispatching what it reports), rebuilt by Chapter 12 (supervision). The manager
owns one subscription, remembers every monitored item, splits creation requests
to ``MaxMonitoredItemsPerCall``, routes notifications to per-key callbacks, can
pause reporting without losing monitored items, and can rebuild the whole
subscription after a session loss.

Design decisions worth understanding:

* **Iterator mode, not handler mode.** asyncua calls a registered handler in a
  new task per notification. That is fine for a print statement and dangerous
  for a historian: tasks pile up, ordering is lost and a slow sink starves
  publishing. We create the subscription without a handler and consume
  ``Subscription.next_event()`` in one worker task. Ordering is preserved,
  backpressure is explicit and the bounded queue applies a documented overflow
  policy.
* **One worker, awaited callbacks.** A sink that writes to a database must hand
  the value to its own queue instead of blocking this loop (Chapter 15).
* **Rebuild instead of hope.** Recorded items make recovery deterministic
  instead of relying on the server keeping our subscriptions alive.
* **Believe the answer, not the request.** A subscription is negotiated: the
  server replies with *revised* timers. :meth:`SubscriptionManager.confirm_subscription`
  asks for them, and :meth:`SubscriptionManager.set_publishing_interval` refuses
  to accept a request a server ignored.
* **A subscription has its own life.** It can die while the session is perfectly
  healthy: a lifetime the client was never told about, a server restart, a
  maintenance window. The manager records what the server said - a
  ``SubscriptionStatus``, a publish it refused with ``BadSubscriptionIdInvalid``, or
  a notification queue that simply stopped - and reports it through
  :meth:`SubscriptionManager.rebuild_reason`. It does not repair behind your back:
  a rebuild is a decision with a cost, and Chapter 12's supervisor makes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from asyncua import Node, ua
from asyncua.client.client import Client
from asyncua.common.subscription import (
    DataChangeEvent,
    OpcEvent,
    OverflowPolicy,
    StatusChangeEvent,
    Subscription,
)
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from opcua_toolkit.errors import NotConnectedError, SubscriptionClosedError, format_node_id
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.settings import SubscriptionDefaults
from opcua_toolkit.subscriptions.model import (
    DEFAULT_SUBSCRIPTION_KEY,
    DataChangeRecord,
    MonitoredItemHandle,
    NotificationCallback,
    SubscriptionOptions,
    SubscriptionRevision,
    SubscriptionStats,
)

_logger = logging.getLogger(__name__)

_DEFAULT_KEY = DEFAULT_SUBSCRIPTION_KEY

#: The status a server answers a publish with when it has deleted the subscription.
_SUBSCRIPTION_GONE = "BadSubscriptionIdInvalid"


class SubscriptionManager:
    """Own one subscription and every monitored item inside it."""

    def __init__(
        self,
        client: Client,
        *,
        limits: OperationLimits | None = None,
        defaults: SubscriptionDefaults | None = None,
        publishing_interval: float | None = None,
        queue_maxsize: int = 1_000,
        overflow: OverflowPolicy = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()
        self._defaults = defaults if defaults is not None else SubscriptionDefaults()
        self._publishing_interval = (
            publishing_interval
            if publishing_interval is not None
            else self._defaults.publishing_interval
        )
        self._queue_maxsize = queue_maxsize
        self._overflow = overflow

        self._subscription: Subscription | None = None
        self._items: dict[str, MonitoredItemHandle] = {}
        self._handlers: dict[str, list[NotificationCallback]] = {}
        self._latest: dict[str, DataChangeRecord] = {}
        self._consumer: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        # One Condition, not one Event per thing to wait for: a Condition cannot
        # lose a notification between "check the counter" and "start waiting",
        # which is exactly the race that turns subscription tests into flaky ones.
        self._condition = asyncio.Condition()
        self._stats = SubscriptionStats(publishing_interval=self._publishing_interval)
        self._revision: SubscriptionRevision | None = None
        # What the server last said about the subscription itself, through a
        # StatusChangeNotification or a publish it refused to answer.
        self._server_status: ua.StatusCode | None = None
        # True when asyncua closed the notification queue under us.
        self._queue_closed = False
        # True while a rebuild is running, including one waiting for a reconnect.
        self._rebuilding = False
        # Timing facts. A monitored item reports a *change*, so silence by itself never
        # proves anything: these only say how long it has been since the manager last
        # heard something, and since it last asked the server about this subscription.
        self._started_at: datetime | None = None
        self._last_report_at: datetime | None = None
        self._last_probe_at: datetime | None = None
        self._received = 0
        self._handled = 0

    @property
    def stats(self) -> SubscriptionStats:
        """Live counters for logs, metrics and tests."""
        return self._stats

    @property
    def revision(self) -> SubscriptionRevision | None:
        """The server's own answer about this subscription, or ``None`` if unknown.

        It is ``None`` after :meth:`start` and after every :meth:`rebuild` on
        purpose: the numbers arrive from a service the manager does not call on
        your behalf, and a stale answer about a deleted subscription is worse than
        no answer. Call :meth:`confirm_subscription` to get it.
        """
        return self._revision

    @property
    def items(self) -> list[MonitoredItemHandle]:
        """Every monitored item this manager knows about."""
        return list(self._items.values())

    @property
    def latest_values(self) -> dict[str, DataChangeRecord]:
        """Last record seen per node, keyed by NodeId text.

        This is the snapshot an edge gateway serves while it waits for the next
        change: the most recent value per tag, with its StatusCode and timestamps
        attached so a stale or uncertain reading cannot be presented as fresh data.

        Records survive a pause and survive a rebuild, which is exactly the trap:
        after a session loss the cached value may be minutes old. Compare
        ``source_timestamp`` with the clock before answering from the cache, and
        use :attr:`SubscriptionStats.recreations` to know that a gap happened.
        """
        return dict(self._latest)

    def last_value(self, node: NodeIdLike) -> DataChangeRecord | None:
        """The most recent record for one node, or ``None`` if none arrived yet."""
        return self._latest.get(format_node_id(to_nodeid(node)))

    @property
    def subscription_id(self) -> int | None:
        """Server assigned subscription id, ``None`` before start."""
        return self._subscription.subscription_id if self._subscription else None

    @property
    def publishing_interval(self) -> float:
        """Milliseconds between publish requests."""
        return self._publishing_interval

    @property
    def subscription_status(self) -> ua.StatusCode | None:
        """What the server last said about this subscription, or ``None`` if never.

        A subscription is not a connection: a server can keep the session open and
        still have thrown the subscription away, and it says so with a
        ``SubscriptionStatus`` in the publish response. Most clients drop that
        notification on the floor, which is how an application ends up waiting
        forever for values from a subscription that no longer exists.
        """
        return self._server_status

    @property
    def rebuild_in_progress(self) -> bool:
        """True while a rebuild is running, including one blocked on a reconnect.

        :meth:`start` waits for the client to have a session again, so a rebuild can
        outlive several supervision polls. Without this flag a watchdog that polls
        quickly rebuilds the same outage over and over and creates subscriptions
        nobody asked for.
        """
        return self._rebuilding

    def rebuild_reason(self) -> str | None:
        """Why this subscription is no longer usable, or ``None`` when it is usable.

        The manager reports the damage and does not repair it on its own. Deciding
        to rebuild is the supervisor's job (Chapter 12) because a rebuild is not
        free: it costs a new SubscriptionId, new MonitoredItemIds, and a gap in the
        values that every downstream consumer has to be told about.

        Returns:
            ``None`` when nothing was ever started (there is nothing to rebuild),
            while a rebuild is in progress (the repair is already happening), or when
            the server has given no reason to doubt the subscription; otherwise a
            short reason a log line or a test can assert on.
        """
        if self._rebuilding:
            return None
        subscription = self._subscription
        if subscription is None:
            return None
        if subscription.is_deleted:
            return "the server no longer has this subscription"
        status = self._server_status
        if status is not None and not status.is_good():
            if status.name == _SUBSCRIPTION_GONE:
                # The same fact a client-side delete produced, learned a different way.
                # One fact deserves one sentence, whichever service happened to say it.
                return "the server no longer has this subscription"
            return f"the server reported subscription status {status.name}"
        if self._queue_closed:
            # asyncua closed the notification queue: the subscription will not
            # report again, and no status code ever said so.
            return "the subscription stopped reporting: its notification queue is closed"
        return None

    def register_handler(self, callback: NotificationCallback, *, key: str | None = None) -> None:
        """Register a callback for one subscription key, or the default one.

        Several handlers may share a key: they run in registration order, one at a
        time, and a failure in one does not stop the ones after it. A key with no
        handler of its own falls back to the handlers registered without a key.

        Args:
            callback: ``async def handler(record: DataChangeRecord) -> None``.
            key: The key passed to :meth:`subscribe`. ``None`` registers the
                fallback handlers used for items without a matching key.

        Note:
            Each call adds a handler. Registering the same callback twice makes it
            run twice, which is a bug in the caller, not a feature: there is no
            unregister, and there is no reason to hide the duplicate behind a
            replacement that would silently drop another handler sharing the key.
        """
        self._handlers.setdefault(key or _DEFAULT_KEY, []).append(callback)

    async def start(self) -> None:
        """Create the subscription and start the notification consumer."""
        async with self._lock:
            if self._subscription is not None:
                return
            self._subscription = await self._client.create_subscription(
                period=self._publishing_interval,
                handler=None,
                # A rebuilt subscription that was paused before the session loss
                # must come back paused: otherwise a reconnect silently turns the
                # data flow back on for an application that switched it off.
                publishing=not self._stats.paused,
                queue_maxsize=self._queue_maxsize,
                overflow=self._overflow,
            )
            self._stats.subscription_id = self._subscription.subscription_id
            # A new subscription is a new negotiation. Whatever the server answered
            # about the previous one no longer describes anything, so the manager
            # forgets it instead of reporting an answer that has expired. The same
            # goes for whatever the server last said about the subscription itself.
            self._revision = None
            self._clear_subscription_status()
            self._started_at = datetime.now(UTC)
            self._stop.clear()
            self._consumer = asyncio.create_task(self._consume())
            _logger.info(
                "subscription %s created with %sms publishing interval",
                self._stats.subscription_id,
                self._publishing_interval,
            )

    async def stop(self) -> None:
        """Delete the subscription and stop the consumer."""
        self._stop.set()
        async with self._condition:
            # Wake anyone waiting for a notification that is never going to arrive.
            self._condition.notify_all()
        consumer = self._consumer
        if consumer is not None:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
            self._consumer = None
        subscription = self._subscription
        self._subscription = None
        if subscription is None:
            return
        try:
            await subscription.delete()
        except (UaStatusCodeError, UaError) as exc:
            _logger.warning("could not delete subscription: %s", exc)
        self._items.clear()
        self._stats.subscription_id = None

    async def subscribe(
        self,
        nodes: Iterable[NodeIdLike | Node],
        *,
        key: str | None = None,
        options: SubscriptionOptions | None = None,
    ) -> list[MonitoredItemHandle]:
        """Monitor one or many nodes, chunked to the server's limits.

        Args:
            nodes: NodeIds or Node objects to monitor.
            key: Groups items so a single callback can dispatch them (Chapter 11).
            options: Sampling, queue size, monitoring mode and attribute.

        Returns:
            Handles of the monitored items that were accepted, carrying the
            MonitoredItemId the server assigned. Items the server rejected are
            logged and counted in ``stats.failed_items`` instead of raising,
            because a partially accepted batch is a normal outcome of monitoring a
            tag list that came from a browse.

        Note:
            A server is allowed to revise a sampling interval or a queue size
            (``MinimumSupportedSampleRate`` is the floor it may clamp to). asyncua's
            high level path answers with the MonitoredItemId only, so
            ``revised_sampling_interval`` stays ``None`` here: the revised numbers
            are in the raw ``CreateMonitoredItems`` response, and the toolkit
            refuses to invent them. What a client *can* ask for is the
            subscription's own timers - use :meth:`confirm_subscription` - and what
            a client must do is verify with a tool that shows the server's answer,
            which is Chapter 10's verification step. Against asyncua's own server,
            note two things worth knowing: every monitored item is reported with the
            subscription's publishing interval as its sampling interval, and the
            published ``MinimumSupportedSampleRate`` is not enforced at all. A
            server that publishes a limit and one that enforces it are different
            machines.
        """
        await self.start()
        subscription = self._require_subscription()
        resolved = (options or SubscriptionOptions()).resolved(self._defaults)
        created: list[MonitoredItemHandle] = []

        node_ids = [node.nodeid if isinstance(node, Node) else to_nodeid(node) for node in nodes]
        for chunk in chunked(node_ids, self._limits.max_monitored_items_per_call):
            targets = [self._client.get_node(node_id) for node_id in chunk]
            try:
                results = await subscription.subscribe_data_change(
                    nodes=targets,
                    attr=resolved.attribute,
                    queuesize=resolved.queue_size or 0,
                    monitoring=resolved.monitoring_mode,
                    sampling_interval=resolved.sampling_interval
                    if resolved.sampling_interval is not None
                    else self._defaults.sampling_interval,
                )
            except (UaStatusCodeError, UaError):
                self._stats.failed_items += len(chunk)
                _logger.exception("create monitored items failed for %d node(s)", len(chunk))
                continue

            per_node = results if isinstance(results, list) else [results]
            for node_id, result in zip(chunk, per_node, strict=True):
                text = format_node_id(node_id)
                if isinstance(result, ua.StatusCode):
                    self._stats.failed_items += 1
                    _logger.warning("monitoring %s rejected: %s", text, result.name)
                    continue
                handle = MonitoredItemHandle(
                    node_id=node_id,
                    monitored_item_id=int(result),
                    key=key or _DEFAULT_KEY,
                    options=resolved,
                    requested_sampling_interval=float(resolved.sampling_interval or 0.0),
                )
                self._items[text] = handle
                created.append(handle)
        self._stats.monitored_items = len(self._items)
        return created

    async def unsubscribe(self, targets: Iterable[MonitoredItemHandle | NodeIdLike]) -> None:
        """Remove monitored items by handle or by node id.

        The number sent to the server is the MonitoredItemId the server assigned,
        not the ClientHandle that appears on notifications: DeleteMonitoredItems
        addresses items by the id the server chose. Chunked, because a client that
        drops 24 tags in one request is a client a server truncates.
        """
        subscription = self._require_subscription()
        handles: list[int] = []
        for target in targets:
            if isinstance(target, MonitoredItemHandle):
                handles.append(target.monitored_item_id)
                self._items.pop(target.node_id_text, None)
                continue
            text = format_node_id(to_nodeid(target))
            item = self._items.pop(text, None)
            if item is not None:
                handles.append(item.monitored_item_id)
        if not handles:
            return
        for chunk in chunked(handles, max(1, self._limits.max_monitored_items_per_call)):
            await subscription.unsubscribe(chunk)
        self._stats.monitored_items = len(self._items)

    async def pause(self) -> None:
        """Stop reporting without losing a single monitored item.

        Two things happen, and each one covers a different failure:

        * ``SetPublishingMode(False)`` tells the server to stop sending
          notifications for this subscription. The monitored items, their
          sampling and their queues stay exactly as they were, so resuming costs
          one small request instead of a re-subscribe storm on a server that is
          already struggling.
        * The manager also refuses to dispatch locally. A client cannot assume a
          server honours ``SetPublishingMode`` - some keep reporting - and a
          paused application must be paused because *it* says so, not because the
          server agreed. Suppressed notifications are counted in
          ``stats.suppressed`` so the gap is visible instead of silent.
        """
        subscription = self._require_subscription()
        await subscription.set_publishing_mode(False)
        self._stats.paused = True
        _logger.info("subscription %s paused (publishing disabled)", self.subscription_id)

    async def resume(self) -> None:
        """Re-enable reporting on the existing monitored items."""
        subscription = self._require_subscription()
        await subscription.set_publishing_mode(True)
        self._stats.paused = False
        _logger.info("subscription %s resumed (publishing enabled)", self.subscription_id)

    async def set_monitoring_mode(self, mode: ua.MonitoringMode) -> None:
        """Set the monitoring mode of every monitored item in this subscription.

        ``Disabled`` means the server stops monitoring entirely. ``Sampling``
        keeps monitoring and keeps queues, but reports nothing: it is the mode you
        want while commissioning a plant, because you can watch the sampled state
        later without pushing data at the client. ``Reporting`` is normal
        operation.

        Note that pausing *reporting* is what :meth:`pause` does; use this method
        when you want to change how the server monitors the items.
        """
        subscription = self._require_subscription()
        await subscription.set_monitoring_mode(mode)
        _logger.info("subscription %s monitoring mode set to %s", self.subscription_id, mode.name)

    async def rebuild(self) -> list[MonitoredItemHandle]:
        """Recreate the subscription and all monitored items on the current session.

        Used by the supervision loop (Chapter 12) when the server dropped our
        subscription. A rebuild is not a fast operation: while the transport is being
        re-established, :meth:`start` waits for the client to come back, and the
        supervision loop must not stack a second rebuild on top of the first. That is
        what :attr:`rebuild_in_progress` is for.
        """
        if self._rebuilding:
            _logger.info("subscription rebuild already running, not starting another")
            return []
        wanted = list(self._items.values())
        if not wanted:
            # An explicitly stopped manager has nothing to restore. Rebuilding it
            # into an empty subscription would leave a subscription the caller
            # never asked for, and a supervisor running in a loop would do it
            # over and over.
            _logger.info("subscription rebuild skipped: no monitored items to restore")
            return []
        self._rebuilding = True
        try:
            async with self._lock:
                self._items.clear()
                old = self._subscription
                self._subscription = None
                # Stop the old consumer before creating the new subscription. A consumer
                # keeps the Subscription it was started with in its own local variable,
                # so replacing self._subscription is not enough: the old task would go
                # on publishing from a dead subscription forever, and every rebuild
                # would add another one.
                self._stop.set()
                consumer = self._consumer
                self._consumer = None
                if consumer is not None:
                    consumer.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await consumer
                if old is not None:
                    with contextlib.suppress(UaStatusCodeError, UaError):
                        await old.delete()
                async with self._condition:
                    self._condition.notify_all()
            await self.start()
            self._stats.recreations += 1
            # Restore in groups instead of one call per item: 24 tags that share a key
            # and options must come back as three CreateMonitoredItems calls, not 24.
            groups: dict[tuple[str | None, SubscriptionOptions], list[ua.NodeId]] = {}
            for item in wanted:
                key = None if item.key == _DEFAULT_KEY else item.key
                groups.setdefault((key, item.options), []).append(item.node_id)
            restored: list[MonitoredItemHandle] = []
            for (key, options), node_ids in groups.items():
                restored.extend(await self.subscribe(node_ids, key=key, options=options))
            _logger.info(
                "subscription rebuilt with %d of %d monitored items",
                len(restored),
                len(wanted),
            )
            return restored
        finally:
            self._rebuilding = False

    async def wait_idle(self, max_wait: float = 5.0) -> bool:
        """Wait until every notification received so far has been dispatched.

        Tests and graceful shutdown use this instead of arbitrary sleeps: it
        returns as soon as the counters match, and reports ``False`` when the
        wait expired while notifications were still pending.

        Note what this does *not* prove: "nothing pending" is also true when no
        notification ever arrived. For "the subscription is alive and reporting",
        use :meth:`wait_for_notifications` or :meth:`wait_for_value`.
        """
        target = self._received
        try:
            await self._await(lambda: self._handled >= target, max_wait, "drain notifications")
        except TimeoutError:
            pending = target - self._handled
            _logger.debug("wait_idle timed out with %d pending notification(s)", pending)
            return False
        return True

    async def wait_for_notifications(self, count: int = 1, *, max_wait: float = 5.0) -> int:
        """Block until at least ``count`` notifications have been dispatched.

        A monitored item subscription is only proven alive when a value actually
        arrived. This is the gate Chapter 10 needs, and it is deliberately a
        different call from :meth:`wait_idle`: waiting for "nothing pending"
        succeeds just as nicely when the subscription reports nothing at all.

        Args:
            count: Notifications to wait for.
            max_wait: Seconds before giving up.

        Returns:
            The number of notifications dispatched so far.

        Raises:
            TimeoutError: Fewer than ``count`` notifications arrived in time, which
                usually means the item is not monitored, the server is not
                reporting, or the value never actually changed.
        """
        try:
            await self._await(
                lambda: self._handled >= count,
                max_wait,
                f"dispatch {count} notification(s), got {self._handled}",
            )
        except TimeoutError as exc:
            raise TimeoutError(f"subscription {self.subscription_id}: {exc}") from None
        return self._handled

    async def wait_for_value(
        self,
        node: NodeIdLike,
        *,
        newer_than: DataChangeRecord | None = None,
        max_wait: float = 5.0,
    ) -> DataChangeRecord:
        """Block until a value for ``node`` has been received and dispatched.

        The answer is the record, not a boolean: a caller that waits for data
        usually wants the value, its StatusCode and its SourceTimestamp.

        Args:
            node: Node to wait for.
            newer_than: Wait for a record that *arrived after* this one instead of
                accepting whatever is already cached. Pass the record a previous
                call returned. This is how Chapter 10 asserts "the value I wrote
                came back" without racing a notification that landed while the
                write was still in flight - the commonest cause of a flaky OPC UA
                test.
            max_wait: Seconds before giving up.

        Returns:
            The most recent record for that node.

        Raises:
            TimeoutError: No qualifying value arrived, which for a monitored item
                means it is not monitored, reporting is off, or the value never
                actually changed.
        """
        text = format_node_id(to_nodeid(node))
        threshold = newer_than.sequence if newer_than is not None else -1

        def arrived() -> bool:
            record = self._latest.get(text)
            return record is not None and record.sequence > threshold

        try:
            await self._await(arrived, max_wait, f"a value for {text}")
        except TimeoutError as exc:
            raise TimeoutError(f"subscription {self.subscription_id}: {exc}") from None
        return self._latest[text]

    async def wait_for_exact(
        self,
        node: NodeIdLike,
        value: Any,
        *,
        newer_than: DataChangeRecord | None = None,
        max_wait: float = 5.0,
    ) -> DataChangeRecord:
        """Block until **this value** has arrived for this node.

        :meth:`wait_for_value` answers "has anything newer arrived?". On a server that
        writes the tag by itself - which is every server that owns the tag - that is not
        the same question as "did my write come back", and an application that confuses
        the two prints somebody else's value as its own. This method asks the second
        question, which is what a write-verify-read cycle actually needs.

        Args:
            node: Node to wait for.
            value: The value the caller is waiting for, compared with ``==`` against the
                unwrapped value of each record.
            newer_than: Ignore records that arrived at or before this one, the same
                watermark rule as :meth:`wait_for_value`. Without it a matching value that
                was already cached answers immediately, which is how a verification passes
                without the write ever having worked.
            max_wait: Seconds before giving up.

        Returns:
            The record whose value matched.

        Raises:
            TimeoutError: No matching record arrived. The message names the newest value it
                did see, because "mine never arrived" and "something else overwrote it"
                need the same evidence to be told apart.

        Note:
            The manager remembers the *newest* record per node. A value that arrived and was
            replaced between two looks is not recoverable here: handlers registered with
            :meth:`register_handler` see every record, and Chapter 15 is where a gateway
            keeps them.
        """
        text = format_node_id(to_nodeid(node))
        seen = newer_than
        newest = self._latest.get(text)
        deadline = asyncio.get_running_loop().time() + max_wait
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                newest_text = (
                    f"{newest.value!r} (seq={newest.sequence})" if newest else "nothing at all"
                )
                raise TimeoutError(
                    f"subscription {self.subscription_id}: never saw {value!r} on {text}; "
                    f"the newest record was {newest_text}"
                )
            try:
                record = await self.wait_for_value(
                    node, newer_than=seen, max_wait=min(2.0, remaining)
                )
            except TimeoutError:
                continue
            if record.value == value:
                return record
            newest = seen = record

    async def _await(
        self, predicate: Callable[[], bool], max_wait: float, description: str
    ) -> None:
        """Wait for a predicate to become true, or raise TimeoutError.

        The predicate is checked *inside* the condition's lock, and the dispatcher
        notifies while holding that same lock, so a change can never slip between
        "check" and "start waiting". That is what makes these waits a replacement
        for sleeps in Chapter 10 and Chapter 11 rather than a shorter sleep.
        """
        stopped = False
        try:
            async with asyncio.timeout(max_wait), self._condition:
                while not predicate():
                    if self._stop.is_set():
                        stopped = True
                        break
                    await self._condition.wait()
        except TimeoutError:
            # Re-check inside the lock: a timeout that races with an arrival must
            # not report a failure that did not happen.
            if not predicate():
                raise TimeoutError(
                    f"timed out after {max_wait}s waiting for {description}"
                ) from None
        if stopped and not predicate():
            raise TimeoutError(f"subscription stopped while waiting for {description}")

    async def confirm_subscription(self) -> SubscriptionRevision:
        """Ask the server what this subscription is really running with.

        ``CreateSubscription`` answers with revised numbers, and asyncua's high
        level helper uses them internally and then throws them away. A client that
        wants to know has to ask a second time: send ``ModifySubscription`` with
        the values we believe we asked for, and read the values that come back.
        Those are the subscription's live timers - the publishing interval, the
        lifetime count and the keepalive count the server is actually using.

        Returns:
            A :class:`SubscriptionRevision`, also available from
            :attr:`revision` until the subscription is replaced.

        Raises:
            NotConnectedError: No subscription to ask about.
        """
        subscription = self._require_subscription()
        self._revision = await self._modify_subscription(subscription, self._publishing_interval)
        return self._revision

    async def probe_subscription(self) -> str | None:
        """Ask the server whether it still has this subscription, and report why not.

        ``ModifySubscription`` with the numbers the subscription already has is the
        cheapest no-op OPC UA has for this question: it is answered straight from the
        server's subscription table, it changes nothing when the numbers do not change,
        and a server that no longer holds the subscription answers
        ``BadSubscriptionIdInvalid`` rather than staying quiet. That status is the only
        trace a *server-side* delete leaves: the session stays open, no notification
        ever arrives again, the client's own counters stop moving, and nothing in the
        address space says the subscription is gone.

        The probe records what it finds, so :meth:`rebuild_reason` reports it too and a
        supervisor can act on the same fact twice from one call.

        Returns:
            The reason this subscription must be rebuilt, or ``None`` when the answer
            says it is alive - or when the transport is too broken to tell, which is
            the session supervisor's problem, not a reason to recreate a subscription.
        """
        self._last_probe_at = datetime.now(UTC)
        subscription = self._subscription
        if subscription is None or subscription.is_deleted:
            return self.rebuild_reason()
        try:
            await self._modify_subscription(subscription, self._publishing_interval)
        except (UaStatusCodeError, UaError, TimeoutError, OSError) as exc:
            code = _status_code_of(exc)
            if code is not None and code.name == _SUBSCRIPTION_GONE:
                self._note_subscription_status(code)
                return self.rebuild_reason()
            # A transport failure says nothing about this subscription's id: a server
            # that is unreachable answers every question the same way.
            _logger.debug("subscription probe inconclusive: %s", exc)
            return None
        return self.rebuild_reason()

    def seconds_since_probe(self) -> float | None:
        """Seconds since the last :meth:`probe_subscription`, or ``None`` if never.

        A plant at steady state reports nothing for hours and is perfectly healthy, so
        a supervisor that probed on every silence window would spend its time sending
        ModifySubscription calls that change nothing. This is how it keeps the probe to
        one check per window instead of one check per poll.
        """
        if self._last_probe_at is None:
            return None
        return (datetime.now(UTC) - self._last_probe_at).total_seconds()

    async def set_publishing_interval(self, interval: float) -> SubscriptionRevision | None:
        """Change the publishing interval, and believe the server's answer.

        ``ModifySubscription`` is the right service for this: it keeps the
        subscription id, the monitored items, their sampling and their sequence
        numbers, which a rebuild throws away. But the response of that service only
        ever contains *revised* values, and a server is free to answer with the
        numbers it already had - asyncua's own server does exactly that, ignoring
        the request and returning the values from creation. The manager compares the
        answer with the request and rebuilds when the server did not move, because
        "we asked" is not "we got".

        Args:
            interval: Milliseconds between publishes the application wants.

        Returns:
            The revision describing the subscription this call ended with, or ``None``
            when there was nothing to rebuild into. Its
            :attr:`SubscriptionRevision.rebuilt` says whether the server cooperated.
        """
        subscription = self._require_subscription()
        answer = await self._modify_subscription(subscription, interval)
        self._revision = answer
        if answer.publishing_interval_accepted:
            self._publishing_interval = interval
            self._stats.publishing_interval = interval
            _logger.info(
                "subscription %s now publishes every %sms",
                self._stats.subscription_id,
                answer.revised_publishing_interval,
            )
            return answer

        _logger.warning(
            "ModifySubscription answered %sms for a request of %sms: the server kept its own "
            "interval, so the subscription is rebuilt instead",
            answer.revised_publishing_interval,
            interval,
        )
        self._publishing_interval = interval
        self._stats.publishing_interval = interval
        await self.rebuild()
        if self._subscription is None:
            # Rebuild found nothing to restore: the interval is remembered for the
            # next subscription, but there is no live subscription to report on.
            self._revision = None
            return None
        fresh = await self._modify_subscription(self._subscription, interval)
        self._revision = dataclasses.replace(fresh, rebuilt=True)
        return self._revision

    async def _modify_subscription(
        self, subscription: Subscription, interval: float
    ) -> SubscriptionRevision:
        """Send ModifySubscription with our current values and read the revised answer."""
        parameters = subscription.parameters
        params = ua.ModifySubscriptionParameters()
        params.SubscriptionId = int(subscription.subscription_id or 0)
        params.RequestedPublishingInterval = interval
        params.RequestedLifetimeCount = int(parameters.RequestedLifetimeCount)
        params.RequestedMaxKeepAliveCount = int(parameters.RequestedMaxKeepAliveCount)
        params.MaxNotificationsPerPublish = int(parameters.MaxNotificationsPerPublish)
        params.Priority = parameters.Priority
        result = await subscription.update(params)
        return SubscriptionRevision(
            service="ModifySubscription",
            requested_publishing_interval=interval,
            requested_lifetime_count=int(parameters.RequestedLifetimeCount),
            requested_max_keepalive_count=int(parameters.RequestedMaxKeepAliveCount),
            revised_publishing_interval=float(result.RevisedPublishingInterval),
            revised_lifetime_count=int(result.RevisedLifetimeCount),
            revised_max_keepalive_count=int(result.RevisedMaxKeepAliveCount),
        )

    def _require_subscription(self) -> Subscription:
        if self._subscription is None:
            raise NotConnectedError("subscription not started - call start() or subscribe() first")
        if self._subscription.is_deleted:
            raise SubscriptionClosedError("subscription was deleted by the server")
        return self._subscription

    async def _consume(self) -> None:
        """Single ordered consumer of the subscription's notification queue."""
        subscription = self._subscription
        if subscription is None:
            return
        while not self._stop.is_set():
            # Only the read off the queue is guarded. A failure inside _dispatch() is
            # a bug in this toolkit and must surface, not be counted as a wobbling
            # subscription and retried forever.
            try:
                event = await subscription.next_event(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except StopAsyncIteration:
                # asyncua's end-of-subscription sentinel: the queue is closed, so
                # nothing will ever arrive on this subscription again. Treating that
                # as a retryable error is how a client spins on a dead subscription.
                self._note_closed_queue()
                return
            except (UaStatusCodeError, UaError, TimeoutError, OSError) as exc:
                self._stats.errors += 1
                code = _status_code_of(exc)
                if code is not None and code.name == _SUBSCRIPTION_GONE:
                    # Not a busy server and not a transport hiccup to wait out: the
                    # server is answering a publish for a subscription it deleted.
                    # Retrying the same subscription id forever is the mistake this
                    # guard exists to make visible.
                    self._note_subscription_status(code)
                else:
                    _logger.warning("notification consumption failed: %s", exc)
                await asyncio.sleep(0.5)
                continue
            if event is None:
                continue
            if isinstance(event, DataChangeEvent):
                await self._dispatch(event)
            elif isinstance(event, StatusChangeEvent):
                # asyncua's StatusCode is a dataclass holding the number in ``.value``,
                # is not an int itself, and wrapping it again produces a StatusCode
                # whose ``.name`` and ``.is_good()`` raise. Normalize at the boundary.
                self._note_subscription_status(_as_status_code(event.notification.Status))
            elif isinstance(event, OpcEvent):
                _logger.debug("event notification received (Chapter 19 covers events)")

    @property
    def last_report_at(self) -> datetime | None:
        """When the last notification was dispatched, or ``None`` if never.

        The manager cannot tell "nothing changed" from "the server stopped talking":
        a monitored item reports a *change*, and a plant at steady state changes
        nothing. What the manager can say is how long it has been since it heard
        anything, which is the number a supervisor warns on and a gateway reports.
        """
        return self._last_report_at

    @property
    def started_at(self) -> datetime | None:
        """When the current subscription was created, or ``None`` before start."""
        return self._started_at

    def seconds_since_report(self) -> float | None:
        """Seconds since this manager last handed anything to a callback.

        Measured from the creation of the current subscription when nothing has arrived
        yet, which is the honest answer for a subscription that has never reported.
        ``None`` when nothing is running.
        """
        reference = self._last_report_at or self._started_at
        if reference is None:
            return None
        return (datetime.now(UTC) - reference).total_seconds()

    def _note_closed_queue(self) -> None:
        """Record that this subscription will never report anything again."""
        if self._stop.is_set() or self._subscription is None:
            # We closed it ourselves: nothing to report and nothing to repair.
            _logger.debug("subscription consumer ended on the client's own close")
            return
        self._queue_closed = True
        _logger.warning(
            "subscription %s stopped reporting: %s",
            self._stats.subscription_id,
            self.rebuild_reason() or "no reason recorded",
        )

    def _note_subscription_status(self, status: ua.StatusCode) -> None:
        """Record what the server said about the subscription itself."""
        self._server_status = status
        self._stats.status_changes += 1
        self._stats.last_status_change = status.name
        if status.is_good():
            _logger.info("server reported subscription status %s", status.name)
            return
        self._stats.bad_status_changes += 1
        if status.name == _SUBSCRIPTION_GONE:
            # The one status a server-side delete leaves behind, and the reason this
            # manager reports instead of repairing: nothing else about a subscription the
            # server threw away changes, not the counters and not the callbacks.
            _logger.warning(
                "subscription %s no longer exists on the server: %s",
                self.subscription_id,
                status.name,
            )
            return
        _logger.warning("server reported subscription status %s", status.name)

    def _clear_subscription_status(self) -> None:
        """Forget what the server said about a subscription that no longer exists."""
        self._server_status = None
        self._queue_closed = False
        self._stats.last_status_change = "none"
        # The clock starts over with the subscription: silence measured against the
        # lifetime of a subscription the server already deleted would make the next
        # one look broken before it has had a chance to report anything.
        self._started_at = None
        self._last_report_at = None
        self._last_probe_at = None

    async def _dispatch(self, event: DataChangeEvent) -> None:
        """Route one data change to every callback registered for its key."""
        text = format_node_id(event.node.nodeid)
        item = self._items.get(text)
        if self._stats.paused:
            # The server kept reporting even though we asked it to stop. Counting
            # what we throw away is what makes a pause auditable.
            self._stats.suppressed += 1
            return
        if item is None:
            # A notification for an item that is already gone: it was on the wire
            # when DeleteMonitoredItems ran. Sending it to the fallback handler
            # would write a tag this application stopped monitoring, so it is
            # counted and dropped instead.
            self._stats.stray_notifications += 1
            _logger.debug("dropped notification for unmonitored %s", text)
            return
        self._received += 1
        self._last_report_at = datetime.now(UTC)
        key = item.key
        callbacks = self._handlers.get(key) or self._handlers.get(_DEFAULT_KEY) or []
        # The sequence is assigned before the counters move, so the record a
        # caller already holds stays a valid watermark for wait_for_value().
        record = _build_record(event, key, sequence=self._stats.notifications + 1)
        self._stats.record_outcome(record.status_code)
        # Stored before the callback runs, so a handler can read the record it is
        # handling through last_value() instead of being handed a copy of it twice.
        self._latest[text] = record
        if not callbacks:
            await self._mark_handled()
            _logger.debug("no handler registered for key %s, dropped %s", key, text)
            return
        for callback in callbacks:
            # One at a time, in registration order, and each failure counted on its
            # own: a broken sink is that sink's bug, and the next handler still gets
            # the record.
            try:
                await callback(record)
            except Exception:
                self._stats.errors += 1
                _logger.exception("notification handler failed for %s", text)
        await self._mark_handled()

    async def _mark_handled(self) -> None:
        """Advance the handled counter and wake every waiter.

        The notify happens under the condition's lock, which is what guarantees a
        waiter that checked its predicate just before waiting cannot miss this
        event until the predicate is checked again.
        """
        self._handled += 1
        async with self._condition:
            self._condition.notify_all()


def _build_record(event: DataChangeEvent, key: str, *, sequence: int) -> DataChangeRecord:
    """Flatten asyncua's per-publish notification object into a frozen record.

    ``DataChangeNotif`` is built fresh for every notification and carries the raw
    ``ua.MonitoredItemNotification``: the ClientHandle, and the DataValue with its
    own StatusCode and timestamps. ``event.value`` is the unwrapped value, which is
    what applications actually want, but the status and timestamps only exist on the
    DataValue - and a value change whose status is Uncertain is exactly the case a
    historian must not pretend is Good.
    """
    notification = event.data.monitored_item
    data_value = notification.Value
    status = getattr(data_value, "StatusCode", None) or ua.StatusCode(0)
    variant = getattr(data_value, "Value", None)
    return DataChangeRecord(
        node_id=event.node.nodeid,
        value=event.value,
        status_code=status,
        source_timestamp=getattr(data_value, "SourceTimestamp", None),
        server_timestamp=getattr(data_value, "ServerTimestamp", None),
        client_handle=int(notification.ClientHandle),
        key=key,
        sequence=sequence,
        replayed=event.replayed,
        variant_type=getattr(variant, "VariantType", None),
    )


def _as_status_code(value: object) -> ua.StatusCode:
    """Build a usable StatusCode from anything asyncua used to represent one.

    asyncua represents a status code as a plain int in some places (an exception's
    ``code``) and as a :class:`asyncua.ua.uatypes.StatusCode` in others (a
    ``SubscriptionStatus``). The dataclass keeps the number in ``.value``, is not an
    ``int``, and wrapping it again produces a StatusCode whose ``.name`` and
    ``.is_good()`` raise. Converting at the boundary is the only safe way to compare
    a status the server sent with a status the toolkit knows.
    """
    # ``.value`` when there is one (the StatusCode dataclass keeps the number there),
    # the object itself when it is already a plain int.
    raw: Any = getattr(value, "value", value)
    return ua.StatusCode(int(raw))


def _status_code_of(exc: BaseException) -> ua.StatusCode | None:
    """The StatusCode an asyncua error carries, or ``None`` when it carries none.

    asyncua raises a subclass per status code, so ``BadSubscriptionIdInvalid`` is
    its own exception class with ``code`` as a class attribute, while a parse or
    transport failure has no code to compare against. Reading it defensively is
    what lets the manager tell "the server deleted my subscription" apart from
    "the socket wobbled".
    """
    code = getattr(exc, "code", None)
    if code is None:
        return None
    try:
        return _as_status_code(code)
    except (TypeError, ValueError):
        return None


__all__ = ["SubscriptionManager"]
