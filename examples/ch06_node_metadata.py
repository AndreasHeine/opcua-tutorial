"""Chapter 6: node metadata - may my application touch this node?

Chapter 5 got you *to* a node. Chapter 6 answers the question that decides whether
you may read or write it, and it answers it from attributes the server publishes:

* ``DataType``, ``ValueRank`` and ``ArrayDimensions`` say what a value must look like;
* ``AccessLevel`` is what the node allows, ``UserAccessLevel`` is what *you* are
  allowed in this session - they are not the same attribute;
* ``MinimumSamplingInterval`` is the server's floor for Chapter 10's sampling rate;
* ``Historizing`` says whether history is even switched on;
* the ``TypeDefinition`` reference says which variable type the node was built from,
  which is what a companion-specification client checks in Chapter 23.

Run it:

    uv run python examples/ch06_node_metadata.py --demo
    uv run python examples/ch06_node_metadata.py --endpoint opc.tcp://127.0.0.1:4840
"""

from __future__ import annotations

import asyncio

from asyncua import ua

from _support import bullet, chapter_run, heading, plant_variables
from opcua_toolkit import (
    NamespaceExplorer,
    NodeDescriptor,
    NodeInspector,
    NodeNotAccessibleError,
    format_node_id,
)

#: How many tags this demonstration describes in full. A terminal shows a page,
#: not a namespace dump; the count printed above says what the scan really found.
INSPECT_LIMIT = 40


async def main() -> int:
    """Describe the tags a browse found, and read the rights attached to them."""
    async with (
        chapter_run(
            "read the attributes that say what a node is and what may be done to it",
            session_name="Chapter 6 - node metadata",
            simulate=False,
        ) as run,
        run.open_client() as client,
    ):
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)
        inspector = NodeInspector(client, limits=limits, include_value=True)

        heading("1. the nodes come from browsing, not from a list in the source")
        found, stats = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        candidates = plant_variables(found)
        bullet(
            f"{len(found)} variables scanned, {len(candidates)} of them outside ns=0 "
            f"({stats.summary()})"
        )
        bullet("ns=0 is the server describing itself; the tags are always somewhere else")

        heading("2. one Read per attribute, one line per node")
        targets = candidates[:INSPECT_LIMIT]
        descriptors: list[NodeDescriptor] = await inspector.inspect_many(
            [node.node_id for node in targets]
        )
        if not descriptors:
            bullet("this server publishes no variables below Objects - nothing to describe")
            return 0
        for descriptor in descriptors:
            print(f"  {descriptor.summary_line()}")
        if len(candidates) > len(descriptors):
            bullet(f"described the first {INSPECT_LIMIT} of {len(candidates)} tags")

        heading("3. the same server holds tags you may write and tags you may not")
        writable = [descriptor for descriptor in descriptors if descriptor.writable]
        blocked = [descriptor for descriptor in descriptors if not descriptor.writable]
        bullet(f"writable     : {len(writable)} nodes")
        bullet(
            f"not writable : {len(blocked)} nodes -> "
            f"{', '.join(descriptor.browse_name.Name for descriptor in blocked[:5])}"
        )
        bullet(
            "InputArguments and OutputArguments are method declaration variables living in "
            "the same namespace: Chapter 9 reads them as signatures, not as tags"
        )
        bullet("Chapter 8 writes to both and compares the status codes")

        heading("4. everything one node can tell you")
        focus = writable[0] if writable else descriptors[0]
        bullet(f"node          : {focus.node_id_text} ({focus.node_class.name})")
        bullet(f"browse name   : {focus.browse_name.Name} (ns={focus.browse_name.NamespaceIndex})")
        bullet(f"display name  : {focus.display_name}")
        bullet(f"description   : {focus.description}")
        bullet(f"data type     : {focus.data_type_text}")
        bullet(f"  as a NodeId : {format_node_id(focus.data_type)}")
        bullet(
            "  named by    : "
            + (
                "the server - a custom type, so one Read named all of them"
                if focus.resolved_data_type
                else "the client - a built-in type, so no Read was needed to name it"
            )
        )
        bullet(f"value rank    : {focus.value_rank} dimensions={focus.array_dimensions}")
        bullet(f"access level  : {focus.access_level.describe()}")
        bullet(f"user access   : {focus.user_access_level.describe()}")
        rights = focus.access_level
        bullet("  the five other named bits of the same byte:")
        bullet(
            f"  history_read={rights.history_read} history_write={rights.history_write} "
            f"semantic_change={rights.semantic_change}"
        )
        bullet(f"  status_write={rights.status_write} timestamp_write={rights.timestamp_write}")
        bullet(f"min sampling  : {focus.minimum_sampling_interval} ms from this node")
        bullet(
            "  0 does not mean 'as fast as you like': the server-wide MinimumSupportedSampleRate"
        )
        bullet("  is the other floor, and Chapter 10 is where the two meet")
        bullet(f"historizing   : {focus.historizing} (Chapter 20 needs this to be true)")
        bullet(f"type          : {focus.type_definition}")
        bullet(f"current value : {focus.value}")

        heading("5. a node the server does not have")
        try:
            await inspector.inspect("ns=2;s=ThisTagDoesNotExist")
        except NodeNotAccessibleError as exc:
            bullet(f"NodeNotAccessibleError: {exc}")
        bullet("a missing node and an unreadable node are different problems: the")
        bullet("inspector keeps the status code that says which one you have")

        heading("building block")
        bullet("NodeInspector: inspect / inspect_many -> NodeDescriptor")
        bullet("AccessRights names each AccessLevelType bit the specification gives a meaning to")
        bullet("NodeDescriptor.summary_line and .as_dict render a node for a log or a JSON payload")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
