"""Scaffolding shared by the chapter example scripts.

Nothing in this module is part of the toolkit: :mod:`opcua_toolkit` is what the
tutorial teaches, and this file is only the plumbing that lets every example run
in exactly two modes, so that a chapter can be verified with no OPC UA product
installed and still be pointed at a real plant afterwards::

    uv run python examples/ch07_bulk_read.py --demo
    uv run python examples/ch07_bulk_read.py --endpoint opc.tcp://127.0.0.1:4840
    uv run python examples/ch07_bulk_read.py --demo --demo-port 48401   # for UaExpert

Three rules keep the examples honest:

* the client always comes from :meth:`ClientSettings.create_client`, the same
  path the library chapters use, so an example can never drift from the tutorial;
* the demo server is a *tutorial* helper (:class:`opcua_toolkit.DemoServer`), not
  a mock: it speaks real OPC UA on a real socket, which is why the examples and
  the integration tests can teach the same behaviour;
* output is printed, not asserted. Examples show you a result; the test suite is
  what proves it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from asyncua import Client, ua

from opcua_toolkit import (
    ClientSettings,
    ConfigurationError,
    DemoServer,
    DemoServerConfig,
    NodeRef,
    OperationLimits,
    configure_logging,
)

LOG_LEVELS: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR")

#: Seconds an example waits for a disconnect before it moves on. A chapter that
#: stopped its own server must still print its own findings.
DISCONNECT_TIMEOUT = 10.0


def build_parser(description: str) -> argparse.ArgumentParser:
    """Build the parser every example shares.

    Args:
        description: One sentence naming the question this chapter answers.
    """
    parser = argparse.ArgumentParser(
        description=description,
        epilog="--demo starts the tutorial's own server; --endpoint uses a server you run.",
    )
    parser.add_argument(
        "--endpoint", help="Server URL to connect to, e.g. opc.tcp://127.0.0.1:4840"
    )
    parser.add_argument("--demo", action="store_true", help="Run against the tutorial demo server")
    parser.add_argument(
        "--demo-port", type=int, default=0, help="Port for --demo (0 picks a free port)"
    )
    parser.add_argument("--log-level", default="INFO", choices=LOG_LEVELS, help="Toolkit log level")
    return parser


class ChapterRun:
    """One example run: the settings to connect with, plus the server we started.

    Created by :func:`chapter_run`; do not build it by hand.
    """

    def __init__(self, settings: ClientSettings, server: DemoServer | None) -> None:
        self._settings = settings
        self._server = server

    @property
    def settings(self) -> ClientSettings:
        """The validated configuration this example is running with."""
        return self._settings

    @property
    def endpoint(self) -> str:
        """The endpoint under test, including the port the demo server bound to."""
        return self._settings.endpoint_url

    @property
    def uses_demo_server(self) -> bool:
        """Whether the server under test is the tutorial's own."""
        return self._server is not None

    @property
    def demo_server(self) -> DemoServer:
        """The demo server, for chapters that change the server side on purpose.

        Raises:
            ConfigurationError: When the example was run with ``--endpoint``,
                because then there is no demo server to change.
        """
        if self._server is None:
            raise ConfigurationError(
                "this step changes the demo server: run the example with --demo instead"
            )
        return self._server

    async def restart_demo_server(self) -> DemoServer:
        """Take the demo server down and bring it back on the same port.

        Chapter 12 needs an outage that a client can recover from, and a fixed
        address is what makes "the same server came back" true.
        """
        server = self.demo_server
        config = dataclasses.replace(server.config, port=server.bound_port)
        await server.stop()
        restarted = DemoServer(config)
        await restarted.start()
        self._server = restarted
        return restarted

    def settings_for(self, **updates: Any) -> ClientSettings:
        """Return these settings with a few fields changed.

        Used by Chapter 13, which configures the client from what the server
        told it rather than from constants in the source.
        """
        return self._settings.model_copy(update=updates)

    @contextlib.asynccontextmanager
    async def open_client(
        self,
        *,
        auto_reconnect: bool = False,
        settings: ClientSettings | None = None,
    ) -> AsyncIterator[Client]:
        """Connect a client built from these settings, and always close it.

        ``settings`` is Chapter 14's lever: a client built from a configuration file
        resolves its own :class:`ClientSettings`, and the run only supplies the endpoint the
        file deliberately does not. Passing nothing keeps the behaviour every other chapter
        uses.

        ``auto_reconnect`` is Chapter 12's lever: asyncua's own supervisor keeps
        the session and its subscriptions alive while the toolkit's supervisor
        decides what the application should do about it.

        The disconnect is deliberately forgiving: a chapter that stopped the
        server as part of the demonstration must end with its own output, not
        with a teardown traceback.
        """
        settings = settings if settings is not None else self._settings
        client = settings.create_client()
        try:
            await client.connect(auto_reconnect=auto_reconnect)
            yield client
        finally:
            try:
                async with asyncio.timeout(DISCONNECT_TIMEOUT):
                    await client.disconnect()
            except Exception:
                # A chapter that stopped its own server must still print its findings.
                pass

    async def read_limits(self, client: Client) -> OperationLimits:
        """Read the server's own operation limits, the numbers every chapter chunks to."""
        return await OperationLimits.read(client)


