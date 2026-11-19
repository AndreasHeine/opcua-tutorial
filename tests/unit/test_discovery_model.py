"""Chapter 4 and Chapter 25 tests: endpoints, profiles and the policy choices.

Endpoint selection is where a client either becomes portable or stays stuck on
one vendor's demo. These tests pin the ranking and the errors a client sees when
a server does not offer what was configured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from asyncua import ua

from opcua_toolkit.discovery import (
    BINARY_TRANSPORT_PROFILE,
    HTTPS_BINARY_TRANSPORT_PROFILE,
    WEBSOCKET_BINARY_TRANSPORT_PROFILE,
    DiscoveredServer,
    EndpointInfo,
    ServerProfile,
)
from opcua_toolkit.errors import EndpointNotFoundError, SecurityConfigurationError
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.settings import RECOMMENDED_POLICIES

POLICY_URI = "http://opcfoundation.org/UA/SecurityPolicy#"
BINARY_TRANSPORT = BINARY_TRANSPORT_PROFILE
HTTPS_TRANSPORT = HTTPS_BINARY_TRANSPORT_PROFILE
WEBSOCKET_TRANSPORT = WEBSOCKET_BINARY_TRANSPORT_PROFILE


def endpoint(
    *,
    policy: str = "None",
    mode: str = "None",
    url: str = "opc.tcp://127.0.0.1:4840",
    security_level: int = 1,
    identities: tuple[str, ...] = ("Anonymous",),
    transport: str = BINARY_TRANSPORT,
    certificate: bool = False,
) -> EndpointInfo:
    return EndpointInfo(
        endpoint_url=url,
        security_policy=f"{POLICY_URI}{policy}",
        security_mode=mode,
        security_level=security_level,
        user_identities=identities,
        transport_profile=transport,
        has_server_certificate=certificate,
        application_uri="urn:opcua-tutorial:demo",
    )


def profile(*endpoints: EndpointInfo, namespaces: tuple[str, ...] = ()) -> ServerProfile:
    return ServerProfile(
        endpoint_url="opc.tcp://127.0.0.1:4840",
        application_uri="urn:opcua-tutorial:demo",
        product_uri="https://github.com/opcua-tutorial/opcua-tutorial",
        application_name="opcua-tutorial demo server",
        namespaces=namespaces,
        endpoints=endpoints,
        limits=None,
        health=None,
    )


def test_endpoint_short_name_and_unsecured_flag_read_the_policy_uri() -> None:
    plain = endpoint()
    assert plain.policy_short_name == "None"
    assert plain.is_unsecured
    assert not plain.preferred

    secured = endpoint(policy="Basic256Sha256", mode="SignAndEncrypt", certificate=True)
    assert secured.policy_short_name == "Basic256Sha256"
    assert not secured.is_unsecured
    assert secured.preferred


def test_policy_none_is_never_preferred_even_when_the_mode_claims_to_sign() -> None:
    # ``is_unsecured`` needs both halves; a server that advertises a None policy with a
    # Sign mode is misconfigured. ``preferred`` is what protects the client anyway.
    odd = endpoint(policy="None", mode="Sign")
    assert not odd.preferred
    assert odd.security_policy.endswith("#None")


def test_endpoint_identity_support_is_what_the_token_type_says() -> None:
    anonymous_only = endpoint()
    assert anonymous_only.supports_identity("Anonymous")
    assert not anonymous_only.supports_identity("UserName")

    login = endpoint(
        policy="Basic256Sha256",
        mode="SignAndEncrypt",
        identities=("Anonymous", "UserName"),
        certificate=True,
    )
    assert login.supports_identity("UserName")


def test_asyncua_spells_the_none_security_mode_none_and_the_toolkit_normalises_it() -> None:
    # asyncua names the enum member None_ because None is a keyword. That spelling
    # must never leak into a comparison with what the client configured.
    raw = ua.MessageSecurityMode.None_
    assert raw.name == "None_"
    discovered = EndpointInfo(
        endpoint_url="opc.tcp://127.0.0.1:4840",
        security_policy=f"{POLICY_URI}None",
        security_mode=raw.name,
        security_level=0,
        user_identities=("Anonymous",),
        transport_profile=BINARY_TRANSPORT,
        has_server_certificate=False,
        application_uri="urn:opcua-tutorial:demo",
    )
    assert discovered.security_mode_name == "None"
    assert discovered.is_unsecured
    assert not discovered.preferred
    assert discovered.as_dict()["security_mode"] == "None"
    assert "unsecured" in discovered.line()


def test_transports_are_classified_by_scheme_because_a_client_has_to_pick_one() -> None:
    """A client does not choose "binary", it chooses a scheme it can actually speak.

    The trap this test exists for: the HTTPS and WebSockets profiles carry the same
    OPC UA binary encoding as ``opc.tcp``, so a client that asks "is this binary?"
    and gets "yes" has learned nothing about whether it can connect. asyncua speaks
    ``opc.tcp``, so only that scheme is usable to it.
    """
    tcp = endpoint(transport=BINARY_TRANSPORT)
    https = endpoint(transport=HTTPS_TRANSPORT)
    websockets = endpoint(transport=WEBSOCKET_TRANSPORT)

    assert tcp.transport_profile_name == "uatcp-uasc-uabinary"
    assert tcp.transport_scheme == "opc.tcp"
    assert tcp.uses_tcp_transport

    assert https.transport_scheme == "https"
    assert not https.uses_tcp_transport
    assert websockets.transport_scheme == "opc.wss"
    assert not websockets.uses_tcp_transport

    # All three carry the OPC UA binary encoding, and the word "binary" appears in
    # every one of their profile URIs. That is precisely why "is it binary?" is not a
    # question that can decide a connection, and the scheme is.
    assert all("binary" in info.transport_profile.lower() for info in (tcp, https, websockets))
    assert {info.transport_scheme for info in (tcp, https, websockets)} == {
        "opc.tcp",
        "https",
        "opc.wss",
    }


def test_an_unknown_transport_profile_is_reported_instead_of_assumed() -> None:
    """A profile this client does not know must not quietly count as usable."""
    odd = endpoint(transport="http://example.org/UA-Profile/Transport/embedded-tcp")
    assert odd.transport_scheme == ""
    assert not odd.uses_tcp_transport
    assert odd.as_dict()["transport_scheme"] == ""


def test_endpoint_line_output_is_stable_and_shows_the_security_level() -> None:
    line = endpoint(policy="Basic256Sha256", mode="Sign", security_level=3).line()
    assert "Basic256Sha256" in line
    assert "level=3" in line
    assert "identities=Anonymous" in line
    assert "unsecured" not in line


def test_endpoint_description_from_the_server_becomes_one_endpoint_info() -> None:
    description = ua.EndpointDescription()
    description.EndpointUrl = "opc.tcp://127.0.0.1:4840"
    description.SecurityPolicyUri = f"{POLICY_URI}Basic256Sha256"
    description.SecurityMode = ua.MessageSecurityMode.SignAndEncrypt
    description.SecurityLevel = 15
    description.TransportProfileUri = BINARY_TRANSPORT
    description.ServerCertificate = b"\x00\x01cert"
    token = ua.UserTokenPolicy()
    token.TokenType = ua.UserTokenType.UserName
    description.UserIdentityTokens = [token]
    description.Server.ApplicationUri = "urn:opcua-tutorial:demo"

    info = EndpointInfo.from_description(description)
    assert info.policy_short_name == "Basic256Sha256"
    assert info.security_mode == "SignAndEncrypt"
    assert info.security_level == 15
    assert info.has_server_certificate
    assert info.user_identities == ("UserName",)


def build_profile() -> ServerProfile:
    return profile(
        endpoint(url="opc.tcp://127.0.0.1:4840"),
        endpoint(
            url="opc.tcp://127.0.0.1:4840",
            policy="Basic256Sha256",
            mode="SignAndEncrypt",
            security_level=15,
            identities=("Anonymous", "UserName"),
            certificate=True,
        ),
        endpoint(
            url="opc.tcp://127.0.0.1:4841",
            policy="Aes256Sha256RsaPss",
            mode="Sign",
            security_level=13,
            certificate=True,
        ),
        namespaces=("http://opcfoundation.org/UA/", "urn:opcua-tutorial:demo"),
    )


def test_a_profile_separates_unsecured_from_secured_endpoints() -> None:
    discovered = build_profile()
    assert len(discovered.endpoints) == 3
    assert len(discovered.secured_endpoints) == 2
    assert discovered.supports_security


def test_best_endpoint_prefers_the_recommended_policy_with_a_certificate() -> None:
    best = build_profile().best_endpoint()
    assert best.policy_short_name in RECOMMENDED_POLICIES
    assert best.has_server_certificate
    assert best.security_mode in {"SignAndEncrypt", "Sign"}


def test_a_server_without_a_certificate_is_a_worse_choice_than_one_with_one() -> None:
    # Same policy and mode: without a published certificate the client cannot trust it.
    candidate = profile(
        endpoint(
            policy="Basic256Sha256",
            mode="SignAndEncrypt",
            security_level=15,
            certificate=True,
            url="opc.tcp://with-cert:4840",
        ),
        endpoint(
            policy="Basic256Sha256",
            mode="SignAndEncrypt",
            security_level=15,
            certificate=False,
            url="opc.tcp://without-cert:4840",
        ),
    )
    assert candidate.best_endpoint().endpoint_url == "opc.tcp://with-cert:4840"


def test_the_tcp_transport_wins_when_the_security_is_identical() -> None:
    """Two endpoints, same policy, same mode, same certificate: transport decides it.

    A client that ranks endpoints by security level alone will happily select the
    HTTPS one and then fail to connect at all, because the failure is in the
    transport, not in the security policy. Ranking the ``opc.tcp`` profile first is
    what turns "cannot connect" into "selected an endpoint we can speak".
    """
    candidate = profile(
        endpoint(
            policy="Basic256Sha256",
            mode="SignAndEncrypt",
            security_level=15,
            certificate=True,
            transport=HTTPS_TRANSPORT,
            url="https://plant.example:8443",
        ),
        endpoint(
            policy="Basic256Sha256",
            mode="SignAndEncrypt",
            security_level=15,
            certificate=True,
            transport=BINARY_TRANSPORT,
            url="opc.tcp://plant.example:4840",
        ),
    )
    best = candidate.best_endpoint()
    assert best.endpoint_url == "opc.tcp://plant.example:4840"
    assert best.uses_tcp_transport
    # The rejected one is identical in every security respect: only the transport
    # differs, and that is enough to make it unusable here.
    runner_up = next(e for e in candidate.endpoints if e is not best)
    assert runner_up.security_level == best.security_level
    assert runner_up.transport_scheme == "https"


def test_an_endpoint_without_endpoints_is_an_error_not_a_none() -> None:
    with pytest.raises(EndpointNotFoundError, match="no endpoints"):
        profile().best_endpoint()


def test_endpoint_for_finds_the_configured_combination_and_names_the_alternatives() -> None:
    discovered = build_profile()
    assert discovered.endpoint_for("Aes256Sha256RsaPss", "Sign").security_mode == "Sign"
    with pytest.raises(EndpointNotFoundError, match="does not offer"):
        discovered.endpoint_for("Aes128Sha256RsaOaep")


def test_security_settings_for_needs_the_files_a_secured_channel_really_requires() -> None:
    discovered = build_profile()

    plain = discovered.security_settings_for("None", "None")
    assert not plain.enabled

    # Knowing the endpoint is not enough: a secured policy needs a certificate and a key.
    with pytest.raises(SecurityConfigurationError, match="certificate and private_key"):
        discovered.security_settings_for("Basic256Sha256", "SignAndEncrypt")

    configured = discovered.security_settings_for(
        "Basic256Sha256",
        "SignAndEncrypt",
        certificate=Path("certs/client.pem"),
        private_key=Path("certs/client.key"),
        private_key_password="s3cret",
    )
    assert configured.enabled
    assert configured.policy == "Basic256Sha256"
    assert configured.mode == "SignAndEncrypt"
    assert "s3cret" not in configured.describe()


def test_a_policy_this_client_does_not_speak_is_refused_and_not_quieted_down() -> None:
    """An old policy on the server gets an answer, not a quieter connection.

    ``Basic128Rsa15`` is exactly what a 2015-era vendor server still publishes. Mapping that
    request onto ``None`` would put the caller on the endpoint they asked about with a
    protection profile they never chose - and Chapter 13's own mistake table says that is the
    failure to design out, so it is refused here with both names in the message.
    """
    discovered = profile(
        endpoint(policy="Basic128Rsa15", mode="SignAndEncrypt", security_level=1),
        namespaces=("http://opcfoundation.org/UA/",),
    )

    with pytest.raises(SecurityConfigurationError, match="does not speak") as raised:
        discovered.security_settings_for("Basic128Rsa15", "SignAndEncrypt")

    message = str(raised.value)
    assert "Basic128Rsa15" in message
    assert "None" in message
    assert "Basic256Sha256" in message


def test_namespace_lookup_is_by_uri_fragment_and_fails_with_the_published_list() -> None:
    discovered = build_profile()
    assert discovered.namespace_index_of("opcua-tutorial") == 1
    assert discovered.application_namespace_index == 1
    with pytest.raises(EndpointNotFoundError, match="no namespace matching"):
        discovered.namespace_index_of("something-else")


def test_a_single_namespace_server_has_no_application_namespace() -> None:
    plain = profile(endpoint(), namespaces=("http://opcfoundation.org/UA/",))
    assert plain.application_namespace_index == 0


def test_profile_describe_and_as_dict_tell_the_same_story() -> None:
    discovered = ServerProfile(
        endpoint_url="opc.tcp://127.0.0.1:4840",
        application_uri="urn:opcua-tutorial:demo",
        product_uri="https://example.invalid/product",
        application_name="demo",
        namespaces=("http://opcfoundation.org/UA/", "urn:opcua-tutorial:demo"),
        endpoints=(endpoint(),),
        limits=OperationLimits(max_nodes_per_read=10),
        health=None,
    )
    described = discovered.describe()
    assert "urn:opcua-tutorial:demo" in described
    assert "max_nodes_per_read: 10" in described
    payload = discovered.as_dict()
    json.dumps(payload)
    assert payload["limits"]["max_nodes_per_read"] == 10
    assert payload["endpoints"][0]["unsecured"] is True


def test_a_server_with_only_unsecured_endpoints_reports_no_security() -> None:
    plain = profile(endpoint(), namespaces=("http://opcfoundation.org/UA/",))
    assert not plain.supports_security
    assert "unsecured endpoints only" in plain.describe()


def test_discovered_server_reads_a_find_servers_description() -> None:
    description = ua.ApplicationDescription()
    description.ApplicationUri = "urn:opcua-tutorial:demo"
    description.ApplicationName = ua.LocalizedText(Text="opcua-tutorial demo server")
    description.ProductUri = "https://example.invalid/product"
    description.ApplicationType = ua.ApplicationType.Server
    description.DiscoveryUrls = ["opc.tcp://127.0.0.1:4840"]

    server = DiscoveredServer.from_description(description)
    assert server.application_name == "opcua-tutorial demo server"
    assert server.application_type == "Server"
    assert server.discovery_urls == ("opc.tcp://127.0.0.1:4840",)
    assert "opc.tcp://127.0.0.1:4840" in server.line()
    json.dumps(server.as_dict())
