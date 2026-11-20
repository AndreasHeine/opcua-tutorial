"""Chapter 7: reading values - bulk is a number you can measure.

"One Read" versus "many Reads" is the difference between a collector that keeps up
with a plant and one that falls behind. This chapter measures it instead of
asserting it:

* a Read request is per *chunk*, and the chunk size is the server's
  ``MaxNodesPerRead``, not a constant you picked;
* the answer is not one status: it is one status *per node*, which is why a
  partially failing batch is normal and must never collapse into one exception;
* a Read is per *attribute*, so metadata and values are the same service.

The example counts the real requests on the client's own session, so the number the
toolkit reports and the number the protocol made have to agree.

Run it:

    uv run python examples/ch07_bulk_read.py --demo
    uv run python examples/ch07_bulk_read.py --endpoint opc.tcp://127.0.0.1:4840
"""

from __future__ import annotations

import asyncio
import math

from asyncua import ua

from _support import bullet, chapter_run, heading, instrument_service_calls, plant_variables
from opcua_toolkit import BulkReader, NamespaceExplorer, describe_status

#: How many descriptions this demonstration prints. A terminal needs a few lines,
#: not a page.
SHOW_DESCRIPTIONS = 3


async def main() -> int:
    """Read the same nodes twice: once as one bulk read, once as a mistake."""
    async with (
        chapter_run(
            "measure one bulk read against one read per node",
            session_name="Chapter 7 - bulk reading",
            simulate=False,
        ) as run,
        run.open_client() as client,
    ):
        counters = instrument_service_calls(client, ("read", "read_attributes"))
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)
        reader = BulkReader(client, limits=limits)

        heading("1. the nodes come from the server")
        found, _ = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        candidates = plant_variables(found)
        nodes = [ref.node_id for ref in candidates]
        counters["read_attributes"] = 0
        counters["read"] = 0
        bullet(f"targets: {len(nodes)} variables, found by browsing")
        bullet(f"MaxNodesPerRead published by this server: {limits.max_nodes_per_read}")
        expected = math.ceil(len(nodes) / limits.max_nodes_per_read)
        bullet(f"a correctly chunked read therefore needs {expected} request(s)")

        heading("2. bulk read")
        results, stats = await reader.read_values(nodes)
        bullet(stats.summary())
        bullet(f"requests counted on the session: {counters['read_attributes']}")
        bullet(f"nodes per request            : {stats.nodes_per_request:.1f}")
        bullet(f"failed                       : {stats.failed}")
        for result in results[:5]:
            bullet(
                f"  {result.node_id_text:<28} {describe_status(result.status)} "
                f"value={result.value!r}"
            )
        if stats.requests != counters["read_attributes"]:
            bullet("MISMATCH between the toolkit's stats and the protocol - a bug worth chasing")

        heading("3. the same values, read one by one")
        counters["read"] = 0
        counters["read_attributes"] = 0
        baseline_results, baseline = await reader.read_one_by_one(nodes)
        bullet(baseline.summary())
        bullet(
            f"requests counted on the session: {counters['read'] + counters['read_attributes']} "
            f"(read={counters['read']}, read_attributes={counters['read_attributes']})"
        )
        bullet(
            "asyncua's Node.read_data_value asks for the value through Read, not "
            "ReadAttributes: same service, different request shape, and one node per request"
        )
        ratio = baseline.elapsed_ms / max(stats.elapsed_ms, 1.0)
        bullet(f"cost ratio: {ratio:.1f}x the bulk read for the same {len(nodes)} values")
        bullet(f"same answers: {sum(1 for item in baseline_results if item.ok) == stats.succeeded}")

        heading("4. a batch is not a promise: per node statuses")
        mixed = [*nodes[:3], "ns=2;s=ThisTagDoesNotExist"]
        partial, partial_stats = await reader.read_values(mixed)
        bullet(partial_stats.summary())
        for result in partial:
            bullet(f"  {result.node_id_text:<28} {describe_status(result.status)}")
        bullet(
            "the batch still returned every node it could - a partial failure is data, not an error"
        )

        heading("5. the same service reads metadata")
        texts, meta_stats = await reader.read_attribute(nodes, ua.AttributeIds.Description)
        bullet(meta_stats.summary())
        shown = 0
        for result in texts:
            if not result.ok or result.value is None:
                continue
            text = str(getattr(result.value, "Text", result.value) or "")
            if not text:
                continue
            bullet(f"  {result.node_id_text:<28} {text}")
            shown += 1
            if shown >= SHOW_DESCRIPTIONS:
                break

        heading("building block")
        bullet("BulkReader: read_values / read_attribute / values_of, plus BulkReadStats")
        bullet("read_one_by_one exists only to prove what not batching costs")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
