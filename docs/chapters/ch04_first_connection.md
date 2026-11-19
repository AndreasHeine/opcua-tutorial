# Chapter 4 - The first connection

*Part 2 · status: built and tested · example: `examples/ch04_first_connection.py` ·
tests: `tests/unit/test_settings.py`, `tests/unit/test_health.py`, `tests/integration/test_server_health.py`*

## 1. Why this matters

The first connection decides the shape of everything after it. A client that connects
and immediately reads a value has already thrown away four facts the server offered:
who it is, which endpoint was actually reached, whether it is in a state where reading
makes sense, and how much it will answer per request.

Those four facts are what the rest of the tutorial is built on - limits make Chapter 7
possible, health makes Chapter 12 possible, the endpoint answer makes Chapter 13
possible. A project that reads them once at startup and reuses them is a project that
does not rediscover them under load at 3 a.m.

## 2. OPC UA theory

A client does not "connect to a server". It performs four separate things:

1. **`GetEndpoints`** (no session, no authentication): the server answers with one
   `EndpointDescription` per endpoint - `EndpointUri`, `TransportProfileUri`,
   `SecurityMode`, `SecurityPolicyUri` and the `UserIdentityTokens` it accepts. The URL
   you typed is how you found the server; the endpoint you received is what you may use.
2. **OpenSecureChannel**: a channel with a security policy and mode applied, and a
   negotiated lifetime.
3. **CreateSession**: the server *revises* what you asked for - most visibly the
   session timeout. A client that asks for an hour and gets ten minutes has to know
   that, because a session that times out silently looks like a server problem.
   asyncua prints exactly this when it happens:
   `Requested session timeout to be 3600000ms, got 600000ms instead`.
4. **ActivateSession**: identity is presented (anonymous, user name, certificate) and
   the session's locale and user permissions become real. Only now do `Read`, `Browse`
   and `Call` work.

Then the three things worth reading before doing anything else:

| What | Where it lives | Why it matters |
| --- | --- | --- |
| `ApplicationInfo` / `Server` node | `ApplicationUri`, `ProductUri`, `ApplicationName`, `ApplicationType`, `DiscoveryUrls` | identifies who you are actually talking to |
| `ServerStatus` (`ServerState`, `ServiceLevel`) | `Server` → `ServerStatus` and the `ServiceLevel` variable | `Running`, `Failed`, `NoConfiguration`, `Paused`, `Unsupported`, `Shutdown` and a 0-255 service level tell you whether readings mean anything |
| `OperationLimits` | `Server` → `ServerCapabilities` | per-request caps: how many NodeIds one `Read`, `Write`, `Browse`, `Call` or `CreateMonitoredItems` may carry |

`ServerState` and `ServiceLevel` are the part almost every tutorial skips and every
production system needs: a server in `Shutdown` with `SecondsTillShutdown` set, or a
server reporting `ServiceLevel = 50` while it is being maintained, is a server whose
values you should keep but should not act on. Chapter 12 is built on that difference.

## 3. Implementation

```python
from opcua_toolkit import ClientSettings, NamespaceExplorer, OperationLimits, ServerHealth

settings = ClientSettings(
    endpoint_url="opc.tcp://127.0.0.1:48401",
    session_timeout=60.0,
)
async with settings.create_client() as client:
    health = await ServerHealth.read(client)
    limits = await OperationLimits.read(client)
    for index, uri in await NamespaceExplorer(client).namespaces():
        print(index, uri)
```

`ClientSettings` is validated configuration (`pydantic`), and `create_client()` is the
only way this project opens a session - so an application cannot accidentally connect
with different timeouts, security or retry settings in different places.

`ServerHealth.read` reads `ServerState`, `ServiceLevel` and `SecondsTillShutdown` and
classifies them (`ServerHealth.band`, `is_operational`, `reason`); `OperationLimits.read`
reads the published caps and records which ones the server did **not** publish
(`unresolved()`, `status_note()`), because "the server published no limits" is a
different situation from "the limits are large".

