"""Chapter 4 and Chapter 25 integration tests: discovery before configuration.

The demo server publishes one unsecured endpoint on a port the operating system
picked. That is the shape of a real first contact with a server: you ask what it
offers, and only then decide what to configure. The last two tests here belong to
Chapter 31 instead: what a server binds, what a server publishes, and why an
announcement is not something a client should connect to.
"""

from __future__ import annotations

import json

import pytest
from asyncua import Client, ua
from asyncua.ua.uaerrors import UaError

from opcua_toolkit.devserver import DemoServer, DemoServerConfig
from opcua_toolkit.discovery import EndpointDiscovery
from opcua_toolkit.errors import EndpointNotFoundError
from opcua_toolkit.settings import ClientSettings
from tests.conftest import free_port

pytestmark = pytest.mark.integration

#: The address a demo server binds when it is asked to bind one at all.
BIND_HOST = "127.0.0.1"

#: The address a container would publish in its place.
EXTERNAL_HOST = "opcua.example"


def discovery_for(demo_server) -> EndpointDiscovery:
    return EndpointDiscovery(ClientSettings(endpoint_url=demo_server.endpoint))


async def test_get_endpoints_answers_without_a_session(demo_server) -> None:
    endpoints = await discovery_for(demo_server).list_endpoints()
    assert endpoints
    endpoint = endpoints[0]
    assert endpoint.endpoint_url.startswith("opc.tcp://")
    assert endpoint.policy_short_name == "None"
    assert endpoint.security_mode_name == "None"
    assert endpoint.is_unsecured
    assert not endpoint.preferred
    assert endpoint.uses_tcp_transport
    assert endpoint.transport_scheme == "opc.tcp"
    assert endpoint.transport_profile_name == "uatcp-uasc-uabinary"
    assert "Anonymous" in endpoint.user_identities
    assert not endpoint.has_server_certificate
    assert endpoint.security_level == 0


async def test_a_profile_brings_together_identity_namespaces_limits_and_health(demo_server) -> None:
    profile = await discovery_for(demo_server).profile()
    assert profile.application_name
    assert profile.application_uri
    assert profile.product_uri
    assert "opcfoundation.org" in profile.namespaces[0]
    assert demo_server.namespace_uri in profile.namespaces
    # The index of a namespace is a lookup, never a guess: asyncua publishes its
    # application URI as NamespaceArray[1] and the demo data namespace after it.
    assert profile.namespaces[profile.application_namespace_index] == profile.application_uri
    assert profile.namespace_index_of("opcua-tutorial") == demo_server.namespace_index
    assert profile.limits is not None
    assert profile.limits.max_nodes_per_read == demo_server.config.max_nodes_per_read
    assert profile.health is not None
    assert profile.health.is_operational


async def test_a_namespace_lookup_namespaces_the_index_a_server_might_change(demo_server) -> None:
    profile = await discovery_for(demo_server).profile()
    index = profile.namespace_index_of("opcua-tutorial")
    assert index == demo_server.namespace_index
    with pytest.raises(EndpointNotFoundError, match="no namespace matching"):
        profile.namespace_index_of("urn:vendor-that-does-not-exist")


async def test_a_profile_names_an_unsecured_server_instead_of_hiding_it(demo_server) -> None:
    profile = await discovery_for(demo_server).profile()
    assert not profile.supports_security
    assert profile.secured_endpoints == ()
    described = profile.describe()
    assert "unsecured endpoints only" in described
    assert profile.best_endpoint().policy_short_name == "None"


async def test_a_profile_reports_the_limits_it_read(demo_server) -> None:
    profile = await discovery_for(demo_server).profile()
    described = profile.describe()
    assert "limits          :" in described
    assert "max_nodes_per_read: 10" in described
    assert "server published all operation limits" in described
    assert "health          :" in described
    assert "ServerState=Running" in described


async def test_limits_can_be_skipped_when_only_the_identity_is_needed(demo_server) -> None:
    profile = await discovery_for(demo_server).profile(with_limits=False)
    assert profile.limits is None
    assert profile.health is not None
    assert "limits" not in profile.describe()


async def test_a_profile_is_serialisable_for_a_diagnostic_endpoint(demo_server) -> None:
    payload = json.dumps((await discovery_for(demo_server).profile()).as_dict())
    decoded = json.loads(payload)
    assert decoded["namespaces"]
    assert decoded["limits"]["max_nodes_per_read"] == 10
    # JSON safety means the frozenset of reported limits became a list.
    assert isinstance(decoded["limits"]["reported"], list)
    assert "max_nodes_per_read" in decoded["limits"]["reported"]


async def test_a_security_policy_the_server_does_not_offer_is_a_typed_error(demo_server) -> None:
    profile = await discovery_for(demo_server).profile()
    with pytest.raises(EndpointNotFoundError, match="does not offer Basic256Sha256") as caught:
        profile.endpoint_for("Basic256Sha256", "SignAndEncrypt")
    assert "None/None" in str(caught.value)


