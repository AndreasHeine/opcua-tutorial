# Chapter 10 - Subscriptions

*Part 3 · status: built and tested · example: `examples/ch10_subscriptions.py` ·
tests: `tests/unit/test_subscription_model.py`, `tests/unit/test_subscription_manager.py`,
`tests/integration/test_subscriptions.py`*

## 1. Why this matters

Polling is a client asking the server the same question repeatedly. A subscription is the
opposite: state you **establish on the server**, which then reports changes to you. It is
the difference between a collector that scales and one that has to be restarted every
night.

It is also the part of OPC UA where a client most easily believes its own request. A
subscription is not configured, it is **negotiated** - and the server's answer arrives as
different numbers, not as an error. A client that never reads those numbers runs on
numbers it invented.

## 2. OPC UA theory

### The services

| Service | What it establishes |
| --- | --- |
| `CreateSubscription` | one subscription: publishing interval, lifetime count, max keepalive count, priority. The answer is a `SubscriptionId` **and revised values** |
| `CreateMonitoredItems` | monitored items on that subscription, bounded by `MaxMonitoredItemsPerCall`. The answer is a `MonitoredItemId` **and revised sampling interval and queue size** |
| `Publish` | the client's request for the next batch of notifications |
| `ModifySubscription` / `SetPublishingMode` / `SetMonitoringMode` | change what was established, or pause it |
| `ModifyMonitoredItems` / `DeleteMonitoredItems` / `DeleteSubscriptions` | change or release items |
| `Republish` | ask for a notification batch the client could not process |

### Negotiation, in four numbers

| Parameter | What it actually controls |
| --- | --- |
| `PublishingInterval` | how often the server **sends a batch** (it sends one when the interval elapses and it has something to say) |
| `SamplingInterval` | how often the server **looks** at the node |
| `LifetimeCount` | how many publishing intervals may pass with nothing to report before the subscription is considered dead by the server |
| `MaxKeepAliveCount` | how often an empty publish is sent so the client knows the subscription is alive |

`CreateSubscription` and `ModifySubscription` answer with `RevisedPublishingInterval`,
`RevisedLifetimeCount` and `RevisedMaxKeepAliveCount`. They do not say "no": a request for
a 10 000 lifetime answered with 1 000 is a *successful* request. A client that ignores the
revision believes its own number, and the difference shows up months later as a
subscription the server dropped.

`MinimumSupportedSampleRate` is the same kind of distinction in a different place: a
server **publishes** it, and enforcing it is a separate decision. Some servers revise every
sampling interval to it; some accept anything. Both are conforming behaviour.

### Reporting is client driven

The server queues notifications; nothing arrives until the client sends a `Publish`. That
is why a subscription can look dead while the server is happily queueing: the client ran
out of available publish requests, or its consumer task stopped taking results. It is also
why a large publishing interval does not make the server quieter - it makes the *client*
ask less often.

### Two ids, and one pair

| Id | Whose number | Where it appears |
| --- | --- | --- |
| `SubscriptionId` | the server's | the subscription handle |
| `MonitoredItemId` | the server's | the item handle, returned by `CreateMonitoredItems` |
| `ClientHandle` | the client's | echoed back in every notification |

`MonitoredItemId` is unique **inside its subscription**, not across a server: two
subscriptions can have items with the same id. A cache keyed on item id alone mixes two
subscriptions. The identity of an item is the pair `(SubscriptionId, MonitoredItemId)`,
and `ClientHandle` is what you use to map a notification back to your own bookkeeping.

## 3. Implementation

`SubscriptionManager` owns one subscription and everything negotiated on it:

| API | What it does |
| --- | --- |
| `subscribe(nodes, options=...)` | creates the subscription and the items, chunked to `MaxMonitoredItemsPerCall` |
| `revision` / `confirm_subscription()` | what the server answered - and a second question when the create path did not report it |
| `set_publishing_interval(ms)` | `ModifySubscription`, then checks the answer against the request |
| `pause()` / `resume()` / `set_monitoring_mode()` | `SetPublishingMode` / `SetMonitoringMode` |
| `unsubscribe(...)` / `stop()` | `DeleteMonitoredItems` / `DeleteSubscriptions` |
| `rebuild()` | recreate the subscription and its items after the server's answer made the old one wrong |
| `stats` | notifications, good/uncertain/bad, suppressed, strays, errors, recreations |
| `wait_for_value(...)` | the practical reason a subscription exists |

`SubscriptionRevision` is the chapter's central data type: `revised`, `revisions()` and
`publishing_interval_accepted()` say what the server actually agreed to, and the manager
acts on it - including rebuilding when a server refused to move.

### What the example actually prints

