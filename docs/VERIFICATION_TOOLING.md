# Verification tooling

Every built chapter has a verification section. This page is the shared part: how to point
independent clients at the tutorial's server, what to compare, and what counts as proof.

## What "verified" means here

A chapter's claim is verified when all three hold:

1. **It runs.** `examples/chNN_*.py --demo` produces the output the chapter page shows.
2. **A test asserts it.** `tests/unit/` for decisions, `tests/integration/` for protocol
   behaviour against a live asyncua server.
3. **A second implementation agrees.** UaExpert or Prosys, pointed at the same server, shows
   the same limits, revisions, status codes and namespace indices.

Three kinds of claim need different evidence, and it is worth knowing which one you are
making:

| Claim type | Example | Checked by |
| --- | --- | --- |
| A fact about this repository | "the chunk size comes from the server" | unit test + example output |
| A fact about the protocol | "a subscription is negotiated, not configured" | integration test + UaExpert/Prosys |
| A fact about **one** server | "asyncua's server ignores `ModifySubscription`" | the demo server's own behaviour, stated as such in the chapter - never generalised |

## The server under test

Everything below assumes a demo server on a fixed port:

```powershell
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401
```

or a chapter that keeps its own server pinned so it can print findings while you browse:

```powershell
uv run python examples/ch10_subscriptions.py --demo --demo-port 48401
```

Address: `opc.tcp://127.0.0.1:48401`. Namespace `urn:opcua-tutorial:demo` (`ns=2`).
Anonymous, unsecured, `OperationLimits` of 10 (browse/read/write/monitored items) and 5
(method calls), `MinimumSupportedSampleRate = 50 ms`, private subscription caps of
lifetime 1 000 / keepalive 100 / queue 100 / max 20 subscriptions.

Those small numbers are **on purpose**: a tutorial needs a server that revises, refuses and
answers differently from what a client asked for, and asyncua's defaults are so generous that
nothing would ever be revised.

Chapter 16 onwards can ask the same server to author its own model as well:

```powershell
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401 --authored-model --no-simulate
```

That authors `MachineType` and `PumpType` under `BaseObjectType`, the `MachineSetPointType`
`VariableType` under `BaseDataVariableType`, and the two instances `Pump_01` and `Pump_02` under a
folder named `ns=2;s=AssetModel`. The flag is **off** by default everywhere, including in the tests,
because Chapter 4 to Chapter 15 quote measurements taken against the plain server above - and those
numbers stay true only if the default stays plain. Chapter 16's own verification commands are the
ones that start the server with it.

## UaExpert (Unified Automation)

Free with an OPC Foundation *UaExpert* licence; the standard tool for this kind of check.

| Check | Where in UaExpert | Chapter |
| --- | --- | --- |
| Endpoint list, policy, mode, identity tokens | the *Connect* / server settings dialog | 4, 13 |
| Namespace array | *Server → Configuration → Namespaces*, or the Address Space view | 2, 5, 13 |
| `ServerState`, `ServiceLevel`, `SecondsTillShutdown` | `Server → ServerStatus`, `Server → ServiceLevel` | 4, 12 |
| `OperationLimits` | `Server → ServerCapabilities → OperationLimits` | 4, 7 |
| Node attributes: `DataType`, `ValueRank`, `AccessLevel`, `UserAccessLevel`, `Historizing`, `MinimumSamplingInterval` | select a node → *Attributes* | 6 |
| Browse children/parents, reference types, NodeClass filter | right-click a node → *Browse Children* / *Browse Parents*, or the *Address Space* window | 2, 5 |
| A type's declarations as children, an instance's `TypeDefinition`, and `IsAbstract` on a type | right-click `PumpType` → *Browse Children* (only its own four), right-click `Pump_01` → *Attributes* → *TypeDefinition*, `MachineType` → *Attributes* → `IsAbstract` | 16 |
| Revised sampling rates and queue sizes | *Subscriptions → Items* columns | 10 |
| Sequence numbers per subscription | *Subscriptions → Items* | 11 |
| A rejected write | right-click `Vibration` → *Write Value* → the error dialog | 8 |
| Method call with named arguments and its outputs | right-click `Line1` → *Call Method...* | 9 |
| `GetMonitoredItems` on the `Server` node | right-click `Server` → *Call Method...* | 11, 25 |
| What a monitored item queue keeps: write values faster than the publishing interval and compare which ones arrive | *Subscriptions → Items*, with an item queue size of 1 next to one large enough for one publish | 10, 15 |

