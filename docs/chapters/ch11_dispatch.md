# Chapter 11 - Dispatch

*Part 3 · status: built and tested · example: `examples/ch11_dispatch.py` ·
tests: `tests/unit/test_subscription_manager.py`, `tests/integration/test_subscriptions.py`*

## 1. Why this matters

A subscription is only useful if every part of your application receives the **same
facts in the same order**. The moment two consumers read a protocol object directly,
three problems appear: the object is reused by the library, the order of arrival stops
being a fact, and one broken consumer stops the others.

Dispatch is the small layer that fixes all three: one ordered consumer per subscription,
a frozen record per notification, and a handler list per group of items.

## 2. OPC UA theory

The protocol side gives you a specific shape, and it is worth naming before adding a
layer on top of it:

- A publish response contains one or more `NotificationMessage`s, each with a
  `SequenceNumber`, an `AvailableSequenceNumber`, and `DataChangeNotification`s carrying
  `MonitoredItemId` + `ClientHandle` + `DataValue` triples.
- The notification arrives in the **server's** order per item, but the order *between*
  items in a batch is not a promise about the plant.
- `MonitoredItemId` + `ClientHandle` are both in the notification: the server's handle and
  your own. A consumer must map notifications to items through your handle.
- If a client cannot process a batch, `Republish` can ask for it again - which means a
  late notification can arrive **after** a newer one.
- `TransferSubscriptions` moves the server's existing subscriptions to a new session after a
  reconnect, keeping their ids and sequence numbers - which is one of the paths where a
  client can be handed a notification it has already processed.

Two protocol facts drive the design decisions in this chapter: batch objects are **reused
and mutable**, and **arrival order is not plant order**.

## 3. Implementation

The manager runs **one** consumer task over the subscription's event iterator and turns
each event into a frozen `DataChangeRecord`:

| Field | Why it is there |
| --- | --- |
| `node_id`, `value`, `status_code` | the tag, the value, and the server's judgement of it (`Good` value with `Uncertain` status is a real case) |
| `source_timestamp`, `server_timestamp` | plant time vs server time - the only correct ordering key |
| `client_handle` | your handle, echoed back: how a notification is mapped to your item |
| `key` | the group the item was created with |
| `sequence` | arrival order **inside this manager**, monotonic - a gap is a dropped record, not a coincidence |
| `replayed` | arrived through `Republish`, so its timestamp may predate something already learned |
| `variant_type` | the wire type, for logs |

Flattening is not cosmetic. asyncua hands out a mutable protocol object that is rebuilt per
publish; an application that keeps it - a historian, a buffer, a cache - stores a
notification the library has already reused for the next one.

### Handlers: ordered, counted, isolated

```python
manager.register_handler(audit)  # runs first
manager.register_handler(dashboard, key="quiet")  # only for items created with key="quiet"
manager.register_handler(historian)  # runs after the unkeyed handlers above
```

- Handlers run **in registration order, one at a time**. Predictable before it is fast;
  Chapter 15 is where a handler hands the value to its own queue instead of doing the work
  inline.
- `register_handler` **appends**. Registering the same callback twice runs it twice - there
  is no unregister and no silent replacement, because a replacement would drop another
  handler sharing the key.
- A handler that raises is logged and **counted** (`stats.errors`), and the handlers after
  it still run. One broken consumer is a bug in that consumer, not a reason to stop the
  subscription.
- A key routes: items created with `key="quiet"` go to the `quiet` handlers, and the default
  handlers do not also see them. A key with no handler of its own falls back to the default.
- A notification for an item you no longer monitor is counted as a **stray** and dropped,
  rather than dispatched into an application that has moved on.

### Waiting for a value

| API | Answers |
| --- | --- |
| `wait_for_value(node, ...)` | the next record after now - "anything newer" |
| `wait_for_notifications(count)` | until N notifications have been dispatched |
| `wait_for_exact(node, value, ...)` | the newest record for that node is the value you wrote |
| `wait_idle(max_wait)` | the queue has drained |
| `last_value(node)` / `latest_values()` | what the manager last learned |

### What the example actually prints

