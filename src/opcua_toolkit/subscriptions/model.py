"""Subscription data models (Chapter 10).

OPC UA subscriptions have four independent timers/limits, and beginners mix them
up constantly:

* **Publishing interval** - how often the *client* asks the server for the
  accumulated notifications. Notifications are pushed to the client's "mailbox"
  and only delivered when a publish request is answered.
* **Sampling interval** - how often the *server* reads the source value.
* **Queue size** - how many notifications the server keeps per monitored item
  between two publishes. Too small and changes are lost, too big and the server
  spends memory on data nobody will read.
* **Monitoring mode** - Disabled / Sampling / Reporting. Sampling monitors the
  item but reports nothing, which is exactly what you want while a link is
  unstable or while a commissioning tool is still discovering the address space.

Deadbands (``ua.DeadbandType.Absolute`` / ``.Percent``) and triggers
(``ua.DataChangeTrigger.Status`` / ``.StatusValue`` / ``.StatusValueTimestamp``)
decide *what counts as a change*. They are part of a monitored item filter.
asyncua's high level ``subscribe_data_change`` exposes sampling, queue size and
monitoring mode; filters are built with
``ua.MonitoredItemCreateRequest`` plus ``ua.DataChangeFilter`` and sent with
``client.uaclient.create_monitored_items`` - Chapter 10 shows that request
explicitly instead of hiding it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from asyncua import ua

from opcua_toolkit.errors import format_node_id
from opcua_toolkit.nodeids import NodeIdLike
from opcua_toolkit.settings import SubscriptionDefaults


@dataclasses.dataclass(frozen=True, slots=True)
class DataChangeRecord:
    """One value change, frozen, ready to hand to an application.

    asyncua gives applications a mutable protocol object that is rebuilt per
    publish. Applications that keep the value - a historian, a buffer, a cache -
    end up storing a notification that has already been reused for the next one.
    :class:`SubscriptionManager` flattens it here, once, so a callback may store
    the record as it is.

    Attributes:
        node_id: Which tag changed.
        value: The unwrapped Python value (``None`` when the server sent no value).
        status_code: The DataValue status. A ``Good`` value with a ``Uncertain``
            status is a real OPC UA case: read it, and report it.
        source_timestamp: When the source produced the value.
        server_timestamp: When the server sampled it.
        client_handle: The ClientHandle the client asked for when it created the
            monitored item, echoed back by the server on every notification. It is
            *not* the number you pass to Modify/DeleteMonitoredItems: that is the
            server's own MonitoredItemId, which :class:`MonitoredItemHandle` keeps.
            A monitored item therefore has two ids, and mixing them up is a classic
            Chapter 10 mistake.
        key: The subscription key the item was created with.
        sequence: Arrival order inside one :class:`SubscriptionManager`: every
            record the manager dispatches gets the next number. Comparing
            sequences is how a caller waits for "the next value after this one"
            without racing against a notification that already arrived. It is
            arrival order, not server order: for that, compare
            :attr:`source_timestamp`.
        replayed: True when the record arrived through Republish after a
            reconnect, so its timestamp may predate something already learned.
        variant_type: The wire type, e.g. ``Double``, useful for logging.
    """

    node_id: ua.NodeId
    value: Any
    status_code: ua.StatusCode
    source_timestamp: datetime | None
    server_timestamp: datetime | None
    client_handle: int
    key: str
    sequence: int = 0
    replayed: bool = False
    variant_type: ua.VariantType | None = None

    @property
    def node_id_text(self) -> str:
        """Canonical ``ns=...;...`` text for logs and dictionary keys."""
        return format_node_id(self.node_id)

    @property
    def is_good(self) -> bool:
        """True when both the status and the value are usable."""
        return self.status_code.is_good() and self.value is not None

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "node_id": self.node_id_text,
            "value": self.value,
            "status": self.status_code.name,
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
            "server_timestamp": (
                self.server_timestamp.isoformat() if self.server_timestamp else None
            ),
            "client_handle": self.client_handle,
            "key": self.key,
            "sequence": self.sequence,
            "replayed": self.replayed,
            "variant_type": self.variant_type.name if self.variant_type else None,
        }


#: Application side callback: what we do with one value change.
NotificationCallback = Callable[[DataChangeRecord], Awaitable[None]]

#: The key an item is created under when the caller does not name one, and the key
#: a fallback handler is registered under. It is a printable word rather than a
#: private sentinel because it ends up in the records an application logs.
DEFAULT_SUBSCRIPTION_KEY = "default"


@dataclasses.dataclass(frozen=True, slots=True)
class SubscriptionOptions:
    """Per monitored item settings, all optional.

    Attributes:
        sampling_interval: Milliseconds the server waits between source reads.
            ``None`` inherits the subscription's publishing interval. ``0``
            means "as fast as the server can", clamped by the server's
            ``MinimumSamplingInterval`` and ``MinSupportedSampleRate``.
        queue_size: Notifications kept per monitored item between publishes.
        monitoring_mode: Disabled, Sampling or Reporting.
        attribute: Which attribute to monitor. Value almost always.
    """

    sampling_interval: float | None = None
    queue_size: int | None = None
    monitoring_mode: ua.MonitoringMode = ua.MonitoringMode.Reporting
    attribute: ua.AttributeIds = ua.AttributeIds.Value

    def resolved(self, defaults: SubscriptionDefaults) -> SubscriptionOptions:
        """Fill unset values from the client's defaults."""
        return SubscriptionOptions(
            sampling_interval=(
                defaults.sampling_interval
                if self.sampling_interval is None
                else self.sampling_interval
            ),
            queue_size=defaults.queue_size if self.queue_size is None else self.queue_size,
            monitoring_mode=self.monitoring_mode,
            attribute=self.attribute,
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "sampling_interval": self.sampling_interval,
            "queue_size": self.queue_size,
            "monitoring_mode": self.monitoring_mode.name,
            "attribute": self.attribute.name,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class MonitoredItemHandle:
    """One monitored item as the toolkit knows it.

    Attributes:
        node_id: The node being monitored.
        monitored_item_id: The id the **server** assigned to this item
            (``MonitoredItemId``). ModifyMonitoredItems and
            DeleteMonitoredItems address items by this number, so
            :meth:`SubscriptionManager.unsubscribe` uses it. The number a
            notification carries is the *client* handle instead - see
            :class:`DataChangeRecord`.
        key: The key notifications of this item are dispatched under.
        options: The options the item was created with, defaults resolved.
        requested_sampling_interval: What the client asked for.
        revised_sampling_interval: What the server actually gave, if it revised it.
        revised_queue_size: What the server actually gave for the queue.
    """

    node_id: ua.NodeId
    monitored_item_id: int
    key: str
    options: SubscriptionOptions
    requested_sampling_interval: float
    revised_sampling_interval: float | None = None
    revised_queue_size: int | None = None

    @property
    def node_id_text(self) -> str:
        """Canonical NodeId text used as the dispatch key."""
        return format_node_id(self.node_id)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "node_id": self.node_id_text,
            "monitored_item_id": self.monitored_item_id,
            "key": self.key,
            "sampling_interval": self.revised_sampling_interval
            if self.revised_sampling_interval is not None
            else self.requested_sampling_interval,
            "queue_size": self.revised_queue_size,
            "monitoring_mode": self.options.monitoring_mode.name,
        }


