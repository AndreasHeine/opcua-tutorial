"""Integration tests for the ``opcua-toolkit`` console script.

The CLI is the first thing anyone tries, and it is also what a shell script, a CI step or a
container healthcheck calls. So the contract tested here is not the output format: it is the
exit code, and the one line of explanation that comes out when a server is not answering.
A tool that prints a traceback for a refused connection is a tool nobody can put in a script.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from opcua_toolkit.cli import EXIT_ERROR, EXIT_OK, main
from tests.conftest import free_port

pytestmark = pytest.mark.integration

#: A tag the demo server always has.
TEMPERATURE = "ns=2;s=Temperature"

#: A tag no server has, spelled so that a test can tell it apart from a typo.
MISSING = "ns=2;s=TagThisTutorialNeverDefines"

#: Seconds the CLI is allowed to wait for an endpoint that is not there.
SHORT_TIMEOUT = 1.0


async def run_cli(*argv: str) -> int:
    """Run one CLI invocation and return its exit code.

    :func:`opcua_toolkit.cli.main` owns its own event loop, so it runs in a thread here.
    That is worth knowing before Chapter 30 puts this CLI behind a web API: an entry point
    that calls ``asyncio.run`` cannot be called from inside a running loop.
    """
    return await asyncio.to_thread(main, list(argv))


async def test_discover_profiles_a_server_without_any_configuration(demo_server, capsys) -> None:
    code = await run_cli("discover", "--endpoint", demo_server.endpoint)
    printed = capsys.readouterr().out

    assert code == EXIT_OK
    assert "endpoint        :" in printed
    assert demo_server.namespace_uri in printed
    assert "unsecured endpoints only" in printed


async def test_discover_json_is_machine_readable(demo_server, capsys) -> None:
    """The same profile a human reads, for a script to consume.

    ``--json`` is what makes the CLI usable from a health endpoint or a CI step, and JSON
    on stdout is only useful if nothing else was printed there.
    """
    code = await run_cli("discover", "--json", "--endpoint", demo_server.endpoint)
    printed = capsys.readouterr().out

    assert code == EXIT_OK
    payload = json.loads(printed)
    assert payload["endpoint_url"] == demo_server.endpoint
    assert demo_server.namespace_uri in payload["namespaces"]


async def test_read_prints_one_line_per_requested_tag(demo_server, capsys) -> None:
    code = await run_cli("read", "--node", TEMPERATURE, "--endpoint", demo_server.endpoint)
    printed = capsys.readouterr().out

    assert code == EXIT_OK
    assert "bulk read :" in printed
    assert TEMPERATURE in printed
    assert "ok" in printed


async def test_a_partly_failed_read_exits_non_zero_and_names_the_status(
    demo_server, capsys
) -> None:
    """A partial failure has to show in the exit code, not only in the text.

    A job that read 24 tags and got 23 did not succeed, and a shell that cannot tell the
    difference is how a plant quietly loses one tag for a month.
    """
    code = await run_cli(
        "read", "--node", TEMPERATURE, "--node", MISSING, "--endpoint", demo_server.endpoint
    )
    printed = capsys.readouterr().out

    assert code == EXIT_ERROR
    assert MISSING in printed
    assert "BadNodeIdUnknown" in printed


async def test_browse_resolves_a_path_from_the_objects_folder(demo_server, capsys) -> None:
    """``--path`` is the command a reader uses first, so its two spellings are tested.

    ``ProductionLine/Line1`` is the path relative to the Objects folder the CLI starts at;
    ``Objects/ProductionLine`` is the same place written the way the address space is drawn,
    which names the starting node itself.
    """
    relative = await run_cli(
        "browse", "--path", "ProductionLine/Line1", "--endpoint", demo_server.endpoint
    )
    printed_relative = capsys.readouterr().out
    assert relative == EXIT_OK
    assert "resolved ProductionLine/Line1 -> ns=2;s=Line1" in printed_relative

    drawn = await run_cli(
        "browse", "--path", "Objects/ProductionLine", "--endpoint", demo_server.endpoint
    )
    printed_drawn = capsys.readouterr().out
    assert drawn == EXIT_OK
    assert "resolved Objects/ProductionLine -> ns=2;s=ProductionLine" in printed_drawn


async def test_browse_looks_for_a_path_under_the_node_it_was_given(demo_server, capsys) -> None:
    """``--node`` decides where the first segment of ``--path`` is looked for.

    Before this was tested, ``--node`` was quietly ignored whenever ``--path`` was present,
    which made a rooted path look like a broken server: the segments were searched from the
    Objects folder, where they are not.
    """
    code = await run_cli(
        "browse",
        "--node",
        "ns=2;s=ProductionLine",
        "--path",
        "Line1/Temperature",
        "--endpoint",
        demo_server.endpoint,
    )
    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "resolved Line1/Temperature -> ns=2;s=Temperature" in printed

    broken = await run_cli(
        "browse",
        "--node",
        "ns=2;s=Line1",
        "--path",
        "ProductionLine/Line1",
        "--endpoint",
        demo_server.endpoint,
    )
    refused = capsys.readouterr().err
    assert broken == EXIT_ERROR
    assert "No node named 'ProductionLine' under ns=2;s=Line1 (segment 1 of 2)" in refused


async def test_a_dead_endpoint_is_one_line_of_output_not_a_traceback(capsys) -> None:
    """The first command anyone runs is usually pointed at the wrong address.

    What Python calls a connection failure (``ConnectionRefusedError``, ``TimeoutError``)
    carries no message of its own, so a CLI that prints the exception prints a line ending
    in a colon, and a CLI that does not catch it prints frames from inside the protocol
    stack. Neither is usable from a shell, and both hide the one fact that matters: nothing
    answered.
    """
    port = free_port()
    code = await run_cli(
        "discover",
        "--endpoint",
        f"opc.tcp://127.0.0.1:{port}",
        "--timeout",
        str(SHORT_TIMEOUT),
    )
    errors = capsys.readouterr().err

    assert code == EXIT_ERROR
    # asyncua writes its own warnings to stderr, and those are the library's business. What
    # has to be true here is that the toolkit says what happened once, in one line, and does
    # not hand the caller a stack of frames from inside the protocol stack.
    own_lines = [line for line in errors.splitlines() if line.startswith("error: ")]
    assert len(own_lines) == 1
    assert "Traceback" not in errors
    # And the sentence is finished: a failure that ends at a colon explains nothing.
    assert own_lines[0].removeprefix("error: discover failed: ")


async def test_call_prints_the_answer_and_not_the_whole_catalogue(demo_server, capsys) -> None:
    """A call is a question about one method, and the answer is one line.

    The CLI has to browse the subtree to find the method at all - that is Chapter 9's rule -
    but printing 130-odd declarations on the way to ``Accepted = True`` makes the answer
    unreadable and buries the status code the caller is actually checking.
    """
    code = await run_cli("call", "--method", "GetLineStatus", "--endpoint", demo_server.endpoint)
    printed = capsys.readouterr().out

    assert code == EXIT_OK
    assert "GetLineStatus [ns=2;s=GetLineStatus] -> Good" in printed
    assert "Status = Line1" in printed
    assert "AddUser" not in printed


async def test_listing_methods_is_a_separate_explicit_request(demo_server, capsys) -> None:
    """``--list-methods`` is the way to ask for the declarations, and it stops there."""
    code = await run_cli(
        "call", "--list-methods", "--root", "i=2253", "--endpoint", demo_server.endpoint
    )
    printed = capsys.readouterr().out

    assert code == EXIT_OK
    assert printed.startswith("methods below i=2253:")
    assert "GetMonitoredItems(SubscriptionId: UInt32)" in printed


async def test_an_argument_the_declaration_does_not_allow_never_reaches_the_server(
    demo_server, capsys
) -> None:
    """``GetMonitoredItems`` needs ``SubscriptionId``, and the toolkit says so before connecting.

    This is the CLI version of Chapter 9's central rule: the address space declares the
    arguments, and a call that cannot match that declaration is a bug in the caller. It is
    refused locally instead of sent off to be answered by some StatusCode nobody asked for.
    """
    code = await run_cli(
        "call", "--method", "GetMonitoredItems", "--endpoint", demo_server.endpoint
    )
    errors = capsys.readouterr().err

    assert code == EXIT_ERROR
    own_lines = [line for line in errors.splitlines() if line.startswith("error: ")]
    assert own_lines == [
        "error: call failed: GetMonitoredItems is missing argument 'SubscriptionId'"
    ]


async def test_a_method_that_is_not_there_names_what_was_searched(demo_server, capsys) -> None:
    """ "Not found" has to say where it looked, or the next step is guessing."""
    code = await run_cli(
        "call",
        "--method",
        "OpenThePlant",
        "--root",
        "ns=2;s=Line1",
        "--endpoint",
        demo_server.endpoint,
    )
    errors = capsys.readouterr().err

    assert code == EXIT_ERROR
    own_lines = [line for line in errors.splitlines() if line.startswith("error: ")]
    assert len(own_lines) == 1
    assert "OpenThePlant" in own_lines[0]
    assert "ns=2;s=Line1" in own_lines[0]
    assert "--list-methods" in own_lines[0]