Good exercises that need two clients at once:

- write `ns=2;s=RecipeName` in UaExpert and watch it arrive in
  `opcua-toolkit subscribe`;
- create a subscription in UaExpert while a chapter example holds one, then compare the
  revised numbers each client was given;
- start the demo server with `--max-nodes-per-read 25` and watch the toolkit's request count
  change with no code change.
- start it with `--authored-model --no-simulate` instead and compare UaExpert's view of `Pump_01`
  with what this chapter's reader reports: seven parts on the instance, four of them declared by
  `PumpType` and three reached through `MachineType`, and the same `AccessLevel` on each.

## Prosys UA Simulation Server

The other independent implementation, and useful because its defaults differ from asyncua's
on purpose.

```text
opc.tcp://127.0.0.1:4840          (its default endpoint)
```

Point the tutorial at it:

```powershell
uv run python examples/ch05_browsing.py --endpoint opc.tcp://127.0.0.1:4840
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:4840
uv run opcua-toolkit read --endpoint opc.tcp://127.0.0.1:4840 --batch
```

What to look for, because it is *different* from the demo server:

- a different namespace index for its tags (its own application namespace is `ns=1`, its
  simulated data somewhere else) - the Chapter 13 lesson made visible;
- different `OperationLimits`, so different request counts for the same node list;
- different security endpoints (it offers secured ones), so `discover` reports a profile
  with `supports_security: True`;
- different subscription behaviour: some Prosys versions enforce their published
  `MinimumSupportedSampleRate`, which is the case the demo server deliberately does not
  demonstrate.
- Chapter 16's reading half, run against a model you did not author: build a type with one
  mandatory and one optional component in Prosys, create one instance of it with the optional part
  and one without, and let `describe_instance` compare the two. Whether the reports agree with
  UaExpert is the check; the chapter's authoring code is deliberately not run against a server this
  repository does not own.

The point is not "Prosys is right". The point is that a client written to read these facts
works against both, and a client written with them hardcoded works against one.

## The toolkit's own CLI

The CLI is a third opinion written in this repository, and it is also what CI and container
healthchecks use:

```powershell
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401          # profile as text
uv run opcua-toolkit discover --json --endpoint opc.tcp://127.0.0.1:48401   # the same as data
uv run opcua-toolkit read --endpoint ... --compare                          # bulk vs one-by-one, measured
uv run opcua-toolkit inspect --endpoint ... --node "ns=2;s=Temperature" --value
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48401
```

### Checking a configuration file before it runs

`config` is the one subcommand that reads a file instead of flags, and the one that reviews a file
before a session exists (Chapter 14). It also refuses a file that is not runnable on its own, so the
first thing it tells you about `examples/gateway.yaml` is that the file writes no address:

```powershell
# no --check-server, so nothing supplies the endpoint this file deliberately leaves out
uv run opcua-toolkit config --file examples/gateway.yaml
# error: config failed: configuration is invalid: client.endpoint_url: required, and nothing supplies it

# the same file with a server to ask: exit 0, and the review is the file's own plus this server's
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48401

# refuse to accept a setting nobody decided (exit 1, one error line naming the setting)
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48401 --require-decided client.password

# for CI: JSON on stdout, one `error:` line on stderr and a non-zero exit code on a refusal
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48401 --json
```

The first three of those were run against a demo server, and the quoted error line is its output.

Two things worth verifying by hand here:

- **A contradiction inside a file needs no server to find.** Copy `examples/gateway.yaml`, write an
  `endpoint_url` into it (so the missing address does not hide the finding), and add
  `max_keepalive_count: 1000` under `client.subscriptions`. Reviewed with no `--check-server` at all,
  the command prints `refusals: 1` - *the server is asked to speak after 500 s of silence, but the
  session carrying this subscription times out after 60 s* - and with `--check-server` it prints the
  same refusal word for word alongside two more findings that only a server can produce. Same
  refusal, same exit code, and the file's own arithmetic is what produced it.
