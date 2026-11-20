"""The ``opcua-toolkit`` command line client.

This is not a demo: it is the verification tool the tutorial uses in every
"Verify it" section, and it doubles as a field diagnostic. If a chapter claims a
behaviour, you can confirm it here without writing a script first::

    uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
    uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48401 --depth 3
    uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature"
    uv run opcua-toolkit read --endpoint opc.tcp://127.0.0.1:48401 --batch --compare
    uv run opcua-toolkit write --endpoint opc.tcp://127.0.0.1:48401 --set "ns=2;s=Setpoint=22.5"
    uv run opcua-toolkit call --endpoint opc.tcp://127.0.0.1:48401 --list-methods
    uv run opcua-toolkit subscribe --endpoint opc.tcp://127.0.0.1:48401 --duration 5
    uv run opcua-toolkit config --file examples/gateway.yaml
    uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48401

Design notes:

* every subcommand builds its client from :class:`~opcua_toolkit.settings.ClientSettings`,
  the same path the library chapters use, so the CLI can never drift from the
  tutorial code;
* output is text by default and ``--json`` on request, which makes the CLI usable
  from CI and from shell scripts;
* a command that saw any failure exits non-zero.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import Any

from asyncua import ua
from asyncua.ua.uaerrors import UaError

from opcua_toolkit.browse import NamespaceExplorer
from opcua_toolkit.config import discovery_settings, load_config
from opcua_toolkit.diagnostics import configure_logging
from opcua_toolkit.discovery import EndpointDiscovery, ServerProfile
from opcua_toolkit.errors import OpcuaToolkitError, format_node_id
from opcua_toolkit.inspector import NodeInspector, format_table
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.methods import MethodCatalog, MethodInvoker
from opcua_toolkit.nodeids import to_nodeid
from opcua_toolkit.read import BulkReader
from opcua_toolkit.settings import (
    ClientSettings,
    RetryPolicy,
    SecuritySettings,
    SubscriptionDefaults,
    format_milliseconds,
)
from opcua_toolkit.subscriptions import DataChangeRecord, SubscriptionManager
from opcua_toolkit.values import format_value
from opcua_toolkit.write import BulkWriter, WriteItem

_logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the ``opcua-toolkit`` console script."""
    parser = argparse.ArgumentParser(
        prog="opcua-toolkit",
        description="OPC UA tutorial toolkit: discover, browse, read, write, call and subscribe.",
    )
    parser.add_argument("--version", action="version", version="opcua-toolkit 2.0.0")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--endpoint", required=True, help="opc.tcp://host:port/path")
    common.add_argument("--user", help="username for a UserNameIdentityToken endpoint")
    common.add_argument("--password", help="password for --user")
    common.add_argument(
        "--security",
        choices=("None", "Basic256Sha256", "Aes128Sha256RsaOaep", "Aes256Sha256RsaPss"),
        default="None",
        help="security policy to use",
    )
    common.add_argument("--mode", choices=("None", "Sign", "SignAndEncrypt"), default="None")
    common.add_argument("--certificate", type=Path, help="client certificate (DER)")
    common.add_argument("--private-key", type=Path, help="client private key (PEM)")
    common.add_argument("--server-certificate", type=Path, help="pinned server certificate (DER)")
    common.add_argument("--timeout", type=float, default=4.0, help="request timeout in seconds")
    common.add_argument(
        "--session-timeout", type=float, default=60.0, help="session timeout in seconds"
    )
    common.add_argument("--log-level", default="WARNING", help="application log level")
    common.add_argument("--json-logs", action="store_true", help="emit JSON log lines")
    common.add_argument("--json", action="store_true", help="print machine readable results")

    _add_discover(subparsers, common)
    _add_browse(subparsers, common)
    _add_inspect(subparsers, common)
    _add_read(subparsers, common)
    _add_write(subparsers, common)
    _add_call(subparsers, common)
    _add_subscribe(subparsers, common)
    _add_config(subparsers)
    return parser