async def test_the_none_profile_a_discovered_endpoint_offers_needs_no_key_material(
    demo_server,
) -> None:
    profile = await discovery_for(demo_server).profile()
    settings = profile.security_settings_for("None", "None")
    assert not settings.enabled
    assert settings.policy == "None"
    assert settings.mode == "None"


async def test_find_servers_reports_the_application_behind_the_endpoint(demo_server) -> None:
    servers = await discovery_for(demo_server).find_servers()
    assert servers
    server = servers[0]
    assert server.application_uri
    # asyncua's server announces itself as ClientAndServer. A client must read the
    # ApplicationType the server actually sends: vendor servers answer Server,
    # ClientAndServer or Client, and none of them is a bug.
    assert server.application_type == "ClientAndServer"
    assert server.discovery_urls
    assert demo_server.endpoint in server.discovery_urls
    assert server.line()


async def test_find_servers_on_network_is_answered_or_reported_as_unsupported(
    demo_server,
) -> None:
    # A server that does not implement LDS discovery must produce a typed error,
    # never a crash: most vendor servers are not a Local Discovery Server.
    try:
        servers = await discovery_for(demo_server).find_servers_on_network()
    except EndpointNotFoundError:
        pytest.skip("demo server does not implement FindServersOnNetwork")
    assert isinstance(servers, tuple)


async def test_health_is_readable_through_discovery_without_a_profile(demo_server) -> None:
    health = await discovery_for(demo_server).health()
    assert health.is_running
    assert health.service_level == 255
    assert demo_server.endpoint in health.reason


async def test_discovery_carries_the_settings_a_later_chapter_reuses(demo_server) -> None:
    discovery = discovery_for(demo_server)
    assert discovery.settings.endpoint_url == demo_server.endpoint


async def test_a_wrong_port_fails_at_the_transport_not_inside_the_protocol_stack(
    demo_server,
) -> None:
    # config.port is 0 in the test fixtures: the port that actually exists is the
    # bound one, so the "wrong endpoint" has to be built from that.
    unreachable_url = demo_server.endpoint.replace(f":{demo_server.bound_port}", ":1")
    assert unreachable_url != demo_server.endpoint
    unreachable = ClientSettings(endpoint_url=unreachable_url, request_timeout=2.0)
    with pytest.raises((OSError, UaError, TimeoutError)):
        await EndpointDiscovery(unreachable).list_endpoints()
    with pytest.raises((OSError, UaError, TimeoutError)):
        await EndpointDiscovery(unreachable).profile()


async def test_a_server_can_bind_one_address_and_publish_another() -> None:
    """The container case, end to end.

    A server inside a container listens on an address the outside world cannot use and
    has to publish the address it can. asyncua gives the two roles to different settings -
    the bind address is a socket setting, the published one is the endpoint URL - and a
    deployment that sets only the second one produces a server that answers locally and
    publishes an address nobody can reach.
    """
    port = free_port()
    server = DemoServer(
        DemoServerConfig(host="127.0.0.1", port=port, advertised_host=EXTERNAL_HOST, simulate=False)
    )
    await server.start()
    try:
        # The bind address still answers, which is the half a container needs working.
        endpoints = await EndpointDiscovery(
            ClientSettings(endpoint_url=f"opc.tcp://{BIND_HOST}:{port}")
        ).list_endpoints()
        assert endpoints
        # ...and the URL this server calls itself is the one its clients can use.
        assert server.endpoint == f"opc.tcp://{EXTERNAL_HOST}:{port}"
    finally:
        await server.stop()


async def test_an_announced_url_is_an_answer_to_a_question_not_a_fact() -> None:
    """A client that trusts an announced host will eventually connect to the wrong machine.

    asyncua rewrites the ``EndpointUrl`` it publishes from the ``EndpointUrl`` in the
    request, so asking about another address gets that other address back. This is
    convenient for a mapped port and useless as a source of truth: the address a client
    must connect to is the one it already has, and an announcement is only worth reading
    for what it says about security policies and identity tokens.
    """
    port = free_port()
    server = DemoServer(
        DemoServerConfig(host=BIND_HOST, port=port, advertised_host=EXTERNAL_HOST, simulate=False)
    )
    await server.start()
    try:
        async with Client(url=f"opc.tcp://{BIND_HOST}:{port}") as client:
            params = ua.GetEndpointsParameters()
            params.EndpointUrl = f"opc.tcp://{EXTERNAL_HOST}:{port}"
            asked_its_own_name = await client.uaclient.get_endpoints(params)
            assert [endpoints.EndpointUrl for endpoints in asked_its_own_name] == [
                f"opc.tcp://{EXTERNAL_HOST}:{port}"
            ]

            params = ua.GetEndpointsParameters()
            params.EndpointUrl = "opc.tcp://some-other-machine:4840"
            asked_somewhere_else = await client.uaclient.get_endpoints(params)
            assert [endpoints.EndpointUrl for endpoints in asked_somewhere_else] == [
                "opc.tcp://some-other-machine:4840"
            ]
    finally:
        await server.stop()
