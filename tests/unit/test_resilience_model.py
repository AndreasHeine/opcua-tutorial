"""Chapter 12 unit tests: the supervisor's four states must not blur together."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from asyncua import ua

from opcua_toolkit.health import ServerHealth
from opcua_toolkit.resilience import ConnectionStatus


def health(state: ua.ServerState = ua.ServerState.Running, level: int = 255) -> ServerHealth:
    return ServerHealth(
        state=state,
        service_level=level,
        sampled_at=datetime(2026, 1, 1, tzinfo=UTC),
        endpoint_url="opc.tcp://127.0.0.1:4840",
    )


def test_a_connected_healthy_link_is_named_that_way() -> None:
    status = ConnectionStatus(
        connected=True, healthy=True, operational=True, attempts=0, message="all good"
    )
    assert status.state_text == "connected and healthy"


def test_a_lost_link_is_named_as_a_link_problem() -> None:
    status = ConnectionStatus(
        connected=False, healthy=False, operational=False, attempts=3, message="Connection refused"
    )
    assert status.state_text == "disconnected (Connection refused)"


def test_a_degraded_server_is_usable_and_says_so() -> None:
    status = ConnectionStatus(
        connected=True,
        healthy=False,
        operational=True,
        attempts=0,
        message="ServiceLevel=120",
        health=health(level=120),
    )
    assert status.state_text == "connected but degraded: ServiceLevel=120"


def test_a_server_that_refuses_data_is_not_the_same_as_a_degraded_one() -> None:
    status = ConnectionStatus(
        connected=True,
        healthy=False,
        operational=False,
        attempts=0,
        message="ServiceLevel=0",
        health=health(level=0),
    )
    assert status.state_text == "connected but not usable: ServiceLevel=0"
    assert "degraded" not in status.state_text


def test_a_shutting_down_server_is_not_usable_even_though_the_socket_is_open() -> None:
    status = ConnectionStatus(
        connected=True,
        healthy=False,
        operational=False,
        attempts=0,
        message="ServerState=Shutdown",
        health=health(state=ua.ServerState.Shutdown, level=0),
    )
    assert not status.operational
    assert "Shutdown" in status.state_text


def test_a_status_without_a_health_snapshot_still_produces_json() -> None:
    status = ConnectionStatus(
        connected=False, healthy=False, operational=False, attempts=2, message="timeout"
    )
    payload = status.as_dict()
    assert payload["server_state"] is None
    assert payload["service_level"] is None
    assert payload["service_level_band"] is None
    assert json.dumps(payload)
    assert payload["attempts"] == 2


def test_a_status_with_a_health_snapshot_reports_what_the_server_said() -> None:
    status = ConnectionStatus(
        connected=True,
        healthy=True,
        operational=True,
        attempts=0,
        message="ok",
        health=health(level=210),
    )
    payload = status.as_dict()
    assert payload["server_state"] == "Running"
    assert payload["service_level"] == 210
    assert payload["service_level_band"] == "healthy"
    assert payload["connected"] is True
    assert payload["operational"] is True


def test_elapsed_time_is_reported_rounded_because_it_is_a_dashboard_number() -> None:
    status = ConnectionStatus(
        connected=True,
        healthy=True,
        operational=True,
        attempts=0,
        message="ok",
        elapsed_ms=12.3456,
    )
    assert status.as_dict()["elapsed_ms"] == 12.35


def test_a_status_is_immutable_so_a_reporter_cannot_rewrite_history() -> None:
    status = ConnectionStatus(
        connected=True, healthy=True, operational=True, attempts=0, message="ok"
    )
    try:
        status.healthy = False  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("ConnectionStatus must be frozen")
