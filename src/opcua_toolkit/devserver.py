"""The tutorial's demo server.

Every client chapter (Chapter 4 to Chapter 13) and the test suite run against
*this* server, so the tutorial never depends on a vendor product being
installed. It is deliberately built to expose the things a real plant exposes:

* a namespace you have to discover (nothing is hardcoded except NodeId 0 nodes);
* variables of different data types, including ``Float`` (``Single``) so that
  Chapter 8 can demonstrate ``BadTypeMismatch`` for real;
* read-only variables so that Chapter 8 can demonstrate ``BadNotWritable``;
* a batch of tags so that Chapter 7 can measure bulk versus one-by-one reads;
* real ``OperationLimits`` values so that chunking is observable instead of
  theoretical;
* methods with declared arguments so that Chapter 9 can discover and call them;
* ``ServerState`` and ``ServiceLevel`` hooks so that Chapter 12 can show a
  server that is reachable but unhealthy.

Run it with ``uv run opcua-tutorial-server`` and point UaExpert or the CLI at
``opc.tcp://127.0.0.1:48401``.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import math
import re
import signal
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from asyncua import Node, Server, ua
from asyncua.server.history_sql import HistorySQLite
from asyncua.ua.uaerrors import UaError

from opcua_toolkit.diagnostics import configure_logging
from opcua_toolkit.errors import format_node_id
from opcua_toolkit.limits import LIMIT_PROPERTY_IDS
from opcua_toolkit.server import (
    AddressSpaceAuthor,
    ComponentSpec,
    InstanceReport,
    InstanceSpec,
    ModellingRule,
    ObjectTypeSpec,
    TypeReport,
    VariableAccess,
    VariableTypeSpec,
)
from opcua_toolkit.values import unwrap as _unwrap_value

_logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 48401
DEFAULT_NAMESPACE_URI = "urn:opcua-tutorial:demo"
DEFAULT_APPLICATION_NAME = "opcua-tutorial demo server"

#: NodeIds of the OperationLimits properties the demo server publishes. The same
#: mapping the client chapters read, which is what makes the demo honest.
_LIMIT_NODES: dict[str, int] = LIMIT_PROPERTY_IDS

#: Alarm ids the demo ``AcknowledgeAlarm`` method accepts. Anything else returns
#: False with a Good status - the lesson that a Good status is not a success.
_KNOWN_ALARMS: frozenset[str] = frozenset({"A1", "A2", "A3"})


@dataclasses.dataclass(frozen=True, slots=True)
class TagSpec:
    """One demo variable: its name, type, starting value and access rights."""

    name: str
    variant_type: ua.VariantType
    initial: object
    writable: bool
    description: str
    value_rank: int = -1
    array_initial: tuple[object, ...] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class DemoServerConfig:
    """How one instance of the demo server should look.

    Attributes:
        port: 0 means "pick any free port", which is what the test fixtures use
            so that parallel test runs never collide.
        publish_limits: Write real OperationLimits values. Turning this off shows
            how the toolkit behaves against a server that publishes nothing.
        max_nodes_per_read: Deliberately small so that Chapter 7 can *see*
            chunking happen instead of trusting that it does.
        min_supported_sample_rate: Published as ``MinimumSupportedSampleRate``.
            asyncua's server publishes it but does not enforce it, which Chapter 10
            uses to teach the difference between a limit a server advertises and a
            limit a server applies.
        subscription_lifetime_cap: The demo server's cap for
            ``RevisedLifetimeCount``, which asyncua's stock default (15 000) never
            revises. Chapter 10 needs a server that *answers with different numbers*
            than the ones it was asked for, because that is what a real server does.
        subscription_keepalive_cap: Same idea for ``RevisedMaxKeepAliveCount``.
        monitored_item_queue_cap: Cap for a monitored item's queue. The client sees
            the effect (dropped notifications), not the number: asyncua's high level
            create path does not report ``RevisedQueueSize``.
        max_subscriptions: Small on purpose, so Chapter 12 can show a server that
            answers ``BadTooManySubscriptions`` instead of quietly accepting more.
        advertised_host: The host this server publishes as its own, when that is not the
            host it binds. This is the container and reverse-proxy case: a server that
            binds ``0.0.0.0`` and announces ``opc.tcp://0.0.0.0:4840`` is telling clients
            to connect to nowhere. It needs a fixed port, because a port the OS picked
            cannot be announced before it is known. Note what asyncua does with it: an
            answered ``GetEndpoints`` rewrites the published URL from the ``EndpointUrl``
            the client asked about, so the announced host is a mirror of the question, and
            the thing that has to be right on the wire is the port mapping.
        authored_model: Off by default. When it is on, the server *authors* an address
            space of its own in ``AssetModel``: declared ObjectTypes, their
            InstanceDeclarations and two pump instances, all through
            :class:`~opcua_toolkit.server.authoring.AddressSpaceAuthor`. It is opt-in
            because Chapters 4 to 15 measure a server whose every variable is a plain
            ``BaseDataVariableType`` - that is the address space they teach against - and
            a tutorial server that quietly changes it under them is worse than two
            servers. Chapter 16 is the chapter that turns this on.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    advertised_host: str | None = None
    namespace_uri: str = DEFAULT_NAMESPACE_URI
    application_name: str = DEFAULT_APPLICATION_NAME
    manufacturer_name: str = "opcua-tutorial"
    product_uri: str = "https://github.com/opcua-tutorial/opcua-tutorial"
    batch_tag_count: int = 24
    publish_limits: bool = True
    max_nodes_per_read: int = 10
    max_nodes_per_write: int = 10
    max_nodes_per_browse: int = 10
    max_nodes_per_method_call: int = 5
    max_monitored_items_per_call: int = 10
    max_browse_continuation_points: int = 20
    simulate: bool = True
    simulation_interval: float = 0.25
    historize: bool = False
    history_path: Path | None = None
    min_supported_sample_rate: float = 50.0
    subscription_lifetime_cap: int = 1_000
    subscription_keepalive_cap: int = 100
    monitored_item_queue_cap: int = 100
    max_subscriptions: int = 20
    authored_model: bool = False

    @property
    def endpoint(self) -> str:
        """The ``opc.tcp`` URL this configuration announces, before anything is bound.

        Not necessarily the address the server listens on: ``host`` is what it binds,
        ``advertised_host`` is what clients are told. Keeping them apart is what makes a
        container reachable at all.
        """
        return f"opc.tcp://{self.advertised_host or self.host}:{self.port}"

    def __post_init__(self) -> None:
        """Reject the one combination that would announce a URL nobody can reach."""
        if self.advertised_host is not None and self.port == 0:
            raise ValueError(
                "advertised_host needs a fixed port: port=0 lets the operating system pick "
                "the port, and a server cannot announce a port it does not know yet"
            )


