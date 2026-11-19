# Migration guide

This repository was, until the `rework/v3` rewrite, a set of small standalone asyncua
scripts (`client/browse.py`, `client/subscribe.py`, `server/BaseServer.py`, a
`requirements.txt`). It is now an installable package, a per-chapter executable tutorial,
and a test suite.

This page says where each old file went, what changed in the code style, and what to do if
you have code written against the old layout.

## Old layout is gone

| Old | Now |
| --- | --- |
| `client/BaseClient.py`, `client/client1.py` | `examples/ch04_first_connection.py` + `opcua_toolkit.settings.ClientSettings` |
| `client/browse.py` | `examples/ch05_browsing.py` + `opcua_toolkit.browse.NamespaceExplorer` |
| `client/read.py` | `examples/ch06_node_metadata.py` and `examples/ch07_bulk_read.py` + `NodeInspector`, `BulkReader` |
| `client/write.py` | `examples/ch08_bulk_write.py` + `BulkWriter` |
| `client/call_method.py` | `examples/ch09_methods.py` + `MethodCatalog`, `MethodInvoker` |
| `client/subscribe.py` | `examples/ch10_subscriptions.py` and `examples/ch11_dispatch.py` + `SubscriptionManager` |
| `client/extension_objects.py` ("coming soon") | removed; structure and custom data types are covered in Part 6 (companion specifications, chapters 21-24) |
| `server/BaseServer.py` | `opcua_toolkit.devserver.DemoServer`, runnable as `opcua-tutorial-server`; server authoring is Part 5 (chapters 16-20) |
| `client/README.md`, `server/README.md` | `docs/chapters/`, `docs/ROADMAP.md` |
| `requirements.txt` | `pyproject.toml` + `uv.lock` |
| no tests, no lint, no types | `tests/unit`, `tests/integration`, `ruff`, `mypy`, `.github/workflows/ci.yml` |

The old scripts are still available in history:

```powershell
git show main:client/subscribe.py
git show main:server/BaseServer.py
```

## What changed in the code, not just the file names

### 1. No external server needed

The old scripts expected something already listening on `opc.tcp://127.0.0.1:48010`
(the Prosys simulation server's default), and `browse.py` worked around its address space
by hand:

```python
nodes = await client.get_node("ns=2;s=Demo.Dynamic.Scalar").get_children()
nodes.pop(8)  # remove the gif (long bytestring) it would dump the console ;)
```

Every chapter example can start its own server, and no chapter depends on a specific vendor
address space:

```powershell
uv run python examples/ch10_subscriptions.py --demo                    # its own server, free port
uv run python examples/ch10_subscriptions.py --demo --demo-port 48401  # fixed address for UaExpert
uv run python examples/ch10_subscriptions.py --endpoint opc.tcp://127.0.0.1:4840  # your server
```

Nodes are always found by browsing, never hardcoded - which is why the same script works
against a server the tutorial does not control.

### 2. Connections come from settings

Old:

```python
client = Client(url="opc.tcp://127.0.0.1:48010", timeout=4)
await client.connect()
...
await client.disconnect()
```

New:

```python
from opcua_toolkit import ClientSettings

settings = ClientSettings(endpoint_url="opc.tcp://127.0.0.1:48401", request_timeout=4.0)
async with settings.create_client() as client:
    ...
```

`ClientSettings` is validated (a URL that is not `opc.tcp://...` is refused before anything
connects), it carries security, retry and subscription defaults, and it is the only way this
project opens a session. The `async with` means a chapter cannot leak a session.

### 3. Bulk operations know about the server's limits

Old code used `client.read_values([...])` or `client.uaclient.read_attributes([...])` with a
list of whatever size. New code sizes the batches from the server's published
`OperationLimits` (Chapter 4) and returns one outcome per item:

```python
limits = await OperationLimits.read(client)
results, stats = await BulkReader(client, limits=limits).read_values(nodes)
# stats: nodes=47 ok=47 failed=0 requests=5 nodes/request=9.4
```

### 4. Subscriptions report the server's answer

Old code created a subscription and trusted the numbers it passed in. New code reads the
revisions and acts on them (Chapter 10), and supervision decides what a silence or a refusal
means (Chapter 12):

```python
from opcua_toolkit import SubscriptionManager

manager = SubscriptionManager(client, limits=limits)
manager.register_handler(handle)
await manager.start()
await manager.subscribe(nodes)
print(manager.revision.summary())  # what the server agreed to, not what we asked for
```

The old `SubscriptionHandler.datachange_notification` pattern still works - the manager's
handler is the same shape - but the record you receive is a frozen `DataChangeRecord`
instead of a protocol object that asyncua rebuilds on the next publish.

### 5. Private asyncua APIs are not used

The old subscribe example showed how to reach into `subscription._subscribe(...)` to set a
data-change filter. Nothing in this repository calls a private asyncua API. Where the high
level API discards information, the toolkit asks the service directly (Chapter 10's
`confirm_subscription()` is exactly that) instead of leaning on an underscore.

### 6. Prints are the lesson, not an afterthought

The old scripts printed separators. The chapter scripts print **findings** - request counts,
revised values, status codes - because a claim that is not printed cannot be checked. The
toolkit's own modules expose the same data as objects (`stats.summary()`,
`SubscriptionRevision.revisions()`, `format_report()`) for code to use.

## Moving your own code onto the toolkit

| You have | Replace with | Chapter |
| --- | --- | --- |
| `Client(url=...)` scattered around | one `ClientSettings` in one config object | 4, 14 |
| hardcoded `"ns=2;s=..."` strings | browse paths resolved at startup, or `ServerProfile.namespace_index_of` | 5, 13 |
| a loop of `node.read_data_value()` | `BulkReader.read_values` with `OperationLimits` | 7 |
| `node.write_value(...)` with a bare `try` | `BulkWriter` + `WriteReport`, then check `outcome.status` | 8 |
| a `SubscriptionHandler` class | `SubscriptionManager` + `register_handler` | 10, 11 |
| `asyncio.sleep(n)` polling loops | a supervised subscription | 10, 12 |
| `print(datachange)` | `DataChangeRecord` handed to your own queue | 11, 15 |
| a hand-rolled reconnect loop | `ConnectionSupervisor` with an observer | 12 |
| `logging.basicConfig()` in each script | `configure_logging(level, json_output=...)` once at the entry point | 3, 28 |
| `requirements.txt` | `pyproject.toml` + `uv.lock` | 3 |

## Things the old tutorial had that this one deliberately does not

- **No dependency on one vendor's demo server.** Verification with UaExpert and Prosys is
  documented (Chapter pages, [docs/VERIFICATION_TOOLING.md](docs/VERIFICATION_TOOLING.md)),
  but nothing *requires* them.
- **No incomplete topics left half-finished.** The old `extension_objects.py` placeholder is
  gone; custom structures and companion models are Part 6, and until they are built the
  roadmap says so rather than implying they exist.
- **No unbounded examples.** Every example has a duration, a node cap or an explicit end, so
  it terminates on a laptop and in CI.

## If you linked to the old files

Update links: `client/*.py` and `server/*.py` no longer exist on `rework/v3`. Point to the
chapter example instead (`examples/chNN_*.py`) and to `docs/chapters/`. The old content stays
reachable on `main` until the rewrite is merged.
