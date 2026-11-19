"""Endpoint discovery and server profiling (Chapter 4, Chapter 25).

Hardcoding an endpoint URL and a namespace index is the first thing that breaks
in a real deployment. OPC UA gives you three services that remove the guesswork:

* **GetEndpoints** - what security, transport and identity combinations this
  server actually offers. Selecting a security policy that the server does not
  publish is the most common reason a "working" client suddenly fails.
* **FindServers / FindServersOnNetwork** - ask a discovery server (or any server)
  which applications it knows about.
* **Server capabilities** - application identity, namespace array and operation
  limits, read once at startup and cached for the whole session.

:class:`EndpointDiscovery` performs those calls; :class:`ServerProfile` is the
immutable result every later chapter consumes.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaError

from opcua_toolkit.browse.explorer import NamespaceExplorer
from opcua_toolkit.errors import EndpointNotFoundError, SecurityConfigurationError, format_node_id
from opcua_toolkit.health import ServerHealth
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.settings import (
    RECOMMENDED_POLICIES,
    SUPPORTED_POLICIES,
    ClientSettings,
    SecuritySettings,
)

if TYPE_CHECKING:
    from opcua_toolkit.settings import SecurityModeName

_logger = logging.getLogger(__name__)

#: The binary transport over ``opc.tcp`` that every chapter of this tutorial uses.
#: asyncua speaks this profile and nothing else, so a client that is told "binary"
#: without being told *which* binary has to find this exact URI on the server.
BINARY_TRANSPORT_PROFILE = "http://opcfoundation.org/UA-Profile/Transport/uatcp-uasc-uabinary"

#: The HTTPS hosted binary transport (OPC 10000-6, 7.4). Same OPC UA binary
#: encoding, different URL scheme and different channel establishment. It is here
#: because a server that publishes it proves that "is the transport binary?" is the
#: wrong question to ask an endpoint: the question is which scheme to speak.
HTTPS_BINARY_TRANSPORT_PROFILE = "http://opcfoundation.org/UA-Profile/Transport/https-uabinary"

#: The WebSockets binary transport (OPC 10000-6, 7.5), scheme ``opc.wss``.
WEBSOCKET_BINARY_TRANSPORT_PROFILE = (
    "http://opcfoundation.org/UA-Profile/Transport/wss-uasc-uabinary"
)

#: Profile URI path segment -> URL scheme, as defined by the transport profiles in
#: OPC 10000-6 and 10000-7. A client picks one of these before it can open a
#: SecureChannel, because the scheme decides how the connection is made at all.
_TRANSPORT_SCHEMES: dict[str, str] = {
    "uatcp": "opc.tcp",
    "https": "https",
    "wss": "opc.wss",
}

#: Security policies in the order a well behaved client should attempt them.
_POLICY_RANKING: dict[str, int] = {
    "http://opcfoundation.org/UA/SecurityPolicy#None": 0,
    "http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256": 1,
    "http://opcfoundation.org/UA/SecurityPolicy#Aes128Sha256RsaOaep": 2,
    "http://opcfoundation.org/UA/SecurityPolicy#Aes256Sha256RsaPss": 3,
}


@dataclasses.dataclass(frozen=True, slots=True)
class EndpointInfo:
    """One endpoint as the server publishes it."""

    endpoint_url: str
    security_policy: str
    security_mode: str
    security_level: int
    user_identities: tuple[str, ...]
    transport_profile: str
    has_server_certificate: bool
    application_uri: str

    @property
    def security_mode_name(self) -> str:
        """``None`` rather than asyncua's ``None_`` spelling of the enum member.

        asyncua names the message security mode ``None_`` because ``None`` is a
        Python keyword. Comparing a configured mode against a discovered one
        without normalising is how "None" mysteriously stops matching.
        """
        return self.security_mode.removesuffix("_")

    @property
    def transport_profile_name(self) -> str:
        """The last path segment of the profile URI, e.g. ``uatcp-uasc-uabinary``."""
        return self.transport_profile.rstrip("/").rsplit("/", 1)[-1]

    @property
    def transport_scheme(self) -> str:
        """The URL scheme this profile requires: ``opc.tcp``, ``https`` or ``opc.wss``.

        Empty when the profile is one this toolkit does not know. A server that
        publishes an endpoint whose scheme the client cannot speak has to be
        rejected at selection time, not discovered again as a connection failure.
        """
        name = self.transport_profile_name
        for marker, scheme in _TRANSPORT_SCHEMES.items():
            if marker in name:
                return scheme
        return ""

    @property
    def uses_tcp_transport(self) -> bool:
        """True for the ``opc.tcp`` + SecureChannel + binary profile asyncua speaks.

        Note what this is *not*: an HTTPS endpoint may carry the very same OPC UA
        binary encoding, so "is it binary?" tells a client nothing useful. The
        distinction that decides whether a connection can be made at all is the
        transport profile's scheme.
        """
        return self.transport_scheme == "opc.tcp"

    @property
    def is_unsecured(self) -> bool:
        """True when this endpoint encrypts nothing."""
        return self.security_policy.endswith("#None") and self.security_mode_name == "None"

    @property
    def policy_short_name(self) -> str:
        """``Basic256Sha256`` instead of the full policy URI."""
        return self.security_policy.rsplit("#", 1)[-1]

    @property
    def preferred(self) -> bool:
        """True when the endpoint uses a policy the tutorial recommends."""
        return self.policy_short_name in RECOMMENDED_POLICIES and not self.is_unsecured

    def supports_identity(self, token_type: str) -> bool:
        """Whether the endpoint accepts a token type such as ``UserName``."""
        return token_type in self.user_identities

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "endpoint_url": self.endpoint_url,
            "security_policy": self.policy_short_name,
            "security_mode": self.security_mode_name,
            "security_level": self.security_level,
            "user_identities": list(self.user_identities),
            "transport_profile": self.transport_profile,
            "transport_scheme": self.transport_scheme,
            "server_certificate": self.has_server_certificate,
            "application_uri": self.application_uri,
            "unsecured": self.is_unsecured,
        }

    def line(self) -> str:
        """One line of terminal output."""
        flag = "unsecured" if self.is_unsecured else self.security_mode_name
        identities = ",".join(self.user_identities) or "anonymous"
        return (
            f"{self.endpoint_url:<42} {self.policy_short_name:<18} {flag:<16} "
            f"level={self.security_level:<3} identities={identities}"
        )

    @classmethod
    def from_description(cls, description: ua.EndpointDescription) -> EndpointInfo:
        """Build one entry from a server supplied ``EndpointDescription``."""
        server = description.Server
        return cls(
            endpoint_url=description.EndpointUrl,
            security_policy=str(description.SecurityPolicyUri),
            security_mode=_normalise_mode(
                str(getattr(description.SecurityMode, "name", description.SecurityMode))
            ),
            security_level=int(description.SecurityLevel or 0),
            user_identities=tuple(
                str(getattr(token.TokenType, "name", token.TokenType))
                for token in (description.UserIdentityTokens or ())
            ),
            transport_profile=str(description.TransportProfileUri or ""),
            has_server_certificate=bool(description.ServerCertificate),
            application_uri=str(getattr(server, "ApplicationUri", "") or ""),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class ServerProfile:
    """Everything discovered about one server during a session handshake."""

    endpoint_url: str
    application_uri: str
    product_uri: str
    application_name: str
    namespaces: tuple[str, ...]
    endpoints: tuple[EndpointInfo, ...]
    limits: OperationLimits | None
    health: ServerHealth | None

    @property
    def secured_endpoints(self) -> tuple[EndpointInfo, ...]:
        """Endpoints that are not ``None/None``."""
        return tuple(endpoint for endpoint in self.endpoints if not endpoint.is_unsecured)

    @property
    def supports_security(self) -> bool:
        """Whether the server offers anything beyond an unsecured endpoint."""
        return bool(self.secured_endpoints)

    @property
    def application_namespace_index(self) -> int:
        """Index of the namespace that carries the server's own application URI.

        asyncua follows OPC 10000-5's rule literally: it rewrites
        ``NamespaceArray[1]`` so that it equals the ApplicationUri. A server that
        *also* registers a separate data namespace therefore publishes two
        namespaces above 0, and neither "index 1 is the data" nor "the last index is
        the data" is a rule a client may rely on. Look the URI up - that is what
        :meth:`namespace_index_of` exists for.
        """
        if self.application_uri and self.application_uri in self.namespaces:
            return self.namespaces.index(self.application_uri)
        return 1 if len(self.namespaces) > 1 else 0

    def namespace_index_of(self, uri_fragment: str) -> int:
        """Namespace index whose URI contains ``uri_fragment``.

        Raises:
            EndpointNotFoundError: No namespace matches.
        """
        for index, uri in enumerate(self.namespaces):
            if uri_fragment in uri:
                return index
        published = ", ".join(self.namespaces) or "none"
        raise EndpointNotFoundError(
            f"no namespace matching {uri_fragment!r}; server publishes: {published}"
        )

    def endpoint_for(self, policy_name: str, mode_name: str | None = None) -> EndpointInfo:
        """Find the endpoint matching a policy (and optionally a mode).

        Raises:
            EndpointNotFoundError: The server does not publish that combination.
        """
        for endpoint in self.endpoints:
            if endpoint.policy_short_name != policy_name:
                continue
            if mode_name is not None and endpoint.security_mode_name != mode_name:
                continue
            return endpoint
        available = ", ".join(
            f"{endpoint.policy_short_name}/{endpoint.security_mode_name}"
            for endpoint in self.endpoints
        )
        raise EndpointNotFoundError(
            f"server does not offer {policy_name}/{mode_name or 'any mode'}; available: {available}"
        )

    def best_endpoint(self) -> EndpointInfo:
        """The strongest endpoint the server offers that a client can realistically use.

        Ordering follows OPC 10000-4's SecurityLevel idea, with two practical twists:
        the ``opc.tcp`` transport beats transports this client cannot speak, and a
        server that publishes a certificate is more useful than one that does not,
        because the client cannot verify an encrypted endpoint without it.

        Raises:
            EndpointNotFoundError: The server published no endpoints.
        """
        if not self.endpoints:
            raise EndpointNotFoundError("server published no endpoints")
        best = max(self.endpoints, key=_endpoint_rank)
        if not best.uses_tcp_transport:
            _logger.warning(
                "best endpoint %s uses the %s transport, which this tutorial's client "
                "cannot speak: expect a connection failure, not a security failure",
                best.endpoint_url,
                best.transport_scheme or best.transport_profile_name or "unknown",
            )
        return best

    def security_settings_for(
        self,
        policy_name: str,
        mode_name: str,
        *,
        certificate: Path | None = None,
        private_key: Path | None = None,
        private_key_password: str | None = None,
    ) -> SecuritySettings:
        """Security settings that match an endpoint this server actually offers.

        Discovering a secured endpoint is half the job: the other half is the
        certificate and key the endpoint expects. Calling this without them for a
        secured policy raises :class:`SecurityConfigurationError` naming exactly
        what is missing, which is a far better outcome than a handshake failure.

        A policy this client cannot speak is refused rather than rounded down. A
        server is free to publish ``Basic128Rsa15`` or a policy this toolkit does not
        implement, and quietly answering that request with ``None`` would put the
        caller on the endpoint they asked about with a protection profile they never
        chose - the one thing a discovery-derived configuration must not do.
        """
        endpoint = self.endpoint_for(policy_name, mode_name)
        policy = endpoint.policy_short_name
        if policy not in SUPPORTED_POLICIES:
            raise SecurityConfigurationError(
                f"endpoint {endpoint.endpoint_url} offers policy {policy!r}, which this client "
                f"does not speak; supported policies: {', '.join(SUPPORTED_POLICIES)}"
            )
        if policy not in RECOMMENDED_POLICIES:
            _logger.warning("server offers %s, which the tutorial does not recommend", policy)
        return SecuritySettings(
            policy=policy,
            mode=cast("SecurityModeName", endpoint.security_mode_name),
            certificate=certificate,
            private_key=private_key,
            private_key_password=private_key_password,
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation used by ``opcua-toolkit discover --json``."""
        return {
            "endpoint_url": self.endpoint_url,
            "application_uri": self.application_uri,
            "product_uri": self.product_uri,
            "application_name": self.application_name,
            "namespaces": list(self.namespaces),
            "endpoints": [endpoint.as_dict() for endpoint in self.endpoints],
            "limits": self.limits.as_dict() if self.limits else None,
            "health": self.health.summary if self.health else None,
        }

    def describe(self) -> str:
        """Multi line human readable report."""
        namespace_text = ", ".join(f"[{index}] {uri}" for index, uri in enumerate(self.namespaces))
        security_text = "yes" if self.supports_security else "NO - unsecured endpoints only"
        lines = [
            f"endpoint        : {self.endpoint_url}",
            f"application     : {self.application_name or 'unknown'}",
            f"application uri : {self.application_uri or 'unknown'}",
            f"product uri     : {self.product_uri or 'unknown'}",
            f"namespaces      : {namespace_text or 'none'}",
            f"security        : {security_text}",
            "endpoints       :",
            *(f"  {endpoint.line()}" for endpoint in self.endpoints),
        ]
        if self.limits is not None:
            lines.append("limits          :")
            lines.extend(f"  {row}" for row in self.limits.describe().splitlines())
            lines.append(f"  note: {self.limits.status_note()}")
        if self.health is not None:
            lines.append(f"health          : {self.health.reason}")
        return "\n".join(lines)