```text
2. one subscription, many monitored items
  subscription id 78, items 30
  CreateSubscription calls      : 1
  CreateMonitoredItems calls    : 3 (expected 3)
    ns=2;s=Temperature         MonitoredItemId=112 asked=10.0ms revised=not reported queue=not reported
  sampling interval asked for: 10ms. The answer is 'not reported', and that is the honest answer:
  asyncua's create path returns the MonitoredItemId and throws the rest of the response away

3. what the server actually agreed to
  ModifySubscription: asked publishing=500.0ms lifetime=10000 keepalive=90; revised publishing=500.0ms lifetime=1000 keepalive=90
    the server changed: lifetime count 10000 -> 1000
  a subscription is negotiated, not configured: CreateSubscription and ModifySubscription
  never answer 'no', they answer with revised numbers
  this server publishes MinimumSupportedSampleRate=50.0ms and does not enforce it
  a limit a server advertises and a limit a server applies are different promises

7. changing the interval is a negotiation, not an assignment
  ModifySubscription: asked publishing=1000.0ms ... [rebuilt]
  rebuilt in 1 CreateSubscription and 3 CreateMonitoredItems calls, every item restored
  the restored items carry the same MonitoredItemIds as the ones that were deleted:
  this server restarts its counter for every subscription
  so (SubscriptionId, MonitoredItemId) is what identifies a monitored item
  asyncua's own server ignores ModifySubscription requests and answers with the values
  from creation: it is not the only server that does
```

That `[rebuilt]` line is the chapter's hardest lesson in practice. A server that answers a
`ModifySubscription` with the values from creation is telling you, in its own way, that it
will not change a live subscription's parameters. The toolkit does not pretend the change
happened: it reports the refusal, recreates the subscription with the parameters it wants,
and restores every item. A vendor server that answers `BadSubscriptionIdInvalid` to a
`ModifySubscription` gets the same treatment for a different reason (Chapter 12).

And the sampling interval of 10 ms against a published `MinimumSupportedSampleRate` of
50 ms: the demo server accepted it and reported at 10 ms. On a server that enforces its
published rate the same request comes back revised. Both behaviours are legal, which is
exactly why the toolkit prints "asked" and "revised" in two columns instead of one.

## 4. Verification

```powershell
uv run python examples/ch10_subscriptions.py --demo --demo-port 48401
uv run opcua-toolkit subscribe --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature" --publishing-interval 250 --duration 15
uv run opcua-toolkit write --endpoint opc.tcp://127.0.0.1:48401 --set "ns=2;s=RecipeName=verification-recipe"
```

- **UaExpert**: create a subscription at 500 ms, add 30 items, and look at the *Items* tab -
  UaExpert shows the **revised** sampling rates and queue sizes it was given. Compare them
  with the toolkit's `asked` / `revised` columns. Then change UaExpert's publishing
  interval and watch whether the revision changes: on the demo server it will not, which
  is the same behaviour the toolkit detected and rebuilt around.
- **Write from UaExpert, read from the toolkit.** Write `RecipeName` in UaExpert and watch
  it arrive in `opcua-toolkit subscribe`. Two independent implementations, one fact: the
  notification is the server's, not the client's.
- **Compare the two views of the same subscription.** The toolkit prints what it established
  (`stats`, `items`, `describe_subscriptions()`); UaExpert shows what it was given. They have
  to agree. Do not plan on `GetMonitoredItems` as the third opinion: it is a real service, and
  this server does not implement it - `Call` on its method node answers `BadNothingToDo` even
  for a live subscription with items in it (Chapter 9 measures it). On a vendor server that
  does implement it, it is the best cross-check available.
- **Prosys**: point Prosys's client at `opc.tcp://127.0.0.1:48401`, create the same
  subscription and read the same revisions. Then run the toolkit against Prosys's own
  server, whose subscription limits and sample-rate enforcement are different.
- **Test proof**: `tests/integration/test_subscriptions.py` asserts the chunking
  (`MaxMonitoredItemsPerCall=10` for 30 items = 3 calls), the revised lifetime count, the
  first-publish snapshot, that a write produces a notification without a new monitored
  item, and the pause/resume round trip.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Believing the numbers you asked for | a subscription the server dropped after its own lifetime | read `SubscriptionRevision`, act on `revisions()` |
| Ignoring revised sampling rates | "the subscription is slow" when the server revised every item | compare asked vs revised |
| Assuming `MinimumSupportedSampleRate` is enforced | works on the demo server, reports nothing on a vendor server | treat published limits as claims, and measure (Chapter 12) |
| One `CreateMonitoredItems` for 500 tags | `BadTooManyNodesSpecified` | chunk to `MaxMonitoredItemsPerCall` |
| Keying a cache on `MonitoredItemId` | two subscriptions mixing items | key on `(SubscriptionId, MonitoredItemId)`, match notifications by `ClientHandle` |
| Treating a monitored item as a callback registration | items left on the server after the client exits | `DeleteMonitoredItems` / `DeleteSubscriptions` (and a server that expires them anyway) |
| Thinking a subscription pushes by itself | a client that stopped calling `Publish`, and a "silent server" | keep the consumer loop draining (Chapter 11) |
| One subscription per tag | 300 subscriptions, and a server that answers `BadTooManySubscriptions` | one subscription, many items; the demo server caps subscriptions at 20 on purpose |
| Subscribing to every variable in a namespace | monitoring method declarations and metadata (Chapter 6) | choose items from browsing (Chapter 5) |
| No monitored item on a tag you write | a write that never comes back as a notification | monitor what you write, and match it by value (Chapter 11) |
| Asking this server to list its monitored items | the `GetMonitoredItems` method node is there, and answers `BadNothingToDo` | compare two independent clients instead of trusting the server's bookkeeping (Chapter 9) |

## 6. What's next

Chapter 11 turns the manager's event iterator into ordered application behaviour: one
consumer task, many handlers, and a sequence number that proves nothing was dropped.
Chapter 12 then deals with the case every long-running client eventually meets - a
subscription that stops reporting, for reasons the client did not cause.
