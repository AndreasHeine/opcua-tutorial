"""Chapter 8: writing values - "no exception" is not "it worked".

A write that does not raise is a write nobody has verified. OPC UA answers a bulk
write with one StatusCode per node, and the codes point at different problems:

* ``BadNotWritable`` - the node's ``AccessLevel`` forbids it. Look at rights, not at
  your value;
* ``BadUserAccessDenied`` - the node would allow it, this session may not;
* ``BadTypeMismatch`` - the value does not fit the node's ``DataType``, which is
  almost always a bug in the caller;
* ``BadNodeIdUnknown`` - the node is not there. Calling that "not writable" sends the
  operator to the wrong machine;
* and the *same* refusal arriving as two different codes. With the client-side check on,
  the toolkit refuses on ``AccessLevel`` and reports ``BadNotWritable``; with it off, the
  request goes out and this server answers ``BadUserAccessDenied``. Some servers answer
  ``Good`` instead, because ``AccessLevel`` is something a server publishes and enforcing
  it is a separate decision the server makes for itself.

Run it:

    uv run python examples/ch08_bulk_write.py --demo
    uv run python examples/ch08_bulk_write.py --endpoint opc.tcp://127.0.0.1:4840

Every successful write writes back the value the node already holds, and a node the
server calls read only is never written when the server is not the tutorial's own. The
one value this example really changes sits on the demo server and is put back after the
demonstration.
"""

from __future__ import annotations

import asyncio
import math

from asyncua import ua

from _support import (
    bullet,
    chapter_run,
    heading,
    instrument_service_calls,
    plant_variables,
)
from opcua_toolkit import (
    BulkReader,
    BulkWriter,
    NamespaceExplorer,
    NodeInspector,
    PartialFailureError,
    WriteItem,
    WriteOutcome,
    WriteReport,
    describe_status,
)

#: Data types that a deliberately wrong value can be rejected by.
NUMERIC_TYPES: frozenset[str] = frozenset(
    {"Double", "Single", "Float", "Int16", "Int32", "Int64", "UInt16", "UInt32", "SByte", "Byte"}
)

#: The value Chapter 8 writes to the demo server's own read-only tag, and then undoes.
PROBE_VALUE = 7.75


async def main() -> int:
    """Write to nodes the server allows, to nodes it refuses, and to a node that is absent."""
    async with (
        chapter_run(
            "write a batch and read the status code of every single item",
            session_name="Chapter 8 - writing values",
            simulate=False,
        ) as run,
        run.open_client() as client,
    ):
        counters = instrument_service_calls(
            client, ("write", "write_attributes", "read", "read_attributes")
        )
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)
        inspector = NodeInspector(client, limits=limits, include_value=True)
        reader = BulkReader(client, limits=limits)

        heading("1. the targets come from browsing and metadata")
        found, _ = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        candidates = plant_variables(found)
        descriptors = [
            descriptor
            for descriptor in await inspector.inspect_many([node.node_id for node in candidates])
            if descriptor.value is not None
        ]
        writable = [descriptor for descriptor in descriptors if descriptor.writable]
        blocked = [descriptor for descriptor in descriptors if not descriptor.writable]
        bullet(f"{len(descriptors)} variables: {len(writable)} writable, {len(blocked)} not")
        bullet(f"MaxNodesPerWrite published by this server: {limits.max_nodes_per_write}")

        heading("2. a batch that should work")
        writer = BulkWriter(client, limits=limits)
        counters["write_attributes"] = 0
        report = await writer.write([WriteItem(item.node_id, item.value) for item in writable[:12]])
        bullet(report.summary())
        bullet(
            f"requests counted on the session: {counters['write_attributes']} "
            f"for {report.requested} items"
        )
        bullet(
            f"expected from MaxNodesPerWrite: "
            f"{math.ceil(report.requested / limits.max_nodes_per_write)}"
        )
        _show(report)

        heading("3. one batch, three different refusals")
        numeric = next((item for item in writable if item.data_type_text in NUMERIC_TYPES), None)
        victim = next(
            (item for item in blocked if item.data_type_text == "Double"),
            blocked[0] if blocked else None,
        )
        probes: list[WriteItem] = []
        if victim is not None:
            probes.append(WriteItem(victim.node_id, victim.value))
        probes.append(WriteItem("ns=2;s=ThisTagDoesNotExist", 1))
        if numeric is not None:
            probes.append(WriteItem(numeric.node_id, "not a number"))
        mixed = await writer.write(probes)
        bullet(mixed.summary())
        _show(mixed)
        bullet("three items, three status codes, and not one exception")

        heading("4. the same item with the client-side check turned off")
        if run.uses_demo_server and victim is not None:
            unchecked = BulkWriter(client, limits=limits, check_access=False)
            counters["write_attributes"] = 0
            counters["read_attributes"] = 0
            server_said = await unchecked.write([WriteItem(victim.node_id, PROBE_VALUE)])
            bullet(server_said.summary())
            bullet(
                f"requests now: write={counters['write_attributes']} "
                f"read={counters['read_attributes']} - the preflight spent three reads "
                "and no write at all"
            )
            _show(server_said)
            if server_said.succeeded:
                bullet("this server accepted a write to a node it publishes as read only")
                bullet("AccessLevel is what a server says; enforcing it is a separate decision")
                bullet("a client that counts on the server to say no will be surprised by a Good")
            else:
                verdict = describe_status(server_said.outcomes[0].status)
                bullet(f"the client's preflight said BadNotWritable; this server said {verdict}")
                bullet(
                    "a verdict and a status code are not the same thing, so a report keeps "
                    "the code and the note instead of a boolean"
                )
            await run.demo_server.set_value(victim.browse_name.Name, victim.value)
            bullet(f"the demo value was put back: {victim.node_id_text} = {victim.value!r}")
        else:
            bullet("this example will not write to a node its server calls read only unless the")
            bullet("server is the tutorial's own: run with --demo to watch that answer arrive")

        heading("5. a collector must not walk past a bad batch")
        try:
            mixed.raise_for_failures()
        except PartialFailureError as exc:
            bullet(f"PartialFailureError: {exc}")
            names = [item.node_id_text for item in mixed.failures()]
            bullet(f"the failures survive as data: {names}")

        heading("6. what the refused writes did not change")
        targets = [victim.node_id] if victim is not None else []
        if numeric is not None:
            targets.append(numeric.node_id)
        for text, value in (await reader.values_of(targets)).items():
            bullet(f"{text:<28} {value!r}")
        results, _ = await reader.read_values(["ns=2;s=ThisTagDoesNotExist"])
        for result in results:
            bullet(
                f"{result.node_id_text:<28} {describe_status(result.status)} value={result.value!r}"
            )
        bullet("a refused write leaves the node as it was: only the report changed")

        heading("building block")
        bullet("BulkWriter: WriteItem in, WriteReport out - one outcome per requested item")
        bullet("WriteReport.raise_for_failures() turns a bad batch into a decision")
    return 0


def _show(report: WriteReport) -> None:
    """Print one line per requested item, because that is what a report is for."""
    outcome: WriteOutcome
    for outcome in report.outcomes:
        note = f" note={outcome.note}" if outcome.note else ""
        bullet(
            f"  {outcome.node_id_text:<28} {describe_status(outcome.status)} "
            f"attempts={outcome.attempts}{note}"
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
