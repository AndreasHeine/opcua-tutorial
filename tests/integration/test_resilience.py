"""Chapter 12 integration tests: supervision turns protocol facts into decisions.

A supervisor is only useful if its four states are distinguishable, so each one
is produced on purpose here: a healthy server, a degraded one (ServiceLevel 120),
one that refuses to serve data (ServiceLevel 0), and a link that is gone. The
demo server can be pushed into the first three; the fourth is a client the test
disconnected.
"""

from __future__ import annotations

import asyncio

import pytest
from asyncua import ua

from opcua_toolkit.errors import NotConnectedError
from opcua_toolkit.resilience import ConnectionStatus, ConnectionSupervisor
from opcua_toolkit.settings import LimitsOverrides
from opcua_toolkit.subscriptions.manager import SubscriptionManager

pytestmark = pytest.mark.integration


class Observer:
    """Records every status change the supervisor reports."""

    def __init__(self) -> None:
        self.seen: list[ConnectionStatus] = []

    async def __call__(self, status: ConnectionStatus) -> None:
        self.seen.append(status)

    @property
    def texts(self) -> list[str]:
        return [status.state_text for status in self.seen]


def reported_status(manager: SubscriptionManager) -> ua.StatusCode | None:
    """What the server last said about this subscription, read through a call.

    These tests say both "nothing has been reported" and later "this has been reported",
    and reading the optional property directly lets the type checker hold on to the first
    answer for the rest of the test.
    """
    return manager.subscription_status


async def test_a_healthy_probe_reports_the_link_the_server_state_and_the_limits(
    client, demo_server
) -> None:
    supervisor = ConnectionSupervisor(client)
    status = await supervisor.probe()

    assert status.connected
    assert status.healthy
    assert status.operational
    assert status.attempts == 0
    assert status.state_text == "connected and healthy"
    assert "ServiceLevel=255" in status.message
    assert status.health is not None
    assert status.health.state is ua.ServerState.Running
    assert status.elapsed_ms >= 0.0

    assert supervisor.describe_limits() == "server published all operation limits"
    assert supervisor.limits.max_nodes_per_read == demo_server.config.max_nodes_per_read
    assert demo_server.namespace_uri in supervisor.namespaces
    assert supervisor.first_application_namespace() >= 1
    assert supervisor.is_running_state()
    assert supervisor.service_level() == 255


async def test_the_supervisor_probes_the_variables_that_describe_the_server(client) -> None:
    supervisor = ConnectionSupervisor(client)
    assert len(supervisor.watched_nodes()) == 3
    texts = supervisor.watched_node_texts()
    assert texts.count("ns=0;i=") == 3
    assert "ns=0;i=2259" in texts  # Server/serverStatus/state
    assert "ns=0;i=2267" in texts  # Server/ServiceLevel
    assert "ns=0;i=2255" in texts  # Server_NamespaceArray


async def test_limits_are_unavailable_until_a_probe_has_read_them(client) -> None:
    supervisor = ConnectionSupervisor(client)
    assert supervisor.status.state_text == "disconnected (not probed)"
    assert supervisor.describe_limits() == "limits not probed"
    assert supervisor.describe_health() == "not probed"
    with pytest.raises(NotConnectedError, match="probe"):
        supervisor.limits  # noqa: B018 - reading the property is what the test is about


async def test_a_degraded_server_is_reported_as_degraded_and_still_usable(
    client, demo_server
) -> None:
    await demo_server.set_service_level(120)
    supervisor = ConnectionSupervisor(client)
    status = await supervisor.probe()

    assert status.connected
    # "degraded" and "usable" are separate facts: OPC 10000-4 puts 120 in the
    # 2..199 band, which means reduced quality, not a dead server.
    assert not status.healthy
    assert status.operational
    assert status.state_text.startswith("connected but degraded:")
    assert supervisor.service_level() == 120
    assert supervisor.status.as_dict()["service_level_band"] == "degraded"


