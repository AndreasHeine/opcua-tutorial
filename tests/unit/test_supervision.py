"""Chapter 12 unit tests: what a supervisor is allowed to conclude.

The transport cases live in the integration suite. These cover the two decisions the
supervision loop makes on its own:

* what a failed probe *was* - a link that is gone, or a live link with a broken read;
* what a quiet subscription *means* - a reason to ask the server, never a reason to
  rebuild, plus that a repair already running is not a second outage and a repair that
  restored nothing is not a repair.

Both are decisions a supervisor makes from evidence, and the stubs here are built so a
test cannot accidentally give the supervisor evidence it should not have.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import cast

from asyncua import Client
from asyncua.ua.uaerrors import BadNodeIdUnknown, BadSessionIdInvalid

from opcua_toolkit.resilience import ConnectionSupervisor
from opcua_toolkit.subscriptions import SubscriptionManager

#: Seconds of silence this suite treats as worth a question about.
SILENCE_WINDOW = 1.0

#: A subscription that has been quiet for longer than the window.
QUIET_FOR = 5.0


class StubSubscription:
    """A subscription that reports facts the way the real manager does, and nothing more.

    The real manager cannot know *why* a subscription went quiet, and this stub is built so
    that a test cannot give it knowledge it should not have: it answers only the questions a
    supervisor is allowed to ask. Its silence is a clock rather than a constant, because
    real silence grows.
    """

    def __init__(
        self,
        *,
        silent_for: float = QUIET_FOR,
        started: bool = True,
        probe_answer: str | None = None,
        monitored_items: int = 1,
        restores: bool = True,
        ever_reported: bool = True,
    ) -> None:
        self.items: list[str] = [f"tag-{index}" for index in range(monitored_items)]
        self.rebuild_in_progress = False
        self.subscription_id = 77
        self.probes = 0
        self.rebuilds = 0
        self._probe_answer = probe_answer
        self._restores = restores
        now = datetime.now(UTC)
        self._started_at = None if not started else now - timedelta(seconds=silent_for)
        # A subscription that has never reported anything measures its silence from the
        # moment it was created, which is the only honest reference it has.
        self._last_report_at = self._started_at if ever_reported else None
        self._last_probe_at: datetime | None = None

    def seconds_since_report(self) -> float | None:
        reference = self._last_report_at or self._started_at
        if reference is None:
            return None
        return (datetime.now(UTC) - reference).total_seconds()

    def seconds_since_probe(self) -> float | None:
        if self._last_probe_at is None:
            return None
        return (datetime.now(UTC) - self._last_probe_at).total_seconds()

    def rebuild_reason(self) -> str | None:
        """Nothing is known until something is asked."""
        return None

    async def probe_subscription(self) -> str | None:
        self.probes += 1
        self._last_probe_at = datetime.now(UTC)
        return self._probe_answer

    async def rebuild(self) -> list[str]:
        self.rebuilds += 1
        if not self._restores:
            self.items = []
            return []
        return list(self.items)


def supervisor_for(
    *subscriptions: StubSubscription, silent_probe_limit: float | None = SILENCE_WINDOW
) -> ConnectionSupervisor:
    """A supervisor pointed at stubs. Repairing a subscription never needs the transport.

    The cast is what the test is doing on purpose: the supervisor only ever asks a
    subscription the questions a real manager can answer, so a stub that answers those
    questions is enough to hold its decisions to account.
    """
    watched = cast("Iterable[SubscriptionManager]", subscriptions)
    return ConnectionSupervisor(
        cast("Client", object()), subscriptions=watched, silent_probe_limit=silent_probe_limit
    )


def as_manager(subscription: StubSubscription) -> SubscriptionManager:
    """One stub, presented as the type a supervisor registers.

    Same reasoning as :func:`supervisor_for`, for the tests that add or remove a
    subscription directly instead of building a supervisor around it.
    """
    return cast("SubscriptionManager", subscription)


async def test_a_quiet_subscription_is_asked_before_it_is_rebuilt() -> None:
    """The supervisor's part is the asking; the server's part is the answer."""
    quiet = StubSubscription(probe_answer="the server no longer has this subscription")
    supervisor = supervisor_for(quiet)

    reasons = await supervisor.repair_subscriptions()

    assert quiet.probes == 1
    assert quiet.rebuilds == 1
    assert reasons == ["the server no longer has this subscription"]
    assert supervisor.subscription_repairs == tuple(reasons)


async def test_a_quiet_subscription_the_server_still_holds_is_left_alone() -> None:
    """Steady state: the question gets asked, and the answer is respected.

    This is the test that keeps a supervisor from becoming a subscription churn machine.
    A plant that changes nothing is not a plant that is broken.
    """
    steady = StubSubscription(probe_answer=None)
    supervisor = supervisor_for(steady)

    reasons = await supervisor.repair_subscriptions()

    assert steady.probes == 1
    assert steady.rebuilds == 0
    assert reasons == []
    assert supervisor.subscription_repairs == ()


async def test_silence_shorter_than_the_window_is_not_asked_about() -> None:
    """A subscription that has barely had time to report is not a suspect."""
    recently = StubSubscription(silent_for=0.0)
    supervisor = supervisor_for(recently, silent_probe_limit=30.0)

    assert await supervisor.repair_subscriptions() == []
    assert recently.probes == 0
    assert recently.rebuilds == 0


async def test_the_question_is_asked_once_per_window_not_once_per_poll() -> None:
    """Polling quickly must not turn into asking the server the same thing repeatedly."""
    steady = StubSubscription(probe_answer=None)
    supervisor = supervisor_for(steady)

    await supervisor.repair_subscriptions()
    await supervisor.repair_subscriptions()
    await supervisor.repair_subscriptions()

    assert steady.probes == 1
    assert steady.rebuilds == 0


async def test_a_supervisor_that_is_told_not_to_ask_never_rebuilds_on_silence() -> None:
    """``silent_probe_limit=None``: silence is not evidence, so nothing is concluded."""
    steady = StubSubscription(probe_answer="the server no longer has this subscription")
    supervisor = supervisor_for(steady, silent_probe_limit=None)

    reasons = await supervisor.repair_subscriptions()

    assert steady.probes == 0
    assert steady.rebuilds == 0
    assert reasons == []


def test_the_window_that_decides_when_to_ask_is_readable() -> None:
    """The silence window decides behaviour, so it has to be visible from the outside.

    "Will this supervisor treat a quiet tag as an outage?" is a question an operator or a
    health endpoint has to be able to answer without reading the source, and
    :attr:`None` (never ask) versus a number (ask after this long) is the whole difference.
    """
    quiet = StubSubscription()

    assert supervisor_for(quiet).silent_probe_limit == SILENCE_WINDOW
    assert supervisor_for(quiet, silent_probe_limit=None).silent_probe_limit is None


async def test_a_subscription_that_never_started_is_not_asked_about() -> None:
    """There is no such thing as a quiet subscription that was never running."""
    never_started = StubSubscription(started=False, monitored_items=0)
    supervisor = supervisor_for(never_started)

    assert never_started.seconds_since_report() is None
    assert await supervisor.repair_subscriptions() == []
    assert await supervisor.repair_subscriptions(force=True) == []
    assert never_started.probes == 0
    assert never_started.rebuilds == 0


async def test_a_subscription_with_nothing_monitored_is_not_asked_about() -> None:
    """A manager nobody subscribed anything on has no subscription to be quiet about."""
    empty = StubSubscription(monitored_items=0)
    supervisor = supervisor_for(empty)

    assert await supervisor.repair_subscriptions() == []
    assert empty.probes == 0
    assert empty.rebuilds == 0


async def test_a_retired_subscription_stops_being_asked_about() -> None:
    """A subscription that has been stopped is taken off the watch list.

    A gateway that retires a tag group stops its subscription, and a supervisor that keeps
    watching it spends every silence window asking a server about a subscription id that no
    longer exists - and prints it in every status line as a subscription nobody is
    collecting. Removing a manager that was never watched is not an error either: retiring
    the same group twice is a normal thing for a config reload to do.
    """
    retired = StubSubscription(probe_answer=None)
    supervisor = supervisor_for(retired)

    supervisor.remove_subscription(as_manager(retired))
    supervisor.remove_subscription(as_manager(retired))

    assert await supervisor.repair_subscriptions() == []
    assert retired.probes == 0
    assert supervisor.describe_subscriptions() == "no subscriptions watched"


async def test_a_subscription_is_only_watched_once() -> None:
    """Registering the same subscription twice would mean rebuilding it twice.

    Two entries for one manager also mean two questions per silence window, which is the
    quiet way a supervision loop doubles its own load.
    """
    watched = StubSubscription(probe_answer="the server no longer has this subscription")
    supervisor = supervisor_for(watched)

    supervisor.add_subscription(as_manager(watched))
    reasons = await supervisor.repair_subscriptions()

    assert watched.probes == 1
    assert watched.rebuilds == 1
    assert reasons == ["the server no longer has this subscription"]


async def test_a_repair_already_running_is_not_counted_as_a_second_outage() -> None:
    """A repair that is waiting for a link outlives several polls, and polls respect it."""
    waiting = StubSubscription()
    waiting.rebuild_in_progress = True
    supervisor = supervisor_for(waiting)

    assert await supervisor.repair_subscriptions(force=True) == []
    assert waiting.rebuilds == 0
    assert supervisor.subscription_repairs == ()


async def test_a_repair_that_restored_nothing_is_not_reported_as_a_repair() -> None:
    """Reporting a repair the subscription did not survive would invent data that does not exist."""
    failed = StubSubscription(
        probe_answer="the server no longer has this subscription", restores=False
    )
    supervisor = supervisor_for(failed)

    assert await supervisor.repair_subscriptions() == []
    assert failed.rebuilds == 1
    assert supervisor.subscription_repairs == ()


async def test_a_lost_link_repairs_every_subscription_that_is_monitoring_something() -> None:
    """``force=True`` is the link-down case: nothing the managers believe is trustworthy."""
    first = StubSubscription(silent_for=0.0)
    second = StubSubscription(silent_for=0.0)
    empty = StubSubscription(monitored_items=0)
    supervisor = supervisor_for(first, second, empty)

    reasons = await supervisor.repair_subscriptions(force=True)

    assert reasons == ["the session was lost", "the session was lost"]
    # A link that is down is not a reason to ask: it is a reason to rebuild.
    assert first.probes == 0
    assert second.probes == 0
    assert empty.rebuilds == 0


class StubUaClient:
    """A client whose every request fails, holding the two flags asyncua reports.

    These tests are about a supervisor reading those flags, so the stub has to be able to
    claim what a real client claims in the middle of a reconnect: a session it believes is
    activated and no transport to carry it.
    """

    def __init__(self, failure: BaseException, *, transport: bool, session: bool) -> None:
        self._failure = failure
        self.has_transport = transport
        self.has_session = session

    async def read_attributes(self, *args: object, **kwargs: object) -> list[object]:
        raise self._failure


class StubClient:
    """A client object the supervisor will accept, with a probe that always fails."""

    def __init__(
        self, failure: BaseException, *, transport: bool = False, session: bool = True
    ) -> None:
        self.uaclient = StubUaClient(failure, transport=transport, session=session)


def supervisor_for_client(client: StubClient) -> ConnectionSupervisor:
    """A supervisor pointed at a client whose probe fails in a chosen way."""
    return ConnectionSupervisor(cast("Client", client))


async def test_a_probe_nobody_answered_is_called_a_lost_link_even_if_the_client_disagrees() -> None:
    """A connection error is evidence nobody answered, whatever the session flag says.

    This is the trap the supervisor exists to avoid: asyncua keeps an activated session
    marked usable while its socket is gone, and a supervisor that copied that flag would
    report a dead server as connected for as long as it kept probing it.
    """
    supervisor = supervisor_for_client(StubClient(ConnectionError("Connection is closed")))

    status = await supervisor.probe()

    assert status.connected is False
    assert status.state_text == "disconnected (Connection is closed)"


async def test_a_session_the_server_stopped_honouring_is_a_lost_link_too() -> None:
    """Some status codes are the server saying the link is over, not that one read failed.

    ``BadSessionIdInvalid`` is that sentence, and it arrives with the client's own flags
    still reading "connected", which is why the classification has to be made here rather
    than asked of the client.
    """
    supervisor = supervisor_for_client(
        StubClient(BadSessionIdInvalid(), transport=True, session=True)
    )

    status = await supervisor.probe()

    assert status.connected is False
    assert status.message == "BadSessionIdInvalid"


async def test_a_probe_the_server_answered_badly_is_still_a_link() -> None:
    """A server that refuses one read is a server, and treating it as an outage is wrong.

    ``BadNodeIdUnknown`` means the probe's node is gone, not the connection. Calling that
    a lost link would tear down working subscriptions over a typo in a node id.
    """
    supervisor = supervisor_for_client(StubClient(BadNodeIdUnknown(), transport=True, session=True))

    status = await supervisor.probe()

    assert status.connected is True
    assert status.operational is False
    assert status.state_text == "connected but not usable: BadNodeIdUnknown"


async def test_repeated_failures_count_up_rather_than_resetting() -> None:
    """The attempt counter is how a retry policy and an operator both read an outage.

    A supervisor that reported the same attempt number forever would make a dashboard look
    frozen, and one that reset it on every poll would hide that the link has been down for
    as long as it has. Clearing the counter is a successful probe's job, which the
    integration suite asserts against a real server.
    """
    supervisor = supervisor_for_client(StubClient(ConnectionError("Connection is closed")))

    attempts = [(await supervisor.probe()).attempts for _ in range(3)]

    assert attempts == [1, 2, 3]