def _add_discover(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("discover", parents=[common], help="profile a server")
    parser.add_argument("--find-servers", action="store_true", help="also call FindServers")
    parser.add_argument(
        "--limits-only", action="store_true", help="print only the operation limits"
    )


def _add_browse(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("browse", parents=[common], help="explore the address space")
    parser.add_argument(
        "--node", default="i=85", help="root node to browse (default: ObjectsFolder)"
    )
    parser.add_argument(
        "--depth", type=int, default=2, help="levels below the root (-1 = unlimited)"
    )
    parser.add_argument("--max-nodes", type=int, default=2_000, help="hard stop for huge servers")
    parser.add_argument(
        "--path",
        help="resolve a slash path such as ProductionLine/Line1, starting at --node",
    )
    parser.add_argument("--flat", action="store_true", help="print a flat list instead of a tree")


def _add_inspect(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser(
        "inspect", parents=[common], help="describe nodes attribute by attribute"
    )
    parser.add_argument(
        "--node", action="append", required=True, help="NodeId to inspect (repeatable)"
    )
    parser.add_argument("--value", action="store_true", help="also read the current value")


def _add_read(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("read", parents=[common], help="bulk read values")
    parser.add_argument("--node", action="append", help="NodeId to read (repeatable)")
    parser.add_argument("--nodes-file", type=Path, help="file with one NodeId per line")
    parser.add_argument(
        "--batch", action="store_true", help="read the demo server's BatchTag block"
    )
    parser.add_argument(
        "--compare", action="store_true", help="also run the one-by-one baseline and compare costs"
    )


def _add_write(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("write", parents=[common], help="bulk write values safely")
    parser.add_argument(
        "--set",
        action="append",
        dest="assignments",
        metavar="NODEID=VALUE",
        help="write VALUE to NODEID (repeatable)",
    )
    parser.add_argument(
        "--max-attempts", type=int, default=3, help="retry attempts for retryable statuses"
    )


def _add_call(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("call", parents=[common], help="discover and invoke methods")
    parser.add_argument("--list-methods", action="store_true", help="discover methods and exit")
    parser.add_argument("--root", default="i=85", help="subtree to search for methods")
    parser.add_argument("--method", help="browse name of the method to call")
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="method argument as name=value (repeatable)",
    )


def _add_subscribe(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser], common: argparse.ArgumentParser
) -> None:
    parser = subparsers.add_parser("subscribe", parents=[common], help="watch values change")
    parser.add_argument("--node", action="append", help="NodeId to monitor (repeatable)")
    parser.add_argument(
        "--batch", action="store_true", help="monitor the demo server's BatchTag block"
    )
    parser.add_argument("--duration", type=float, default=10.0, help="seconds to stay subscribed")
    parser.add_argument(
        "--publishing-interval", type=float, default=500.0, help="ms between publishes"
    )
    parser.add_argument(
        "--sampling-interval", type=float, default=100.0, help="ms between server samples"
    )
    parser.add_argument(
        "--queue-size", type=int, default=10, help="notifications kept per monitored item"
    )


def _add_config(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The one subcommand that reads a configuration file instead of command line flags.

    Deliberately not built from ``common``: ``config`` is the Chapter 14 command, and the
    point of Chapter 14 is that the file is where the settings live. It takes the endpoint
    of a server to *check against*, not the endpoint to connect to as the task.
    """
    parser = subparsers.add_parser(
        "config", help="resolve and review a configuration file before anything connects"
    )
    parser.add_argument("--file", type=Path, help="YAML configuration file to load")
    parser.add_argument(
        "--check-server",
        metavar="ENDPOINT",
        help="opc.tcp:// URL of a server to discover and check this file against",
    )
    parser.add_argument(
        "--require-decided",
        action="append",
        default=[],
        metavar="SETTING",
        help="setting path this deployment must have written (repeatable)",
    )
    parser.add_argument("--log-level", default="WARNING", help="application log level")
    parser.add_argument("--json-logs", action="store_true", help="emit JSON log lines")
    parser.add_argument("--json", action="store_true", help="print machine readable results")


def settings_from_args(args: argparse.Namespace) -> ClientSettings:
    """Build the chapter-identical client settings from CLI arguments."""
    return ClientSettings(
        endpoint_url=args.endpoint,
        request_timeout=args.timeout,
        session_timeout=args.session_timeout,
        username=args.user,
        password=args.password,
        security=SecuritySettings(
            policy=args.security,
            mode=args.mode,
            certificate=args.certificate,
            private_key=args.private_key,
            server_certificate=args.server_certificate,
        ),
        subscriptions=SubscriptionDefaults(
            publishing_interval=getattr(args, "publishing_interval", 500.0),
            sampling_interval=getattr(args, "sampling_interval", 100.0),
            queue_size=getattr(args, "queue_size", 10),
        ),
    )


async def _discover(args: argparse.Namespace) -> int:
    discovery = EndpointDiscovery(settings_from_args(args))
    profile = await discovery.profile()
    if args.json:
        payload: dict[str, Any] = profile.as_dict()
        if args.find_servers:
            payload["servers"] = [server.as_dict() for server in await discovery.find_servers()]
        _emit(payload)
        return EXIT_OK
    if args.limits_only and profile.limits is not None:
        print(profile.limits.describe())
        print(profile.limits.status_note())
        return EXIT_OK
    print(profile.describe())
    if args.find_servers:
        servers = await discovery.find_servers()
        print("find servers:")
        for server in servers:
            print(f"  {server.line()}")
    return EXIT_OK


async def _browse(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    client = settings.create_client()
    async with client:
        limits = await OperationLimits.read(client)
        explorer = NamespaceExplorer(client, limits=limits)
        print("namespaces:")
        for index, uri in await explorer.namespaces():
            print(f"  [{index}] {uri}")
        if args.path:
            segments = [segment for segment in args.path.replace(".", "/").split("/") if segment]
            resolved = await explorer.find_by_browse_path(segments, root=to_nodeid(args.node))
            print(f"resolved {args.path} -> {format_node_id(resolved.node_id)}")
            print(f"  browse name: {resolved.name}")
            print(f"  node class : {resolved.node_class.name}")
            return EXIT_OK
        tree, stats = await explorer.walk(args.node, max_depth=args.depth, max_nodes=args.max_nodes)
        if args.flat:
            for reference in tree.walk():
                node_text = format_node_id(reference.node_id)
                print(f"{node_text:<24} {reference.node_class.name:<9} {reference.name}")
        else:
            print(tree.render())
        print(
            f"browse stats: nodes={stats.nodes_visited} references={stats.references_seen} "
            f"browse={stats.browse_calls} next={stats.browse_next_calls} errors={stats.errors}"
        )
        if stats.stopped_reason:
            print(f"stopped: {stats.stopped_reason}")
    return EXIT_OK


async def _inspect(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    client = settings.create_client()
    async with client:
        limits = await OperationLimits.read(client)
        inspector = NodeInspector(client, limits=limits, include_value=args.value)
        descriptors = await inspector.inspect_many(args.node)
        if args.json:
            _emit([descriptor.as_dict() for descriptor in descriptors])
        else:
            print(format_table(descriptors))
            for descriptor in descriptors:
                print(descriptor.summary_line())
        if len(descriptors) != len(args.node):
            missing = len(args.node) - len(descriptors)
            _fail(f"{missing} of {len(args.node)} requested nodes could not be inspected")
            return EXIT_ERROR
    return EXIT_OK


async def _read(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    client = settings.create_client()
    async with client:
        limits = await OperationLimits.read(client)
        explorer = NamespaceExplorer(client, limits=limits)
        nodes = await _node_arguments(args, explorer)
        reader = BulkReader(client, limits=limits)
        results, stats = await reader.read_values(nodes)
        print(f"bulk read : {stats.summary()}")
        if args.json:
            _emit([result.as_dict() for result in results])
        else:
            for result in results:
                marker = "ok" if result.ok else result.status.name
                print(f"{result.node_id_text:<28} {marker:<22} {format_value(result.value)}")
        if args.compare:
            _, baseline = await reader.read_one_by_one(nodes)
            print(f"baseline  : {baseline.summary()}")
            speedup = baseline.elapsed_ms / stats.elapsed_ms if stats.elapsed_ms else 0.0
            request_ratio = baseline.requests / max(1, stats.requests)
            print(f"batching advantage: {request_ratio:.1f}x fewer requests, {speedup:.1f}x faster")
        return EXIT_OK if stats.failed == 0 else EXIT_ERROR


async def _write(args: argparse.Namespace) -> int:
    items = _write_arguments(args)
    settings = settings_from_args(args)
    client = settings.create_client()
    async with client:
        limits = await OperationLimits.read(client)
        writer = BulkWriter(client, limits=limits, retry=_retry_from_args(args))
        report = await writer.write(items)
        print(f"bulk write: {report.summary()}")
        if args.json:
            _emit([outcome.as_dict() for outcome in report.outcomes])
        else:
            for outcome in report.outcomes:
                marker = "ok" if outcome.ok else outcome.status.name
                print(f"{outcome.node_id_text:<28} {marker:<22} {outcome.note}")
    return EXIT_OK if report.failed == 0 else EXIT_ERROR


async def _call(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    client = settings.create_client()
    async with client:
        limits = await OperationLimits.read(client)
        catalog = MethodCatalog(client, limits=limits)
        methods = await catalog.discover_under(args.root)
        if not methods:
            _fail(f"no methods found below {args.root}")
            return EXIT_ERROR
        if args.list_methods:
            if args.json:
                _emit([method.as_dict() for method in methods])
            else:
                print(f"methods below {args.root}: {len(methods)}")
                for method in methods:
                    flag = "callable" if method.callable_by_user else "not executable"
                    print(f"  {method.signature():<58} {format_node_id(method.node_id):<22} {flag}")
            return EXIT_OK
        if not args.method:
            _fail("--method is required unless --list-methods is set")
            return EXIT_ERROR
        chosen = next((item for item in methods if item.browse_name == args.method), None)
        if chosen is None:
            _fail(
                f"method {args.method!r} not found below {args.root} "
                f"({len(methods)} methods searched; --list-methods shows them)"
            )
            return EXIT_ERROR
        _logger.debug(
            "found %s among %d methods below %s", chosen.browse_name, len(methods), args.root
        )
        arguments = _method_arguments(args.arg)
        invoker = MethodInvoker(client, limits=limits, raise_on_error=False)
        result = await invoker.call(chosen, arguments)
        if args.json:
            _emit(result.as_dict())
        else:
            print(
                f"{chosen.browse_name} [{format_node_id(chosen.node_id)}] -> {result.status.name}"
            )
            for name, value in result.outputs.items():
                print(f"  {name} = {format_value(value)}")
            if result.rejected_arguments:
                print(f"  rejected arguments: {', '.join(result.rejected_arguments)}")
    return EXIT_OK if result.ok else EXIT_ERROR


async def _subscribe(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)
    client = settings.create_client()
    async with client:
        limits = await OperationLimits.read(client)
        explorer = NamespaceExplorer(client, limits=limits)
        nodes = await _node_arguments(args, explorer)
        manager = SubscriptionManager(
            client,
            limits=limits,
            defaults=settings.subscriptions,
            publishing_interval=args.publishing_interval,
        )
        seen = 0

        async def handler(record: DataChangeRecord) -> None:
            nonlocal seen
            seen += 1
            if args.json:
                _emit(record.as_dict())
            else:
                print(
                    f"{record.node_id_text:<28} {record.status_code.name:<10} "
                    f"{format_value(record.value)}"
                )

        manager.register_handler(handler)
        handles = await manager.subscribe(nodes)
        print(f"monitoring {len(handles)} of {len(nodes)} requested nodes")
        for handle in handles:
            print(
                f"  {handle.node_id_text:<28} item={handle.monitored_item_id} "
                f"sampling={format_milliseconds(handle.requested_sampling_interval)} "
                f"queue={handle.options.queue_size}"
            )
        await asyncio.sleep(args.duration)
        stats = manager.stats
        print(f"notifications delivered: {seen}")
        print(f"subscription stats: {stats.summary()}")
        await manager.stop()
    return EXIT_OK if stats.errors == 0 else EXIT_ERROR


async def _config(args: argparse.Namespace) -> int:
    """Resolve a configuration file, review it, and only then ask a server about it.

    The order is the whole chapter: the file is read and reviewed before anything connects,
    and a running server is asked whether this configuration is possible on it afterwards.
    A deployment that connects first and argues with the server afterwards has already left
    a session open on a configuration it had not checked.

    A refusal is not printed as data: :meth:`~opcua_toolkit.ConfigReview.require_startable`
    raises, ``main`` turns it into the CLI's one ``error:`` sentence, and the exit code is
    non-zero. A configuration that cannot work is a failed command, not a report.
    """
    profile: ServerProfile | None = None
    if args.check_server:
        discovery = EndpointDiscovery(
            ClientSettings(
                endpoint_url=args.check_server,
                session_name="Chapter 14 - configuration check",
            )
        )
        profile = await discovery.profile()
    resolved = load_config(args.file, derived=discovery_settings(profile) if profile else None)
    review = resolved.check_server(profile) if profile is not None else resolved.review()

    if args.json:
        _emit(
            {
                "sources": list(resolved.sources),
                "settings": [value.as_dict() for value in resolved.resolved_values()],
                "review": review.as_dict(),
            }
        )
    else:
        for line in resolved.describe():
            print(line)
        print()
        for line in review.describe():
            print(line)
    if args.require_decided:
        resolved.require_decided(*args.require_decided)
    review.require_startable()
    return EXIT_OK


async def _node_arguments(args: argparse.Namespace, explorer: NamespaceExplorer) -> list[ua.NodeId]:
    """Collect the nodes a command should operate on.

    ``--batch`` is resolved by browsing, not by hardcoding NodeIds: the whole
    point of Chapter 5 and Chapter 6 is that a client finds its tags at runtime.
    """
    nodes = [to_nodeid(node) for node in (args.node or [])]
    if args.nodes_file:
        text = await asyncio.to_thread(Path(args.nodes_file).read_text, encoding="utf-8")
        nodes.extend(to_nodeid(line.strip()) for line in text.splitlines() if line.strip())
    if args.batch:
        nodes.extend(await _discover_batch_nodes(explorer))
    if not nodes:
        raise OpcuaToolkitError("no nodes given: use --node, --nodes-file or --batch")
    return nodes


async def _discover_batch_nodes(explorer: NamespaceExplorer) -> list[ua.NodeId]:
    """Find the demo server's ``BatchTagNNN`` variables with a namespace walk."""
    refs, _ = await explorer.walk_flat(
        ua.ObjectIds.ObjectsFolder, max_nodes=5_000, node_class=ua.NodeClass.Variable
    )
    batch = [reference.node_id for reference in refs if reference.name.startswith("BatchTag")]
    if not batch:
        raise OpcuaToolkitError(
            "--batch found no BatchTag variables; start the demo server with opcua-tutorial-server"
        )
    batch.sort(key=lambda node_id: str(node_id.Identifier))
    return batch


def _write_arguments(args: argparse.Namespace) -> list[WriteItem]:
    if not args.assignments:
        raise OpcuaToolkitError("no --set arguments given")
    items: list[WriteItem] = []
    for assignment in args.assignments:
        node_text, _, raw_value = assignment.partition("=")
        if not node_text or not raw_value:
            raise OpcuaToolkitError(f"--set expects NODEID=VALUE, got {assignment!r}")
        items.append(WriteItem(node_id=to_nodeid(node_text), value=_parse_scalar(raw_value)))
    return items


def _retry_from_args(args: argparse.Namespace) -> RetryPolicy:
    """Retry policy for writes; attempts come from the command line."""
    return RetryPolicy(max_attempts=args.max_attempts)


def _method_arguments(raw: Sequence[str]) -> dict[str, Any]:
    """Parse ``NAME=VALUE`` method arguments into a name keyed mapping."""
    arguments: dict[str, Any] = {}
    for item in raw:
        name, _, value = item.partition("=")
        if not name or not value:
            raise OpcuaToolkitError(f"--arg expects NAME=VALUE, got {item!r}")
        arguments[name] = _parse_scalar(value)
    return arguments


def _parse_scalar(text: str) -> Any:
    """Interpret a command line value as bool, int, float or string."""
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


#: One CLI subcommand: takes its parsed arguments, returns the process exit code.
CommandHandler = Callable[[argparse.Namespace], Coroutine[Any, Any, int]]

_COMMANDS: dict[str, CommandHandler] = {
    "discover": _discover,
    "browse": _browse,
    "inspect": _inspect,
    "read": _read,
    "write": _write,
    "call": _call,
    "subscribe": _subscribe,
    "config": _config,
}


def main(argv: list[str] | None = None) -> int:
    """Console entry point for the ``opcua-toolkit`` script."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level, json_output=args.json_logs)
    handler = _COMMANDS[args.command]
    try:
        return asyncio.run(handler(args))
    except (OpcuaToolkitError, UaError, OSError) as exc:
        # Transport failures belong here too: the first thing anyone does with this CLI is
        # point it at an address that is not answering, and the failures Python reports for
        # that (ConnectionRefusedError, TimeoutError) carry no message of their own. A
        # traceback is what a library produces; a command line has to produce a sentence.
        _fail(f"{args.command} failed: {_failure_text(exc)}")
        _logger.debug("%s failed", args.command, exc_info=True)
        return EXIT_ERROR
    except KeyboardInterrupt:
        _logger.info("%s interrupted", args.command)
        return EXIT_ERROR


def _fail(message: str) -> None:
    """Say "this did not work" once, on stderr, in the shape a shell can rely on.

    Everything else the CLI prints is data. A failure is not data and it does not belong in
    the log format, because the next line may be a healthcheck, a CI step or a script that
    only reads the exit code and the first word of the message.
    """
    print(f"error: {message}", file=sys.stderr)


def _failure_text(exc: BaseException) -> str:
    """An error message that is never empty.

    ``TimeoutError`` and its relatives say nothing beyond their own name, so printing the
    exception alone would end the line at a colon.
    """
    return str(exc) or type(exc).__name__


if __name__ == "__main__":
    sys.exit(main())