async def test_a_server_in_maintenance_is_connected_but_not_usable(client, demo_server) -> None:
    await demo_server.set_service_level(0)
    supervisor = ConnectionSupervisor(client)
    status = await supervisor.probe()

    assert status.connected
    assert not status.healthy
    assert not status.operational
    assert "not usable" in status.state_text
    assert "maintenance" in status.state_text


async def test_a_server_that_is_shutting_down_is_never_reported_as_usable(
    client, demo_server
) -> None:
    await demo_server.set_server_state(ua.ServerState.Shutdown)
    supervisor = ConnectionSupervisor(client)
    status = await supervisor.probe()

    assert not status.healthy
    assert not status.operational
    assert not supervisor.is_running_state()
    assert "Shutdown" in status.message


async def test_a_lost_transport_is_classified_as_a_reconnect_problem(client, demo_server) -> None:
    supervisor = ConnectionSupervisor(client)
    await supervisor.probe()
    await client.disconnect()

    status = await supervisor.probe()
    assert not status.connected
    assert not status.healthy
    assert not status.operational
    assert status.state_text.startswith("disconnected (")
    assert status.attempts == 1
    assert status.health is None
    assert supervisor.service_level() is None


async def test_observers_are_notified_once_per_change_not_once_per_probe(
    client, demo_server
) -> None:
    supervisor = ConnectionSupervisor(client)
    observer = Observer()
    supervisor.add_observer(observer)

    await supervisor.probe()
    await supervisor.probe()
    assert observer.texts == ["connected and healthy"]

    await demo_server.set_service_level(120)
    await supervisor.probe()
    assert len(observer.seen) == 2
    assert observer.texts[1].startswith("connected but degraded:")

    await demo_server.set_service_level(0)
    await supervisor.probe()
    assert len(observer.seen) == 3
    assert "not usable" in observer.texts[2]


async def test_an_observer_that_crashes_does_not_take_the_probe_with_it(client) -> None:
    reported: list[ConnectionStatus] = []

    async def broken_observer(status: ConnectionStatus) -> None:
        raise RuntimeError("dashboard is down")

    async def healthy_observer(status: ConnectionStatus) -> None:
        reported.append(status)

    supervisor = ConnectionSupervisor(client)
    supervisor.add_observer(broken_observer)
    supervisor.add_observer(healthy_observer)

    status = await supervisor.probe()
    assert status.healthy
    assert len(reported) == 1


async def test_a_background_supervisor_can_be_started_and_stopped(client) -> None:
    supervisor = ConnectionSupervisor(client, poll_interval=0.2)
    observer = Observer()
    supervisor.add_observer(observer)

    await supervisor.start()
    await supervisor.start()  # idempotent: supervision is not a fan-out of tasks
    await asyncio.sleep(0.6)
    assert supervisor.status.healthy

    await supervisor.stop()
    await supervisor.stop()
    assert observer.texts == ["connected and healthy"]


async def test_supervision_keeps_polling_a_degraded_server_instead_of_rebuilding(
    client, demo_server
) -> None:
    await demo_server.set_service_level(150)
    manager = SubscriptionManager(client, publishing_interval=200.0)
    supervisor = ConnectionSupervisor(client, subscriptions=[manager], poll_interval=0.2)
    await manager.subscribe(["ns=2;s=Temperature"])
    subscription_id = manager.subscription_id

    await supervisor.start()
    await asyncio.sleep(0.6)
    await supervisor.stop()
    await manager.stop()

    # Degraded is not broken: the supervisor must not tear a working subscription down,
    # even though it now inspects every subscription on every poll.
    assert manager.subscription_id is None
    assert manager.stats.recreations == 0
    assert supervisor.subscription_repairs == ()
    assert subscription_id is not None


