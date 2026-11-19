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
| Revised sampling rates and queue sizes | *Subscriptions → Items* columns | 10 |
| Sequence numbers per subscription | *Subscriptions → Items* | 11 |
| A rejected write | right-click `Vibration` → *Write Value* → the error dialog | 8 |
| Method call with named arguments and its outputs | right-click `Line1` → *Call Method...* | 9 |
| `GetMonitoredItems` on the `Server` node | right-click `Server` → *Call Method...* | 11, 25 |

Good exercises that need two clients at once:

- write `ns=2;s=RecipeName` in UaExpert and watch it arrive in
  `opcua-toolkit subscribe`;
- create a subscription in UaExpert while a chapter example holds one, then compare the
  revised numbers each client was given;
- start the demo server with `--max-nodes-per-read 25` and watch the toolkit's request count
  change with no code change.

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
```

Its contract - exit code 0/1, machine-readable output on stdout with `--json`, and exactly
one error line on stderr for a dead endpoint - is itself tested (`tests/integration/test_cli.py`).

## Other tools worth knowing

| Tool | Use here |
| --- | --- |
| `opcua-commander` (NodeOPCUA), `open62541` example clients | free open-source clients when UaExpert's licence is inconvenient |
| Wireshark with the OPC UA dissector | seeing the actual services: who sent `Publish`, which request carried a continuation point, what a revised lifetime looks like on the wire (Chapters 10, 31) |
| a device's own OPC UA browser or export tool | checking a companion model against a real device (Part 6, chapters 21-24) |
| `Test-NetConnection 127.0.0.1 -Port 48401` | the least useful check there is: a TCP port that answers proves nothing about OPC UA. The container's healthcheck does a `GetEndpoints` instead |

## Faults to inject on purpose

Chapters 8, 12 and 29 all need failures that a healthy demo server will not produce by
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

## Reporting a disagreement

If UaExpert or Prosys contradicts a chapter, that is the most useful bug report this
repository can get. Include: the tool and version, the chapter file, the exact command you
ran, what the toolkit printed, and what the other client showed. See
[CONTRIBUTING.md](../CONTRIBUTING.md).
