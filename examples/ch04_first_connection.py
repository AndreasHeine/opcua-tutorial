"""Chapter 4: the first connection.

A connection is not a yes/no. Before a client reads a single value it can already
answer four questions that shape the rest of the application:

* *who* is on the other end (ApplicationUri, ProductUri, namespaces);
* *which endpoint* it actually got (security policy, security mode, transport
  profile) - the URL you typed is not the endpoint you received;
* whether the server is in a state where reading makes sense
  (``ServerState``, ``ServiceLevel``);
* what it is willing to answer *per request* (OperationLimits).

Run it:

    uv run python examples/ch04_first_connection.py --demo
    uv run python examples/ch04_first_connection.py --endpoint opc.tcp://127.0.0.1:4840

Keep this file. The shape - settings in, client out, report, clean disconnect -
is the shape of every later chapter and of the final project.
"""

from __future__ import annotations

import asyncio

from asyncua.ua.uaerrors import UaError

from _support import ChapterRun, bullet, chapter_run, heading
from opcua_toolkit import (
    ClientSettings,
    EndpointDiscovery,
    NamespaceExplorer,
    ServerHealth,
)


async def main() -> int:
    """Run Chapter 4's verification steps in order."""
    async with chapter_run(
        "open one connection and report what the server actually is",
        session_name="Chapter 4 - first connection",
        simulate=False,
    ) as run:
        heading("1. the configuration the client was built from")
        bullet(run.settings.describe())
        bullet("nothing below this line is configured anywhere else")

        async with run.open_client() as client:
            heading("2. what the server says about itself")
            health = await ServerHealth.read(client)
            bullet(f"ServerState   : {health.state.name}")
            bullet(f"ServiceLevel  : {health.service_level} ({health.band})")
            bullet(f"operational   : {health.is_operational} ({health.reason})")
            explorer = NamespaceExplorer(client)
            for index, uri in await explorer.namespaces():
                bullet(f"namespace     : ns={index} {uri}")

            heading("3. the limits this server will honour")
            limits = await run.read_limits(client)
            for row in limits.describe().splitlines():
                bullet(row)
            bullet(f"note          : {limits.status_note()}")

        heading("4. the endpoint the URL resolved to")
        profile = await EndpointDiscovery(run.settings).profile()
        for endpoint in profile.endpoints:
            bullet(endpoint.line())
        bullet(f"preferred       : {profile.best_endpoint().line()}")

        heading("5. what a wrong endpoint looks like")
        await _report_connection_failure(run)

        heading("building block")
        bullet("ClientSettings.create_client() is the only way this project opens a session")
        bullet("ServerHealth + OperationLimits are read once at startup and reused everywhere")
    return 0


async def _report_connection_failure(run: ChapterRun) -> None:
    """Connect to a port where nothing listens, and read the failure back.

    The point is not the traceback: it is that a refused connection is a
    *classifiable* event. Chapter 12 turns it into a reconnect decision instead
    of an application crash.
    """
    broken: ClientSettings = run.settings_for(endpoint_url="opc.tcp://127.0.0.1:1")
    client = broken.create_client()
    try:
        await client.connect()
    except (UaError, OSError) as exc:
        bullet(f"endpoint        : {broken.endpoint_url}")
        bullet(f"error type      : {type(exc).__name__}")
        bullet(f"error text      : {exc}")
        return
    bullet("something is listening on port 1 - this demonstration expected nothing there")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
