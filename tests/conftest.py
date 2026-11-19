"""Fixtures shared by the whole test suite.

Every integration test runs against the tutorial's own demo server on a port the
OS picks (``port=0``), so the suite is hermetic: no vendor product, no network,
no fixed port, and it runs in CI on a machine that has never seen OPC UA.

The fixtures are deliberately small and predictable:

* ``demo_server``   - one live :class:`DemoServer` per test.
* ``client``        - a connected asyncua client for that server.
* ``limits``        - the limits that server actually published.
* ``service_calls`` - a counter per OPC UA service, used to *prove* that
  batching and chunking happened instead of trusting that they did.

Where the counters sit is a lesson in itself. asyncua's ``Client`` delegates to
``uaclient``, and ``uaclient`` delegates to a ``UaSession``. Subscriptions and
nodes hold the *session*, not the ``uaclient`` facade, so wrapping ``uaclient``
silently records zero for exactly the calls a subscription makes. Counting on the
session counts everything, which is what a Chapter 7 measurement has to be.
"""

from __future__ import annotations

import contextlib
import socket
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
from asyncua import Client

from opcua_toolkit import ClientSettings, DemoServer, DemoServerConfig, OperationLimits
from opcua_toolkit.errors import format_node_id

#: The service calls the toolkit uses. Counting these is how a test can show that
#: 24 nodes became 3 Read requests, not 24. The names are the ones asyncua's session
#: object exposes, not the OPC UA service names: ModifySubscription is
#: ``update_subscription`` there, and counting it under its specification name would
#: record nothing at all.
COUNTED_SERVICES: tuple[str, ...] = (
    "read",
    "read_attributes",
    "write",
    "write_attributes",
    "browse",
    "browse_next",
    "register_nodes",
    "unregister_nodes",
    "call",
    "translate_browsepaths_to_nodeids",
    "create_subscription",
    "update_subscription",
    "publish",
    "set_publishing_mode",
    "set_monitoring_mode",
    "create_monitored_items",
    "modify_monitored_items",
    "delete_monitored_items",
    "delete_subscriptions",
    "republish",
)


def free_port() -> int:
    """A port nothing is listening on, for the rare test that has to name one.

    The suite runs servers on ``port=0`` so parallel runs never collide. A test that
    asserts on an *announced* address cannot: a server cannot publish a port the OS has
    not chosen yet, so it has to be told one, and this is how it gets one without hoping
    a magic number is free.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def demo_config() -> DemoServerConfig:
    """Demo server settings tuned for tests: a free port and no simulator.

    The simulator is switched off because it writes process values every 150 ms.
    A subscription test that expects "one notification because I wrote a value"
    must not have to compete with a background writer that changes the same tag.
    Tests that *want* spontaneous changes drive them explicitly with
    :meth:`DemoServer.set_value`.
    """
    return DemoServerConfig(port=0, simulate=False)


@pytest.fixture
async def demo_server(demo_config: DemoServerConfig) -> AsyncIterator[DemoServer]:
    """A running demo server, always shut down even when the test fails."""
    server = DemoServer(demo_config)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
async def connect() -> AsyncIterator[Callable[[str], Awaitable[Client]]]:
    """Factory that opens (and always closes) a client for an endpoint."""
    opened: list[Client] = []

    async def _connect(endpoint: str) -> Client:
        settings = ClientSettings(endpoint_url=endpoint, request_timeout=8.0, session_timeout=30.0)
        client = settings.create_client()
        await client.connect()
        opened.append(client)
        return client

    try:
        yield _connect
    finally:
        for client in opened:
            with contextlib.suppress(Exception):
                await client.disconnect()


@pytest.fixture
async def client(connect: Callable[[str], Awaitable[Client]], demo_server: DemoServer) -> Client:
    """A connected client for the demo server."""
    return await connect(demo_server.endpoint)


@pytest.fixture
def service_calls(client: Client) -> dict[str, int]:
    """Per-service call counters for ``client``.

    The counters wrap the client's *session*, which is where every service request
    is actually sent: node reads go through ``session.read``, and a subscription
    calls ``session.create_monitored_items`` on the session object it was handed.
    Wrapping the ``uaclient`` facade instead counts nothing for those paths, which
    is a bug a test suite must not have. Call ``.clear()`` before the section you
    want to measure when an earlier fixture already used the service.
    """
    counts: defaultdict[str, int] = defaultdict(int)
    session = client.uaclient.session
    for name in COUNTED_SERVICES:
        original = getattr(session, name, None)
        if original is None:
            continue
        counts[name] = 0

        def wrap(service_name: str = name, wrapped: Any = original) -> Any:
            async def counted(*args: Any, **kwargs: Any) -> Any:
                counts[service_name] += 1
                return await wrapped(*args, **kwargs)

            return counted

        setattr(session, name, wrap())
    return counts


@pytest.fixture
async def limits(client: Client, service_calls: dict[str, int]) -> OperationLimits:
    """The operation limits the demo server publishes."""
    return await OperationLimits.read(client)


@pytest.fixture
def demo_tag_texts(demo_server: DemoServer) -> list[str]:
    """Canonical NodeId texts of the demo batch tags (the bulk IO playground)."""
    return [format_node_id(node_id) for node_id in demo_server.tag_ids()]
