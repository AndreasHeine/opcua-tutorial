"""Chapter 12: supervision - four problems, four reactions.

asyncua already reconnects: ``client.connect(auto_reconnect=True)`` re-establishes
the secure channel, the session, and the subscriptions it still believes are open.
What it cannot do is tell your application anything - no log line to alert on, no
health signal for a dashboard, no pause for a historian while the values are stale,
and no decision about whether the server came back in a state worth collecting from.

:class:`~opcua_toolkit.ConnectionSupervisor` is the thin deciding layer on top:

* every probe reads ``ServerState``, ``ServiceLevel`` and the namespace array;
* **healthy**, **degraded**, **connected but unusable** and **disconnected** are four
  states, because tearing down a working subscription over a degraded server creates
  an outage of your own;
* observers are notified once per change, which is where a historian pauses and a
  dashboard turns yellow;
* monitored items the server actually dropped get rebuilt - and a subscription you
  stopped yourself is never resurrected;
* **a usable link is not a usable subscription.** A server can delete one on its own
  while the session stays open, and the only trace is silence - so the supervisor *asks*
  a subscription that has gone quiet (see :attr:`ConnectionSupervisor.silent_probe_limit`)
  and rebuilds only on the answer. Steady state is not an outage, and a supervisor that
  treated it as one would recycle subscriptions forever.

Run it:

    uv run python examples/ch12_supervision.py --demo

This one changes the server on purpose, so it needs the demo server. Expect asyncua's
own warning logs while the link is down: that noise is what the supervisor turns into
a state an operator can read.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from asyncua import ua

from _support import bullet, chapter_run, heading
from opcua_toolkit import ConnectionStatus, ConnectionSupervisor, RetryPolicy, SubscriptionManager

#: How long the example waits for the recovered link before it gives up.
RECOVERY_TIMEOUT = 30.0

#: Seconds between supervision probes.
POLL_INTERVAL = 1.0

#: How long a subscription may stay quiet before the supervisor asks the server about it.
#: The real default is 60 seconds: this demo waits seconds instead of minutes, which is
#: the only reason the number is changed here.
SILENCE_WINDOW = 2.0


class Reporter:
    """An observer that prints each decision the supervisor makes."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.changed = asyncio.Event()

    async def __call__(self, status: ConnectionStatus) -> None:
        """Record one state change, and wake anything that is waiting for it."""
        self.texts.append(status.state_text)
        self.changed.set()
        print(f"  observer -> {status.state_text} [{status.message}]")


def _status_text(manager: SubscriptionManager) -> str:
    """What the server last said about one subscription, as a name rather than a number.

    ``ua.StatusCode`` prints as ``StatusCode(value=2150105088)``: that is the same fact a
    log reader has to look up in a spec table, which is why the toolkit reports
    ``status.name`` everywhere a human reads it, and keeps the number for comparisons.
    """
    status = manager.subscription_status
    return status.name if status is not None else "nothing reported"


async def _wait_until_usable(
    reporter: Reporter, supervisor: ConnectionSupervisor, max_wait: float
) -> bool:
    """Wait for the link to be worth collecting from, or for patience to run out.

    ``connected`` alone is not enough, and the outage below shows how a caller gets hurt
    by trusting it. asyncua's own view of a client that lost the server is a transport
    that is gone but a session that is still marked activated, so a supervisor that only
    asked asyncua "do you have a session?" would call a dead server connected and wait
    for values from it. The supervisor instead asks what the failed probe *was*: a status
    code is a server answering, an ``OSError`` is nobody answering.

    This waits on the observer instead of sleeping blind: the example wakes when the
    supervisor decides something changed, which is the same signal a historian or a
    dashboard keys on. A test asserts on it the same way.
    """
    deadline = time.monotonic() + max_wait
    while not (supervisor.status.connected and supervisor.status.operational):
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        reporter.changed.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(reporter.changed.wait(), timeout=min(remaining, POLL_INTERVAL))
    return True


