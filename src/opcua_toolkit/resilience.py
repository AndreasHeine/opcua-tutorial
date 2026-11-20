"""Connection supervision (Chapter 12).

asyncua already ships a real reconnect mechanism, and the first lesson of this
chapter is to *use* it instead of writing your own socket loop::

    client = Client(url)
    await client.connect(auto_reconnect=True, reconnect_max_delay=30.0)

That supervisor re-establishes the secure channel and the session, and it
re-creates monitored items for subscriptions that are still open. What it does
*not* do is tell your application anything: no log line you can alert on, no
health signal for a dashboard, no pause of a downstream historian while values
are stale, and no decision about whether the server came back in a state you can
trust (``ServerState == Running``? ``ServiceLevel == 0`` means maintenance?).

:class:`ConnectionSupervisor` is the thin layer on top of the built-in
mechanism:

* it probes ``ServerState``, ``ServiceLevel`` and the namespace array;
* it classifies the outage with the retry policy, so a rejected login is not
  hammered with the same frequency as a dropped cable;
* it reports state changes to observers, which is where a historian pauses, a
  dashboard turns yellow and a test asserts recovery;
* it rebuilds subscriptions when the server refused to keep them - including the
  case where the session stayed healthy and only the subscription died, which a
  transport only watchdog never sees.

Everything here is additive: delete the supervisor and the client still
reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from typing import Any

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from opcua_toolkit.diagnostics import log_with
from opcua_toolkit.errors import NotConnectedError, format_node_id
from opcua_toolkit.health import ServerHealth, ServiceLevelBand
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.settings import LimitsOverrides, RetryPolicy
from opcua_toolkit.subscriptions.manager import SubscriptionManager
from opcua_toolkit.values import unwrap

_logger = logging.getLogger(__name__)

ConnectionObserver = Callable[["ConnectionStatus"], Awaitable[None]]


@dataclasses.dataclass(frozen=True, slots=True)
class ConnectionStatus:
    """What the supervisor currently believes about the link.

    ``connected`` here is a *conclusion*, not a copy of a client flag: asyncua reports
    the transport and the session as two independent booleans, and during a reconnect it
    keeps reporting an activated session for a server that stopped answering. A supervisor
    that copied those flags would report a dead link as connected until it gave up, which
    is exactly the window in which a gateway silently collects nothing.
    """

    connected: bool
    healthy: bool
    attempts: int
    message: str
    operational: bool = False
    health: ServerHealth | None = None
    observed_at: datetime = dataclasses.field(default_factory=lambda: datetime.now(UTC))
    elapsed_ms: float = 0.0

    @property
    def state_text(self) -> str:
        """Short human readable classification.

        Three different problems need three different names, because they have
        three different remedies: the link is gone (reconnect), the server is up
        but refuses to serve data (wait or fail over), and the server serves data
        of reduced quality (keep going, but say so).
        """
        if not self.connected:
            return f"disconnected ({self.message})"
        if not self.operational:
            return f"connected but not usable: {self.message}"
        if not self.healthy:
            return f"connected but degraded: {self.message}"
        return "connected and healthy"

    def as_dict(self) -> dict[str, object]:
        """JSON-safe representation for logs and health endpoints."""
        return {
            "connected": self.connected,
            "healthy": self.healthy,
            "operational": self.operational,
            "attempts": self.attempts,
            "message": self.message,
            "observed_at": self.observed_at.isoformat(),
            "elapsed_ms": round(self.elapsed_ms, 2),
            "server_state": self.health.state.name if self.health else None,
            "service_level": self.health.service_level if self.health else None,
            "service_level_band": self.health.band.value if self.health else None,
        }


class ConnectionSupervisor:
    """Watchdog that keeps a client useful across outages.

    Args:
        client: A client created with ``ClientSettings.create_client()``, which
            you connect with ``auto_reconnect=True``.
        subscriptions: Managers to rebuild when the server dropped their items.
        retry: Backoff policy for the supervision loop. ``max_attempts=0`` means
            keep trying forever, which is what you want for a supervisor.
        poll_interval: Seconds between health probes while connected.
        limits_overrides: Client-side caps applied on top of the published ones.
        silent_probe_limit: Seconds a subscription may stay quiet before the supervisor
            *asks* the server whether it still has it, or ``None`` to never ask. Silence
            alone is not damage - a plant at steady state changes nothing - so it triggers
            a question and never a rebuild. See
            :meth:`SubscriptionManager.probe_subscription`.
    """

    def __init__(
        self,
        client: Client,
        *,
        subscriptions: Iterable[SubscriptionManager] = (),
        retry: RetryPolicy | None = None,
        poll_interval: float = 5.0,
        limits_overrides: LimitsOverrides | None = None,
        silent_probe_limit: float | None = 60.0,
    ) -> None:
        self._client = client
        self._subscriptions = list(subscriptions)
        self._retry = retry if retry is not None else RetryPolicy(max_attempts=0)
        self._poll_interval = poll_interval
        self._limits_overrides = limits_overrides
        self._silent_probe_limit = silent_probe_limit
        self._status = ConnectionStatus(
            connected=False, healthy=False, operational=False, attempts=0, message="not probed"
        )
        self._observers: list[ConnectionObserver] = []
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._limits: OperationLimits | None = None
        self._namespaces: tuple[str, ...] = ()
        self._last_signature: tuple[bool, bool, bool, str] | None = None
        self._repairs: list[str] = []

    @property
    def status(self) -> ConnectionStatus:
        """Latest classification of the link."""
        return self._status

    @property
    def limits(self) -> OperationLimits:
        """Limits discovered from the server, available after :meth:`probe`."""
        if self._limits is None:
            raise NotConnectedError("limits not read yet - call probe() first")
        return self._limits

    @property
    def namespaces(self) -> tuple[str, ...]:
        """Namespace URIs of the connected server."""
        return self._namespaces

    @property
    def silent_probe_limit(self) -> float | None:
        """How long a subscription may stay quiet before the supervisor asks about it.

        Set at construction, read by anyone who has to know what the supervisor will do
        with a quiet tag: :attr:`None` means silence is never questioned at all, and a
        small number means steady state gets probed often. Chapter 12's rule - silence is
        a reason to ask, never a reason to rebuild - is implemented through this window.
        """
        return self._silent_probe_limit

    def add_observer(self, observer: ConnectionObserver) -> None:
        """Register an async callback invoked on every status change."""
        self._observers.append(observer)

    def add_subscription(self, manager: SubscriptionManager) -> None:
        """Watch one more subscription from now on.

        A gateway that adds a tag group while it runs has to supervise it too: a
        subscription the supervisor does not know about is one nobody will notice is gone.
        Registering the same manager twice is ignored, because two entries would mean two
        rebuilds of one subscription per outage.
        """
        if manager not in self._subscriptions:
            self._subscriptions.append(manager)

    def remove_subscription(self, manager: SubscriptionManager) -> None:
        """Stop watching one subscription.

        A gateway that retires a tag group stops its subscription, and a supervisor that
        keeps watching a stopped one spends its silence window asking a server about a
        subscription id that no longer exists - and reports it in every status line, which
        is how a dashboard ends up full of subscriptions nobody is collecting.
        """
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(manager)

    @property
    def subscription_repairs(self) -> tuple[str, ...]:
        """Why each subscription this supervisor rebuilt was no longer usable.

        Rebuilds are not free, and a supervisor that repairs in silence hides the
        only evidence that values were missing: every entry here is a gap a
        historian has to be told about.
        """
        return tuple(self._repairs)

    async def probe(self) -> ConnectionStatus:
        """Read limits, namespaces and health once and record the result."""
        started = time.monotonic()
        try:
            limits = await OperationLimits.read(self._client)
            if self._limits_overrides is not None:
                limits = limits.with_overrides(self._limits_overrides.as_dict())
            health = await ServerHealth.read(self._client)
            uris = await self._read_namespace_uris()
        except (TimeoutError, UaStatusCodeError, UaError, OSError) as exc:
            # A failed probe is not the same as a lost link: the session can be
            # perfectly open while the server answers with BadNodeIdUnknown, and a
            # reconnect in progress can leave the client's own flags claiming a link
            # that no longer exists. Which one happened decides whether this supervisor
            # starts recovering or keeps polling a server that is answering.
            transport_down = _transport_is_down(exc)
            self._status = ConnectionStatus(
                connected=not transport_down and _session_is_open(self._client),
                healthy=False,
                operational=False,
                attempts=self._status.attempts + 1,
                message=_error_text(exc),
                observed_at=datetime.now(UTC),
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
            log_with(
                _logger,
                logging.WARNING,
                "health probe failed",
                attempts=self._status.attempts,
                error=self._status.message,
            )
            await self._publish()
            return self._status

        self._limits = limits
        self._namespaces = uris
        self._status = ConnectionStatus(
            connected=_session_is_open(self._client),
            healthy=health.band is ServiceLevelBand.HEALTHY and health.is_running,
            operational=health.is_operational,
            attempts=0,
            message=health.reason,
            health=health,
            observed_at=datetime.now(UTC),
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
        await self._publish()
        return self._status

    async def watch(self) -> ConnectionStatus:
        """Run the supervision loop until :meth:`stop`.

        The loop never raises on transport errors: an outage is an expected
        condition, and the loop's job is to report it and recover from it.
        """
        self._stop.clear()
        delay_index = 1
        while not self._stop.is_set():
            status = await self.probe()
            if status.connected and status.operational:
                # A degraded server is still a server: keep polling at the normal
                # rate instead of tearing down subscriptions that are working.
                delay_index = 1
                if not status.healthy:
                    _logger.warning("server degraded: %s", status.state_text)
                # A usable link is not a usable subscription. A server can keep the
                # session for hours and still have deleted this particular
                # subscription, and only the subscription knows that.
                await self.repair_subscriptions()
                await asyncio.sleep(self._poll_interval)
                continue
            delay = min(max(self._retry.delay_before(delay_index), 0.5), 30.0)
            _logger.warning("server unusable: %s (next probe in %.1fs)", status.state_text, delay)
            await self.repair_subscriptions(force=True)
            delay_index += 1
            await asyncio.sleep(delay)
        return self._status

    async def start(self) -> None:
        """Run :meth:`watch` as a background task."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._watch_guarded())

    async def stop(self) -> None:
        """Stop the background supervision task."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _watch_guarded(self) -> None:
        try:
            await self.watch()
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("connection supervisor crashed")

    async def repair_subscriptions(self, *, force: bool = False) -> list[str]:
        """Rebuild the subscriptions that are no longer usable, and return the reasons.

        Two different questions, answered by the same call:

        * the link is down, so every subscription the managers still believe they
          have is suspect - pass ``force=True``;
        * the link is fine, and only a subscription that can *say* it is damaged
          (deleted, reported with a bad ``SubscriptionStatus``, or silent because its
          notification queue was closed) is repaired.

        A subscription that says nothing is asked rather than rebuilt: silence is what a
        server-side delete looks like from the client, but it is also what a plant at
        steady state looks like, and only the server knows which one it is. See
        :attr:`silent_probe_limit`.

        A manager whose rebuild is still running is skipped: waiting for a transport
        to come back is not a new outage, and stacking rebuilds on top of each other
        creates subscriptions nobody asked for.

        A rebuild is not free: it costs a new SubscriptionId, new MonitoredItemIds,
        and a window in which no value is reported. Returning the reason instead of
        only logging it is what lets a historian record the gap rather than invent it.

        Args:
            force: Rebuild every manager that has monitored items, even when nothing
                reports damage.

        Returns:
            One reason per rebuilt manager, in registration order. Empty when nothing
            needed repair, which is the normal answer.
        """
        repaired: list[str] = []
        for manager in self._subscriptions:
            if manager.rebuild_in_progress:
                # A rebuild that is waiting for the transport to come back is not a new
                # outage. Stacking another one on it creates subscriptions nobody asked
                # for and turns recreations into a number that means nothing.
                _logger.debug("subscription rebuild already running, skipping this poll")
                continue
            reason = manager.rebuild_reason()
            if reason is None and not force:
                reason = await self._ask_about_silence(manager)
            if reason is None and force:
                if not manager.items:
                    # Nothing was ever monitored: rebuilding would create a
                    # subscription nobody asked for, on a loop.
                    continue
                reason = "the session was lost"
            if reason is None:
                continue
            _logger.warning("rebuilding subscription: %s", reason)
            try:
                await manager.rebuild()
            except (TimeoutError, UaStatusCodeError, UaError, OSError) as exc:
                _logger.warning("subscription rebuild failed: %s", _error_text(exc))
                continue
            if not manager.items:
                # Nothing came back: reporting this as a repair would claim a
                # subscription the manager does not have.
                _logger.warning("subscription rebuild restored no monitored items")
                continue
            self._repairs.append(reason)
            repaired.append(reason)
        return repaired

    async def _ask_about_silence(self, manager: SubscriptionManager) -> str | None:
        """Ask a subscription that has stopped reporting whether the server still has it.

        Silence is not damage. A monitored item reports a *change*, and a plant at steady
        state changes nothing for hours, so a supervisor that rebuilt on silence would
        recycle SubscriptionIds forever and still not tell an operator anything. What it
        can do is ask, at most once per window: the answer either says the subscription is
        alive, or it names the damage the server knows about.
        """
        limit = self._silent_probe_limit
        if limit is None or not manager.items:
            return None
        silence = manager.seconds_since_report()
        if silence is None or silence < limit:
            return None
        since_probe = manager.seconds_since_probe()
        if since_probe is not None and since_probe < limit:
            return None
        _logger.debug(
            "subscription %s has reported nothing for %.1fs, asking the server about it",
            manager.subscription_id,
            silence,
        )
        return await manager.probe_subscription()

    async def _publish(self) -> None:
        signature = (
            self._status.connected,
            self._status.healthy,
            self._status.operational,
            self._status.message,
        )
        if signature == self._last_signature:
            return
        self._last_signature = signature
        log_with(
            _logger,
            logging.INFO if self._status.connected else logging.ERROR,
            f"connection status: {self._status.state_text}",
            fields={key: str(value) for key, value in self._status.as_dict().items()},
        )
        for observer in self._observers:
            try:
                await observer(self._status)
            except Exception:
                _logger.exception("connection observer failed")

    async def _read_namespace_uris(self) -> tuple[str, ...]:
        results = await self._client.uaclient.read_attributes(
            [ua.NodeId(ua.ObjectIds.Server_NamespaceArray, 0)], ua.AttributeIds.Value
        )
        value = unwrap(results[0].Value)
        if value is None:
            return ()
        return tuple(str(uri) for uri in value)

    def describe_limits(self) -> str:
        """Summary of the limits currently in force."""
        if self._limits is None:
            return "limits not probed"
        return self._limits.status_note()

    def describe_health(self) -> str:
        """Summary of the last health probe."""
        return self._status.message or "not probed"

    def is_running_state(self) -> bool:
        """True when the last probe saw ``ServerState == Running``."""
        return bool(self._status.health and self._status.health.is_running)

    def service_level(self) -> int | None:
        """The ServiceLevel byte of the last probe."""
        return self._status.health.service_level if self._status.health else None

    def watched_nodes(self) -> list[NodeIdLike]:
        """NodeIds the supervisor depends on, useful for a smoke test."""
        return [
            ua.NodeId(ua.ObjectIds.Server_ServerStatus_State, 0),
            ua.NodeId(ua.ObjectIds.Server_ServiceLevel, 0),
            ua.NodeId(ua.ObjectIds.Server_NamespaceArray, 0),
        ]

    def first_application_namespace(self) -> int:
        """Index of the first namespace above 0 - the demo space in every chapter."""
        return 1 if len(self._namespaces) > 1 else 0

    def describe_subscriptions(self) -> str:
        """One note per watched subscription: what it holds and how long it has been quiet.

        The question this answers is the one an operator actually asks: "the link says
        healthy, so why is nothing arriving?" A subscription id, an item count, the silence
        and whatever the server last said about that subscription are the four numbers that
        separate a plant at steady state from a subscription the server threw away.
        """
        if not self._subscriptions:
            return "no subscriptions watched"
        notes: list[str] = []
        for manager in self._subscriptions:
            silence = manager.seconds_since_report()
            status = manager.subscription_status
            silence_text = "never reported" if silence is None else f"{silence:.1f}s"
            notes.append(
                f"subscription={manager.subscription_id} items={len(manager.items)} "
                f"silence={silence_text} status={status.name if status else 'none'} "
                f"damage={manager.rebuild_reason() or 'none'}"
            )
        return "; ".join(notes)

    def format_report(self) -> str:
        """One block of text for terminals and CI logs."""
        repairs = f"{len(self._repairs)} rebuilt"
        if self._repairs:
            repairs += f": {'; '.join(self._repairs)}"
        return "\n".join(
            [
                f"link       : {self._status.state_text}",
                f"health     : {self.describe_health()}",
                f"limits     : {self.describe_limits()}",
                f"namespaces : {', '.join(self._namespaces) or 'none'}",
                f"subscriptions: {self.describe_subscriptions()}",
                f"repairs    : {repairs}",
                f"elapsed    : {self._status.elapsed_ms:.1f} ms",
            ]
        )

    def watched_node_texts(self) -> str:
        """Comma separated text of the watched NodeIds."""
        return ", ".join(format_node_id(to_nodeid(node)) for node in self.watched_nodes())


def _error_text(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, (int, ua.StatusCode)):
        return _status_name(code)
    return str(exc) or type(exc).__name__


def _session_is_open(client: Client) -> bool:
    """Whether asyncua currently believes its session is usable.

    asyncua exposes the state as :class:`asyncua.client.ua_client.UaClientState`
    (``DISCONNECTED``, ``CONNECTING``, ``SOCKET_OPEN``, ``CHANNEL_OPEN``,
    ``CONNECTED``, ``RECONNECTING``, ``DISCONNECTING``), and the transport and
    session as booleans on the low level client. Only ``CONNECTED`` with both a
    transport and a session means "this client can serve a request right now":
    ``SOCKET_OPEN`` means the secure channel is not up yet, and ``RECONNECTING``
    means the server is gone.

    These are asyncua's own beliefs, and they are only half the answer: a client in the
    middle of a reconnect reports an activated session that no server will honour, so
    every caller here pairs this with what the failed request actually was.
    """
    uaclient = getattr(client, "uaclient", None)
    if uaclient is not None:
        has_transport = getattr(uaclient, "has_transport", None)
        has_session = getattr(uaclient, "has_session", None)
        if isinstance(has_transport, bool) and isinstance(has_session, bool):
            return has_transport and has_session
    state = getattr(client, "state", None)
    if state is None:
        return False
    return str(getattr(state, "value", state)).lower() == "connected"


#: Status codes that mean "the link or the session is gone", as opposed to "this one
#: read failed". The distinction decides whether a supervisor starts recovering.
_TRANSPORT_STATUS_NAMES = frozenset(
    {
        "BadCommunicationError",
        "BadConnectionClosed",
        "BadServerHalted",
        "BadServerUnavailable",
        "BadSessionClosed",
        "BadSessionIdInvalid",
    }
)


def _transport_is_down(exc: BaseException) -> bool:
    """Whether a failed probe means the link is gone, or only that this read failed.

    Two outages look identical in a log unless somebody decides which one happened: a
    server that answers ``BadNodeIdUnknown`` is a live link with a broken read, and a
    client whose socket died is a link that is gone. asyncua's own flags cannot settle
    it: through a reconnect it keeps a transport object and an activated session that
    the server stopped honouring long ago (``has_session`` stayed ``True`` the whole
    time the demo server was down), so a client that trusts those flags calls a dead
    server "connected" for as long as it keeps trying. What decides is the *kind* of
    failure, because a status code is an answer from a server and an ``OSError`` is the
    absence of one.
    """
    if isinstance(exc, OSError):
        # ConnectionError, ConnectionRefusedError and TimeoutError are all OSError
        # families: the request never reached a server that could answer.
        return True
    if not isinstance(exc, UaStatusCodeError):
        return False
    code = getattr(exc, "code", None)
    if code is None:
        return False
    return _status_name(code) in _TRANSPORT_STATUS_NAMES


def _status_name(code: Any) -> str:
    """The name of a status code held as an ``int`` or a ``ua.StatusCode``.

    asyncua hands out both, and :class:`ua.StatusCode` can be passed to neither ``int``
    nor another ``StatusCode``, so the number has to be read out of it first. The value
    is typed as it arrives rather than narrowed, because the only thing this does with it
    is hand it back to asyncua.
    """
    return str(ua.StatusCode(int(getattr(code, "value", code))).name)


__all__ = ["ConnectionStatus", "ConnectionSupervisor"]
