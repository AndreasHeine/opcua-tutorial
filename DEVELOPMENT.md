# Development guide

How to work inside this repository: environment, commands, the reasoning behind the tooling
configuration, the test fixtures, the demo server, containers and CI.

## Environment

```powershell
uv sync              # create .venv from uv.lock and install the package (editable)
uv run python ...    # run anything inside that environment
uv run opcua-toolkit ...
uv run opcua-tutorial-server ...
```

- Python 3.13 (`requires-python = ">=3.13"`).
- Dependencies: `asyncua>=2.1,<3`, `pydantic>=2.10,<3`, `pyyaml>=6.0,<7`.
- Development group (`[dependency-groups] dev`): `mypy`, `pytest`, `pytest-asyncio`,
  `pytest-cov`, `ruff`, `types-pyyaml`.
- Optional extras for the planned parts: `uv sync --extra historian` (Chapter 20),
  `uv sync --extra rest` (Chapter 30).

Add a dependency with `uv add`; commit both `pyproject.toml` and `uv.lock`.

## Commands

```powershell
uv run ruff format .              # format
uv run ruff format --check .      # format check, what CI runs
uv run ruff check .               # lint
uv run ruff check --fix .         # lint, applying safe fixes
uv run mypy src tests examples    # types
uv run pytest tests/unit -q       # seconds, no server
uv run pytest tests/integration -q --tb=short   # starts its own servers, ~10 min
uv run pytest tests/integration/test_subscriptions.py -v -x   # one file, stop at first failure
uv run pytest -m integration -k supervision     # selection across files
uv run pytest --cov --cov-report=term-missing   # coverage of src/opcua_toolkit
```

The integration suite takes a little under ten minutes on a laptop (`183 passed, 2 skipped in
569.90s` on the run that finished Chapter 16); the unit suite takes about five seconds
(`426 passed in 4.81s`). The integration half is slow because it really starts asyncua servers per
test.

## Repository layout

```
pyproject.toml            package metadata, dependency groups, tool configuration
uv.lock                   locked dependency set that CI installs
src/opcua_toolkit/        the toolkit (one module per OPC UA concept)
  browse/                 explorer.py (NamespaceExplorer), model.py (NodeRef, BrowseTree, BrowseStats)
  subscriptions/          manager.py (SubscriptionManager), model.py (records, options, revisions)
  server/                 model.py (ObjectTypeSpec, VariableTypeSpec, ComponentSpec, InstanceSpec, TypeReport, InstanceReport, TypeCoverage), reader.py (AddressSpaceReader), authoring.py (AddressSpaceAuthor)
  buffer.py / spool.py    NotificationBuffer and BufferStats, JsonlSpool and SpoolStats
  config.py               ApplicationConfig, layered sources with provenance, ConfigReview
  discovery.py            ServerProfile, endpoints, identity tokens, namespaces
  settings.py             ClientSettings, SecuritySettings, RetryPolicy, SubscriptionDefaults, LimitsOverrides
  resilience.py           ConnectionSupervisor, ConnectionStatus
  read.py / write.py / methods.py / inspector.py / limits.py / health.py / values.py / nodeids.py / errors.py / diagnostics.py
  devserver.py            DemoServer, DemoServerConfig, the opcua-tutorial-server CLI
  cli.py                  the opcua-toolkit CLI
examples/                 one script per chapter, _support.py (the shared harness), README.md
tests/conftest.py         fixtures, service-call counting, free_port()
tests/unit/               decisions, no server
tests/integration/        claims, live asyncua server
docs/chapters/            chapter pages, one per chapter
docs/ROADMAP.md           the plan and what exists
docker/Dockerfile         runtime image, non-root, real OPC UA healthcheck
docker-compose.yml        demo server, one-shot discover + collector, historized variant
.github/workflows/ci.yml  the gates, on Linux and Windows, plus the container job
```

## Why the tool configuration looks like this

### ruff

`line-length = 100`, `target-version = "py313"`, Google docstring convention, and a broad rule
selection (including `ASYNC`, `B`, `PERF`, `TRY`, `SIM`, `SLF`, `T20`, `D`, `N`, `PT`, `PTH`).

Deliberate ignores, each with a reason in `pyproject.toml`:

| Ignored | Why |
| --- | --- |
| `EM101`, `EM102`, `TRY003` | toolkit errors carry full, actionable messages inline on purpose |
| `PLR0911`, `PLR0912` | status-code handling *is* a long branch list; hiding it makes the code worse |
| `PLR0913` | keyword-only APIs with several arguments are the intended style here |
| `D107` | class docstrings document the constructor arguments instead |
| `TC001-003` | runtime imports of toolkit types are intentional, and a tutorial should read like Python, not like a typing exercise |
| `COM812` | the formatter decides trailing commas |