# asyncua's history backend is an untyped boundary for this project (see the mypy
# overrides in pyproject.toml), so its base class reaches here as Any.
class DemoHistorySQLite(HistorySQLite):  # type: ignore[misc]
    """SQLite history backend with table names that survive string NodeIds.

    asyncua derives the table name from ``repr()`` of the NodeId identifier, which
    for a string NodeId is ``2_'Temperature'`` - and its own SQL injection guard
    then rejects the quotes. Overriding the table name is the honest fix: the
    tutorial's demo tags use string NodeIds on purpose. Chapter 20 revisits this
    when we build a historian that is ours instead of the server's.
    """

    def _get_table_name(self, node_id: ua.NodeId) -> str:
        safe_identifier = re.sub(r"[^A-Za-z0-9_]", "_", str(node_id.Identifier))
        return f"history_ns{node_id.NamespaceIndex}_{safe_identifier}"


class DemoServer:
    """An in-process OPC UA server with a tutorial shaped address space.

    Used as an async context manager so failures still release the port::

        async with DemoServer(DemoServerConfig(port=0)) as server:
            settings = ClientSettings(endpoint_url=server.endpoint)

    With :attr:`DemoServerConfig.authored_model` switched on it *also* authors the declared
    model of Chapter 16 - ``MachineType``, ``PumpType`` and two pumps - in its own
    ``AssetModel`` folder. Switched off, which is the default, nothing about the address
    space the earlier chapters measure changes.
    """

    def __init__(self, config: DemoServerConfig | None = None) -> None:
        self._config = config if config is not None else DemoServerConfig()
        self._server: Server | None = None
        self._bound_endpoint: str | None = None
        self._namespace_index: int = 0
        self._tags: dict[str, ua.NodeId] = {}
        self._method_names: dict[str, ua.NodeId] = {}
        self._writable_names: set[str] = set()
        self._batch_names: list[str] = []
        self._nodes: dict[str, Node] = {}
        self._authored_types: dict[str, TypeReport] = {}
        self._authored_instances: dict[str, InstanceReport] = {}
        self._author: AddressSpaceAuthor | None = None
        self._simulator: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._history: HistorySQLite | None = None
        self._run_mode = 1
        self._tick = 0

    @property
    def config(self) -> DemoServerConfig:
        """The configuration this server was built from."""
        return self._config

    @property
    def endpoint(self) -> str:
        """The URL to connect to: the announced URL, with the port actually bound.

        With ``port=0`` the OS picks the port, and asyncua writes the real binding back
        onto its binary server. Reading it here is what lets tests run in parallel without
        fighting over a fixed port - and it stays readable after :meth:`stop`, so a test
        can report the endpoint it just used.

        The host part comes from ``advertised_host`` when one is configured: a client has
        to be told the address it can actually reach, not the one the process bound to.
        """
        if self._bound_endpoint is not None:
            return self._bound_endpoint
        host, port = self._bound_address()
        return f"opc.tcp://{self._config.advertised_host or host}:{port}"

    def _bound_address(self) -> tuple[str, int]:
        """Where this server is actually listening, once it is listening."""
        server = self._server
        if server is not None:
            binary_server = server.bserver
            if binary_server is not None and binary_server.port:
                return str(binary_server.hostname), int(binary_server.port)
            if server.socket_address is not None:
                host, port = server.socket_address
                return str(host), int(port)
        return self._config.host, self._config.port

    @property
    def bound_port(self) -> int:
        """The TCP port actually in use, which is not ``config.port`` when it was 0."""
        return int(self.endpoint.rsplit(":", 1)[-1])

    @property
    def namespace_index(self) -> int:
        """Namespace index assigned to the demo namespace by the server."""
        return self._namespace_index

    @property
    def namespace_uri(self) -> str:
        """Namespace URI registered by this server."""
        return self._config.namespace_uri

    @property
    def tags(self) -> dict[str, ua.NodeId]:
        """``{browse name: NodeId}`` for every demo variable."""
        return dict(self._tags)

    @property
    def methods(self) -> dict[str, ua.NodeId]:
        """``{browse name: NodeId}`` for every demo method."""
        return dict(self._method_names)

    @property
    def authored_types(self) -> dict[str, TypeReport]:
        """``{BrowseName: TypeReport}`` for the ObjectTypes this server authored.

        Empty unless :attr:`DemoServerConfig.authored_model` was on. Each report was read
        back from the server, so it says what the address space now holds - including the
        corrections the author had to make to get there.
        """
        return dict(self._authored_types)

    @property
    def authored_instances(self) -> dict[str, InstanceReport]:
        """``{BrowseName: InstanceReport}`` for the instances the authored types produced."""
        return dict(self._authored_instances)

    @property
    def authored_folder_id(self) -> ua.NodeId | None:
        """The ``AssetModel`` folder the authored model lives in, or ``None`` when off."""
        node = self._nodes.get("asset_model")
        return node.nodeid if node is not None else None

    @property
    def author(self) -> AddressSpaceAuthor | None:
        """The author that wrote the authored section, or ``None`` when it is switched off."""
        return self._author

    @property
    def line_node_id(self) -> ua.NodeId:
        """NodeId of the ``Line1`` object that owns the methods."""
        node = self._nodes.get("line")
        return node.nodeid if node is not None else ua.NodeId(0, 0)

    @property
    def running(self) -> bool:
        """Whether the server is currently listening."""
        return self._server is not None

    @property
    def subscription_caps(self) -> dict[str, int]:
        """The private subscription caps this server applies when it answers requests.

        These are not OperationLimits: nothing in the address space publishes them, so
        a client cannot read them. They are here for the tests and the chapter that
        teaches the difference - a limit a server advertises and a limit a server
        applies are different things, and only the second one shows up in the revised
        numbers a subscription comes back with.
        """
        if self._server is None:
            return {}
        iserver = self._server.iserver
        return {
            "max_lifetime_count": int(iserver.max_lifetime_count),
            "max_keep_alive_count": int(iserver.max_keep_alive_count),
            "max_monitored_item_queue_size": int(iserver.max_monitored_item_queue_size),
            "max_subscriptions": int(iserver.max_subscriptions),
        }

    async def start(self) -> Server:
        """Build the address space and start listening."""
        server = Server()
        await server.init()
        server.set_endpoint(self._config.endpoint)
        if self._config.advertised_host is not None:
            # asyncua binds and announces the same URL unless socket_address says
            # otherwise. Saying which host to bind is the whole difference between a
            # container that answers on its mapped port and one that announces a host no
            # client can reach.
            server.socket_address = (self._config.host, self._config.port)
        server.set_server_name(self._config.application_name)
        server.manufacturer_name = self._config.manufacturer_name
        server.product_uri = self._config.product_uri
        index = await server.register_namespace(self._config.namespace_uri)
        self._namespace_index = index
        self._server = server

        await self._build_address_space(server, index)
        try:
            if self._config.publish_limits:
                await self._publish_operation_limits(server)
            self._apply_subscription_limits(server)
            if self._config.historize:
                await self._enable_historization(server)
            await server.start()
            self._bound_endpoint = self.endpoint
        except Exception:
            # Never leak a half started server: the history backend owns a thread,
            # and a port is not something a test suite wants left open.
            await self.stop()
            raise
        _logger.info(
            "demo server listening on %s (namespace %d = %s)",
            self.endpoint,
            index,
            self._config.namespace_uri,
        )
        if self._config.simulate:
            self._stop.clear()
            self._simulator = asyncio.create_task(self._simulate())
        return server

    async def stop(self) -> None:
        """Stop the simulator, the history backend and the server."""
        self._stop.set()
        simulator = self._simulator
        self._simulator = None
        if simulator is not None:
            simulator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await simulator
        server = self._server
        self._server = None
        history = self._history
        self._history = None
        if history is not None:
            with contextlib.suppress(Exception):
                await history.stop()
        if server is None:
            return
        with contextlib.suppress(UaError, OSError):
            await server.stop()

    async def __aenter__(self) -> DemoServer:
        """Start the server and hand out the running instance."""
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop the server even when the body of the ``async with`` raised."""
        await self.stop()

    async def drop_subscription(self, subscription_id: int) -> str:
        """Delete a subscription this server holds, from the server side.

        The Chapter 12 outage a client cannot cause from its own side: a restart, a
        lifetime the client was never told about, a maintenance window. The session stays
        open, the client's counters simply stop moving, and the only trace left is the
        answer a client gets when it asks about a subscription id the server no longer
        knows - which is why asking is part of supervision and not paranoia.

        Args:
            subscription_id: The id the server handed out when the subscription was made.

        Returns:
            The status name the server answered with: ``Good`` when it forgot the
            subscription, ``BadSubscriptionIdInvalid`` when it never had it.
        """
        service = self._require_server().iserver.subscription_service
        results = await service.delete_subscriptions([int(subscription_id)])
        return results[0].name if results else "no answer"

    async def set_service_level(self, value: int) -> None:
        """Set ``ServiceLevel`` so Chapter 12 can show a degraded server.

        Args:
            value: 0 for maintenance, 1 for no data, 2..199 degraded,
                200..255 healthy (OPC 10000-4, Table 105).
        """
        server = self._require_server()
        await server.nodes.service_level.write_value(ua.Variant(value, ua.VariantType.Byte))

    async def set_server_state(self, state: ua.ServerState) -> None:
        """Set ``ServerState`` so Chapter 12 can show a server that is up but not running."""
        server = self._require_server()
        await server.nodes.server_state.write_value(ua.Variant(state, ua.VariantType.Int32))

    async def set_value(
        self, name: str, value: object, variant_type: ua.VariantType | None = None
    ) -> None:
        """Write one demo variable from the server side."""
        node_id = self._tags.get(name)
        if node_id is None:
            raise KeyError(f"unknown demo tag {name!r}")
        server = self._require_server()
        node = server.get_node(node_id)
        await node.write_value(ua.Variant(value, variant_type) if variant_type else value)

    async def read_value(self, name: str) -> object:
        """Read one demo variable from the server side."""
        node_id = self._tags.get(name)
        if node_id is None:
            raise KeyError(f"unknown demo tag {name!r}")
        server = self._require_server()
        return await server.get_node(node_id).read_value()

    def tag_ids(self, start: int = 0, stop: int | None = None) -> list[ua.NodeId]:
        """NodeIds of the ``BatchTagNNN`` block used for bulk read/write chapters."""
        return [self._tags[name] for name in self._batch_names[start:stop]]

    def writable_tag_ids(self) -> list[ua.NodeId]:
        """NodeIds of the demo tags a client is allowed to write."""
        return [self._tags[name] for name in self._writable_names]

    def readonly_tag_ids(self) -> list[ua.NodeId]:
        """NodeIds of the tags that must fail a write with ``BadNotWritable``."""
        return [node_id for name, node_id in self._tags.items() if name not in self._writable_names]

    async def _build_address_space(self, server: Server, index: int) -> None:
        objects = server.nodes.objects
        production = await objects.add_folder(*_node_pair("ProductionLine", index))
        line = await production.add_object(*_node_pair("Line1", index))
        maintenance = await production.add_object(*_node_pair("Maintenance", index))
        self._nodes["line"] = line
        self._nodes["maintenance"] = maintenance

        for spec in _TAG_SPECS:
            # Process tags are *variables* here (HasComponent, BaseDataVariableType),
            # which is what a plant server exposes. asyncua's add_property would have
            # made them Properties with a HasProperty reference - useful for metadata
            # like engineering units, wrong for a tag you intend to subscribe to.
            node = await line.add_variable(
                *_node_pair(spec.name, index),
                spec.array_initial if spec.array_initial is not None else spec.initial,
                varianttype=spec.variant_type,
            )
            if spec.array_initial is not None:
                await node.write_value_rank(spec.value_rank)
            await self._write_description(node, spec.description)
            if spec.writable:
                # Be explicit about access rights instead of inheriting whatever the
                # server library decided. Forgetting this call is the single most
                # common "why is my write rejected" question, so Chapter 8 shows both
                # the writable tag and the read-only one side by side.
                await node.set_writable()
                self._writable_names.add(spec.name)
            else:
                await node.set_read_only()
            self._tags[spec.name] = node.nodeid

        for number in range(1, self._config.batch_tag_count + 1):
            name = f"BatchTag{number:03d}"
            node = await line.add_variable(
                *_node_pair(name, index), number, varianttype=ua.VariantType.Int32
            )
            await self._write_description(node, f"bulk read/write tag {number}")
            await node.set_writable()
            self._tags[name] = node.nodeid
            self._writable_names.add(name)
            self._batch_names.append(name)

        for name, (initial, variant_type, description) in _READ_ONLY_TAGS.items():
            node = await maintenance.add_variable(
                *_node_pair(name, index), initial, varianttype=variant_type
            )
            await node.set_read_only()
            await self._write_description(node, description)
            self._tags[name] = node.nodeid

        await self._add_methods(line, index)
        if self._config.authored_model:
            await self._author_model(server, index)

    async def _author_model(self, server: Server, index: int) -> None:
        """Author the declared part of this server's address space (Chapter 16).

        The authored model lives in its own ``AssetModel`` folder and its types hang off
        ``BaseObjectType``, so it adds structure without touching the tag and method nodes
        the earlier chapters measure. It runs through the toolkit's own author rather than
        through asyncua helpers directly: that is the point of the chapter, and it is the
        same code that later writes the gateway's address space on a server it does not own.
        """
        folder = await server.nodes.objects.add_folder(*_node_pair(AUTHORED_FOLDER_NAME, index))
        self._nodes["asset_model"] = folder
        author = AddressSpaceAuthor(folder, namespace_index=index)
        self._author = author
        for type_report in await author.author_model(AUTHORED_TYPES):
            self._authored_types[type_report.browse_name] = type_report
        for instance_report in await author.instantiate_all(AUTHORED_INSTANCES, parent=folder):
            self._authored_instances[instance_report.browse_name] = instance_report

    async def _add_methods(self, line: Node, index: int) -> None:
        """Declare the methods Chapter 9 discovers instead of hardcoding.

        Two things a real server does and a naive example forgets:

        * arguments are declared as named :class:`asyncua.ua.Argument` objects, so a
          client can call them *by name*; a bare ``VariantType`` list leaves the
          names empty and the method becomes positional-only;
        * a server-side callback receives the parent NodeId first and ``Variant``
          values after that, and returns a list of typed ``Variant`` outputs.
        """

        async def start_line(_parent: ua.NodeId, mode: Any) -> list[ua.Variant]:
            self._run_mode = int(_unwrap_argument(mode))
            return [_output_variant(value=True, variant_type=ua.VariantType.Boolean)]

        async def stop_line(_parent: ua.NodeId) -> list[ua.Variant]:
            self._run_mode = 0
            return [_output_variant(value=True, variant_type=ua.VariantType.Boolean)]

        async def add_values(_parent: ua.NodeId, first: Any, second: Any) -> list[ua.Variant]:
            total = int(_unwrap_argument(first)) + int(_unwrap_argument(second))
            return [_output_variant(value=total, variant_type=ua.VariantType.Int32)]

        async def line_status(_parent: ua.NodeId) -> list[ua.Variant]:
            text = f"Line1 mode={self._run_mode} reported {datetime.now(UTC).isoformat()}"
            return [_output_variant(value=text, variant_type=ua.VariantType.String)]

        async def acknowledge_alarm(_parent: ua.NodeId, alarm_id: Any) -> list[ua.Variant]:
            accepted = str(_unwrap_argument(alarm_id)) in _KNOWN_ALARMS
            return [_output_variant(value=accepted, variant_type=ua.VariantType.Boolean)]

        declarations: tuple[tuple[str, object, list[ua.Argument], list[ua.Argument]], ...] = (
            (
                "StartLine",
                start_line,
                [_argument("Mode", ua.VariantType.UInt32, "run mode to start with")],
                [_argument("Accepted", ua.VariantType.Boolean)],
            ),
            ("StopLine", stop_line, [], [_argument("Accepted", ua.VariantType.Boolean)]),
            (
                "AddValues",
                add_values,
                [
                    _argument("First", ua.VariantType.Int32, "first addend"),
                    _argument("Second", ua.VariantType.Int32, "second addend"),
                ],
                [_argument("Sum", ua.VariantType.Int32, "sum of the two addends")],
            ),
            ("GetLineStatus", line_status, [], [_argument("Status", ua.VariantType.String)]),
            (
                "AcknowledgeAlarm",
                acknowledge_alarm,
                [_argument("AlarmId", ua.VariantType.String, "alarm instance to acknowledge")],
                [_argument("Accepted", ua.VariantType.Boolean, "False for an unknown alarm id")],
            ),
        )
        for name, callback, inputs, outputs in declarations:
            method = await line.add_method(*_node_pair(name, index), callback, inputs, outputs)
            self._method_names[name] = method.nodeid

    def _apply_subscription_limits(self, server: Server) -> None:
        """Lower the server's private subscription caps so revisions are visible.

        OperationLimits are attributes a server *publishes*. These are not: they are
        the numbers asyncua's server keeps to itself and applies when it answers a
        subscription request. Its stock caps (lifetime 15 000, keepalive 5 000, queue
        10 000) sit above anything a tutorial client ever asks for, so every request
        would be accepted unchanged and Chapter 10 would have nothing to show. The
        demo server uses small caps on purpose: the client asks for a 10 000 lifetime,
        the server answers 1 000, and that is exactly the conversation a real server
        has with a client.

        ``max_subscriptions`` is deliberately small too, so Chapter 12 can meet
        ``BadTooManySubscriptions`` instead of assuming a server accepts forever.
        """
        iserver = server.iserver
        iserver.max_lifetime_count = self._config.subscription_lifetime_cap
        iserver.max_keep_alive_count = self._config.subscription_keepalive_cap
        iserver.max_monitored_item_queue_size = self._config.monitored_item_queue_cap
        iserver.max_subscriptions = self._config.max_subscriptions
        _logger.debug(
            "demo server subscription caps: lifetime=%s keepalive=%s queue=%s subscriptions=%s",
            iserver.max_lifetime_count,
            iserver.max_keep_alive_count,
            iserver.max_monitored_item_queue_size,
            iserver.max_subscriptions,
        )

    async def _publish_operation_limits(self, server: Server) -> None:
        """Write OperationLimits values so chunking is measurable.

        The demo values are intentionally small: with ``MaxNodesPerRead=10`` and
        24 batch tags, a correct client issues three Read calls and a naive one
        is rejected. That is the difference Chapter 7 teaches.
        """
        values = {
            "max_nodes_per_read": self._config.max_nodes_per_read,
            "max_nodes_per_write": self._config.max_nodes_per_write,
            "max_nodes_per_method_call": self._config.max_nodes_per_method_call,
            "max_nodes_per_browse": self._config.max_nodes_per_browse,
            "max_nodes_per_register_nodes": 100,
            "max_nodes_per_translate_browse_paths": 100,
            "max_nodes_per_node_management": 100,
            "max_monitored_items_per_call": self._config.max_monitored_items_per_call,
        }
        for name, limit in values.items():
            node = server.get_node(ua.NodeId(_LIMIT_NODES[name], 0))
            try:
                await node.write_value(ua.Variant(limit, ua.VariantType.UInt32))
            except UaError as exc:
                _logger.warning("could not publish %s: %s", name, exc)

        sample_rate = server.get_node(
            ua.NodeId(ua.ObjectIds.Server_ServerCapabilities_MinSupportedSampleRate, 0)
        )
        with contextlib.suppress(UaError):
            await sample_rate.write_value(
                ua.Variant(self._config.min_supported_sample_rate, ua.VariantType.Double)
            )

        # MaxBrowseContinuationPoints lives on ServerCapabilities, not on
        # OperationLimits, and asyncua's stock server publishes it as a null value.
        # The demo writes a real number so Chapter 5 can show the difference between
        # "the server said 20" and "the server said nothing", which a client must
        # treat as unknown rather than unlimited.
        continuation = server.get_node(
            ua.NodeId(ua.ObjectIds.Server_ServerCapabilities_MaxBrowseContinuationPoints, 0)
        )
        with contextlib.suppress(UaError):
            # MaxBrowseContinuationPoints is a UInt16. Writing a UInt32 here is refused
            # by the server with BadTypeMismatch - the same rule as every tag write,
            # applied to a server capability node.
            await continuation.write_value(
                ua.Variant(
                    self._config.max_browse_continuation_points,
                    ua.VariantType.UInt16,
                )
            )

    async def _enable_historization(self, server: Server) -> None:
        """Attach the SQLite history backend to the temperature tag (Chapter 20)."""
        path = self._config.history_path or Path("demo-history.db")
        history = DemoHistorySQLite(str(path))
        # HistorySQLite opens its aiosqlite connection in init(), not in __init__().
        await history.init()
        self._history = history
        server.iserver.history_manager.set_storage(history)
        temperature = self._tags.get("Temperature")
        if temperature is not None:
            await server.historize_node_data_change(
                server.get_node(temperature), period=None, count=10_000
            )

    @staticmethod
    async def _write_description(node: Node, text: str) -> None:
        """Publish the Description attribute.

        asyncua has no ``write_description`` helper, so the honest way is a plain
        attribute write. Chapter 6 reads this attribute back with the inspector.
        """
        await node.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(ua.LocalizedText(Text=text))),
        )

    async def _simulate(self) -> None:
        """Move the process values so subscriptions have something to report."""
        server = self._server
        if server is None:
            return
        interval = self._config.simulation_interval
        while not self._stop.is_set():
            self._tick += 1
            angle = self._tick * 0.25
            try:
                await self._write(server, "Temperature", 21.5 + 3.5 * math.sin(angle))
                await self._write(server, "Pressure", 4.2 + 0.6 * math.sin(angle * 1.7))
                # Integer tags are written as explicit variants: a bare Python int becomes
                # Int64, and the server rejects it with BadTypeMismatch. That is Chapter 8.
                await self._write(
                    server,
                    "MotorRpm",
                    ua.Variant(1_450 + int(80 * math.sin(angle * 0.9)), ua.VariantType.UInt32),
                )
                await self._write(
                    server, "BatchProgress", ua.Variant(self._tick % 101, ua.VariantType.Int32)
                )
                await self._write(
                    server,
                    "Vibration",
                    ua.Variant(2.4 + 0.8 * math.sin(angle * 2.3), ua.VariantType.Float),
                )
                await self._write(server, "LastSampledAt", datetime.now(UTC))
            except (UaError, OSError) as exc:
                _logger.warning("simulation write failed: %s", exc)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=interval)

    async def _write(self, server: Server, name: str, value: object) -> None:
        node_id = self._tags.get(name)
        if node_id is None:
            return
        await server.get_node(node_id).write_value(value)

    def _require_server(self) -> Server:
        if self._server is None:
            raise RuntimeError("demo server is not running - call start() first")
        return self._server


def _node_pair(name: str, index: int) -> tuple[ua.NodeId, ua.QualifiedName]:
    """NodeId and BrowseName for one demo node.

    Demo nodes get *string* NodeIds (``ns=2;s=Temperature``) on purpose: they are
    what informational models look like in the field, they are easy to type into a
    client or UaExpert, and they make every example in this tutorial copy runnable.
    """
    return ua.NodeId(name, index), ua.QualifiedName(name, index)


def _argument(
    name: str, variant_type: ua.VariantType, description: str | None = None
) -> ua.Argument:
    """A named method argument declaration.

    asyncua accepts a plain ``VariantType`` list, but then ``Argument.Name`` stays
    empty and clients can only pass values positionally. Declaring the name is what
    makes Chapter 9's name-based calls possible on a real server too.
    """
    argument = ua.Argument()
    argument.Name = name
    argument.DataType = ua.NodeId(variant_type.value, 0)
    argument.ValueRank = -1
    argument.ArrayDimensions = []
    argument.Description = ua.LocalizedText(description or name)
    return argument


def _output_variant(*, value: object, variant_type: ua.VariantType) -> ua.Variant:
    """One output value, pinned to the type the method declared.

    Keyword-only on purpose: naming the type at every call site is what keeps a
    method's declared types and its returned types the same thing. Returning a bare
    Python value is how a server-side method looks fine in a quick test and then
    fails to encode for a real client.
    """
    return ua.Variant(value, variant_type)


def _unwrap_argument(value: Any) -> Any:
    """Server-side argument values arrive as ``Variant``, whatever the client sent."""
    return _unwrap_value(value)


_TAG_SPECS: tuple[TagSpec, ...] = (
    TagSpec(
        name="Temperature",
        variant_type=ua.VariantType.Double,
        initial=21.5,
        writable=True,
        description="Line temperature in degrees Celsius (Double, writable)",
    ),
    TagSpec(
        name="Setpoint",
        variant_type=ua.VariantType.Float,
        initial=22.0,
        writable=True,
        description=(
            "Target temperature as Single/Float - writing a plain Python float here is the "
            "classic BadTypeMismatch demo"
        ),
    ),
    TagSpec(
        name="Pressure",
        variant_type=ua.VariantType.Double,
        initial=4.2,
        writable=True,
        description="Line pressure in bar",
    ),
    TagSpec(
        name="MotorRpm",
        variant_type=ua.VariantType.UInt32,
        initial=1450,
        writable=True,
        description="Motor speed in revolutions per minute",
    ),
    TagSpec(
        name="BatchProgress",
        variant_type=ua.VariantType.Int32,
        initial=0,
        writable=True,
        description="Batch completion percentage",
    ),
    TagSpec(
        name="RunState",
        variant_type=ua.VariantType.Boolean,
        initial=True,
        writable=True,
        description="Whether the line is running",
    ),
    TagSpec(
        name="RecipeName",
        variant_type=ua.VariantType.String,
        initial="default-recipe",
        writable=True,
        description="Active recipe name",
    ),
    TagSpec(
        name="LastSampledAt",
        variant_type=ua.VariantType.DateTime,
        initial=datetime.now(UTC),
        writable=True,
        description="When the simulator last wrote a value",
    ),
    TagSpec(
        name="Vibration",
        variant_type=ua.VariantType.Float,
        initial=2.4,
        writable=False,
        description="Vibration sensor - read only for clients (Single)",
    ),
    TagSpec(
        name="QualityScore_RO",
        variant_type=ua.VariantType.Byte,
        initial=97,
        writable=False,
        description="Quality score - read only, Byte so ServiceLevel semantics stay comparable",
    ),
    TagSpec(
        name="TemperatureHistory_RO",
        variant_type=ua.VariantType.Double,
        initial=21.5,
        writable=False,
        description="Frozen temperature snapshot - a second read-only node for access checks",
    ),
    TagSpec(
        name="SampleArray",
        variant_type=ua.VariantType.Double,
        initial=None,
        writable=True,
        description="One dimensional Double array (ValueRank=1)",
        value_rank=1,
        array_initial=(1.0, 2.0, 3.0, 4.0, 5.0),
    ),
)

_READ_ONLY_TAGS: dict[str, tuple[object, ua.VariantType, str]] = {
    "MaintenanceMode": (False, ua.VariantType.Boolean, "Maintenance latch, read only for clients"),
    "FirmwareVersion": ("2.4.1", ua.VariantType.String, "Device firmware, read only"),
    "CommissioningCompleted_RO": (True, ua.VariantType.Boolean, "Read only commissioning flag"),
}

#: Name of the folder the opt-in authored model lives in. Separate from ``ProductionLine``
#: on purpose: the chapters before Chapter 16 measure a server whose variables are all
#: plain ``BaseDataVariableType`` objects, and those measurements have to stay valid.
AUTHORED_FOLDER_NAME = "AssetModel"

#: The tutorial's declared model, in dependency order: an abstract contract, a concrete
#: type that inherits it, and one component typed with a VariableType of its own - the
#: shape of OPC 10000-3, 4.6.1's ``SetPoint`` example, where many objects share one common
#: VariableType so a client can recognise the definition without reading a description.
#:
#: The simulator does not touch these nodes. An authored instance's value is whatever the
#: declaration carried plus whatever the instance spec said, which is what makes the
#: Chapter 16 numbers repeatable.
AUTHORED_TYPES: tuple[ObjectTypeSpec, ...] = (
    ObjectTypeSpec(
        name="MachineType",
        abstract=True,
        description="Abstract contract every machine in this plant shares",
        components=(
            ComponentSpec(
                name="Temperature",
                data_type="Double",
                initial=21.5,
                access=VariableAccess.READ_WRITE,
                rule=ModellingRule.MANDATORY,
                min_sampling_interval=100.0,
                description="Machine temperature in degrees Celsius",
            ),
            ComponentSpec(
                name="Running",
                data_type="Boolean",
                initial=False,
                rule=ModellingRule.MANDATORY,
                description="Whether this machine is running",
            ),
        ),
        properties=(
            ComponentSpec(
                name="AssetTag",
                data_type="String",
                initial="UNLABELLED",
                rule=ModellingRule.MANDATORY,
                description="Rising asset tag of this machine",
            ),
        ),
    ),
    ObjectTypeSpec(
        name="PumpType",
        supertype="MachineType",
        description="A pump: everything a machine has, plus what makes it a pump",
        components=(
            ComponentSpec(
                name="Pressure",
                data_type="Double",
                initial=4.2,
                access=VariableAccess.READ_WRITE,
                rule=ModellingRule.MANDATORY,
                description="Discharge pressure in bar",
            ),
            ComponentSpec(
                name="Vibration",
                data_type="Float",
                initial=2.4,
                rule=ModellingRule.OPTIONAL,
                description="Vibration sensor in mm/s - not every pump carries one",
            ),
            ComponentSpec(
                name="SetPoint",
                data_type="Float",
                initial=60.0,
                access=VariableAccess.READ_WRITE,
                rule=ModellingRule.MANDATORY,
                variable_type=VariableTypeSpec(
                    name="MachineSetPointType",
                    data_type="Float",
                    description="A target value an operator is allowed to change",
                ),
                min_sampling_interval=250.0,
                description="Target discharge pressure in bar",
            ),
        ),
        properties=(
            ComponentSpec(
                name="MaintenanceNote",
                data_type="String",
                rule=ModellingRule.OPTIONAL,
                description="Free text maintenance note, only where one was written",
            ),
        ),
    ),
)

#: Two instances of the same type, differing only in their values. The values arrive after
#: instantiation, so every pump is born with what its type declared and then becomes itself.
AUTHORED_INSTANCES: tuple[InstanceSpec, ...] = (
    InstanceSpec(
        name="Pump_01",
        type_name="PumpType",
        values={
            "AssetTag": "PUMP-01",
            "Temperature": 61.5,
            "Running": True,
            "Pressure": 6.4,
            "Vibration": 1.2,
            "SetPoint": 60.0,
            "MaintenanceNote": "bearing check due 2026-12-01",
        },
        description="Pump 01: the one with a maintenance note",
    ),
    InstanceSpec(
        name="Pump_02",
        type_name="PumpType",
        values={
            "AssetTag": "PUMP-02",
            "Temperature": 58.2,
            "Running": False,
            "Pressure": 5.1,
            "Vibration": 0.8,
            "SetPoint": 55.0,
        },
        description="Pump 02: the one that is standing still",
    ),
)


def build_argument_parser() -> argparse.ArgumentParser:
    """Command line interface of ``opcua-tutorial-server``."""
    parser = argparse.ArgumentParser(
        prog="opcua-tutorial-server",
        description="Run the OPC UA tutorial demo server.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind host (default: 127.0.0.1)")
    parser.add_argument(
        "--advertise-host",
        default=None,
        help="host to announce to clients when it is not the bind host (containers)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="bind port (default: 48401, 0 = random)"
    )
    parser.add_argument(
        "--namespace", default=DEFAULT_NAMESPACE_URI, help="namespace URI to register"
    )
    parser.add_argument(
        "--name", default=DEFAULT_APPLICATION_NAME, help="ServerName reported to clients"
    )
    parser.add_argument("--batch-tags", type=int, default=24, help="number of bulk read/write tags")
    parser.add_argument(
        "--max-nodes-per-read", type=int, default=10, help="published MaxNodesPerRead"
    )
    parser.add_argument(
        "--max-nodes-per-write", type=int, default=10, help="published MaxNodesPerWrite"
    )
    parser.add_argument(
        "--max-monitored-items-per-call",
        type=int,
        default=10,
        help="published MaxMonitoredItemsPerCall",
    )
    parser.add_argument(
        "--interval", type=float, default=0.25, help="simulation interval in seconds"
    )
    parser.add_argument("--no-simulate", action="store_true", help="freeze all process values")
    parser.add_argument(
        "--no-limits", action="store_true", help="publish no OperationLimits at all"
    )
    parser.add_argument(
        "--historize", action="store_true", help="historize Temperature in a SQLite file"
    )
    parser.add_argument("--history-path", type=Path, default=None, help="history database path")
    parser.add_argument(
        "--authored-model",
        action="store_true",
        help=(
            "author a declared address space in an extra AssetModel folder "
            "(MachineType, PumpType and two pumps) - see Chapter 16"
        ),
    )
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING or ERROR")
    parser.add_argument("--json-logs", action="store_true", help="emit JSON log lines")
    return parser


def config_from_args(args: argparse.Namespace) -> DemoServerConfig:
    """Translate CLI arguments into a :class:`DemoServerConfig`."""
    return DemoServerConfig(
        host=args.host,
        port=args.port,
        advertised_host=args.advertise_host,
        namespace_uri=args.namespace,
        application_name=args.name,
        batch_tag_count=args.batch_tags,
        publish_limits=not args.no_limits,
        max_nodes_per_read=args.max_nodes_per_read,
        max_nodes_per_write=args.max_nodes_per_write,
        max_monitored_items_per_call=args.max_monitored_items_per_call,
        simulation_interval=args.interval,
        simulate=not args.no_simulate,
        historize=args.historize,
        history_path=args.history_path,
        authored_model=args.authored_model,
    )


async def run_forever(config: DemoServerConfig) -> None:
    """Start the demo server and serve until the process is interrupted."""
    server = DemoServer(config)
    await server.start()
    print(f"demo server ready on {server.endpoint}")
    print(f"namespace {server.namespace_index} = {server.namespace_uri}")
    print(f"tags: {len(server.tags)} methods: {len(server.methods)}")
    if server.authored_types:
        print(
            f"authored model: {len(server.authored_types)} types "
            f"{', '.join(sorted(server.authored_types))} "
            f"in {format_node_id(server.authored_folder_id)}"
        )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, signal_name, None)
        if handler is None:
            continue
        with contextlib.suppress(NotImplementedError, ValueError, RuntimeError):
            loop.add_signal_handler(handler, stop.set)
    try:
        await stop.wait()
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the ``opcua-tutorial-server`` script."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, json_output=args.json_logs)
    config = config_from_args(args)
    try:
        asyncio.run(run_forever(config))
    except KeyboardInterrupt:
        _logger.info("demo server stopped by user")
    return 0


async def demo_server(config: DemoServerConfig | None = None) -> AsyncIterator[DemoServer]:
    """Async context manager form used by the test fixtures."""
    server = DemoServer(config)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


if __name__ == "__main__":
    # Without this, `python -m opcua_toolkit.devserver` imports the module, defines main()
    # and exits quietly - which looks like a server that starts and immediately stops. The
    # console script does not need it; a debugger launch config that runs the module does.
    raise SystemExit(main())


__all__ = [
    "AUTHORED_FOLDER_NAME",
    "AUTHORED_INSTANCES",
    "AUTHORED_TYPES",
    "DEFAULT_APPLICATION_NAME",
    "DEFAULT_NAMESPACE_URI",
    "DEFAULT_PORT",
    "DemoServer",
    "DemoServerConfig",
    "TagSpec",
    "build_argument_parser",
    "config_from_args",
    "demo_server",
    "main",
]
