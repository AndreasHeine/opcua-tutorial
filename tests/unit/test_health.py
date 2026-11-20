"""Chapter 12 tests: ServiceLevel bands and the two variables that decide trust.

Ranges are from OPC 10000-4 Table 105. The point of these tests is that a server
can be perfectly reachable and still be useless, and the toolkit must tell the
difference.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_toolkit.errors import ServerUnhealthyError
from opcua_toolkit.health import (
    SERVICE_LEVEL_DEGRADED_MIN,
    SERVICE_LEVEL_HEALTHY_MIN,
    SERVICE_LEVEL_MAX,
    SERVICE_LEVEL_MIN,
    ServerHealth,
    ServiceLevelBand,
    classify_service_level,
)


def test_service_level_boundaries_match_the_specification() -> None:
    assert classify_service_level(SERVICE_LEVEL_MAX) is ServiceLevelBand.HEALTHY
    assert classify_service_level(SERVICE_LEVEL_HEALTHY_MIN) is ServiceLevelBand.HEALTHY
    assert classify_service_level(SERVICE_LEVEL_HEALTHY_MIN - 1) is ServiceLevelBand.DEGRADED
    assert classify_service_level(SERVICE_LEVEL_DEGRADED_MIN) is ServiceLevelBand.DEGRADED
    assert classify_service_level(1) is ServiceLevelBand.NO_DATA
    assert classify_service_level(SERVICE_LEVEL_MIN) is ServiceLevelBand.MAINTENANCE


def test_service_level_outside_a_byte_is_an_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="Byte"):
        classify_service_level(256)
    with pytest.raises(ValueError, match="Byte"):
        classify_service_level(-1)


def make_health(state: ua.ServerState, service_level: int) -> ServerHealth:
    return ServerHealth(
        state=state,
        service_level=service_level,
        sampled_at=datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC),
        endpoint_url="opc.tcp://127.0.0.1:48401",
    )


def test_running_and_healthy_is_the_only_fully_operational_combination() -> None:
    healthy = make_health(ua.ServerState.Running, 255)
    assert healthy.is_running
    assert healthy.is_operational
    assert healthy.band is ServiceLevelBand.HEALTHY

    degraded = make_health(ua.ServerState.Running, 120)
    assert degraded.is_operational
    assert degraded.band is ServiceLevelBand.DEGRADED

    no_data = make_health(ua.ServerState.Running, 1)
    assert no_data.is_running
    assert not no_data.is_operational


def test_a_shutting_down_server_is_never_operational() -> None:
    for state in (ua.ServerState.Shutdown, ua.ServerState.Suspended, ua.ServerState.Failed):
        health = make_health(state, 255)
        assert not health.is_running
        assert not health.is_operational


def test_ensure_operational_accepts_running_and_degraded_by_default() -> None:
    make_health(ua.ServerState.Running, 255).ensure_operational()
    make_health(ua.ServerState.Running, 100).ensure_operational()


def test_ensure_operational_can_be_strict_when_quality_matters() -> None:
    degraded = make_health(ua.ServerState.Running, 100)
    with pytest.raises(ServerUnhealthyError, match="not serving data"):
        degraded.ensure_operational(allow_degraded=False)


def test_ensure_operational_reports_the_reason_the_operator_needs() -> None:
    health = make_health(ua.ServerState.NoConfiguration, 0)
    with pytest.raises(ServerUnhealthyError) as caught:
        health.ensure_operational()
    error = caught.value
    assert error.state is ua.ServerState.NoConfiguration
    assert error.service_level == 0
    assert "ServerState=NoConfiguration" in str(error)
    assert "opc.tcp://127.0.0.1:48401" in health.reason


def test_summary_is_json_safe_for_structured_logging() -> None:
    summary = make_health(ua.ServerState.Running, 200).summary
    encoded = json.dumps(summary)
    assert "healthy" in encoded
    assert summary["operational"] is True
    assert summary["sampled_at"].startswith("2026-05-04T09:00:00")