async def test_the_supervisor_recreates_monitored_items_the_server_dropped(
    client, demo_server
) -> None:
    manager = SubscriptionManager(client, publishing_interval=200.0)
    supervisor = ConnectionSupervisor(client, subscriptions=[manager], poll_interval=0.2)
    await manager.subscribe(["ns=2;s=Temperature", "ns=2;s=RecipeName"])
    old_id = manager.subscription_id

    # Simulate the server dropping the subscription: the manager still believes it
    # has its items, and the supervisor is what puts them back.
    dropped = manager._subscription
    assert dropped is not None
    await dropped.delete()

    reasons = await supervisor.repair_subscriptions()
    assert reasons == ["the server no longer has this subscription"]
    assert supervisor.subscription_repairs == tuple(reasons)
    assert manager.subscription_id is not None
    assert manager.subscription_id != old_id
    assert manager.stats.recreations == 1
    assert len(manager.items) == 2

    await demo_server.set_value("RecipeName", "rebuilt-recipe")
    assert await manager.wait_idle(max_wait=5.0)
    await manager.stop()


async def test_a_healthy_link_repairs_a_subscription_the_server_deleted_on_its_own(
    client, demo_server
) -> None:
    """The session survives and the subscription does not, and that is its own outage.

    Supervising only the transport is the half of Chapter 12 people implement and
    then wonder about: the client stays connected, every probe says "healthy", and
    the collector receives nothing because the server deleted this subscription - a
    lifetime it was never told about, or a maintenance window that outlived it.
    """
    manager = SubscriptionManager(client, publishing_interval=200.0)
    supervisor = ConnectionSupervisor(client, subscriptions=[manager], poll_interval=0.2)
    await manager.subscribe(["ns=2;s=Temperature"])
    await manager.wait_for_notifications(1, max_wait=10.0)
    old_id = manager.subscription_id

    dropped = manager._subscription
    assert dropped is not None
    await dropped.delete()

    await supervisor.start()
    try:
        for _ in range(50):
            await asyncio.sleep(0.2)
            if manager.stats.recreations >= 1:
                break
    finally:
        await supervisor.stop()

    # The supervisor never once called the link unusable: the repair happened on a
    # server every probe reported as healthy.
    assert supervisor.status.connected
    assert supervisor.status.healthy
    assert supervisor.subscription_repairs == ("the server no longer has this subscription",)
    assert manager.subscription_id is not None
    assert manager.subscription_id != old_id
    assert manager.stats.monitored_items == 1

    record = await manager.wait_for_value("ns=2;s=Temperature", max_wait=10.0)
    assert record.value is not None
    await manager.stop()


async def test_a_subscription_the_server_forgot_is_found_by_asking_not_by_guessing(
    client, demo_server
) -> None:
    """A server that forgets a subscription leaves the client no symptom but silence.

    This is the outage a gateway can neither schedule nor see coming: a restart, a
    lifetime it was never told about, maintenance. The session stays open, every health
    probe says healthy, no error is ever raised, and the only trace is the answer to a
    question the client has to decide to ask.
    """
    manager = SubscriptionManager(client, publishing_interval=200.0)
    await manager.subscribe(["ns=2;s=Temperature"])
    await manager.wait_for_notifications(1, max_wait=10.0)
    forgotten = manager.subscription_id
    assert forgotten is not None

    assert await demo_server.drop_subscription(forgotten) == "Good"

    # Nothing has happened as far as the client can tell, which is the whole problem.
    assert manager.rebuild_reason() is None
    assert reported_status(manager) is None

    supervisor = ConnectionSupervisor(
        client,
        subscriptions=[manager],
        poll_interval=0.2,
        silent_probe_limit=0.5,
    )

    reasons: list[str] = []
    for _ in range(10):
        await asyncio.sleep(0.3)
        reasons.extend(await supervisor.repair_subscriptions())
        if reasons:
            break

    assert reasons == ["the server no longer has this subscription"]
    # The reason came from an answer the client chose to ask for: asyncua's own session
    # publish loop does get a fault from a server that dropped the subscription, and it
    # retries that fault inside the session where no subscription-level client can see it.
    assert manager.stats.bad_status_changes == 1
    assert manager.subscription_id is not None
    assert manager.subscription_id != forgotten
    assert manager.stats.recreations == 1
    assert manager.items
    # The verdict belonged to the subscription that got deleted, not to this one.
    assert reported_status(manager) is None
    # The point of repairing is that values arrive again, not that a counter moved.
    record = await manager.wait_for_value("ns=2;s=Temperature", max_wait=10.0)
    assert record.value is not None
    await manager.stop()