Per-file ignores are also documented: examples may print (`T20`) and may be long scripts
(`PLR0915`), `cli.py` and `devserver.py` print by design, and tests may inspect private state
and use magic numbers in assertions.

### mypy

`strict = true`, `files = ["src", "tests", "examples"]`, `mypy_path = ["src", "examples"]`
(the chapter scripts import their sibling `examples/_support.py` by module name, exactly as
Python runs them), `explicit_package_bases`, `namespace_packages`, `warn_unreachable`.

Two overrides with reasons that matter:

- **`asyncua` / `asyncua.*` → `follow_imports = "skip"`.** asyncua ships `py.typed` but
  annotates the wire with its own types (`Int32`, `UInt32`, `Boolean`, `String`, `DateTime`)
  instead of `int`, `float`, `bool`, `str` and `datetime`. Measured against that, a plain
  Python value is always the wrong type, and every call site becomes an error a reader cannot
  act on. The checks exist to protect this repository's own API, so the wire boundary is
  treated as untyped - and `nodeids.py` documents the consequence (`NodeIdLike` includes
  `Any`, with `to_nodeid` enforcing it at runtime).
- **`tests.*`** relaxes annotation requirements for fixture parameters and test functions
  while still checking the bodies.

`warn_unreachable` is strict in a specific way worth knowing: narrowing an optional
attribute twice makes the second check unreachable, so tests read optional properties through
a helper (`reported_status(...)`, `current_revision(...)`) instead of asserting twice.

### pytest

`asyncio_mode = "auto"` (no decorators on async tests),
`asyncio_default_fixture_loop_scope = "function"`, `--strict-config --strict-markers`,
`testpaths = ["tests"]`, and one registered marker: `integration`.

### coverage

`branch = true`, source is `src/opcua_toolkit`, `show_missing = true`. Coverage is a
development tool for finding untested decisions, not a badge to chase.

## Test fixtures

`tests/conftest.py` provides:

| Fixture | What it is |
| --- | --- |
| `demo_config` | `DemoServerConfig(port=0, simulate=False)` |
| `demo_server` | one live `DemoServer` per test, on an OS-chosen port |
| `connect` | a factory that opens - and always closes - a client for any endpoint, for a test that needs a second or third session |
| `client` | a connected asyncua client for that server |
| `limits` | the `OperationLimits` that server actually published |
| `service_calls` | a per-service call counter, for proving batching happened |
| `demo_tag_texts` | the canonical NodeId texts of the demo batch tags, the same texts the CLI prints |
| `free_port()` | a helper (not a fixture) for the rare test that must name a port |

Chapter files add their own fixtures when a chapter needs a different server: the Chapter 16
integration file defines `authored_server` (a `DemoServer` started with `authored_model=True`),
`admin_client` (a session that really holds `UserRole.Admin`, which is what an authoring session
needs) and `model_client` (a plain session against that same authored server). Nothing in
`conftest.py` knows about the authored namespace, so the fifteen chapters before it keep running
against the plain server they measured.

The counter placement is a lesson in itself: asyncua's `Client` delegates to `uaclient`,
which delegates to a `UaSession`. Subscriptions and nodes hold the **session**, not the
`uaclient` facade - so wrapping `uaclient` records zero for exactly the calls a subscription
makes. The fixtures count on the session, which is what a Chapter 7 measurement has to be.

`COUNTED_SERVICES` uses asyncua's own method names (`update_subscription`, not
`ModifySubscription`): counting a service under its specification name records nothing.

## Demo server, as a development tool

`DemoServer` is a real asyncua server, and its oddities are features:

| Setting | Default | Why it is that way |
| --- | --- | --- |
| `port` | `48401` (`0` in tests) | tests need a port the OS picks; a human needs a fixed one |
| `publish_limits` | `True` | `--no-limits` shows how the toolkit behaves with a server that publishes nothing |
| `max_nodes_per_read/write/browse` | 10 | so chunking is visible instead of theoretical |
| `max_nodes_per_method_call` | 5 | so `Call` chunking shows up in Chapter 9 |
| `max_monitored_items_per_call` | 10 | 30 items become 3 `CreateMonitoredItems` calls |
| `min_supported_sample_rate` | 50 ms | published and **not enforced**: Chapter 10's central distinction |
| `subscription_lifetime_cap` | 1 000 | asyncua's own default (15 000) never revises anything, so a tutorial would have no negotiation to show |
| `subscription_keepalive_cap` | 100 | same |
| `monitored_item_queue_cap` | 100 | same, and the source of `RevisedQueueSize` |
| `max_subscriptions` | 20 | so Chapter 12 can meet `BadTooManySubscriptions` instead of reading about it |
| `advertised_host` | `None` | container case: what a server binds and what it announces are different settings |

