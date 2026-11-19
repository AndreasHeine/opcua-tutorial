# Chapter 12 - Supervision

*Part 3 · status: built and tested · example: `examples/ch12_supervision.py` ·
tests: `tests/unit/test_supervision.py`, `tests/unit/test_resilience_model.py`,
`tests/integration/test_resilience.py`*

## 1. Why this matters

asyncua already reconnects. `client.connect(auto_reconnect=True)` re-establishes the
secure channel, the session, and the subscriptions it still believes are open. What it
cannot do is tell your application anything: no log line to alert on, no health signal for
a dashboard, no pause for a historian while values are stale, and no decision about whether
the server came back in a state worth collecting from.

So the question a long-running OPC UA client has to answer is not "can it reconnect?" but
"what do I do about this, and how do I know?".

**Healthy**, **degraded**, **connected but unusable** and **disconnected** are four
different problems with four different correct reactions. Tearing down a working
subscription because a server reported a low `ServiceLevel` creates an outage of your own.

## 2. OPC UA theory

### What a client can actually know

There is no "is the server OK?" service. There are four separate observations, and they do
not agree with each other:

| Observation | Source | What it can wrongly suggest |
| --- | --- | --- |
| The transport is open | your socket | the server is fine (it is not: a session can be gone while the socket lives) |
| The session is activated | the client library's own state | the link works (asyncua keeps a stale `ACTIVATED` session flag while its transport is gone) |
| The server reports `ServerState` and `ServiceLevel` | `Read` on the `Server` node | nothing is wrong (a server in `Shutdown` still answers `Read`) |
| A subscription is reporting | notifications arriving | the subscription exists (silence is also what steady state looks like) |

The server side of this is Part 4's `ServerStatus` (`ServerState` with `Running`, `Failed`,
`NoConfiguration`, `Paused`, `Unsupported`, `Shutdown`, plus `SecondsTillShutdown`) and
`ServiceLevel` (0-255). A server under maintenance answers your reads correctly at
`ServiceLevel = 0`. Those values are true and useful, and they are not a health flag.

### A usable link is not a usable subscription

A server may delete a subscription on its own side - a restart of a service, an operator
action, a resource limit - while your session stays open. Then:

- no error reaches your monitored items;
- `subscription_status` stays `None`, error counters stay frozen;
- the only trace is **silence**.

The cheapest question that settles it is a `ModifySubscription` with the numbers already in
use. If the subscription is gone, the server answers `BadSubscriptionIdInvalid`. That is
the whole trick: **silence is a reason to ask, never a reason to rebuild.** A supervisor
that treats steady state as an outage recycles subscriptions forever, and every
recycle is a chance to lose data.

### What reconnecting does to a subscription

After a session is re-established, subscriptions have to be dealt with, and OPC UA offers
two different answers:

- `TransferSubscriptions` asks the server to move subscriptions it still has from the old
  session to the new one, so the `SubscriptionId`s, the monitored items and the server's
  sequence numbers survive. It requires the client to name the subscriptions it is claiming,
  and it only works if the server kept them - which is exactly the situation Chapter 12's
  section 6 proves cannot be assumed.
- Re-creating them: `CreateSubscription` plus `CreateMonitoredItems`, from what the client
  knows it wanted. New `SubscriptionId`s, new item ids, a known starting point.