@contextlib.asynccontextmanager
async def chapter_run(
    description: str,
    *,
    session_name: str,
    simulate: bool = True,
    publish_limits: bool = True,
    authored_model: bool = False,
) -> AsyncIterator[ChapterRun]:
    """Parse the shared arguments, start the demo server if asked, hand over the run.

    Args:
        description: The chapter's question, shown by ``--help``.
        session_name: OPC UA session name, which is what the server shows in its
            own session list - useful when you verify with UaExpert side by side.
        simulate: Whether the demo server keeps writing its process tags. On for
            the subscription chapters (data has to change for it to arrive), off
            for chapters that write values themselves and must not compete with
            the simulator.
        publish_limits: Set to ``False`` to see how the toolkit behaves against a
            server that publishes no OperationLimits.
        authored_model: Set to ``True`` for the chapters that need a server with a
            *declared* address space: ObjectTypes, InstanceDeclarations and instances
            of them, in an extra ``AssetModel`` folder (Chapter 16). Left off, the demo
            server is exactly the server Chapters 4 to 15 measured.
    """
    args = build_parser(description).parse_args()
    configure_logging(args.log_level)
    if args.endpoint and args.demo:
        raise ConfigurationError("choose one: --endpoint points at a server, --demo starts one")

    server: DemoServer | None = None
    if args.endpoint:
        endpoint = args.endpoint
    elif args.demo:
        config = DemoServerConfig(
            port=args.demo_port,
            simulate=simulate,
            publish_limits=publish_limits,
            authored_model=authored_model,
        )
        server = DemoServer(config)
        await server.start()
        endpoint = server.endpoint
        print(f"demo server: {endpoint} (namespace {server.namespace_index})")
    else:
        raise ConfigurationError(
            "pass --demo to start the tutorial server, or --endpoint to use yours"
        )

    settings = ClientSettings(endpoint_url=endpoint, session_name=session_name)
    try:
        yield ChapterRun(settings=settings, server=server)
    finally:
        if server is not None:
            await server.stop()


def heading(title: str) -> None:
    """Print a section title so a reader can follow what the example is proving."""
    print(f"\n{title}")
    print("-" * len(title))


def bullet(text: str) -> None:
    """Print one line of findings."""
    print(f"  {text}")


#: Namespace 0 is the OPC UA standard namespace. Everything in it is the server
#: describing itself: ServerStatus, ServiceLevel, BuildInfo. The plant's tags are
#: never in ns=0, which is the one namespace rule a client may rely on.
STANDARD_NAMESPACE_INDEX = 0