@dataclasses.dataclass(frozen=True, slots=True)
class DiscoveredServer:
    """One server reported by a discovery service."""

    application_uri: str
    application_name: str
    product_uri: str
    application_type: str
    discovery_urls: tuple[str, ...]

    def line(self) -> str:
        """One line of terminal output."""
        urls = ", ".join(self.discovery_urls)
        return f"{self.application_name:<28} {self.application_uri:<38} {urls}"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "application_uri": self.application_uri,
            "application_name": self.application_name,
            "product_uri": self.product_uri,
            "application_type": self.application_type,
            "discovery_urls": list(self.discovery_urls),
        }

    @classmethod
    def from_description(cls, description: ua.ApplicationDescription) -> DiscoveredServer:
        """Build one entry from a ``FindServers`` result."""
        return cls(
            application_uri=str(description.ApplicationUri or ""),
            application_name=str(
                getattr(description.ApplicationName, "Text", description.ApplicationName) or ""
            ),
            product_uri=str(description.ProductUri or ""),
            application_type=str(
                getattr(description.ApplicationType, "name", description.ApplicationType) or ""
            ),
            discovery_urls=tuple(str(url) for url in (description.DiscoveryUrls or ())),
        )


class EndpointDiscovery:
    """Ask a server what it is and what it offers.

    Args:
        settings: Client settings whose endpoint URL is probed.
    """

    def __init__(self, settings: ClientSettings) -> None:
        self._settings = settings

    @property
    def settings(self) -> ClientSettings:
        """Settings the discovery client is built from."""
        return self._settings

    async def list_endpoints(self) -> tuple[EndpointInfo, ...]:
        """GetEndpoints without creating a session.

        This works even against a server that refuses our identity, which makes
        it the right call for a first diagnostic: it answers "is the server
        reachable and what security does it want?" before you argue about
        usernames.
        """
        client = self._settings.create_client()
        try:
            descriptions = await client.connect_and_get_server_endpoints()
        finally:
            await _safe_close(client)
        return tuple(EndpointInfo.from_description(description) for description in descriptions)

    async def find_servers(self, discovery_url: str | None = None) -> tuple[DiscoveredServer, ...]:
        """FindServers against a discovery server or any server.

        Args:
            discovery_url: Optional alternative URL. ``None`` uses the endpoint
                from the settings, which every OPC UA server must answer.
        """
        client = self._settings.create_client()
        if discovery_url is not None:
            client = Client(
                url=discovery_url,
                timeout=self._settings.request_timeout,
                watchdog_intervall=self._settings.watchdog_interval,
            )
        try:
            descriptions = await client.connect_and_find_servers()
        finally:
            await _safe_close(client)
        return tuple(DiscoveredServer.from_description(description) for description in descriptions)

    async def find_servers_on_network(self) -> tuple[DiscoveredServer, ...]:
        """FindServersOnNetwork, the LDS discovery call.

        Raises:
            EndpointNotFoundError: The server does not implement LDS discovery,
                which is normal for embedded devices and most vendor servers that
                are not a Local Discovery Server.
        """
        client = self._settings.create_client()
        try:
            result = await client.connect_and_find_servers_on_network()
        except UaError as exc:
            raise EndpointNotFoundError(
                f"server does not support FindServersOnNetwork: {exc}"
            ) from exc
        finally:
            await _safe_close(client)
        return tuple(
            DiscoveredServer(
                application_uri=str(server.DiscoveryUrl or ""),
                application_name=str(server.ServerName or ""),
                product_uri="",
                application_type="Server",
                discovery_urls=(str(server.DiscoveryUrl or ""),),
            )
            for server in result.Servers
        )

    async def profile(self, *, with_limits: bool = True) -> ServerProfile:
        """Open a session and build the full profile used by later chapters.

        Returns:
            A :class:`ServerProfile` with endpoints, namespaces, limits and a
            health snapshot, all read with a handful of service calls.
        """
        client = self._settings.create_client()
        try:
            await client.connect()
            descriptions = await client.get_endpoints()
            explorer = NamespaceExplorer(client)
            namespaces = tuple(uri for _, uri in await explorer.namespaces())
            health = await ServerHealth.read(client)
            limits = await OperationLimits.read(client) if with_limits else None
        finally:
            await _safe_close(client)

        application = _application_of(descriptions)
        return ServerProfile(
            endpoint_url=_endpoint_of(application) or self._settings.endpoint_url,
            application_uri=str(application.ApplicationUri or ""),
            product_uri=str(application.ProductUri or ""),
            application_name=_name_of(application),
            namespaces=namespaces,
            endpoints=tuple(EndpointInfo.from_description(item) for item in descriptions),
            limits=limits,
            health=health,
        )

    async def health(self) -> ServerHealth:
        """Read ServerState and ServiceLevel through a short-lived session."""
        client = self._settings.create_client()
        try:
            await client.connect()
            return await ServerHealth.read(client)
        finally:
            await _safe_close(client)

    async def server_node_texts(self) -> list[str]:
        """NodeIds of the standard Server object children this toolkit reads."""
        return [format_node_id(node) for node in _SERVER_DIAGNOSTIC_NODES]