- **Some refusals need a server, and that is worth seeing.** In the same copy, change
  `limits.max_nodes_per_read` from `5` to `999` and delete the keepalive line. Reviewed with no
  `--check-server`, the file is clean: `refusals: 0  reviews: 2`. Reviewed against the demo server,
  which publishes `max_nodes_per_read = 10`, the same file gives `refusals: 1` and exits `1` with
  one `error:` sentence on stderr and no traceback:

  ```text
  refusal: client.limits.max_nodes_per_read: the configuration allows 999 items in one request while
  this server publishes 10. An override exists to be more careful than the server, not to imagine
  that it is more generous
  ```

  A validator that never met the server could not produce that sentence, which is why
  `--check-server` is part of a deployment's CI step and not an optional extra.

- **A refusal is an exit code, not a warning.** With `--json`, a refused configuration prints
  **nothing** on stdout: the one `error:` sentence goes to stderr and the exit code is `1`. `--json`
  is for the case where the file passes, so a CI step reads the payload from stdout and the reason
  from stderr - and never has to parse a traceback. Then create the same over-generous subscription
  in UaExpert and read the revised numbers it reports: a client dialog takes whatever you type into
  it, this command checks what it was given, and the two answers are not the same thing.

Its contract - exit code 0 or 1, machine-readable output on stdout with `--json`, and exactly one
`error:` line on stderr for a failure - is itself tested (`tests/integration/test_cli.py` and the
`config` test in `tests/integration/test_config_review.py`).

## Other tools worth knowing

| Tool | Use here |
| --- | --- |
| `opcua-commander` (NodeOPCUA), `open62541` example clients | free open-source clients when UaExpert's licence is inconvenient |
| Wireshark with the OPC UA dissector | seeing the actual services: who sent `Publish`, which request carried a continuation point, what a revised lifetime looks like on the wire (Chapters 10, 31) |
| a device's own OPC UA browser or export tool | checking a companion model against a real device (Part 6, chapters 21-24) |
| `Test-NetConnection 127.0.0.1 -Port 48401` | the least useful check there is: a TCP port that answers proves nothing about OPC UA. The container's healthcheck does a `GetEndpoints` instead |

## Faults to inject on purpose

Chapters 8, 12, 15 and 29 all need failures that a healthy demo server will not produce by
itself. These are the ones the tests and examples use:

| Fault | How | Chapter |
| --- | --- | --- |
| a server that is in maintenance | `DemoServer.set_server_state(ua.ServerState.Shutdown)`, `set_service_level(0)` | 4, 12 |
| a server that deleted your subscription | `DemoServer.drop_subscription(id)` | 12 |
| a link that goes away and comes back | `ChapterRun.restart_demo_server()` | 12 |
| a server that publishes no limits | `opcua-tutorial-server --no-limits` | 7 |
| a server with smaller limits than you assumed | `--max-nodes-per-read 25`, `--max-monitored-items-per-call 3` | 7, 10 |
| a frozen process (no spontaneous values) | `--no-simulate` | 10, 12 |
| a node that does not exist | `ns=2;s=ThisTagDoesNotExist` in a read/write/inspect batch | 6, 7, 8 |
| a value of the wrong type | writing a Python `float` to the `Float` tag `Setpoint` | 8 |
| a handler that raises | the Chapter 11 example installs one deliberately | 11 |
| a handler with a cost (a historian that spends time on every record) | `SlowCollector` in `examples/ch15_buffering.py`, `SlowRecorder` in `tests/integration/test_buffering.py` | 11, 15 |
| a notification queue too small for its consumer | `SubscriptionManager(client, queue_maxsize=2, …)` in `examples/ch15_buffering.py` | 15 |
| a downstream sink that is down while records arrive | the `Historian` in `examples/ch15_buffering.py` section 8, and the one in `tests/integration/test_buffering.py` | 15 |
| a session with no role trying to author a type | the anonymous run of section 9 of `examples/ch16_authored_address_space.py`, and `test_an_anonymous_session_cannot_author_a_type` | 16 |
| an address space that somebody edited by hand | the Chapter 16 integration test deletes `Pump_01.Temperature`, and the next read reports it as drift | 16 |

## Reporting a disagreement

If UaExpert or Prosys contradicts a chapter, that is the most useful bug report this
repository can get. Include: the tool and version, the chapter file, the exact command you
ran, what the toolkit printed, and what the other client showed. See
[CONTRIBUTING.md](../CONTRIBUTING.md).