def plant_variables(refs: Iterable[NodeRef]) -> list[NodeRef]:
    """Select the variables that belong to the server's own namespaces.

    A scan from ``ObjectsFolder`` collects the server's diagnostic variables too,
    and a chapter that subscribes to all of them spends its monitored items on
    ``CurrentTime`` instead of on the process. Every chapter that needs "the tags"
    filters a discovered list this way rather than trusting a name it remembered.
    """
    return [
        ref
        for ref in refs
        if ref.node_class is ua.NodeClass.Variable
        and ref.node_id.NamespaceIndex != STANDARD_NAMESPACE_INDEX
    ]


#: The OPC UA services the toolkit uses. Counting these is how an example shows a
#: request count instead of asserting one, and it is the same list the integration
#: tests count on, which is why an example and a test can agree. The names are the
#: ones *asyncua's session object* exposes, not the OPC UA service names: the
#: service called ModifySubscription is ``update_subscription`` here, and counting
#: it under its specification name would record nothing - a lesson worth learning
#: before you debug a real client by counting calls.
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
)


def instrument_service_calls(
    client: Client, names: Sequence[str] = COUNTED_SERVICES
) -> dict[str, int]:
    """Count OPC UA service calls on the client's own session.

    Deliberate instrumentation, never production code. A chapter that claims "24 tags
    became 3 Read requests" has to show the count, and the honest place to count is
    the layer the requests are actually made on.

    Args:
        client: A connected client.
        names: Session methods to wrap.

    Returns:
        Call counts, zeroed. The wrap sits on ``client.uaclient.session`` and not on
        the ``uaclient`` facade because subscriptions and nodes hold the session:
        wrapping the facade records zero for exactly the calls you wanted to see.
    """
    counters: dict[str, int] = {}
    session = client.uaclient.session
    for name in names:
        original = getattr(session, name, None)
        if original is None:
            continue
        counters[name] = 0

        def wrap(service_name: str = name, wrapped: Any = original) -> Any:
            async def counted(*args: Any, **kwargs: Any) -> Any:
                counters[service_name] += 1
                return await wrapped(*args, **kwargs)

            return counted

        setattr(session, name, wrap())
    return counters


#: Values for a data type you know only by name. This is the minimum that makes
#: Chapter 9's "call a method you have never seen" possible; it is not a general
#: purpose converter, and it refuses to guess when a guess would write something real.
SAMPLE_VALUES: dict[str, Any] = {
    "Boolean": True,
    "Byte": 1,
    "SByte": 1,
    "Int16": 7,
    "UInt16": 7,
    "Int32": 7,
    "UInt32": 7,
    "Int64": 7,
    "UInt64": 7,
    "Float": 3.5,
    "Double": 3.5,
    "Decimal": 3.5,
    "Duration": 3.5,
    "Integer": 7,
    "UInteger": 7,
    "String": "written by the tutorial",
    "ByteString": b"written by the tutorial",
    "XmlElement": "<tutorial/>",
}


def sample_value(type_name: str, *, default: Any = None) -> Any:
    """Return a value a server is likely to accept for a data type name.

    Args:
        type_name: The name the server published for a data type (``UInt32``, ``Double``, ...).
        default: What to return for a type this helper does not know. Left as ``None``,
            the helper raises instead of inventing a value.

    Raises:
        ConfigurationError: Unknown type and no default.
    """
    if type_name in {"DateTime", "UtcTime"}:
        return datetime.now(UTC)
    if type_name in {"Guid", "Uuid"}:
        return uuid.uuid4()
    if type_name == "NodeId":
        return ua.NodeId(0, 0)
    if type_name == "StatusCode":
        return ua.StatusCode(0)
    if type_name in {"LocalizedText", "LocaleText", "Text"}:
        return ua.LocalizedText("written by the tutorial")
    if type_name in SAMPLE_VALUES:
        return SAMPLE_VALUES[type_name]
    if default is not None:
        return default
    raise ConfigurationError(f"no sample value for data type {type_name!r}")