### What the example actually prints

```text
1. the configuration the client was built from
  endpoint=opc.tcp://127.0.0.1:56422 app=urn:opcua-tutorial:client session='Chapter 4 - first connection' timeout=60.0s security=unsecured (policy None, mode None) auth=anonymous retry=forever, delay capped at 30.0s
  nothing below this line is configured anywhere else

2. what the server says about itself
  ServerState   : Running
  ServiceLevel  : 255 (healthy)
  namespace     : ns=0 http://opcfoundation.org/UA/
  namespace     : ns=1 urn:freeopcua:python:server
  namespace     : ns=2 urn:opcua-tutorial:demo

3. the limits this server will honour
  max_nodes_per_read: 10
  ...
  note          : server published all operation limits

5. what a wrong endpoint looks like
  endpoint        : opc.tcp://127.0.0.1:1
  error type      : ConnectionRefusedError
  error text      : [WinError 1225] Der Remotecomputer hat die Netzwerkverbindung abgelehnt
```

Read step 5 again: a transport failure is a **classifiable event**. Its type
(`ConnectionRefusedError`, `TimeoutError`, `ConnectionError`) is a fact; its text is
the operating system's localised message, and on a German Windows box it is German.
Any code that matches on the text of a connection error is broken on another machine.
Chapter 12 classifies by type.

## 4. Verification

```powershell
uv run python examples/ch04_first_connection.py --demo --demo-port 48401
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
uv run opcua-toolkit discover --limits-only --json --endpoint opc.tcp://127.0.0.1:48401
```

- **UaExpert**: connect to `opc.tcp://127.0.0.1:48401`. The *Endpoints* dialog shows the
  single unsecured endpoint and the identity tokens (`Anonymous`, `Certificate`,
  `UserName`) - the same list `discover` prints. Then open
  `Server → ServerStatus → State`, `Server → ServiceLevel` and
  `Server → ServerCapabilities → OperationLimits` and compare each value with the
  example's output.
- **Prosys UA Simulation Server**: run its server and do the same against
  `opc.tcp://127.0.0.1:4840`. Its limits are different from the demo server's, and
  `ApplicationType` is different too. The chapter's point is that you read them, not
  that you assume them.
- **Test proof**: `tests/integration/test_server_health.py` starts the demo server, sets
  `ServerState` to `Shutdown` and a degraded `ServiceLevel`, and asserts what
  `ServerHealth` concludes.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Assuming your session timeout was accepted | the session dies after the server's shorter timeout, mid-collection | read the answer; `ClientSettings.session_timeout` is a request |
| Treating the URL as the endpoint | two endpoints on one port, one unsecured and one not | `EndpointDiscovery` first, then choose (Chapter 13) |
| Ignoring `ServerState` | a server that is shutting down is polled as if healthy | `ServerHealth.ensure_operational()` and Chapter 12 |
| Treating `ServiceLevel` as a percentage of tags | a degraded server read as "some tags are broken" | it is a whole-server signal; per-tag truth comes from per-item StatusCodes |
| Hardcoding limits "to be safe" | batching that is smaller than the server allows, or larger than it accepts | read `OperationLimits`, override only deliberately (`LimitsOverrides`) |
| Catching connection errors by their message | works on your machine, fails on a localised one | classify by exception type |
| Letting the process exit without disconnecting | the server keeps a dead session until its timeout | `async with settings.create_client()` |
| Reading health in a loop per tag | a health request per value, and a slow client | read it once per probe, reuse it (Chapter 12) |

## 6. What's next

Chapter 5 uses the namespace array and the limits from this chapter to reach the tags by
name instead of hardcoding them. The shape established here - settings in, client out,
report what the server actually is, disconnect cleanly - is the shape of every chapter
that follows, and of the capstone.
