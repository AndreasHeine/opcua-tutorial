"""Chapter 9: methods - call a method you have never seen.

A method is the only OPC UA service that asks the server to *do* something, and a
client that hardcodes its NodeId and its positional arguments breaks the moment the
plant redeploys. The chapter's rule is therefore: read the declaration first, then
call by argument name.

* ``MethodCatalog`` reads ``InputArguments``/``OutputArguments`` and their data types,
  so the values you send are built from what the server declared;
* most methods on any server belong to the **server** (``GetMonitoredItems``,
  ``AddUser``, ``Open``): they live in ns=0 and they are not your plant's operations;
* and a method being *declared* is not the same as a server being able to *do* it -
  this example calls ``GetMonitoredItems`` and prints what this server answers;
* ``Executable`` and ``UserExecutable`` are separate facts - a method you can see is
  not a method you may run;
* a call can be ``Good`` and still have rejected arguments, and a method can answer
  ``Good`` with ``False`` in its output. Neither is an exception, and both are answers.

Run it:

    uv run python examples/ch09_methods.py --demo
    uv run python examples/ch09_methods.py --endpoint opc.tcp://127.0.0.1:4840
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from asyncua import ua

from _support import (
    STANDARD_NAMESPACE_INDEX,
    bullet,
    chapter_run,
    heading,
    instrument_service_calls,
    sample_value,
)
from opcua_toolkit import (
    ArgumentSpec,
    MethodCallError,
    MethodCatalog,
    MethodDescriptor,
    MethodInvoker,
    MethodNotFoundError,
    describe_status,
)

#: How deep the hunt for methods goes, and how many nodes it may touch on the way.
SEARCH_DEPTH = 6
SEARCH_NODES = 4_000

#: How many calls this chapter batches to show ``MaxNodesPerMethodCall``.
CALL_REPEATS = 7


async def main() -> int:
    """Discover the methods a server exposes, then call them from their declaration."""
    async with (
        chapter_run(
            "discover method signatures and call them by argument name",
            session_name="Chapter 9 - methods",
            simulate=False,
        ) as run,
        run.open_client() as client,
    ):
        counters = instrument_service_calls(client, ("call",))
        limits = await run.read_limits(client)
        catalog = MethodCatalog(client, limits=limits)
        invoker = MethodInvoker(client, limits=limits)

        heading("1. every method this server advertises")
        methods = await catalog.discover_under(
            ua.ObjectIds.ObjectsFolder, max_depth=SEARCH_DEPTH, max_nodes=SEARCH_NODES
        )
        servers_own = [
            method
            for method in methods
            if method.parent_node_id.NamespaceIndex == STANDARD_NAMESPACE_INDEX
        ]
        applications = [
            method
            for method in methods
            if method.parent_node_id.NamespaceIndex != STANDARD_NAMESPACE_INDEX
        ]
        bullet(f"{len(methods)} methods found by browsing, none hardcoded")
        bullet(f"  {len(servers_own)} of them belong to the server itself (ns=0)")
        bullet("  GetMonitoredItems, AddUser, Open: OPC UA's own services, declared by every")
        bullet("  conforming server - and section 7 asks what this one actually answers")
        bullet(
            f"  {len(applications)} belong to an application namespace: the ones an operator means"
        )
        for method in applications:
            bullet(f"  {method.browse_name:<20} {method.signature()}")
            bullet(
                f"  {'':<20} parent={method.parent_node_id_text} "
                f"executable={method.executable} callable_by_user={method.callable_by_user}"
            )
        if not applications:
            bullet("this server exposes no methods of its own - the rest has nothing to call")
            return 0

        heading("2. the declaration tells you what to pass")
        callable_methods = [method for method in applications if method.callable_by_user]
        target = next((method for method in callable_methods if method.inputs), applications[0])
        bullet(f"target: {target.signature()} on {target.parent_node_id_text}")
        for spec in target.inputs:
            bullet(f"  argument {spec.name:<14} {spec.signature()}")
        for spec in target.outputs:
            bullet(f"  output   {spec.name:<14} {spec.signature()}")
        bullet(
            "the names come from InputArguments and OutputArguments: positional arguments "
            "break the day somebody reorders them, which is exactly when nobody notices"
        )

        heading("3. call it with values built from the declaration")
        arguments = {spec.name: _argument_value(spec) for spec in target.inputs}
        bullet(f"{target.browse_name} <- {arguments}")
        try:
            result = await invoker.call(target, arguments)
        except MethodCallError as exc:
            bullet(f"MethodCallError: {exc}")
            bullet("a server may answer a method call with a status code of its own, and an")
            bullet("example that crashed here would hide what the server actually said")
            result = None
        if result is not None:
            bullet(f"status  : {describe_status(result.status)}")
            bullet(f"outputs : {result.outputs}")
            for spec in target.outputs:
                bullet(f"  {spec.name:<14} = {result.output_of(spec.name)!r}")

        heading("4. an argument the server never declared")
        try:
            await invoker.call(target, {"totally_made_up": 1})
        except MethodNotFoundError as exc:
            bullet(f"MethodNotFoundError: {exc}")
        bullet(
            "the request never left the client: a wrong argument name is a bug in the caller, "
            "and the cheapest place to catch it is before the call"
        )

        heading("5. Good is not the same as success")
        acknowledgement = next(
            (method for method in callable_methods if method.browse_name == "AcknowledgeAlarm"),
            None,
        )
        if acknowledgement is not None:
            alarm = {spec.name: "alarm-that-does-not-exist" for spec in acknowledgement.inputs}
            try:
                verdict = await invoker.call(acknowledgement, alarm)
            except MethodCallError as exc:
                bullet(f"MethodCallError: {exc}")
            else:
                bullet(
                    f"{acknowledgement.browse_name} status={describe_status(verdict.status)} "
                    f"outputs={verdict.outputs}"
                )
                bullet(
                    "a Good status with False in the output is a refused action, not an error: "
                    "log the output or you will record a success that never happened"
                )
        else:
            bullet("this server has no AcknowledgeAlarm to demonstrate that with")

        heading("6. calls are chunked too")
        quiet = [method for method in callable_methods if not method.inputs]
        if quiet:
            calls: list[tuple[MethodDescriptor, dict[str, Any]]] = [
                (quiet[0], {}) for _ in range(CALL_REPEATS)
            ]
            counters["call"] = 0
            batch = await invoker.call_many(calls)
            bullet(
                f"{CALL_REPEATS} calls to {quiet[0].browse_name} became "
                f"{counters['call']} CallMethod request(s)"
            )
            expected = math.ceil(CALL_REPEATS / limits.max_nodes_per_method_call)
            bullet(
                f"MaxNodesPerMethodCall={limits.max_nodes_per_method_call} -> expected {expected}"
            )
            bullet(f"all Good: {all(item.ok for item in batch)}")
        else:
            bullet("this server exposes no method that takes no arguments")

        heading("7. a method every server declares, and what this one answers")
        bookkeeping = next(
            (method for method in servers_own if method.browse_name == "GetMonitoredItems"),
            None,
        )
        if bookkeeping is None:
            bullet("this server exposes no GetMonitoredItems to try")
        else:
            bullet(f"{bookkeeping.signature()}")
            bullet(
                f"  node={bookkeeping.node_id_text} parent={bookkeeping.parent_node_id_text} "
                f"executable={bookkeeping.executable} "
                f"callable_by_user={bookkeeping.callable_by_user}"
            )
            declared_arguments = {spec.name: _argument_value(spec) for spec in bookkeeping.inputs}
            try:
                answer = await invoker.call(bookkeeping, declared_arguments)
            except MethodCallError as exc:
                bullet(f"MethodCallError: {exc}")
                bullet("declared, executable, callable by this user - and nothing implements it:")
                bullet("a method node is a name and a signature, not a promise that the server")
                bullet("can do the work")
            else:
                bullet(
                    f"{bookkeeping.browse_name} -> {describe_status(answer.status)} "
                    f"outputs={answer.outputs}"
                )
                bullet("where a server really does answer this call it is the best cross-check a")
                bullet("subscription client has - ask before an architecture depends on it")

        heading("8. the state all this left behind")
        status_method = next(
            (method for method in callable_methods if method.browse_name == "GetLineStatus"),
            None,
        )
        if status_method is not None:
            try:
                state = await invoker.call(status_method, {})
            except MethodCallError as exc:
                bullet(f"MethodCallError: {exc}")
            else:
                bullet(f"GetLineStatus -> {describe_status(state.status)} outputs={state.outputs}")
                bullet("a method is how a client changes a plant; the outputs are how it finds out")
        else:
            bullet("this server exposes no status method to check")

        heading("building block")
        bullet("MethodCatalog.discover_under / MethodInvoker.call / call_many")
        bullet("CallResult keeps outputs named and keeps InputArgumentResults visible")
    return 0


def _argument_value(spec: ArgumentSpec) -> Any:
    """Build a value for one declared argument, including the array case."""
    value = sample_value(spec.data_type_name)
    if spec.is_array:
        return [value, value]
    return value


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
