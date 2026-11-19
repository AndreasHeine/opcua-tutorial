"""Server health: ServerState and ServiceLevel.

A connection is not the same thing as a usable server. An OPC UA server can be
reachable, authenticated and perfectly happy on the transport level while it is
shutting down, has no configuration, or has lost its data source. The
:class:`~asyncua.ua.ServerState` and ``ServiceLevel`` variables (OPC 10000-5,
6.3.1 ServerType; ranges in OPC 10000-4, Table 105) are how a server tells
clients that. This module is the migrated core of the old tutorial's "best
practices" notes, expressed as code that is actually executed by the
reconnect supervisor in Chapter 12.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from asyncua import ua
from asyncua.client.client import Client

from opcua_toolkit.errors import ServerUnhealthyError
from opcua_toolkit.values import unwrap

#: ServiceLevel sub-ranges from OPC 10000-4, Table 105.
SERVICE_LEVEL_HEALTHY_MIN = 200
SERVICE_LEVEL_DEGRADED_MIN = 2
SERVICE_LEVEL_NO_DATA = 1
SERVICE_LEVEL_MAINTENANCE = 0
#: ServiceLevel is a Byte, so 255 is the best level a server can report.
SERVICE_LEVEL_MAX = 255
SERVICE_LEVEL_MIN = 0


class ServiceLevelBand(StrEnum):
    """Which ServiceLevel sub-range a value falls into."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    NO_DATA = "no_data"
    MAINTENANCE = "maintenance"
    INVALID = "invalid"


def classify_service_level(value: int) -> ServiceLevelBand:
    """Map a ServiceLevel byte to its sub-range name.

    Args:
        value: ServiceLevel, a Byte (0..255). 255 is the best level a server can
            report, 0 means the server is in maintenance and no new client
            should connect.

    Raises:
        ValueError: The value is outside the Byte range.
    """
    if value < SERVICE_LEVEL_MIN or value > SERVICE_LEVEL_MAX:
        raise ValueError(f"ServiceLevel must be a Byte (0..{SERVICE_LEVEL_MAX}), got {value}")
    if value >= SERVICE_LEVEL_HEALTHY_MIN:
        return ServiceLevelBand.HEALTHY
    if value >= SERVICE_LEVEL_DEGRADED_MIN:
        return ServiceLevelBand.DEGRADED
    if value == SERVICE_LEVEL_NO_DATA:
        return ServiceLevelBand.NO_DATA
    return ServiceLevelBand.MAINTENANCE


@dataclasses.dataclass(frozen=True, slots=True)
class ServerHealth:
    """One snapshot of the two variables that decide whether to trust a server."""

    state: ua.ServerState
    service_level: int
    sampled_at: datetime
    endpoint_url: str = ""

    @property
    def band(self) -> ServiceLevelBand:
        """ServiceLevel sub-range of this snapshot."""
        return classify_service_level(self.service_level)

    @property
    def is_running(self) -> bool:
        """True when ServerState is Running."""
        return self.state is ua.ServerState.Running

    @property
    def is_operational(self) -> bool:
        """True when the server is running *and* can deliver data."""
        return self.is_running and self.band in (
            ServiceLevelBand.HEALTHY,
            ServiceLevelBand.DEGRADED,
        )

    @property
    def reason(self) -> str:
        """One line explanation used in logs and error messages."""
        return (
            f"ServerState={self.state.name} ServiceLevel={self.service_level} ({self.band.value})"
            + (f" on {self.endpoint_url}" if self.endpoint_url else "")
        )

    @property
    def summary(self) -> dict[str, Any]:
        """JSON-safe representation for structured logging.

        The keys are the OPC UA variable names, because this dictionary ends up in
        log lines that an operator reads: ``server_state`` and ``service_level`` are
        what they will search for.
        """
        return {
            "server_state": self.state.name,
            "service_level": self.service_level,
            "service_level_band": self.band.value,
            "operational": self.is_operational,
            "sampled_at": self.sampled_at.isoformat(),
            "endpoint_url": self.endpoint_url,
        }

    @classmethod
    async def read(cls, client: Client) -> ServerHealth:
        """Read ServerState and ServiceLevel in one Read service call.

        Reading them one by one doubles the request count for a health probe
        that runs every few seconds, so the toolkit always batches them.
        """
        node_ids = [
            ua.NodeId(ua.ObjectIds.Server_ServerStatus_State, 0),
            ua.NodeId(ua.ObjectIds.Server_ServiceLevel, 0),
        ]
        results = await client.uaclient.read_attributes(node_ids, ua.AttributeIds.Value)
        state_value = unwrap(results[0].Value)
        level_value = unwrap(results[1].Value)

        state = _as_server_state(state_value)
        service_level = int(level_value) if level_value is not None else SERVICE_LEVEL_MAINTENANCE
        return cls(
            state=state,
            service_level=service_level,
            sampled_at=datetime.now(UTC),
            endpoint_url=client.server_url.geturl(),
        )

    def ensure_operational(self, *, allow_degraded: bool = True) -> None:
        """Raise :class:`ServerUnhealthyError` unless the server can be trusted.

        Args:
            allow_degraded: Accept a degraded ServiceLevel. A degraded server is
                still alive, and dropping the session on every dip usually makes
                things worse than continuing with reduced quality data.

        Raises:
            ServerUnhealthyError: State is not Running, or the ServiceLevel is
                NoData/Maintenance.
        """
        if not self.is_running:
            raise ServerUnhealthyError(
                f"server is not running: {self.reason}",
                state=self.state,
                service_level=self.service_level,
            )
        if self.band is ServiceLevelBand.HEALTHY:
            return
        if self.band is ServiceLevelBand.DEGRADED and allow_degraded:
            return
        raise ServerUnhealthyError(
            f"server is not serving data: {self.reason}",
            state=self.state,
            service_level=self.service_level,
        )


def _as_server_state(value: Any) -> ua.ServerState:
    """Coerce whatever the server returned into a ServerState enum member."""
    if isinstance(value, ua.ServerState):
        return value
    try:
        return ua.ServerState(int(value))
    except (TypeError, ValueError):
        return ua.ServerState.Unknown


__all__ = [
    "SERVICE_LEVEL_DEGRADED_MIN",
    "SERVICE_LEVEL_HEALTHY_MIN",
    "SERVICE_LEVEL_MAX",
    "SERVICE_LEVEL_MIN",
    "ServerHealth",
    "ServiceLevelBand",
    "classify_service_level",
]