async def main() -> int:
    """Push the server through healthy, degraded, maintenance and an outage."""
    async with chapter_run(
        "classify a healthy server, a degraded server, and an outage",
        session_name="Chapter 12 - supervision",
    ) as run:
        if not run.uses_demo_server:
            print("this example stops and restarts the server, so it needs --demo")
            return 2

        async with run.open_client(auto_reconnect=True) as client:
            limits = await run.read_limits(client)
            manager = SubscriptionManager(client, limits=limits, publishing_interval=250.0)
            supervisor = ConnectionSupervisor(
                client,
                subscriptions=[manager],
                poll_interval=POLL_INTERVAL,
                retry=RetryPolicy(initial_delay=0.5, max_delay=4.0),
                silent_probe_limit=SILENCE_WINDOW,
            )
            reporter = Reporter()
            supervisor.add_observer(reporter)
            tags = list(run.demo_server.tags)[:2]
            await manager.subscribe([run.demo_server.tags[name] for name in tags])

            heading("1. a probe is a classification, not a ping")
            status = await supervisor.probe()
            bullet(status.state_text)
            bullet(f"message  : {status.message}")
            bullet(f"limits   : {supervisor.describe_limits()}")
            bullet(f"health   : {supervisor.describe_health()}")
            for line in supervisor.format_report().splitlines():
                bullet(line)

            heading("2. degraded is not broken")
            await run.demo_server.set_service_level(120)
            status = await supervisor.probe()
            bullet(status.state_text)
            bullet(
                f"healthy={status.healthy} operational={status.operational} "
                f"ServiceLevel={supervisor.service_level()}"
            )
            bullet(
                f"subscriptions rebuilt: {manager.stats.recreations} - a degraded server "
                "keeps its subscriptions, and it should"
            )

            heading("3. connected, running, and still not worth collecting from")
            await run.demo_server.set_service_level(0)
            status = await supervisor.probe()
            bullet(status.state_text)
            bullet(
                f"operational={status.operational} - ServiceLevel 0 means the server is in "
                "maintenance: reads work, but a collector that keeps storing them is "
                "recording stale data as if it were live"
            )

            heading("4. a server that says it is shutting down")
            await run.demo_server.set_server_state(ua.ServerState.Shutdown)
            status = await supervisor.probe()
            bullet(status.state_text)
            bullet(f"running state: {supervisor.is_running_state()}")
            await run.demo_server.set_server_state(ua.ServerState.Running)
            await run.demo_server.set_service_level(255)

            heading("5. the link goes away and comes back")
            await supervisor.start()
            await asyncio.sleep(POLL_INTERVAL)
            bullet(f"before the outage: {supervisor.status.state_text}")
            seen_before = len(reporter.texts)
            await run.restart_demo_server()
            bullet("the demo server stopped and started again on the same port")
            recovered = await _wait_until_usable(reporter, supervisor, RECOVERY_TIMEOUT)
            bullet(f"after the outage : {supervisor.status.state_text}")
            bullet(f"recovered        : {recovered}")
            states = reporter.texts[seen_before:]
            bullet(f"states the observer saw: {states}")
            bullet(
                "the supervisor calls an outage what the failed probe actually was: a "
                "status code is a server that answered, an OSError is nobody answering, "
                "and asyncua's own 'I have a session' flag survives the server being gone"
            )
            await supervisor.stop()

            heading("6. a link that is fine and a subscription that is gone")
            # The outage a gateway can neither schedule nor notice from its own side: the
            # session survives, every health probe says healthy, and this subscription is
            # simply not there any more. Nothing arrives, nothing fails, no counter moves.
            working = SubscriptionManager(client, limits=limits, publishing_interval=250.0)
            supervisor.add_subscription(working)
            untouched = manager.stats.recreations
            try:
                await working.subscribe([run.demo_server.tags[tags[1]]])
                await working.wait_for_notifications(1, max_wait=10.0)
                forgotten = working.subscription_id
                bullet(
                    f"subscription {forgotten} is live and reporting: "
                    f"{working.stats.notifications} values so far"
                )
                answer = await run.demo_server.drop_subscription(int(forgotten or 0))
                bullet(f"the server deleted it from its own side and answered {answer}")
                await asyncio.sleep(POLL_INTERVAL)
                bullet(f"what the client can see on its own: reason={working.rebuild_reason()}")
                bullet(f"status={_status_text(working)} errors={working.stats.errors}")
                bullet(f"the supervisor's own view: {supervisor.describe_subscriptions()}")
                bullet(
                    "a monitored item reports a *change*, so silence is not evidence: it is "
                    "a reason to ask, and ModifySubscription with the numbers already in use "
                    "is the cheapest question there is"
                )
                asked = await working.probe_subscription()
                bullet(f"asking found: {asked}")
                bullet(f"the server's answer is now on record: {_status_text(working)}")
                repaired = await supervisor.repair_subscriptions()
                bullet(f"the supervisor repaired only what the answer named: {repaired}")
                bullet(
                    f"subscription {working.subscription_id} now holds {len(working.items)} "
                    f"monitored items after {working.stats.recreations} recreations"
                )
                bullet(
                    f"the subscription the answer did not name kept its id and its items: "
                    f"{manager.subscription_id} recreations {untouched} -> "
                    f"{manager.stats.recreations}"
                )
                record = await working.wait_for_value(run.demo_server.tags[tags[1]], max_wait=10.0)
                bullet(f"values arriving again: {record.node_id_text} = {record.value!r}")
            finally:
                await working.stop()
                supervisor.remove_subscription(working)

            heading("7. a subscription that is quiet because nothing is changing")
            steady_key = "RecipeName"
            steady = SubscriptionManager(client, limits=limits, publishing_interval=250.0)
            supervisor.add_subscription(steady)
            await steady.subscribe([run.demo_server.tags[steady_key]])
            await steady.wait_for_notifications(1, max_wait=10.0)
            held = steady.subscription_id
            try:
                bullet(
                    f"{steady_key} never changes on this server, so this subscription goes "
                    f"quiet on its own: {supervisor.describe_subscriptions()}"
                )
                reasons = await supervisor.repair_subscriptions()
                bullet(f"it asked the server and rebuilt nothing: {reasons}")
                bullet(f"still the same subscription id: {held == steady.subscription_id}")
                bullet(f"recreations: {steady.stats.recreations}")
                bullet(
                    "it asked once per silence window, not once per poll: the last probe was "
                    f"{round(steady.seconds_since_probe() or 0.0, 1)}s ago"
                )
                bullet(f"the supervisor's view now: {supervisor.describe_subscriptions()}")
            finally:
                await steady.stop()
                supervisor.remove_subscription(steady)
            bullet(
                "and a retired subscription is taken off the watch list too: a stopped one "
                "that stays on it is asked about forever, and shows up in every status line "
                f"as a subscription nobody is collecting: {supervisor.describe_subscriptions()}"
            )

            heading("8. what the supervisor did while it was down")
            bullet(f"observer notifications: {len(reporter.texts)}")
            for text in reporter.texts:
                bullet(f"  {text}")
            bullet(
                f"monitored items now: {manager.stats.monitored_items}, "
                f"rebuilds: {manager.stats.recreations}"
            )
            bullet(manager.stats.summary())
            if manager.stats.monitored_items:
                record = await manager.wait_for_value(run.demo_server.tags[tags[0]], max_wait=10.0)
                bullet(f"values are flowing again: {record.node_id_text} = {record.value!r}")

            heading("building block")
            bullet(
                "ConnectionSupervisor.probe / watch / start / add_observer / "
                "add_subscription / remove_subscription / repair_subscriptions / format_report"
            )
            bullet(
                "asyncua's reconnect keeps the transport alive: the supervisor decides "
                "what that means for the application"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