This repository takes the second path deliberately: a client that recreates is asking for the
subscription it *intends*, and it can compare that with the answer it gets (`stats.recreations`,
the new ids in `describe_subscriptions()`). That is why a reconnect in this tutorial produces new
`SubscriptionId`s, and why a monitored item's identity is the pair `(SubscriptionId,
MonitoredItemId)` from Chapter 10 rather than the item id alone.

## 3. Implementation

Two cooperating pieces:

- **`SubscriptionManager`** *reports* damage: `probe_subscription()`, `confirm_subscription()`,
  `subscription_status`, `rebuild_reason()`, `seconds_since_report()`, `seconds_since_probe()`,
  `rebuild_in_progress`, `rebuild()`.
- **`ConnectionSupervisor`** *decides*: `probe()` classifies, `watch()` probes on a schedule,
  `add_observer()` notifies once per state change, `add_subscription()` /
  `remove_subscription()` manage the watch list, `repair_subscriptions(force=False)` rebuilds
  only what an answer named, `describe_subscriptions()` and `format_report()` make the whole
  state readable.

### `connected` is a conclusion, not a copy of a flag

`ConnectionStatus.connected` is computed, not read:

- a failed probe with an **`OSError`** (`ConnectionRefusedError`, `ConnectionError("Connection
  is closed")`, a timeout) means **nobody answered** → disconnected;
- a failed probe with an **OPC UA status code** (`BadSessionIdInvalid`, `BadUserAccessDenied`)
  means **a server answered and refused** → connected, and something is wrong with the session;
- asyncua's own `has_session` / session state flags stay stale during an outage, so they are
  read as one input among others and never as the verdict.

That classification is the difference between "the link is down, pause the historian" and
"the link is up, the session is broken, re-authenticate". They need different fixes, and a
client that conflates them spends the night doing the wrong one.

### Silent probe

`ConnectionSupervisor.silent_probe_limit` is the window of subscription silence that earns a
question. One probe per window, not one per poll - and the answer decides:

- `BadSubscriptionIdInvalid` → rebuild that subscription only;
- a revision that matches what is already in use → the subscription is fine, the tags are
  just quiet, and nothing is rebuilt.

### What the example actually prints

```text
1. a probe is a classification, not a ping
  subscriptions: subscription=78 items=2 silence=0.0s status=none damage=none

2. degraded is not broken
  connected but degraded: ServerState=Running ServiceLevel=120 (degraded) ...
  subscriptions rebuilt: 0 - a degraded server keeps its subscriptions, and it should

3. connected, running, and still not worth collecting from
  operational=False - ServiceLevel 0 means the server is in maintenance: reads work, but a
  collector that keeps storing them is recording stale data as if it were live

5. the link goes away and comes back
  observer -> disconnected (Connection is closed)
  observer -> connected and healthy
  the supervisor calls an outage what the failed probe actually was: a status code is a server
  that answered, an OSError is nobody answering, and asyncua's own 'I have a session' flag
  survives the server being gone

6. a link that is fine and a subscription that is gone
  the server deleted it from its own side and answered Good
  what the client can see on its own: reason=None    status=nothing reported errors=0
  asking found: the server no longer has this subscription
  the server's answer is now on record: BadSubscriptionIdInvalid
  the supervisor repaired only what the answer named: ['the server no longer has this subscription']
  the subscription the answer did not name kept its id and its items: 78 recreations 1 -> 1

7. a subscription that is quiet because nothing is changing
  it asked the server and rebuilt nothing: []
  still the same subscription id: True    recreations: 0
  it asked once per silence window, not once per poll
```

Section 6 is the chapter's core demonstration. The demo server deletes a subscription from
its own side (`DemoServer.drop_subscription`) and answers `Good` to the delete request. The
client sees **nothing** - no status, no error, no counter movement. The supervisor asks, the
server answers `BadSubscriptionIdInvalid`, and only then does one subscription get rebuilt.
Section 7 is the mirror image: `RecipeName` never changes on this server, so its subscription
goes quiet legitimately, gets asked about once per window, and is left exactly as it was.

### The log noise you are expected to see

Running this example produces asyncua's own noise during the outage, and understanding it is
part of the lesson:

```text
ERROR asyncua.client.ua_session.UaSession | Publish iteration crashed; retrying in 1s
WARNING asyncua.client.client | Reconnect attempt failed: ConnectionRefusedError(...)
ERROR asyncua.server.uaprocessor | sending service fault response: The session id is not valid. (BadSessionIdInvalid)
WARNING opcua_toolkit.subscriptions.manager | subscription 79 no longer exists on the server: BadSubscriptionIdInvalid
WARNING asyncua.client.ua_session.UaSession | delete_subscriptions failed for 79: StatusCode(value=2150105088)
```

- `Publish iteration crashed; retrying in 1s` is asyncua's publish loop failing while the
  transport is down. It is not an error in your code and it does not mean data loss.
- `delete_subscriptions failed for 79: StatusCode(value=2150105088)` is the teardown of a
  subscription the server already deleted. The number is `BadSubscriptionIdInvalid` written
  as a raw integer: asyncua's `StatusCode` repr shows `.value`, not the name, which is why
  the toolkit's own lines print `status.name` and never a bare code.
- The toolkit's line - `subscription 79 no longer exists on the server: BadSubscriptionIdInvalid`
  - is the same event, stated once, in a sentence an operator can read. Chapter 28 makes that
  a rule instead of an example.

## 4. Verification

```powershell
uv run python examples/ch12_supervision.py --demo --demo-port 48401
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401        # then stop and restart it
```

- **Break it from the other side.** Start the demo server on a fixed port, create a
  subscription in **UaExpert**, then stop and restart the server. Watch what UaExpert does to
  its own subscription (it re-creates it, and its item list briefly empties), and compare
  with the toolkit's observer states. Neither is "the correct behaviour"; both are decisions,
  and yours has to be deliberate.
- **Check `ServerState` from a second client**: in UaExpert read `Server → ServerStatus → State`
  and `Server → ServiceLevel` while the example is running. When the example sets the server
  to `Shutdown`, UaExpert sees the same thing the supervisor classified as *not usable*.
- **Prosys**: connect to the demo server, create a subscription, restart the server process,
  and watch how long Prosys takes to notice. That delay is the reason a supervisor exists:
  the client library notices late, and your application needs to know now.
- **Test proof**: `tests/unit/test_supervision.py` covers the decisions without a server -
  ask-then-rebuild, steady subscriptions left alone, one probe per window, no stacking of
  repairs, retired subscriptions removed from the watch list, and the failure classification
  (`OSError` vs status code) with stub clients. `tests/integration/test_resilience.py` proves
  the same against a live server, including the server-side delete.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Treating silence as an outage | subscriptions recycled forever, data lost on every recycle | probe first, act on the answer (`silent_probe_limit`) |
| Rebuilding on a degraded server | a maintenance window turns into a collection outage | degraded ≠ broken: keep the subscription, pause the *use* of the data |
| Copying a library flag into `connected` | "connected but not usable" during an outage, and the wrong repair chosen | classify the failure: `OSError` means nobody answered |
| Retrying forever with no backoff | a server restart turns into a request storm | `RetryPolicy` caps delay; a probe is scheduled, not spun |
| Alerting on every asyncua warning | an operator reading stack traces during a 3-second blip | alert on state *changes*, which is what observers get |
| Never removing a stopped subscription from the watch list | a retired subscription probed forever, and a report full of noise | `remove_subscription()` when you stop one |
| Blindly trusting auto-reconnect to restore items | sessions come back, monitored items quietly do not | supervised rebuild, then verify with `GetMonitoredItems` (Chapter 11) |
| Printing a `StatusCode` directly | `StatusCode(value=2150105088)` in a log nobody can read | print `.name` (and read `.value`, never `int(code)`) |
| One reconnect strategy for every failure | re-authentication attempted when the host is down, and vice versa | four states, four reactions, and the observer carries the distinction |
| No health signal a dashboard can consume | the first alarm is an operator noticing stale values | `ConnectionStatus.state_text()` / `as_dict()` to your health endpoint (Chapter 30) |

## 6. What's next

Part 4 starts at Chapter 13: instead of hardcoding the numbers that Chapters 4-12 depended
on, ask the server for them and build the client from the answer. Chapter 14 makes that
configuration typed and validated, and Chapter 15 gives the gateway somewhere to put values
while the supervision you built here says "not right now".
