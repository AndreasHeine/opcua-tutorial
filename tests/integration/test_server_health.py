"""Chapter 12 and Chapter 14 integration tests: what the server says about itself.

These are the tests that prove the demo server is a *useful* test server: it
publishes operation limits, reports a health state a client can read, and can be
put into a degraded state on purpose so the client's reaction is observable.
"""

from __future__ import annotations

import math

import pytest
from asyncua import ua

from opcua_toolkit.errors import ServerUnhealthyError
from opcua_toolkit.health import SERVICE_LEVEL_HEALTHY_MIN, ServerHealth, ServiceLevelBand
from opcua_toolkit.limits import LIMIT_PROPERTY_IDS, OperationLimits

pytestmark = pytest.mark.integration


async def test_the_demo_server_reports_itself_as_running_and_serving_data(
    client, demo_server
) -> None:
    health = await ServerHealth.read(client)
    assert health.state is ua.ServerState.Running
    assert health.service_level == 255
    assert health.band is ServiceLevelBand.HEALTHY
    assert health.is_running
    assert health.is_operational
    health.ensure_operational()


async def test_service_level_and_endpoint_are_part_of_the_report(client, demo_server) -> None:
    health = await ServerHealth.read(client)
    assert health.service_level >= SERVICE_LEVEL_HEALTHY_MIN
    assert health.summary["server_state"] == "Running"
    assert health.summary["service_level_band"] == ServiceLevelBand.HEALTHY.value


async def test_a_server_in_maintenance_is_reachable_but_not_usable(client, demo_server) -> None:
    # ServiceLevel 0 is a server that is up and refuses to serve data (OPC 10000-4 Table 105).
    await demo_server.set_service_level(0)
    health = await ServerHealth.read(client)
    assert health.state is ua.ServerState.Running
    assert health.band is ServiceLevelBand.MAINTENANCE
    assert not health.is_operational
    with pytest.raises(ServerUnhealthyError, match="ServerState=Running"):
        health.ensure_operational()


async def test_a_server_without_data_is_reported_as_no_data(client, demo_server) -> None:
    await demo_server.set_service_level(1)
    health = await ServerHealth.read(client)
    assert health.band is ServiceLevelBand.NO_DATA
    assert not health.is_operational


async def test_a_degraded_server_is_usable_only_if_the_client_accepts_it(
    client, demo_server
) -> None:
    await demo_server.set_service_level(120)
    health = await ServerHealth.read(client)
    assert health.band is ServiceLevelBand.DEGRADED
    health.ensure_operational()  # default: tolerate degraded
    with pytest.raises(ServerUnhealthyError):
        health.ensure_operational(allow_degraded=False)


async def test_a_shutting_down_server_is_never_operational(client, demo_server) -> None:
    await demo_server.set_server_state(ua.ServerState.Shutdown)
    health = await ServerHealth.read(client)
    assert health.state is ua.ServerState.Shutdown
    assert not health.is_running
    assert "Shutdown" in health.reason
    with pytest.raises(ServerUnhealthyError):
        health.ensure_operational()


async def test_the_server_publishes_every_operation_limit_the_toolkit_asks_for(
    client, limits
) -> None:
    assert limits.reported == frozenset(LIMIT_PROPERTY_IDS)
    assert limits.unresolved() == ()
    assert limits.status_note() == "server published all operation limits"


async def test_the_published_limits_are_small_on_purpose_so_chunking_is_visible(
    client, limits
) -> None:
    assert limits.max_nodes_per_read == 10
    assert limits.max_nodes_per_write == 10
    assert limits.max_nodes_per_browse == 10
    assert limits.max_monitored_items_per_call == 10
    assert limits.max_nodes_per_method_call == 5
    assert limits.min_supported_sample_rate == 50.0
    assert limits.max_browse_continuation_points is not None


async def test_limits_are_a_snapshot_of_one_server_not_a_global_setting(client) -> None:
    fallback = OperationLimits()
    assert fallback.reported == frozenset()
    assert len(fallback.unresolved()) == len(LIMIT_PROPERTY_IDS)


async def test_describe_lists_each_limit_once(client, limits) -> None:
    described = limits.describe()
    for name in LIMIT_PROPERTY_IDS:
        assert described.count(f"{name}:") == 1
    assert "not published" not in described


async def test_overrides_cap_a_limit_without_losing_the_published_ones(client, limits) -> None:
    capped = limits.with_overrides({"max_nodes_per_read": 4})
    assert capped.max_nodes_per_read == 4
    assert capped.max_nodes_per_write == limits.max_nodes_per_write
    # Overrides are a client side cap: the server's own report is unchanged.
    assert limits.max_nodes_per_read == 10
    assert math.ceil(24 / capped.max_nodes_per_read) == 6
