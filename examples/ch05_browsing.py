"""Chapter 5: browsing - finding your tags instead of hardcoding them.

A hardcoded NodeId is a promise about somebody else's address space, and it is the
first thing that breaks when a vendor ships a new build or a customer renames a
folder. This example reaches every tag **by name** and shows what that costs:

* Browse is a *reference* service: it tells you what is connected to what, not what
  a value is;
* NodeClass is a server-side filter, and it knows nothing about access rights;
* a big folder costs more than one request because the server publishes a
  continuation point, which is why ``BrowseNext`` exists;
* ``TranslateBrowsePathsToNodeIds`` reaches a known path in one request once you
  know the namespace index - and Chapter 13 is what tells you the index.

Run it:

    uv run python examples/ch05_browsing.py --demo
    uv run python examples/ch05_browsing.py --endpoint opc.tcp://127.0.0.1:4840
"""

from __future__ import annotations

import asyncio

from asyncua import ua

from _support import bullet, chapter_run, heading, plant_variables
from opcua_toolkit import BrowsePathError, NamespaceExplorer


async def main() -> int:
    """Walk a server, resolve a path, and read what exploring it cost."""
    async with (
        chapter_run(
            "explore an address space without hardcoding a single NodeId",
            session_name="Chapter 5 - browsing",
            simulate=False,
        ) as run,
        run.open_client() as client,
    ):
        limits = await run.read_limits(client)
        explorer = NamespaceExplorer(client, limits=limits)

        heading("1. the namespaces, in the order the server publishes them")
        namespaces = await explorer.namespaces()
        for index, uri in namespaces:
            bullet(f"ns={index}  {uri}")
        application = [index for index, uri in namespaces if index != 0]
        bullet(f"application namespaces: {application or 'server publishes ns=0 only'}")

        heading("2. the tree, walked to the server's own limits")
        tree, stats = await explorer.walk(ua.ObjectIds.ObjectsFolder, max_depth=4, max_nodes=300)
        print(tree.render(max_rows=25))
        bullet(stats.summary())
        bullet(f"MaxNodesPerBrowse={limits.max_nodes_per_browse} is what shaped that walk")

        heading("3. the same walk as a flat list, keeping only the variables")
        flat, flat_stats = await explorer.walk_flat(
            ua.ObjectIds.ObjectsFolder, max_nodes=2_000, node_class=ua.NodeClass.Variable
        )
        own = plant_variables(flat)
        bullet(f"variables collected: {len(flat)}, of which {len(own)} hold this server's data")
        for ref in own[:8]:
            bullet(f"{ref.name:<22} {ref.node_id_text}")
        bullet(flat_stats.summary())
        bullet(
            "the walk browsed every level and filtered on the way home: a server-side "
            "Variable filter here would have stopped at the Objects and found nothing"
        )

        heading("4. a browse path, resolved by name")
        line = await explorer.find_by_browse_path(["ProductionLine", "Line1"])
        bullet(f"ProductionLine/Line1 -> {line.node_id_text} ({line.node_class.name})")
        variables = await explorer.children(line.node_id, node_class=ua.NodeClass.Variable)
        bullet(f"{len(variables)} variables under {line.name} (server-side NodeClass filter)")
        bullet(
            "read-only tags appear here too: NodeClass is about the kind of node, "
            "never about who may write it"
        )

        heading("5. the same path with one TranslateBrowsePathsToNodeIds")
        index = line.node_id.NamespaceIndex
        try:
            await explorer.resolve_path_with_translate(
                ["ProductionLine", "Line1", "Temperature"], namespace_index=0
            )
        except BrowsePathError as exc:
            bullet(f"ns=0:Temperature -> {exc}")
        resolved = await explorer.resolve_path_with_translate(
            ["ProductionLine", "Line1", "Temperature"], namespace_index=index
        )
        bullet(f"resolved in one request -> {resolved.node_id_text}")
        bullet(f"ns={index} came from the browse above, not from a constant in this file")
        bullet(f"application namespaces this server published: {application}")
        bullet("a BrowseName is matched *with* its namespace, so guessing the index is a bug")

        heading("6. a path that does not exist")
        try:
            await explorer.find_by_browse_path(["ProductionLine", "NotThere"])
        except BrowsePathError as exc:
            bullet(f"BrowsePathError: {exc}")
            bullet(f"the path that failed: {'/'.join(exc.path)}")

        heading("building block")
        bullet(
            "NamespaceExplorer: walk / walk_flat / children / find_by_browse_path / "
            "resolve_path_with_translate"
        )
        bullet("BrowseStats is the number you tune against, and it is always printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
