"""Unit tests for the demo server's bind/advertise split.

A server has two addresses and people routinely write only one of them down: the host it
listens on, and the host it tells clients about. Inside a container those are different
hosts, and getting them the wrong way round produces a server that works locally and is
unreachable from anywhere else - which reads like a network problem and is a config one.
"""

from __future__ import annotations

import pytest

from opcua_toolkit.devserver import (
    DEFAULT_HOST,
    DemoServerConfig,
    build_argument_parser,
    config_from_args,
)

#: The host a client outside a container would use.
EXTERNAL_HOST = "opcua.example"

#: Any port a test can safely name, because the server is never started here.
PORT = 48401


def test_a_server_announces_what_it_binds_when_nothing_else_is_configured() -> None:
    config = DemoServerConfig(host=DEFAULT_HOST, port=PORT)
    assert config.endpoint == f"opc.tcp://{DEFAULT_HOST}:{PORT}"


def test_the_advertised_host_changes_the_published_url_and_not_the_bind_host() -> None:
    """Binding and announcing are separate settings, and both have to be readable."""
    config = DemoServerConfig(host="0.0.0.0", port=PORT, advertised_host=EXTERNAL_HOST)

    assert config.host == "0.0.0.0"
    assert config.endpoint == f"opc.tcp://{EXTERNAL_HOST}:{PORT}"


def test_an_advertised_host_needs_a_port_the_server_already_knows() -> None:
    """``port=0`` and an advertised host cannot both be true.

    The port is chosen by the operating system, so a server that announces a fixed host
    would have to announce a port it has not been given yet. Failing at construction is
    the only honest option: a wrong announcement is not something a client can detect.
    """
    with pytest.raises(ValueError, match="fixed port"):
        DemoServerConfig(port=0, advertised_host=EXTERNAL_HOST)


def test_the_command_line_exposes_the_same_split_as_the_config() -> None:
    """``--host`` binds and ``--advertise-host`` announces; a container needs both.

    The demo server is what people run first, so the knob has to be a flag and not a
    detail of the Python API.
    """
    args = build_argument_parser().parse_args(
        ["--host", "0.0.0.0", "--port", str(PORT), "--advertise-host", EXTERNAL_HOST]
    )
    config = config_from_args(args)

    assert config.host == "0.0.0.0"
    assert config.advertised_host == EXTERNAL_HOST
    assert config.endpoint == f"opc.tcp://{EXTERNAL_HOST}:{PORT}"