```text
2. the first publish fills every handler
  recorder saw 3 records: [1, 2, 3]
  second consumer saw 3 records
  broken handler failed 1 time(s), subscription errors=1

3. order is a promise, not a side effect
  recorder sequences: [1, 2, 3, 4, 5, 6, 7, 8]
  strictly increasing: True
  no gaps from the first record: True
  this handler saw 8 of 8 records: every item is unkeyed so far, so a gap here would mean a lost notification

4. waiting for the value I wrote, not for any value
  the example wrote 99.25 on ns=2;s=Temperature
  the first record newer than the one it waited for says 23.885735660081668
  the server's own simulator won this race: 'newer' is not 'mine'
  the value this example wrote: ns=2;s=RecipeName = 'dispatch-recipe'
  sequence 8 -> 8
  wait_for_value answers 'anything newer'; wait_for_exact turns that into 'the value I wrote', which is what an application usually means

5. keys group items so one callback can serve several sources
  key 'quiet' handler saw: [12, 13, 14]
  records with key 'quiet' that reached the default handler: 0

6. an absence is a TimeoutError, never a None
  wait_for_value raised TimeoutError: the value did not arrive, and the example knows that
```

Section 4 is the one that saves people months. Waiting for "a newer value" after a write
is a race you usually lose on a live server, because the server's own simulator or the PLC
wrote something else in the meantime. `wait_for_exact` is what an application actually
means when it says "wait until my value arrived" - and knowing which of the two you wanted
is the difference between a test that passes and a test that sometimes passes.

`sequence 8 -> 8` in that output is the detail to notice: the example had already seen the
record carrying its own value, so `wait_for_exact` answered from what the manager had
established rather than waiting for a notification that was not coming. Two rules follow
from how the wait is implemented, and both are worth knowing before you lean on it:

- it compares against the **newest record the manager holds for that node**, one per node,
  not against a history. A value that arrived and was overwritten before you asked is not
  something `wait_for_exact` can find. It answers "is my value what this node reports now",
  which is the question a write-and-confirm asks; it is not a historian, and Chapter 20 is
  where the historian question gets answered properly;
- it waits through other people's changes by repeatedly asking for the next record
  (`newer_than=` the last one it looked at), so a timeout means "this node never reported my
  value while I was waiting", and the error names the newest value it did see.

## 4. Verification

```powershell
uv run python examples/ch11_dispatch.py --demo --demo-port 48401
uv run opcua-toolkit subscribe --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature" --node "ns=2;s=Pressure" --duration 20
uv run opcua-toolkit write --endpoint opc.tcp://127.0.0.1:48401 --set "ns=2;s=RecipeName=dispatch-from-uaexpert"
```

- **UaExpert**: create a subscription with the same items and watch the *SequenceNumber*
  column in the items view. It is the **server's** sequence per subscription, and it can
  skip numbers when a batch is dropped - which is what `Republish` is for. The manager's
  `sequence` is a different, local number: comparing the two is a good way to understand
  the difference between server order and arrival order.
- **Write from UaExpert, read in the toolkit**: the write above must appear in the
  subscribe output. If it does not, the item you are watching is not the item the server
  is monitoring - go back to Chapter 10's ids.
- **Look at a failure on purpose**: the example installs a handler that raises on
  `RecipeName`. In UaExpert, nothing about the subscription changes; in the toolkit's stats
  line, `errors=2` and the other handlers kept running. Two views of the same fact.
- **Test proof**: `tests/unit/test_subscription_manager.py` covers ordered dispatch,
  handler failure isolation, stray counting, key routing, and a closed queue;
  `tests/integration/test_subscriptions.py` proves it against a live server.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Storing the notification object asyncua handed you | a historian full of the newest value repeated | use the frozen `DataChangeRecord` |
| Registering the same handler twice | duplicated writes, doubled counters | `register_handler` appends; build one handler per job |
| Doing slow work inline in a handler | publish requests exhausted, notifications delayed, subscription looks dead | hand the record to a queue (Chapter 15) |
| Letting one handler's exception kill the loop | the whole collection stops because of a dashboard bug | handlers are isolated and counted (that is what `errors` is for) |
| Assuming arrival order is plant order | a value pair looks inverted in a historian | order by `source_timestamp`; `sequence` is arrival order only |
| Waiting for "a newer value" after a write | flaky verification, occasional wrong value | `wait_for_exact`, or match on the value you wrote |
| Treating a timeout as `None` | a `None` value propagating into a historian | `wait_for_value` raises `TimeoutError`: an absence is a fact |
| Ignoring `replayed` | storing a stale value after a reconnect as if it were new | check `replayed` before treating a record as current |
| Not noticing strays | items removed, notifications still arriving from a race | `stats` counts them; a nonzero count is a hint, not an error |

## 6. What's next

Chapter 12 is the chapter that decides *what to do* when a subscription stops reporting.
Dispatch gives the manager the visibility (records, errors, silence) that supervision needs
in order to make a decision instead of a guess.
