"""Chapter 13: discovery - ask the server before you configure the client.

Every number Chapter 4 to Chapter 12 relied on was already known to the server: which
endpoint to use, which security policies it offers, which namespace its tags live in,
what sample rate it will actually honour, and whether it is in maintenance right now.
A client that reads those facts instead of assuming them survives a server move, a
namespace renumbering and a policy change.

* ``GetEndpoints`` answers before a session exists, and the endpoint it names is not
  necessarily the URL you typed;
* a transport profile name tells you the *transport*, while ``SecurityPolicy`` tells
  you the protection - both are needed, and "it is binary" tells you nothing;
* the namespace index of a tag is a fact about this server, so a browse path is
  resolved with the index the server published (Part 23 depends on the same discipline);
* ``FindServers`` is not the same as ``FindServersOnNetwork``: one is a server call, the
  other needs a Local Discovery Server.

Run it:

    uv run python examples/ch13_discovery.py --demo
    uv run python examples/ch13_discovery.py --endpoint opc.tcp://127.0.0.1:4840

The example ends by printing the client configuration it derived from the server - the
YAML file Chapter 14 loads instead of a source file full of constants.
"""

from __future__ import annotations

import asyncio

from pydantic import ValidationError

from _support import bullet, chapter_run, heading
from opcua_toolkit import (
    BrowsePathError,
    ClientSettings,
    EndpointDiscovery,
    EndpointNotFoundError,
    NamespaceExplorer,
    SubscriptionDefaults,
)

#: The namespace this tutorial's tags live in. A real project keeps this in the
#: config file Chapter 14 loads: the URI is the engineer's decision, the index is
#: the server's, and only a lookup connects the two.
DATA_NAMESPACE = "urn:opcua-tutorial:demo"

#: The browse path Chapter 5 found by name and Chapter 13 resolves by URI + index.
DEMO_PATH: tuple[str, ...] = ("ProductionLine", "Line1", "Temperature")


async def main() -> int:
    """Profile a server, then configure a client from what the profile said."""
    async with chapter_run(
        "profile a server and build the client configuration from the answer",
        session_name="Chapter 13 - discovery",
        simulate=False,
    ) as run:
        discovery = EndpointDiscovery(run.settings)

        heading("1. what the server offers, before any session exists")
        endpoints = await discovery.list_endpoints()
        bullet(f"{len(endpoints)} endpoint(s) published")
        for endpoint in endpoints:
            bullet(endpoint.line())
            bullet(
                f"  transport : {endpoint.transport_scheme} "
                f"(profile {endpoint.transport_profile_name}, tcp={endpoint.uses_tcp_transport})"
            )
            bullet(f"  identity  : {', '.join(endpoint.user_identities) or 'none offered'}")
            bullet(
                f"  security  : mode={endpoint.security_mode_name} "
                f"policy={endpoint.policy_short_name} level={endpoint.security_level} "
                f"certificate={'yes' if endpoint.has_server_certificate else 'no'}"
            )

        heading("2. the profile: who it is, what it published, how it is feeling")
        profile = await discovery.profile()
        for line in profile.describe().splitlines():
            bullet(line)

        heading("3. namespaces are discovered, not remembered")
        for position, uri in enumerate(profile.namespaces):
            bullet(f"ns={position}  {uri}")
        bullet(f"the server's own application URI: {profile.application_uri}")
        bullet(f"which it publishes at ns={profile.application_namespace_index}")
        bullet(
            "OPC 10000-5 says ns=1 is the server's application namespace. It does not say "
            "the tags live there, and a client that assumes it does is written in the wrong file"
        )
        try:
            index = profile.namespace_index_of(DATA_NAMESPACE)
            bullet(f"the tags this tutorial uses live in {DATA_NAMESPACE!r} -> ns={index}")
        except EndpointNotFoundError as exc:
            index = profile.application_namespace_index
            bullet(f"{DATA_NAMESPACE!r} is not published here: {exc}")
            bullet(f"falling back to the application namespace ns={index}")
        try:
            profile.namespace_index_of("urn:not-a-server-this-client-talks-to")
        except EndpointNotFoundError as exc:
            bullet(f"looking up a URI that is not there: {exc}")

        heading("4. the resolved client configuration")
        endpoint = profile.best_endpoint()
        bullet(
            f"best endpoint: {endpoint.endpoint_url} policy={endpoint.policy_short_name} "
            f"mode={endpoint.security_mode_name} level={endpoint.security_level} "
            f"identities={', '.join(endpoint.user_identities)}"
        )
        if endpoint.is_unsecured:
            bullet(
                "and the strongest endpoint this server has is an unsecured one: "
                "best_endpoint ranks what exists, so 'best available' is not 'secure'"
            )
        if endpoint.transport_scheme != "opc.tcp":
            bullet(
                "the best endpoint speaks a transport this client cannot use yet - "
                "Chapter 25 and Chapter 27 are about that decision"
            )
        sampling = (profile.limits.min_supported_sample_rate if profile.limits else None) or 0.0
        derived = run.settings_for(
            endpoint_url=endpoint.endpoint_url,
            session_name="Chapter 13 - discovered client",
            subscriptions=SubscriptionDefaults(
                publishing_interval=500.0, sampling_interval=max(sampling, 1.0)
            ),
        )
        bullet(derived.describe())
        if not profile.supports_security:
            bullet(
                "this server offers no secured endpoint: the security settings stay "
                "SecurityPolicyNone, and Chapter 25 shows what real certificate material "
                "looks like"
            )

        heading("5. one request reaches a tag, using the discovered namespace")
        async with run.open_client() as client:
            explorer = NamespaceExplorer(client, limits=await run.read_limits(client))
            try:
                resolved = await explorer.resolve_path_with_translate(
                    DEMO_PATH, namespace_index=index
                )
            except BrowsePathError as exc:
                bullet(f"{exc}")
                bullet("this server does not have that path: the discovery still told you the")
                bullet(
                    "endpoint, the namespaces and the limits, which is what Chapter 14 builds on"
                )
            else:
                bullet(f"{'/'.join(DEMO_PATH)} -> {resolved.node_id_text}")
                bullet(f"the ns={index} came from the NamespaceArray, not from this source file")
            health = profile.health
            if health is not None:
                bullet(f"health before connecting: {health.reason}")
                bullet(f"  band={health.band.value} operational={health.is_operational}")

        heading("6. the same profile as a config file")
        print(derived.to_yaml())

        heading("7. what the discovery services are, and are not")
        try:
            servers = await discovery.find_servers()
            bullet(f"FindServers reported {len(servers)} application(s):")
            for server in servers:
                bullet(f"  {server.line()}")
        except EndpointNotFoundError as exc:
            bullet(f"FindServers: {exc}")
        try:
            on_network = await discovery.find_servers_on_network()
            for server in on_network:
                bullet(f"  on network: {server.line()}")
        except EndpointNotFoundError as exc:
            bullet(f"FindServersOnNetwork: {exc}")
            bullet(
                "that service needs a Local Discovery Server, not this server: the two are "
                "routinely confused, and the error message is the giveaway"
            )

        heading("8. a URL that is not an endpoint")
        try:
            ClientSettings(endpoint_url="127.0.0.1:4840")
        except ValidationError as exc:
            bullet(f"pydantic rejected it before anything connected: {exc.errors()[0]['msg']}")

        heading("building block")
        bullet("EndpointDiscovery: list_endpoints / profile / find_servers / health")
        bullet("ServerProfile.best_endpoint, .namespaces, .namespace_index_of, .limits")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