Handy commands:

```powershell
uv run opcua-tutorial-server --port 48401 --no-simulate --log-level DEBUG
uv run opcua-tutorial-server --port 0 --json-logs          # JSON lines, for logging experiments
uv run python -m opcua_toolkit.devserver --help           # also works, with a harmless runpy warning
```

Server-side test hooks (`DemoServer`): `set_server_state`, `set_service_level`, `set_value`,
`read_value`, `drop_subscription`, `tag_ids`, `writable_tag_ids`, `readonly_tag_ids`,
`subscription_caps`. `drop_subscription` exists because a Chapter 12 client must be able to
meet a subscription the server deleted on its own - a failure no client-side code can cause.

## Containers

```powershell
docker build -f docker/Dockerfile -t opcua-tutorial/toolkit:2.0.0 .
docker compose up --build
docker compose --profile tools up          # discover + collector as one-shot services
docker compose --profile history up        # historized server on a named volume
docker compose run --rm demo-server opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
docker inspect --format="{{.State.Health.Status}}" (docker compose ps -q demo-server)
```

The image installs from `uv.lock` in two layers (dependencies first, then the project), runs as
a non-root user, creates `/data` so a mounted history volume is writable, and its healthcheck
is `opcua-toolkit discover` - a real `GetEndpoints` request, because a TCP connection that
succeeds proves nothing about a server that failed to build its address space.

## CI

`.github/workflows/ci.yml` runs on push to `main` and `rework/**`, on pull requests, on a
weekly schedule (a tutorial that depends on a moving library has to find out when the library
moved), and on `workflow_dispatch`.

| Job | Does |
| --- | --- |
| Lint and types | `ruff format --check`, `ruff check`, `mypy src tests examples` |
| Tests | unit then integration, on `ubuntu-latest` and `windows-latest` |
| Demo server image | builds the Dockerfile, starts compose, waits for the **OPC UA** healthcheck, then discovers the server from a second container |

`astral-sh/setup-uv` caches the environment; the concurrency group cancels superseded runs.
Dependabot/Renovate updates are configured in `renovate.json` - an asyncua update is a normal
pull request, and the integration suite is what tells you whether a chapter survived it.

## Debugging a chapter

- VS Code: `.vscode/launch.json` has *Tutorial server (demo namespace)*, *Current chapter
  example* and *Toolkit CLI*. `justMyCode: false`, so you can step into asyncua and see what
  the protocol actually did.
- Chapter examples accept `--log-level DEBUG` and `--demo-port`, which is the combination you
  want when a second client is watching the same server.
- `configure_logging(level, json_output=True)` (from `opcua_toolkit.diagnostics`) gives JSON
  log lines - useful when a subscription issue has to be correlated with something else.
- Windows: a hung pytest run is usually a test waiting on a real session timeout. Find the PID
  and kill the tree: `taskkill /PID <pid> /T /F`.

## Branches and versions

- `main` holds the old tutorial until the rewrite replaces it.
- `rework/v2` is an earlier attempt, kept only for history.
- `rework/v3` is the rewrite: the toolkit, chapters 1-15, the tests, the container setup and
  these docs.
- The distribution is `opcua-tutorial` version `2.0.0`; the toolkit package is
  `opcua_toolkit`. Chapters land as minor versions, and a chapter that changes public API
  updates the examples, the tests and [MIGRATION.md](MIGRATION.md) in the same commit.

## Known rough edges

- asyncua's own log lines are noisy during an outage (`Publish iteration crashed; retrying in
  1s`) and its `StatusCode` repr prints `StatusCode(value=2150105088)` instead of the name.
  Chapter 12 explains both, and the toolkit's own lines print `.name`.
- `python -m opcua_toolkit.devserver` prints a harmless `RuntimeWarning` from `runpy` because
  `opcua_toolkit/__init__.py` imports the module first. The console script does not.
- The demo server does not implement `FindServersOnNetwork`; the test suite records that as a
  skip rather than pretending it works.
- `tests/integration` is slow by design. Run the unit suite while iterating, and the
  integration suite before you believe a chapter works.