async def test_a_steady_value_is_asked_about_and_left_alone(client, demo_server) -> None:
    """Silence with nothing wrong behind it must not become churn.

    ``RecipeName`` is a tag the demo server never rewrites, so its subscription goes quiet
    on its own. The supervisor asks, is told the subscription is fine, and leaves it
    running: the same SubscriptionId, no recreations, no reported gap.
    """
    manager = SubscriptionManager(client, publishing_interval=200.0)
    supervisor = ConnectionSupervisor(
        client,
        subscriptions=[manager],
        poll_interval=0.2,
        silent_probe_limit=0.5,
    )
    await manager.subscribe(["ns=2;s=RecipeName"])
    await manager.wait_for_notifications(1, max_wait=10.0)
    held = manager.subscription_id

    await supervisor.start()
    await asyncio.sleep(1.6)
    await supervisor.stop()

    assert manager.seconds_since_probe() is not None
    assert supervisor.subscription_repairs == ()
    assert manager.stats.recreations == 0
    assert manager.subscription_id == held
    assert manager.subscription_status is None
    await manager.stop()


async def test_a_healthy_link_does_not_rebuild_a_subscription_that_is_fine(
    client, demo_server
) -> None:
    """Inspecting every subscription every poll must not turn into rebuilding."""
    manager = SubscriptionManager(client, publishing_interval=200.0)
    supervisor = ConnectionSupervisor(client, subscriptions=[manager], poll_interval=0.2)
    await manager.subscribe(["ns=2;s=Temperature"])

    await supervisor.start()
    await asyncio.sleep(1.0)
    await supervisor.stop()
    await manager.stop()

    assert supervisor.subscription_repairs == ()
    assert manager.stats.recreations == 0


async def test_a_subscription_the_caller_stopped_is_not_resurrected(client) -> None:
    manager = SubscriptionManager(client, publishing_interval=200.0)
    supervisor = ConnectionSupervisor(client, subscriptions=[manager], poll_interval=0.2)
    await manager.subscribe(["ns=2;s=Temperature"])
    await manager.stop()

    await supervisor.repair_subscriptions(force=True)
    assert manager.subscription_id is None
    assert manager.items == []
    assert manager.stats.recreations == 0
    assert supervisor.subscription_repairs == ()


async def test_the_supervision_loop_recovers_after_a_link_comes_back(client, demo_server) -> None:
    supervisor = ConnectionSupervisor(client, poll_interval=0.2)
    observer = Observer()
    supervisor.add_observer(observer)

    await client.disconnect()
    await supervisor.start()
    await asyncio.sleep(0.8)
    assert not supervisor.status.connected
    assert supervisor.status.attempts >= 1

    await supervisor.stop()
    assert not supervisor.status.connected

    await client.connect()
    status = await supervisor.probe()
    assert status.connected
    assert status.healthy
    assert observer.texts[0].startswith("disconnected (")
    assert observer.texts[-1] == "connected and healthy"


async def test_a_report_says_everything_an_operator_needs_to_know(client, demo_server) -> None:
    supervisor = ConnectionSupervisor(
        client, limits_overrides=LimitsOverrides(max_nodes_per_read=4)
    )
    await supervisor.probe()
    report = supervisor.format_report()

    assert "link       : connected and healthy" in report
    assert "health     : ServerState=Running" in report
    assert "limits     : server published all operation limits" in report
    assert demo_server.namespace_uri in report
    assert supervisor.limits.max_nodes_per_read == 4
