# OPC UA Tutorial

Build OPC UA applications that work on the first run and still make sense two
years later. Every chapter is a working program, every program is a building
block the next chapter reuses, and the last chapter assembles them into an OPC UA
edge gateway.

The stack is current on purpose: **asyncua 2.x**, **Python 3.13**, **uv**,
**pytest**, **ruff**, **mypy**, **Docker**, VS Code. No notebooks, no pseudocode,
no code that "should" run.

## What is here

```
src/opcua_toolkit/   the toolkit: browse, read, write, methods, subscriptions,
                     supervision, discovery, limits, health, settings, config, buffer, spool,
                     server authoring, demo server, CLI
examples/            one runnable script per chapter (ch04 … ch16 today), plus gateway.yaml
tests/               unit tests plus integration tests against a live asyncua server
docs/                the chapters, the roadmap, and how to verify with UaExpert or Prosys
docker/ + compose    the demo server as a container
```

Run an example and it starts its own server, so this works on a laptop with no
OPC UA server and no vendor product installed:

```powershell
uv sync
uv run python examples/ch10_subscriptions.py --demo
```

Run the whole thing:

```powershell
uv run ruff format --check .    # format
uv run ruff check .             # lint
uv run mypy src tests examples  # types
uv run pytest tests/unit -q     # seconds, no server
uv run pytest tests/integration -q  # starts its own servers on OS-chosen ports
```

Start the demo server on a fixed address and point a real client at it:

```powershell
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
uv run opcua-toolkit subscribe --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature"
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48401
```

Start it in a container instead:

```powershell
docker compose up --build            # opc.tcp://localhost:48401
docker compose --profile tools up    # plus a discovery run and a collector
```

## Where to start reading

| If you want | Start at |
| --- | --- |
| the plan and what each chapter builds | [docs/ROADMAP.md](docs/ROADMAP.md) |
| to get something running in five minutes | [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) |
| to check the protocol with someone else's tool | [docs/VERIFICATION_TOOLING.md](docs/VERIFICATION_TOOLING.md) |
| the chapter list | [docs/chapters/](docs/chapters/) |
| the tooling, gates and test fixtures | [DEVELOPMENT.md](DEVELOPMENT.md) |
| to change or extend the tutorial | [CONTRIBUTING.md](CONTRIBUTING.md) |
| what changed from the old version of this repo | [MIGRATION.md](MIGRATION.md) |

## The toolkit

`opcua_toolkit` is not a wrapper that hides OPC UA. Each piece turns one thing a
server does into something an application can act on - and each one prints or
records the server's own answer, because "we asked" and "we got" are different
facts.

| Module | What it is |
| --- | --- |
| `discovery` | Endpoints, identity tokens, namespaces, security policy: what a server offers before a session exists |
| `limits` | The `OperationLimits` a server publishes, and the batches they make legal |
| `health` | `ServerState` and `ServiceLevel`: running, degraded, in maintenance |
| `browse` | Reference-driven navigation, browse filters, continuation points, `TranslateBrowsePathsToNodeIds` |
| `inspector` | What a node says about itself: data type, value rank, access rights |
| `read` / `write` | Chunked bulk reads and writes, one outcome per requested item |
| `methods` | Reading a method's argument signature and calling it by name |
| `subscriptions` | Subscriptions, monitored items, the server's revised numbers, dispatch to handlers |
| `resilience` | Supervision: what a failed probe means, when to rebuild a subscription, when to leave it alone |
| `settings` | Typed configuration that refuses the mistakes a config file makes |
| `config` | One application's configuration as types: layers, provenance, and a review that refuses contradictions and reports weaknesses |
| `buffer` / `spool` | The bounded points between a tag and a sink: a buffer that names every record it dropped, and a file that survives the restart |
| `server` | Authoring an address space over a client session (`ObjectTypeSpec` / `ComponentSpec` / `AddressSpaceAuthor`), and reading back what a server promises about its own types (`AddressSpaceReader`) |
| `devserver` | The demo server, with the limits and refusals a tutorial needs |
| `cli` | `opcua-toolkit discover / browse / inspect / read / write / call / subscribe` |

## The idea running through it

A server *publishes* limits, access levels and sample rates. A server *enforces*
other things, and sometimes not the same ones. A client that treats the published
numbers as the enforced ones writes an application that works against the tutorial's
demo server and fails against a real one. Chapters 7, 8, 10 and 12 are four
different versions of that one sentence.

Chapter 16 turns the same idea to the other side of the wire: a server also
*publishes a model* - the types it declares and the parts every instance of them
must carry - and a client that never reads that promise cannot tell a model from a
pile of variables. The demo server's own address space is exactly that pile until a
chapter asks it to author a model, and Chapter 16 is where that request gets written
down.

## Status

Chapters 1-16 are complete: the toolkit core, thirteen chapter examples, the unit
and integration suites, the container setup and CI. Parts 1-4 and chapter 16 of Part 5
are built, and the rest of Parts 5-8 (chapters 17-31) build on what exists now - the
rest of the server side, companion specifications, security, logging, testing,
architecture - and end with the edge gateway.
[docs/ROADMAP.md](docs/ROADMAP.md) lists what exists and what is next.

## Origin and licence

This repository began as [AndreasHeine/opcua-tutorial](https://github.com/AndreasHeine/opcua-tutorial),
a set of small asyncua examples. The `rework/v3` branch is a rewrite of that
material against today's asyncua: same subject, new structure, executable code and
tests. It is licensed under the MIT licence - see [LICENSE](LICENSE).

## Links

- OPC UA specifications: [reference.opcfoundation.org](https://reference.opcfoundation.org/)
- asyncua: [github.com/FreeOpcUa/opcua-asyncio](https://github.com/FreeOpcUa/opcua-asyncio)
- uv: [docs.astral.sh/uv](https://docs.astral.sh/uv/)
