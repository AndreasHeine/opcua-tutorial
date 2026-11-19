# Getting started

Five minutes to a running OPC UA server, a client that reports what it found, and a test
suite you can prove is green.

## 1. What you need

| Requirement | Version | Why |
| --- | --- | --- |
| Python | 3.13 | what `pyproject.toml` requires (`requires-python = ">=3.13"`) and what CI runs |
| [uv](https://docs.astral.sh/uv/) | any recent release | installs the locked environment; `uv.lock` pins exactly what CI tested |
| Git | any | the chapters are branches and tags, not downloads |
| Docker (optional) | with compose v2 | runs the demo server as a container |
| UaExpert or Prosys (optional) | any recent | independent verification of the protocol claims |

No OPC UA server, vendor product or OPC Foundation account is required: the tutorial runs
its own asyncua server.

## 2. Install

```powershell
git clone https://github.com/AndreasHeine/opcua-tutorial.git
cd opcua-tutorial
git checkout rework/v3
uv sync
```

`uv sync` creates `.venv` from `uv.lock` and installs the package in editable form, so the
two console scripts become available:

| Script | Entry point | What it does |
| --- | --- | --- |
| `opcua-toolkit` | `opcua_toolkit.cli:main` | `discover`, `browse`, `inspect`, `read`, `write`, `call`, `subscribe` |
| `opcua-tutorial-server` | `opcua_toolkit.devserver:main` | runs the tutorial's demo server |

Optional extras for later parts: `uv sync --extra rest` (Chapter 30) and
`uv sync --extra historian` (Chapter 20).

## 3. Run the first chapter

```powershell
uv run python examples/ch04_first_connection.py --demo
```

The example starts its own demo server on a free port, connects, and prints what the server
actually is - state, service level, namespaces, limits, endpoints - and then what a wrong
endpoint looks like. The output is the lesson; you do not have to read the code to learn
from it.

Add `--demo-port 48401` whenever you want a **fixed** address so a second client (UaExpert,
Prosys) can be pointed at the same server while the example runs. Use `--endpoint` to run
the same script against a server you do not control:

```powershell
uv run python examples/ch05_browsing.py --endpoint opc.tcp://127.0.0.1:4840
```

The chapter list is [docs/chapters/README.md](chapters/README.md).

## 4. Run the server on its own

```powershell
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401
```

Useful variants:

```powershell
uv run opcua-tutorial-server --port 48401 --no-simulate            # frozen values
uv run opcua-tutorial-server --port 48401 --max-nodes-per-read 25  # a different server, same client
uv run opcua-tutorial-server --port 48401 --no-limits              # a server that publishes no limits
uv run opcua-tutorial-server --port 48401 --historize --history-path %TEMP%\temperature.db
uv run opcua-tutorial-server --port 48401 --json-logs --log-level DEBUG
uv run opcua-tutorial-server --help
```

Every chapter is written so that changing the **server** changes the **output**, not the
code. `--max-nodes-per-read` and `--no-limits` are the two flags to try first, with Chapter 7.

## 5. Use the CLI against it

In a second terminal:

```powershell
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48401 --path "ProductionLine/Line1"
uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Vibration"
uv run opcua-toolkit read --endpoint opc.tcp://127.0.0.1:48401 --batch --compare
uv run opcua-toolkit write --endpoint opc.tcp://127.0.0.1:48401 --set "ns=2;s=RecipeName=hello"
uv run opcua-toolkit call --endpoint opc.tcp://127.0.0.1:48401 --root "ns=2;s=Line1" --list-methods
uv run opcua-toolkit subscribe --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature" --duration 10
uv run opcua-toolkit discover --json --endpoint opc.tcp://127.0.0.1:48401
```

`--json` on stdout and one line of error on stderr is the CLI's contract: it is meant to be
used from a shell, a CI step or a health check. `--user` / `--password` / `--security` /
`--mode` / `--certificate` are there for Part 7.

One shell detail worth knowing: **quote NodeIds and browse paths**. `ns=2;s=Temperature`
contains a `;`, which PowerShell treats as a statement separator - unquoted, it is two
commands there before the toolkit sees any of it. In `cmd` the same argument survives
unquoted, which is why this mistake appears on one machine and not the other. Quoting works in
both shells, so quote:

```powershell
--node "ns=2;s=Temperature"        # PowerShell and cmd both give the toolkit one argument
```

The opposite mistake is worth knowing too, because it is what nesting one shell inside another
produces (`cmd /c "..."` from PowerShell, or a script that builds its own command line): the
quote characters arrive **as part of the argument**, and the toolkit reports that text instead
of guessing what was meant -

```text
error: call failed: NodeId '"i=2267"' must start with 'ns=0;' or be a numeric or string NodeId
error: browse failed: No node named '"ProductionLine' under ns=0;i=85 (segment 1 of 2)
```

Those two lines are the toolkit being honest about the text it actually received. If one of
them appears, a shell mangled the argument: fix the quoting, do not go looking for a server
problem.

## 6. In a container

```powershell
docker compose up --build                       # opc.tcp://localhost:48401
docker compose --profile tools up               # plus a discovery run and a collector
docker compose --profile history up             # server with historization on a volume
docker compose run --rm demo-server opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
```

The container binds `0.0.0.0` so the published port works, and its healthcheck is a real
`GetEndpoints` request rather than a port probe. Why an announced `EndpointUrl` is not
something to connect to is Chapter 31's subject; the compose file's comment says the same in
two lines.

## 7. Prove the whole thing is green

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests examples
uv run pytest tests/unit -q
uv run pytest tests/integration -q --tb=short
```

Unit tests take seconds and start no server. Integration tests start their own asyncua
servers on OS-chosen ports, so they need no network and no vendor product - and they are
the tests that prove the tutorial's OPC UA claims rather than its Python style.

## 8. In VS Code

`.vscode/` is committed and already correct for this repository:

- interpreter: `.venv/Scripts/python.exe` (`.venv/bin/python` on Unix, adjust if you change it);
- strict Pylance type checking, `src` on the analysis path;
- Ruff as the formatter with fix-on-save;
- pytest integration (`tests`);
- three debug configurations: the demo server, the chapter example you have open, and the
  CLI.

No extension other than the Python extension and Ruff is needed. The recommended list is in
`.vscode/extensions.json`.

## 9. When it does not work

| Symptom | Usual cause | What to do |
| --- | --- | --- |
| `ConnectionRefusedError` on the demo port | the example was run without `--demo`, or the server is not running | start `opcua-tutorial-server`, or add `--demo` |
| `Address already in use` | something else owns 48401 | `--port 0` picks a free port, or stop the other server |
| A client on another machine cannot connect | the server binds `127.0.0.1`, or a firewall blocks the port | `--host 0.0.0.0`, open the port, then read Chapter 31 about announced addresses |
| Lots of asyncua warning lines | the demo server has no certificate material, and a broken publish loop logs loudly | expected; `--log-level ERROR` quiets it. Chapter 12 explains what each line means |
| `ModuleNotFoundError: _support` | the example was run from the wrong directory | run it as `uv run python examples/chNN_*.py` from the repository root |
| `mypy` cannot import asyncua types | asyncua is deliberately treated as an untyped boundary | see the rationale in `pyproject.toml` and Chapter 28 |
| An integration test hangs | a test that waits on a real timeout | run one file with `-v` and `-x`; see [DEVELOPMENT.md](DEVELOPMENT.md) |
| A chapter says something UaExpert contradicts | the chapter is wrong | that is a bug worth reporting - see [CONTRIBUTING.md](CONTRIBUTING.md) |

## 10. Where to go next

1. Read [chapters 1-3](chapters/README.md) in order - they are short and make the rest readable.
2. Run chapters 4-13 with `--demo`, then run chapters 5, 7 and 10 again with
   `--endpoint` against Prosys or a vendor server.
3. Skim [docs/ROADMAP.md](ROADMAP.md) to see what the parts you have not built yet are for.
4. If you came from the old version of this repository, read [MIGRATION.md](../MIGRATION.md).
