# Chapter 3 - How to read this repository

*Part 1 · status: written · no example file: this chapter is the tooling and the conventions*

## 1. Why this matters

A tutorial you cannot run is a blog post. Claims about OPC UA are claims about what a
server answers, and text cannot demonstrate that - a program against a real server can.

So this repository is a codebase first and documentation second. Each chapter is a
program that runs, a set of tests that prove its claims, and a page that explains why
the code looks like that. This chapter explains the rules, so that any chapter can be
read, run, doubted and verified in the same way.

## 2. OPC UA theory: how a claim is checked

There are three ways to check a statement about OPC UA, and a good project uses all
three.

### Read the specification

OPC 10000-x is free at [reference.opcfoundation.org](https://reference.opcfoundation.org/).
The parts this tutorial leans on:

| Part | Title | Used here for |
| --- | --- | --- |
| 1 | Overview and Concepts | vocabulary, profiles, the client/server model |
| 2 | Security | policies, modes, certificates, sessions (Chapters 25-27) |
| 3 | Address Spaces | NodeClasses, attributes, references, NodeIds (Chapter 2) |
| 4 | Services | every request and response, and the StatusCode tables |
| 5 | Companion Specifications for Information Models | modelling rules (Part 6 of this tutorial) |
| 6 | Transport Mappings | binary over TCP, HTTPS, message chunking |
| 7 | Information Model | the base types every server must provide |
| 8 | Data Access | `AccessLevel`, `EURange`, `InstrumentRange`, data quality |
| 9 | Alarms and Conditions | events (Chapter 19) |
| 10 | Historical Access | history read/update (Chapter 20) |
| 12 | Discovery | `FindServers`, `FindServersOnNetwork`, LDS (Chapter 13) |

How to read one: a Service clause states the request, the response, the Service
criteria and the StatusCodes that can come back. **The StatusCodes and the Service
criteria are the part that settles arguments** - for example Part 4's
`ModifySubscription` clause is what Chapter 10 uses to show that subscription
parameters are negotiated, not assigned. Annex A's NodeId tables are the reference for
standard nodes: `i=2253` is the `Server` object, `i=2267` is its `ServiceLevel` variable,
`i=2004` is the `ServerType` they are instances of. Reading the number up rather than
remembering it is the habit: `--list-methods --root i=2253` on the demo server finds the 133
method declarations hanging off the `Server` object, which is the same fact stated as
something you can ask a server instead of something you have to know.

### Run it against a real server

The demo server in this repository is a real asyncua server. Where the specification
leaves room - does a server enforce `MinimumSupportedSampleRate`? does it revise
`RequestedLifetimeCount`? - the answer here is *measured*, and the measurement is in a
test you can run.

### Check it with a second client

UaExpert and Prosys are independent implementations. If the toolkit prints a revised
publishing interval and UaExpert shows the same number, the claim is about the
protocol and not about this code. [../VERIFICATION_TOOLING.md](../VERIFICATION_TOOLING.md)
lists the exact checks.

## 3. Implementation

### Layout

```
pyproject.toml            package, dependencies, ruff / mypy / pytest / coverage config
uv.lock                   the exact dependency set CI installs
src/opcua_toolkit/        the toolkit: one module per OPC UA concept
examples/                 one runnable script per chapter, plus _support.py
tests/unit/               decisions, tested without a server
tests/integration/        claims, tested against a live asyncua server
docs/chapters/            the chapter pages
docs/ROADMAP.md           the 31-chapter plan and status
docker/                   the demo server image, and docker-compose.yml
.github/workflows/ci.yml  the gates, run on every push and weekly
```

### The rules a chapter has to satisfy

| Rule | What it means in this repository |
| --- | --- |
| Runs on a clean machine | Every example can start its own demo server (`--demo`) |
| Runs against a real server too | `--endpoint opc.tcp://...` works, and demo-only steps are skipped, not faked |
| Typed | `mypy --strict` over `src`, `tests` and `examples` |
| Lint-clean | `ruff check` with a broad rule set, formatter-clean |
| Tested | a unit test for each decision, an integration test for each protocol claim |
| Reusable | each chapter leaves a class or module that a later chapter imports |
| Verifiable | each chapter page has a verification section for UaExpert / Prosys / the CLI |

### Naming, so a chapter, a file and a test agree

Chapter numbers are fixed by the toolkit's own docstrings, so `Chapter 12` means the
same thing in `src/opcua_toolkit/resilience.py`, `examples/ch12_supervision.py`,
`tests/unit/test_supervision.py` and `docs/chapters/ch12_supervision.md`.

| Chapter | Module | Example | Unit tests | Integration tests |
| --- | --- | --- | --- | --- |
| 4 | `settings`, `health`, `discovery` | `ch04_first_connection.py` | `test_settings.py`, `test_health.py` | `test_server_health.py` |
| 5 | `browse/` | `ch05_browsing.py` | `test_browse_model.py` | `test_browse.py` |
| 6 | `inspector` | `ch06_node_metadata.py` | `test_inspector_model.py` | `test_inspect.py` |
| 7 | `read`, `limits` | `ch07_bulk_read.py` | `test_limits.py` | `test_read.py` |
| 8 | `write`, `values` | `ch08_bulk_write.py` | `test_write_model.py`, `test_values.py` | `test_write.py` |
| 9 | `methods` | `ch09_methods.py` | `test_method_model.py` | `test_methods.py` |
| 10 | `subscriptions/` | `ch10_subscriptions.py` | `test_subscription_model.py`, `test_subscription_manager.py` | `test_subscriptions.py` |
| 11 | `subscriptions/manager.py` | `ch11_dispatch.py` | `test_subscription_manager.py` | `test_subscriptions.py` |
| 12 | `resilience` | `ch12_supervision.py` | `test_supervision.py`, `test_resilience_model.py` | `test_resilience.py` |
| 13 | `discovery` | `ch13_discovery.py` | `test_discovery_model.py` | `test_discovery.py`, `test_cli.py` |
| 14 | `config` | `ch14_configuration.py`, `gateway.yaml` | `test_config.py` | `test_config_review.py` |
| 15 | `buffer`, `spool` | `ch15_buffering.py` | `test_buffer_model.py`, `test_spool_model.py` | `test_buffering.py` |
| 16 | `server/` (`model`, `reader`, `authoring`) | `ch16_authored_address_space.py` | `test_address_space_model.py` | `test_address_space.py`, `test_browse.py`, `test_cli.py` |

### The example harness

`examples/_support.py` is shared by the chapter scripts and is deliberately *not*
part of the toolkit: it exists so that examples do not each invent their own server
management. It provides `chapter_run(...)` (the run, its settings, its client),
`ChapterRun.uses_demo_server` and `ChapterRun.demo_server` (so a script never hides
which steps need a server it controls), `restart_demo_server()` for the outage
chapters, and `instrument_service_calls()` for the chapters that count real requests.

### The gates

Run them before believing anything here, and before committing anything:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests examples
uv run pytest tests/unit -q
uv run pytest tests/integration -q --tb=short
```

Unit tests take seconds and start no server. Integration tests start their own asyncua
server on a port the OS picked, so they need no vendor product, no network and no
fixed port. The whole suite runs in CI on Linux and Windows, and a scheduled weekly run
catches asyncua moving underneath the tutorial. Details:
[../DEVELOPMENT.md](../DEVELOPMENT.md).

## 4. Verification

Three things to verify about the tutorial itself:

1. **The gates pass on your machine**, not only on the author's. Run all five commands
   above; a clean checkout should be green in a few minutes.
2. **An example runs against a server you do not control.** Point any chapter at a
   vendor server or at the Prosys simulation server and confirm it prints real answers
   instead of demo-shaped assumptions:
   `uv run python examples/ch05_browsing.py --endpoint opc.tcp://127.0.0.1:4840`.
3. **The toolkit's statements match a second client.** For any chapter's claim, check
   the same fact in UaExpert or Prosys. If they disagree, the chapter is wrong and the
   issue is worth reporting.

## 5. Common mistakes

| Mistake | Why it hurts | Fix |
| --- | --- | --- |
| Copying an example into production and deleting the tests | The example's value was the proof, not the layout | Keep the tests with the code you keep |
| Running only the unit tests | The interesting OPC UA failures only appear against a live server | Run `tests/integration` too |
| Reading a chapter without running it | The surprising behaviour is in the output, not in the text | Run it, then read it |
| Treating the demo server as a typical vendor server | The demo server is deliberately unusual: small limits, small subscription caps, refusals on purpose | Chapter pages say which limits are published and which are private |
| "It works on my laptop" as verification | OPC UA failures are server-side and network-side | Verify with a second client and a second machine |
| Skipping `mypy` because asyncua is untyped | The toolkit's own API is what the checks protect | See the rationale in `pyproject.toml` and Chapter 28 |

## 6. What's next

Chapter 4 opens the first connection. It is also the first time you see the shape every
later chapter reuses: settings in, client out, report what the server actually is,
disconnect cleanly.