@dataclasses.dataclass(slots=True)
class SubscriptionStats:
    """Runtime counters that make subscription health visible.

    Attributes:
        subscription_id: The id the server gave this subscription, ``None`` after
            :meth:`SubscriptionManager.stop`.
        publishing_interval: The interval the manager asked for.
        monitored_items: Items the server accepted.
        failed_items: Items the server refused, per item or per failed request.
        notifications: Records handed to an application callback.
        suppressed: Records the manager refused to deliver because it was paused.
            On a server that honours ``SetPublishingMode`` this stays 0; on one
            that does not, it is the number that proves the client's own guard did
            the work.
        stray_notifications: Records that arrived for an item no longer monitored,
            e.g. a notification that was already on the wire when the item was
            deleted. Deliberately dropped instead of being routed to the fallback
            handler.
        good_values / uncertain_values / bad_values: The same notifications sorted
            by StatusCode.
        errors: Handler failures and consumption failures.
        status_changes: How often the server reported a ``SubscriptionStatus`` for
            this subscription, either in a publish response or by refusing a publish
            for a subscription id it no longer knows.
        bad_status_changes: The subset of those reports that were not Good, i.e. the
            times the server said the subscription is damaged.
        subscription_status: The latest such report as text, ``"none"`` when the
            server has never said anything about this subscription. Kept as text so
            a counter dump stays JSON friendly; the StatusCode itself is
            :attr:`SubscriptionManager.subscription_status`.
        recreations: How often the subscription had to be rebuilt.
        paused: Whether reporting is currently switched off client side.
    """

    subscription_id: int | None = None
    publishing_interval: float = 0.0
    monitored_items: int = 0
    failed_items: int = 0
    notifications: int = 0
    suppressed: int = 0
    stray_notifications: int = 0
    good_values: int = 0
    uncertain_values: int = 0
    bad_values: int = 0
    errors: int = 0
    status_changes: int = 0
    bad_status_changes: int = 0
    last_status_change: str = "none"
    recreations: int = 0
    paused: bool = False

    def record_outcome(self, status: ua.StatusCode) -> None:
        """Count one notification by the StatusCode the server attached to it.

        Separating Good from Uncertain and Bad is not decoration: an application
        that only counts notifications cannot tell a live plant from a plant whose
        sensors are stuck.
        """
        self.notifications += 1
        if status.is_good():
            self.good_values += 1
        elif status.is_uncertain():
            self.uncertain_values += 1
        else:
            self.bad_values += 1

    def summary(self) -> str:
        """One line summary for terminal and CI output."""
        return (
            f"subscription={self.subscription_id} publishing={self.publishing_interval}ms "
            f"items={self.monitored_items} failed={self.failed_items} "
            f"notifications={self.notifications} good={self.good_values} "
            f"uncertain={self.uncertain_values} bad={self.bad_values} "
            f"suppressed={self.suppressed} stray={self.stray_notifications} "
            f"errors={self.errors} recreations={self.recreations} "
            f"status={self.last_status_change} "
            f"mode={'paused' if self.paused else 'reporting'}"
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SubscriptionRevision:
    """What the server answered about one subscription's timers.

    Neither ``CreateSubscription`` nor ``ModifySubscription`` ever answers "no".
    Both answer with *revised* numbers, and the revised numbers are the
    subscription that actually runs. A client that never reads them believes its
    own request, which is why a publishing interval that "we set to 1000 ms" and a
    publishing interval the server agreed to are two different facts in this
    toolkit.

    Attributes:
        service: The service that produced the answer: ``CreateSubscription`` or
            ``ModifySubscription``.
        requested_publishing_interval: Milliseconds between publishes the client
            asked for.
        requested_lifetime_count: How many publishing intervals a subscription may
            survive without a publish request before the server deletes it.
        requested_max_keepalive_count: How many publishing intervals of silence the
            server tolerates before it sends a keepalive.
        revised_publishing_interval: What the server says it is using.
        revised_lifetime_count: What the server says it is using, clamped to its
            own cap.
        revised_max_keepalive_count: What the server says it is using, clamped to
            its own cap.
        rebuilt: True when :class:`SubscriptionManager` had to recreate the
            subscription because the server's answer did not move.
    """

    service: str
    requested_publishing_interval: float
    requested_lifetime_count: int
    requested_max_keepalive_count: int
    revised_publishing_interval: float | None = None
    revised_lifetime_count: int | None = None
    revised_max_keepalive_count: int | None = None
    rebuilt: bool = False

    @property
    def publishing_interval_accepted(self) -> bool:
        """True when the server answered with the interval that was asked for."""
        return (
            self.revised_publishing_interval is not None
            and abs(self.revised_publishing_interval - self.requested_publishing_interval)
            < _INTERVAL_TOLERANCE_MS
        )

    @property
    def revised(self) -> bool:
        """True when the server changed at least one of the three numbers."""
        return bool(self.revisions)

    @property
    def revisions(self) -> tuple[str, ...]:
        """The numbers the server moved, as short sentences for a log line."""
        changed: list[str] = []
        pairs = (
            (
                "publishing interval",
                self.requested_publishing_interval,
                self.revised_publishing_interval,
            ),
            ("lifetime count", self.requested_lifetime_count, self.revised_lifetime_count),
            (
                "keepalive count",
                self.requested_max_keepalive_count,
                self.revised_max_keepalive_count,
            ),
        )
        for name, requested, revised in pairs:
            if revised is None or revised == requested:
                continue
            changed.append(f"{name} {requested} -> {revised}")
        return tuple(changed)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "service": self.service,
            "requested_publishing_interval": self.requested_publishing_interval,
            "requested_lifetime_count": self.requested_lifetime_count,
            "requested_max_keepalive_count": self.requested_max_keepalive_count,
            "revised_publishing_interval": self.revised_publishing_interval,
            "revised_lifetime_count": self.revised_lifetime_count,
            "revised_max_keepalive_count": self.revised_max_keepalive_count,
            "rebuilt": self.rebuilt,
        }

    def summary(self) -> str:
        """One line summary for terminal and CI output."""
        revised = (
            f"revised publishing={self.revised_publishing_interval}ms "
            f"lifetime={self.revised_lifetime_count} keepalive={self.revised_max_keepalive_count}"
            if self.revised_publishing_interval is not None
            else "revised values not reported by this server"
        )
        return (
            f"{self.service}: asked publishing={self.requested_publishing_interval}ms "
            f"lifetime={self.requested_lifetime_count} "
            f"keepalive={self.requested_max_keepalive_count}; "
            f"{revised}" + (" [rebuilt]" if self.rebuilt else "")
        )


#: A server is allowed to answer 999.7ms for a request of 1000ms. This is the
#: tolerance the toolkit treats as "the same interval", not a general comparison.
_INTERVAL_TOLERANCE_MS = 0.5


__all__ = [
    "DEFAULT_SUBSCRIPTION_KEY",
    "DataChangeRecord",
    "MonitoredItemHandle",
    "NodeIdLike",
    "NotificationCallback",
    "SubscriptionOptions",
    "SubscriptionRevision",
    "SubscriptionStats",
]