def _application_of(descriptions: Iterable[ua.EndpointDescription]) -> ua.ApplicationDescription:
    """Take the application identity the server published with its endpoints.

    GetEndpoints returns an ``ApplicationDescription`` per endpoint, so a client
    learns who it is talking to before it reads a single variable. When a server
    leaves the field empty (some embedded stacks do) we fall back to an
    identity built from the endpoint URL alone.
    """
    for description in descriptions:
        server = description.Server
        if server is not None and server.ApplicationUri:
            return server
    # Some embedded stacks publish no ApplicationDescription at all.
    return ua.ApplicationDescription()


def _normalise_mode(value: str) -> str:
    """Map asyncua's ``None_`` enum spelling back to the ``None`` a client configures."""
    return value.removesuffix("_")


def _name_of(application: ua.ApplicationDescription) -> str:
    raw = application.ApplicationName
    return str(getattr(raw, "Text", raw) or "")


def _endpoint_of(application: ua.ApplicationDescription) -> str:
    urls = list(application.DiscoveryUrls or [])
    return urls[0] if urls else ""


def _endpoint_rank(endpoint: EndpointInfo) -> tuple[int, int, int]:
    """Order endpoints the way a client should attempt them.

    Security policy first: a strong policy is worth more than a convenient
    transport. Transport second, and here the ranking is deliberately not
    "binary is good" - asyncua speaks the ``opc.tcp`` profile only, so an HTTPS or
    WebSockets endpoint is unusable for this client no matter how good its policy
    is, and picking it turns a selection problem into a connection failure.
    """
    policy_rank = _POLICY_RANKING.get(endpoint.security_policy, -1)
    transport_rank = 1 if endpoint.uses_tcp_transport else 0
    certificate_rank = 1 if endpoint.has_server_certificate else 0
    return (policy_rank, transport_rank, certificate_rank)


_SERVER_DIAGNOSTIC_NODES: tuple[ua.NodeId, ...] = (
    ua.NodeId(ua.ObjectIds.Server_ServerStatus_State, 0),
    ua.NodeId(ua.ObjectIds.Server_ServiceLevel, 0),
    ua.NodeId(ua.ObjectIds.Server_NamespaceArray, 0),
    ua.NodeId(ua.ObjectIds.Server_ServerCapabilities_OperationLimits_MaxNodesPerRead, 0),
)


async def _safe_close(client: Client) -> None:
    try:
        await client.disconnect()
    except (UaError, OSError) as exc:
        _logger.debug("disconnect during discovery: %s", exc)


__all__ = [
    "BINARY_TRANSPORT_PROFILE",
    "HTTPS_BINARY_TRANSPORT_PROFILE",
    "WEBSOCKET_BINARY_TRANSPORT_PROFILE",
    "DiscoveredServer",
    "EndpointDiscovery",
    "EndpointInfo",
    "ServerProfile",
]
