# Chapter 15 - Queues, buffering, and the file that survives the outage

*Part 4 · status: built and tested · examples: `examples/ch15_buffering.py` · tests:
`tests/unit/test_buffer_model.py`, `tests/unit/test_spool_model.py`,
`tests/integration/test_buffering.py`*

## 1. Why this matters

Chapter 11 handed a record to a handler and Chapter 12 built a supervisor that can say "the
historian is not taking data". Neither chapter answered the obvious next question: **where did the
values go in the meantime?** "In the meantime" is most of a real outage.

And the default answer is worse than no answer. This is what one run of this chapter's example
printed while a subscription reported itself healthy:

```text
20 tags: each item owes one notification the moment the server creates it and one for the value
written here, so 40 were owed
records that reached a handler at all: 2
what the manager's own counters say: subscription=79 publishing=250 ms items=20 failed=0
notifications=4 good=4 uncertain=0 bad=0 suppressed=0 stray=0 errors=0 recreations=0
status=none mode=reporting
```

36 of the 40 owed notifications never arrived. The counters this application owns reported 4, said
nothing was suppressed, nothing was an error, and never mentioned the 36. A gateway in exactly that
shape alerts on nothing and files a report that reads like a quiet plant.

So the chapter is not "add a queue". It is three claims, each of them measured below:

- a queue whose bound nobody chose is not a bounded queue, it is an unmeasured one;
- a buffer is only bounded if a **policy** says what it gives up, and only useful if it can name
  the records it gave up;
- of the three places data waits between a tag and a historian, only a file can answer "what did
  you lose?" after the process restarts.

## 2. OPC UA theory

### Three queues, three owners

| | the monitored item queue | asyncua's notification queue | the application buffer |
| --- | --- | --- | --- |
| Who owns it | the server | the client library | your process |
| Granularity | one per monitored item | one per subscription | one per buffering point you choose |
| Sized by | `queueSize` requested, `revisedQueueSize` answered | `queue_maxsize` (asyncua's default 1 000) | `BufferSettings.capacity` |
| What it does when full | trims: this server removes the oldest entry | `OverflowPolicy` (`DROP_OLDEST` by default) | the policy you named |
| Can it report a loss | not to this client | only if you chose `WARN` or `DISCONNECT` | yes, per record, with the sequence number |
| Survives | the session only | nothing: it is memory in the client process | a restart, if it is a file |

The middle column is the one to be suspicious of. It is not part of OPC UA at all - it is one
client library's implementation detail, it exists whether or not you configured it, and it is the
queue that overflowed in this chapter's run. Knowing the first column is a question you answer from
the specification and from a server's answer; knowing the second is a question you answer by reading
the library's code, which this chapter does and quotes.

### `queueSize` is a request; `revisedQueueSize` is the queue you have

OPC UA Part 4, 7.21 `MonitoringParameters`, is exact about what a request means. For **data**
monitored items:

> 0 or 1: the Server returns the default queue size which shall be 1 as revisedQueueSize for data
> monitored items. The queue has a single entry, effectively disabling queuing. For values larger
> than one a first-in-first-out queue is to be used. The Server may limit the size in
> revisedQueueSize. In the case of a queue overflow, the Overflow bit (flag) in the InfoBits portion
> of the DataValue statusCode is set in the new value.

For **event** monitored items the same field means something else again - a second table in the same
row: 0 asks for the server's default, 1 asks for "the minimum queue size the Server requires",
`MaxUInt32` asks for "the maximum queue size that the Server can support", and "if the requested
queueSize is outside the minimum or maximum, the Server shall return the corresponding bounding
value" rather than refuse the item. Event overflow is also reported differently: an event queue that
overflows generates "an Event of the type `EventQueueOverflowEventType`" instead of setting a bit.

5.13.2.2 (`CreateMonitoredItems`) and 5.13.3.2 (`ModifyMonitoredItems`) both define
`revisedQueueSize` as "the actual queue size that the Server will use", so it is the only place a
client can hear the answer.

What this server answered is in section 2 of the run: asked `1`, `5`, `0` and answered `1`, `5`,
`100`. The `0` is where a specification claim and a real implementation part company. The
specification's default for a data item is **1**; this server answered its own cap. From the installed
library rather than from inference, `asyncua/server/monitored_item_service.py` (131-135):

```python
def _revise_queue_size(self, requested: int) -> int:
    cap = self.isub.max_queue_size
    if requested == 0:
        return cap
    return min(int(requested), cap)
```

So a request of 0 means "give me this server's largest queue", not "give me the spec's default of
one", and a request of 1 arrives as 1 only because `min(1, cap)` is 1. That cap appears in **no**
published limit - not in `OperationLimits`, not in any variable - which is why the chapter measured
it through the answer instead of looking it up. A deployment that writes `queue_size: 0` and never
reads `revisedQueueSize` has delegated the decision in whichever direction its server happens to go:
to a queue of one on a strict one, to a queue of a hundred on this one. The request is not the queue.

Part 4, 5.13.1.5 *Queue parameters*, is the clause that decides what a queue *is*:

> If a Notification is discarded for a DataValue and the size of the queue is larger than one, then
> the Overflow bit (flag) in the InfoBits portion of the DataValue statusCode is set. If
> discardOldest is TRUE, the oldest value gets deleted from the queue and the next value in the
> queue gets the flag set. If discardOldest is FALSE, the last value added to the queue gets
> replaced with the new value.

> If the queue size is one, the queue becomes a buffer that always contains the newest Notification.
> In this case, if the sampling interval of the MonitoredItem is faster than the publishing interval
> of the Subscription, the MonitoredItem will be over sampling and the Client will always receive the
> most up-to-date value. The discard policy is ignored if the queue size is one.

> In this case, the MonitoredItem would be created with a queue size large enough to hold all
> Notifications generated between two consecutive publishing cycles. […] The Server shall return
> Notifications for any particular item in the same order they are in the queue.

Three consequences this chapter leans on:

1. **A queue of one is a defined thing, not a small queue.** It is the spec's "always the newest
   value" buffer, and the discard policy does not apply to it. A cache of current values is not a
   queue you sized badly.
2. **The sizing question has an arithmetic answer:** the queue has to hold what one publishing
   interval produces. Section 9 of the run does that arithmetic with numbers measured from the run
   instead of typed in.
3. **Order is promised.** Notifications for one item come back in queue order - which is why
   `keep_latest`'s "the newest N in order" report, and the spool's "oldest first", are worth
   building.

### `discardOldest` is defined by the spec and unused by this pair

The spec offers two discard policies and names them with one boolean. What this pair of
implementations does with it, read from the installed library rather than assumed:

- asyncua's client hard-codes it: `mparams.DiscardOldest = True` appears in the two places
  `asyncua/common/subscription.py` builds monitored item parameters (lines 600 and 842), with no
  option to send anything else.
- asyncua's server does not read it. Its queue trim, in
  `asyncua/server/internal_subscription.py`, removes the oldest entry when the queue has reached
  its size and never consults `discardOldest`.

So on this client-server pair the field decides nothing, and the trimming behaviour happens to match
`discardOldest=TRUE`. That coincidence is a trap: **a server that implements
`discardOldest=FALSE` returns a different set of values, and nothing in this run would have told you
the difference.** Part 5 is where a server author gets to decide that flag at all.

### The Overflow bit: a spec feature this client cannot even read

5.13.1.5 says a trimmed notification gets the Overflow bit set in the `InfoBits` portion of the
`DataValue` status code, and 7.21 adds that an event queue that overflows queues an
`EventQueueOverflowEventType` instead. Two measured facts about this stack:

- asyncua's server never sets it: the trim code pops an entry and moves on, with nothing written to
  a status code.
- even if a conformant server set it, this client could not see it. asyncua's `StatusCode`
  (`asyncua/ua/uatypes.py`, 396-424) exposes `is_good`, `is_bad` and `is_uncertain`, all of which
  read the two severity bits at the top of the number. There is no accessor for `InfoBits`, and the
  toolkit's `DataChangeRecord` therefore carries no field that such a flag could land in.

That is the reason the application's own buffer exists. A loss this client can be told about is a
loss the client asked for and applied itself, with its own counters.

### When a value enters the queue (measured, and not what most clients expect)

The run offers each monitored item 8 written values, and each queue was offered **9**: the value the
item had at the moment the server created it, then the 8 writes. In asyncua's
`asyncua/server/monitored_item_service.py`, creating a data change monitored item registers the
write callback and immediately triggers one data change, so the first notification in a queue is not
a sample of a change - it is the current value.

Two more measured facts about this server, both from the same file:

- it is **write-triggered**: a data change is enqueued when the value is written and the filter or
  deadband passes, not on a sampling timer that re-reads the source;
- it answers every item's sampling interval as the subscription's publishing interval
  (`RevisedSamplingInterval = RevisedPublishingInterval`), so asking for `100 ms` sampling inside a
  `2 000 ms` subscription bought nothing.

The practical consequence: **the monitored item queue is about bursts between two publishes, not
about samples.** A queue is only exercised in a plant that changes faster than it publishes - which
is exactly what section 2 of the run builds, deliberately, with 24 writes inside one 2 000 ms
publishing interval.

### What the specification says about a queue that must outlive a session

Nothing in the monitored item queue survives the session: the server's queue is memory attached to
the subscription. Part 4 5.14.1.1 is explicit that the durable case is a separate mechanism - a
high `RequestedLifetimeCount` and large `QueueSize` are what make **durable subscriptions**
possible, and it points at subclause 6.8 for how they are created and used. Chapter 12's
`TransferSubscriptions` path is that mechanism; a bigger `queueSize` is not a substitute for it, and
the two are answers to different questions.

## 3. Implementation

### `src/opcua_toolkit/buffer.py` - the bounded point that reports

| Piece | What it is for |
| --- | --- |
| `DropPolicy` | the four named decisions: `keep_latest` (the spec's `discardOldest=TRUE`: drop the oldest stored record), `keep_window` (the spec's `discardOldest=FALSE`: replace the newest stored record, so the front of the window survives and the tail collapses), `reject_new` (refuse the incoming record and keep one unbroken run), `wait` (apply back-pressure instead of deciding) |
| `BufferSettings(capacity, policy, max_wait)` | the bound in records, the policy, and the only budget `wait` may spend (milliseconds). `max_wait` is **required** for `wait` and **refused** for every other policy: a setting no policy can read is a setting nobody notices is wrong |
| `NotificationBuffer` | `offer()`, `drain(max_records=None)`, `clear()`, `records()`, `peek_oldest()`, `peek_newest()`, `oldest_age_ms()`, `describe()`, `len()` |
| `BufferDecision` | what one offer cost: `accepted`, `policy`, `record`, **`discarded`** (the stored record destroyed to make room, named rather than only counted), `reason`, `depth_after`, `waited_ms`, `timed_out`, `lost` |
| `BufferStats` | `accepted`, `refused`, `discarded_oldest`, `collapsed`, `drained`, `drains`, `cleared`, `waits`, `waited_ms`, `timeouts`, `peak_depth`, `oldest_sequence`, `newest_sequence`, plus `lost`, `contiguous`, `is_full`, `ever_hit_the_limit` |
| `buffering_handler(buffer, *, on_lost=…)` | a Chapter 11 handler that hands every record to a buffer and never blocks; `on_lost` is called for every decision that cost a record |
| `offer_many(buffer, records)` | offer a run of records and get every decision back, which is how a policy comparison is written |

`contiguous` is the counter that makes `keep_window` honest: replacing the tail of a window leaves a
hole, and the report says `contiguous=false` instead of leaving a reader to do the arithmetic.

### `src/opcua_toolkit/spool.py` - the store in store-and-forward

| Piece | What it is for |
| --- | --- |
| `SpoolFullPolicy` | `trim_oldest` (destroy the oldest spooled record to make room) or `refuse` (leave the file exactly as it is and report the refusal) |
| `SpoolSettings(path, capacity, max_bytes, full_policy, flush_on_write, sync_on_write)` | the file, and **two** independent bounds: how many records and how many bytes. A spool bounded only by record count is a spool that can be enormous |
| `SpoolEntry` | one record as it exists on disk: `node_id`, `value`, `status`, `tag`, `source_timestamp`, `server_timestamp`, `arrived_at`, `sequence`, `replayed`, `variant_type`, `value_as_text` - one JSON object per line |
| `SpoolWrite` | one append: `accepted`, `entry`, `reason`, `records_after`, `bytes_after`, `trimmed` (the entries destroyed to make room), `unreadable_dropped` |
| `SpoolStats` | `records`, `bytes_used`, `capacity`, `max_bytes`, `appended`, `refused`, `trimmed`, `corrupt_lines`, `taken`, `cleared`, `oldest`, `newest`, plus `lost`, `is_full`, `backlog_age_ms` |
| `JsonlSpool` | `open()`, `append()`, `read(limit=None)`, `take(limit)`, `clear()`, `stats()`, `exists()` - all file IO on a thread, all guarded by one lock, so a spool is safe to share between a consumer task and a replay task |

Three rules this module is built on, because they are the ones people get wrong about disk buffers:

- **the file is the truth.** The counters in `SpoolStats` are re-derived from the file on read-back,
  so a crash between a write and a report cannot leave a spool claiming records that are not there;
- **a torn line is a loss with a name.** A half-written final line is counted once, as
  `corrupt_lines`, and is not silently skipped;
- **`take()` is the acknowledgement.** Records leave the file when a replay was accepted, which is
  why "0 bytes after the replay" in the run means the forward half of store-and-forward finished.

### The queue this chapter did not write: asyncua's own

`SubscriptionManager` takes `queue_maxsize` (default `1 000`) and `overflow` (default
`OverflowPolicy.DROP_OLDEST`) and passes both straight to `client.create_subscription(...)`, which
builds `asyncio.Queue(maxsize=queue_maxsize)` per subscription. The library's four policies, from
`asyncua/common/subscription.py` (166-172, 345-367):

| `OverflowPolicy` | What it does when the queue is full |
| --- | --- |
| `DROP_OLDEST` | pops the oldest queued event and pushes the new one - **silently**, no log line |
| `DROP_NEWEST` | drops the incoming event - **silently** |
| `WARN` | logs `Subscription %s event queue full; dropping event`, then drops the newest |
| `DISCONNECT` | logs an error and hands the owner a hook to force a full reconnect |

The toolkit's consumer reads that queue with **one** task and awaits each handler in order
(`_consume` → `await self._dispatch(event)` in `src/opcua_toolkit/subscriptions/manager.py`). That
single ordered consumer is why a handler with a cost turns the queue in front of it into a dropping
queue: while one record is being written, nothing is being dequeued.

### What is deliberately *not* configured yet

`ApplicationConfig` from Chapter 14 has no `buffer:` or `spool:` section, and this chapter did not
add one. `BufferSettings` and `SpoolSettings` are standalone models for now, for a specific reason:
a spool needs a **path**, and a path is a deployment decision about where a machine keeps the thing
that survives it. Choosing it here, in a chapter that has no gateway, would be a default nobody
chose - exactly what Chapter 14's `require_decided()` exists to catch. The capstone adds the section
and makes the path a required decision.

`client.subscriptions.queue_size` *is* in the config model, because it is a wire parameter. The
library's `queue_maxsize` is not: it belongs to a client library, and Part 5 shows what happens when
you decide not to use that library's queue at all.

### What the example adds on top (instrumentation, not toolkit code)

`examples/ch15_buffering.py` measures the answers asyncua's high level API throws away:

- `AnswerRecorder` wraps `create_monitored_items` and `create_subscription` on
  `client.uaclient.session` - the object a subscription actually calls - to read `revisedQueueSize`
  and `RevisedPublishingInterval` out of responses the library discards after taking the ids it
  needs. It is wrapped instrumentation, and the file says so: production code asks for the answer
  through the low level call, it does not monkey-patch a session.
- `Collector`, `SlowCollector` (a historian with a cost), `Historian` (a sink that can be down),
  `Elapsed` (a monotonic stopwatch) and `scratch_directory()` (temporary spool files, because a
  demo may use a temporary path and an operating gateway may not).

### What the example actually prints

One run of `examples/ch15_buffering.py --demo --demo-port 48435`, with the demo server's simulator
off so every value counted here is a value the example wrote. What is shortened, and nothing else:

- the 16 `opcua_toolkit.buffer` WARNING lines in the run's section 4, of which 3 are quoted (the same
  shape, different sequence numbers - the example's own `...and 13 more decisions in the same words`
  bullet is printed output, not a shortening by this document);
- the 12 buffer WARNING lines in the run's section 6, of which 3 are quoted, one per policy;
- the 6 buffer WARNING lines in the run's section 8, of which 1 is quoted;
- the 15 `opcua_toolkit.spool` WARNING lines in the run's section 7, of which 3 are quoted, one per
  spool file (`refused.jsonl` prints 7 of them and 2 are shown);
- four `asyncua.client.client` / `opcua_toolkit.subscriptions.manager` lines that repeat the same
  shape once per subscription, and the timestamps on the log lines, which the toolkit's
  `%(asctime)s` format prints as **local** time - every timestamp inside a spool file is UTC, so
  `18:15:17` in the log and `17:15:17+00:00` in the file are the same instant on this machine.

Every count and duration quoted below is printed output from that run.

```text
2026-11-19 18:15:06 | WARNING  | asyncua.server.server | No encrypting policy available, password may get transferred in plaintext
2026-11-19 18:15:06 | WARNING  | asyncua.server.server | Endpoints other than open requested but private key and certificate are not set.
2026-11-19 18:15:06 | INFO     | opcua_toolkit.devserver | demo server listening on opc.tcp://127.0.0.1:48435 (namespace 2 = urn:opcua-tutorial:demo)
demo server: opc.tcp://127.0.0.1:48435 (namespace 2)

1. three queues, three owners
-----------------------------
  endpoint: opc.tcp://127.0.0.1:48435
  queue 1, the server's monitored item queue: per monitored item, drained by the next publish, sized by the client's request and the server's own cap
     this server answers a queue request of 0 with 100 notifications, and publishes no such limit anywhere
  queue 2, asyncua's notification queue: one asyncio.Queue per subscription, maxsize=1000 unless configured otherwise (section 3 uses 2), overflow=DROP_OLDEST
  queue 3, the application buffer: capacity=4 records policy=keep_latest
  only queue 3 can answer 'what is missing?', and only a file answers it after a restart

2. what the server's queue keeps between two publishes
------------------------------------------------------
2026-11-19 18:15:07 | WARNING  | asyncua.client.client | Revised values returned differ from subscription values: CreateSubscriptionResult(SubscriptionId=78, RevisedPublishingInterval=2000.0, RevisedLifetimeCount=1000, RevisedMaxKeepAliveCount=22)
2026-11-19 18:15:07 | INFO     | opcua_toolkit.subscriptions.manager | subscription 78 created with 2000 ms publishing interval
  one subscription, 3 monitored items, queueSize asked: 1, 5, 0
  publishing asked 2000 ms, answered 2000 ms
  read before subscribing (1 Read request): ns=2;s=BatchTag001=1, ns=2;s=BatchTag002=2, ns=2;s=BatchTag003=3
  24 of 24 values written in 30.2703 ms across 3 Write request(s): the burst fits inside one 2000 ms publishing interval
  ns=2;s=BatchTag001: asked queueSize=1, answered revisedQueueSize=1; 9 values offered to the queue, 1 arrived
     offered: [1, 101, 102, 103, 104, 105, 106, 107, 108]
     arrived: [108]
     never arrived: [101, 102, 103, 104, 105, 106, 107]
     kept 1 of 9: a queue of 1 keeps the newest that many
  ns=2;s=BatchTag002: asked queueSize=5, answered revisedQueueSize=5; 9 values offered to the queue, 5 arrived
     offered: [2, 201, 202, 203, 204, 205, 206, 207, 208]
     arrived: [204, 205, 206, 207, 208]
     never arrived: [201, 202, 203]
     kept 5 of 9: a queue of 5 keeps the newest that many
  ns=2;s=BatchTag003: asked queueSize=0, answered revisedQueueSize=100; 9 values offered to the queue, 9 arrived
     offered: [3, 301, 302, 303, 304, 305, 306, 307, 308]
     arrived: [3, 301, 302, 303, 304, 305, 306, 307, 308]
     kept 9 of 9: a queue of 100 keeps the newest that many
  each item was offered the value it had when it was created plus this burst: a monitored item queues its current value the moment the server creates it
  this server queues a data change when the value is written, not on a sampling timer that re-reads it - and it answers every item's sampling interval as the subscription's publishing interval
  a queue of one is not a small queue: 5.13.1.5 describes it as a buffer that always contains the newest notification, and says the discard policy is not used for it
  asked for 0, this server answered its own cap: 'the server decides' means the server says, and revisedQueueSize is the only place a client can hear it
  asyncua's server trims a full queue by removing the oldest entry and never reads discardOldest, and asyncua's client always sends that field as True: neither side of this pair is using it
  and nothing in the notification says it happened: the Overflow bit the specification defines for a trimmed queue is never set by this server either

3. the client library's queue drops without telling anybody
-----------------------------------------------------------
2026-11-19 18:15:10 | WARNING  | asyncua.client.client | Revised values returned differ from subscription values: CreateSubscriptionResult(SubscriptionId=79, RevisedPublishingInterval=250.0, RevisedLifetimeCount=1000, RevisedMaxKeepAliveCount=100)
2026-11-19 18:15:10 | INFO     | opcua_toolkit.subscriptions.manager | subscription 79 created with 250 ms publishing interval
  20 tags: each item owes one notification the moment the server creates it and one for the value written here, so 40 were owed
  the server's queue for each item: 8 notifications
  this subscription's notification queue: maxsize=2, overflow=DROP_OLDEST, where asyncua's default is 1000
  the handler spends 0.06 s on every record
  records that reached a handler at all: 2
  of the 20 values written, the handler saw 2: [2018, 2019]
  the values that vanished between the server and the handler: [2000, 2001, 2002, 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014, 2015, 2016, 2017]
  what the manager's own counters say: subscription=79 publishing=250 ms items=20 failed=0 notifications=4 good=4 uncertain=0 bad=0 suppressed=0 stray=0 errors=0 recreations=0 status=none mode=reporting
  nothing in that report mentions the missing ones: a notification removed by the library's overflow policy never reaches any counter the application owns, and DROP_OLDEST does not even log
  the queue filled because the consumer was slow, not because the plant was fast: a historian that spends time on every record is every historian

4. a run of records this chapter can decide about
-------------------------------------------------
2026-11-19 18:15:14 | WARNING  | asyncua.client.client | Revised values returned differ from subscription values: CreateSubscriptionResult(SubscriptionId=80, RevisedPublishingInterval=250.0, RevisedLifetimeCount=1000, RevisedMaxKeepAliveCount=100)
2026-11-19 18:15:14 | INFO     | opcua_toolkit.subscriptions.manager | subscription 80 created with 250 ms publishing interval
2026-11-19 18:15:15 | WARNING  | opcua_toolkit.buffer | buffer plant: kept seq=25: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=21) [4 records stored]
2026-11-19 18:15:15 | WARNING  | opcua_toolkit.buffer | buffer plant: kept seq=26: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=22) [4 records stored]
2026-11-19 18:15:15 | WARNING  | opcua_toolkit.buffer | buffer plant: kept seq=27: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=23) [4 records stored]
  … (16 buffer WARNING lines in this section, 13 of them not shown here)
  the same 20 values, with the library queue back at 1000 and the buffer as the bounded point:
     buffer 'plant': capacity=4 records policy=keep_latest
     policy=keep_latest capacity=4 records
     holds 4 of 4 records (seq=37..40)
     accepted 20 refused 0 discarded_oldest 16 collapsed 0
     drained 0 records in 0 drain(s) cleared 0 peak_depth 4
     waited 0 ms across 0 wait(s), 0 ended in a refusal
     this buffer has admitted losing 16 records
     kept seq=25: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=21) [4 records stored]
     kept seq=26: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=22) [4 records stored]
     kept seq=27: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=23) [4 records stored]
     ...and 13 more decisions in the same words
  records handed to the buffer: 20 of 20 written; values unaccounted for: none
  every record is accounted for here, kept or dropped, and the dropped ones are named by sequence: that is the difference between a bounded buffer and a queue that drops quietly

5. back-pressure, and the wrong place to use it
-----------------------------------------------
  offered seq=21, then seq=22, to a buffer of one
  a consumer took 1 record 0.06 s later
  kept seq=22: room was available after waiting 71.9819 ms [1 records stored]
  the offer parked instead of deciding: nothing was thrown away, and the waiting became a cost the application paid instead of a loss the plant suffered
2026-11-19 18:15:17 | WARNING  | opcua_toolkit.buffer | buffer stuck refused seq=22: 1 of 1 records stored and no room appeared in 159.389 ms
  refused seq=22: no room appeared in 159.389 ms of waiting: the consumer took nothing while 1 records were stored after waiting 159.389 ms [1 records stored]
     buffer 'stuck': capacity=1 records policy=wait max_wait=150 ms
     policy=wait capacity=1 records
     holds 1 of 1 records (seq=21..21)
     accepted 1 refused 1 discarded_oldest 0 collapsed 0
     drained 0 records in 0 drain(s) cleared 0 peak_depth 1
     waited 159.389 ms across 1 wait(s), 1 ended in a refusal
     this buffer has admitted losing 1 records
  the wait is bounded on purpose: a producer that waits forever has stopped collecting, and a refusal after a wait is counted in the same report as the records it dropped
  the warning that comes with it: a handler that waits stops draining the library's queue, which puts the silent loss of section 3 back where it came from. Wait between two application tasks, never on the publish path

6. three policies over the same run of records
----------------------------------------------
  replaying 8 records into a buffer of 4
2026-11-19 18:15:17 | WARNING  | opcua_toolkit.buffer | buffer keep_latest: kept seq=25: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=21) [4 records stored]
  … (12 buffer WARNING lines in this section, one shape per policy, 9 of them not shown here)
  policy keep_latest: kept [3004, 3005, 3006, 3007]
     accepted 8 refused 0 discarded_oldest 4 collapsed 0 contiguous=true
     values destroyed to make room: [3000, 3001, 3002, 3003]
  policy keep_window: kept [3000, 3001, 3002, 3007]
     accepted 8 refused 0 discarded_oldest 0 collapsed 4 contiguous=false
     values destroyed to make room: [3003, 3004, 3005, 3006]
  policy reject_new: kept [3000, 3001, 3002, 3003]
     accepted 4 refused 4 discarded_oldest 0 collapsed 0 contiguous=true
  keep_window is the policy that leaves a hole in the middle of a run of values, and the report says so with contiguous=false instead of leaving the reader to do the arithmetic

7. the spool: the only thing here that survives a restart
---------------------------------------------------------
2026-11-19 18:15:17 | WARNING  | opcua_toolkit.spool | spool C:\Users\andre\AppData\Local\Temp\opcua-ch15-9rlooblp\outage.jsonl: spooled ns=2;s=BatchTag005: made room by removing the oldest spooled records [4 spooled records, 1220 bytes] (made room by removing 1 spooled record(s), oldest ns=2;s=BatchTag001 arrived 2026-11-19T17:15:17.703700+00:00)
  … (4 spool WARNING lines for outage.jsonl, 4 for tight.jsonl, 7 for refused.jsonl; only one of each file's is shown here)
  spool 'C:\Users\andre\AppData\Local\Temp\opcua-ch15-9rlooblp\outage.jsonl' capacity=4 spooled records max_bytes=8388608 bytes full=trim_oldest flush=True sync=False
  appended 8 records to it
  spooled ns=2;s=BatchTag008: made room by removing the oldest spooled records [4 spooled records, 1220 bytes] (made room by removing 1 spooled record(s), oldest ns=2;s=BatchTag004 arrived 2026-11-19T17:15:17.737897+00:00)
     spool file 'C:\Users\andre\AppData\Local\Temp\opcua-ch15-9rlooblp\outage.jsonl'
     holds 4 of 4 spooled records in 1220 of 8388608 bytes
     appended 8 taken 0 cleared 0 refused 0 trimmed 4 unreadable lines 0
     oldest spooled record arrived 2026-11-19 17:15:17.747+00:00
     the backlog is 0.050636 s old
     this spool has admitted losing 4 record(s): 0 refused, 4 trimmed, 0 unreadable
  the file holds 4 lines in 1220 bytes
  first line on disk: {"arrived_at": "2026-11-19T17:15:17.747927+00:00", "node_id": "ns=2;s=BatchTag005", "replayed": false, "sequence": 25, "server_timestamp": "2026-11-19T17:15:15.445990+00:00", "source_timestamp": null, "status": "Good", "tag": "BatchTag005", "value": 3004, "value_as_text": false, "variant_type": "Int32"}
  read back oldest first: [3004, 3005, 3006, 3007]
  the same records against a byte bound of 1220 bytes: 4 records survive in 1220 bytes, 4 trimmed
2026-11-19 18:15:17 | WARNING  | opcua_toolkit.spool | spool C:\Users\andre\AppData\Local\Temp\opcua-ch15-9rlooblp\refused.jsonl: the spool is at its bound (2 of 2 spooled records, 610 of 8388608 bytes) and policy 'refuse' does not remove anything already spooled
  policy 'refuse' turned down 7 of 9 writes and removed 0 records that were already on disk: the file is unchanged (yes)
  refused ns=2;s=BatchTag003: the spool is at its bound (2 of 2 spooled records, 610 of 8388608 bytes) and policy 'refuse' does not remove anything already spooled [2 spooled records, 610 bytes]
  the write it refused last: 3007 -> the spool is at its bound (2 of 2 spooled records, 610 of 8388608 bytes) and policy 'refuse' does not remove anything already spooled

8. store and forward: a historian that was down
-----------------------------------------------
2026-11-19 18:15:17 | WARNING  | opcua_toolkit.buffer | buffer during-outage: kept seq=23: policy 'keep_latest' removed the oldest stored record to make room (made room by removing seq=21) [2 records stored]
  … (6 buffer WARNING lines in this section, 5 of them not shown here)
  historian down while 8 records arrived, into a buffer of 2
     buffer: buffer 'during-outage': capacity=2 records policy=keep_latest
     buffer: policy=keep_latest capacity=2 records
     buffer: holds nothing
     buffer: accepted 8 refused 0 discarded_oldest 6 collapsed 0
     buffer: drained 2 records in 1 drain(s) cleared 0 peak_depth 2
     buffer: waited 0 ms across 0 wait(s), 0 ended in a refusal
     buffer: this buffer has admitted losing 6 records
     spool file 'C:\Users\andre\AppData\Local\Temp\opcua-ch15-9rlooblp\forward.jsonl'
     holds 8 of 1000 spooled records in 2440 of 8388608 bytes
     appended 8 taken 0 cleared 0 refused 0 trimmed 0 unreadable lines 0
     oldest spooled record arrived 2026-11-19 17:15:17.870+00:00
     the backlog is 0.063404 s old
     this spool has admitted losing 0 record(s): 0 refused, 0 trimmed, 0 unreadable
  records spilled straight to the file while the historian was down: [3000, 3001, 3002, 3003, 3004, 3005]
  the historian refused 2 while it was down
  after it came back, take() handed over 8 spooled records
  the historian accepted: [3000, 3001, 3002, 3003, 3004, 3005, 3006, 3007]
  every value that arrived during the outage, in arrival order: True
     after the replay: this spool has admitted losing 0 record(s): 0 refused, 0 trimmed, 0 unreadable
  the spool file after the replay: 0 bytes
  the same 8 records took 35.5392 ms to handle while the handler was also writing a file
  and the caveat that comes with it: on_lost runs on the subscription's consumer task, so a spool write there is an await on a thread - safe for the event loop, still paid for by the consumer. A gateway that spills at rate hands the decision to its own drain task instead

9. sizing the queues for an outage you can name
-----------------------------------------------
  20 notifications over 2.00979 s on 20 tags is 9.95 records/s in total and 0.498 records/s per tag
  that rate is this example's own write burst - every tag written once - not a plant's rate. The arithmetic is only as honest as the window it was measured from, which is why it is measured rather than typed in
  one spooled record measured 305 bytes, so a 300 s outage at this rate needs 2986 records and about 889 KiB of file
  the server's own queue holds 100 notifications per item, which at 0.498 records/s per tag is 200.979 s of this outage - and it dies with the session
  so the sizing splits in three: the server's queue covers a client that stopped publishing, the application buffer covers a sink that is slow, and only the file covers a sink that is gone

10. what to take from this chapter
----------------------------------
  NotificationBuffer(capacity=4 records policy=keep_latest) and buffering_handler() are the pair: a handler that never blocks, and a bounded buffer that names every record it could not keep
  JsonlSpool(settings) is the store in store-and-forward: bounded by records and by bytes, trimming or refusing by policy, and read back oldest first with take() as the acknowledgement that a replay was accepted
  read revisedQueueSize out of the answer instead of trusting the request, keep the handler on the publish path non-blocking, and put a number on every record that did not make it
  next: Part 5 turns to the server side, where these limits are set in the first place
```

#### Reading the run

Below, "Section N" is always the numbered section **of the run above**, not of this page.

**Section 2 is the specification's queue semantics, measured.** Three items in one subscription,
asked for queue sizes 1, 5 and 0, answered 1, 5 and 100. Each was offered 9 values - the 8 this
example wrote plus the value the item held when it was created - and each kept exactly the newest
`revisedQueueSize` of them, in queue order, as 5.13.1.5 promises. The `30.2703 ms` of writes inside
a `2 000 ms` publishing interval is what makes the test mean anything: the burst is entirely between
two publishes, so what did not arrive did not arrive because the queue was too small.

**Section 3 is the one that should be uncomfortable.** 40 notifications owed, 2 reached a handler,
18 of the 20 written values vanished, and the manager's own report - the report Chapter 12's
supervisor reads and Chapter 28's diagnostics print - shows `notifications=4`, `suppressed=0`,
`errors=0`. The queue that dropped them was `maxsize=2`, chosen by this example, with asyncua's
default `1 000` named next to it so the reader can see how ordinary the situation is: 20 tags, one
slow handler, 60 ms per record. The loss is invisible because the drop happened in a queue the
application does not own.

**Section 4 is the same 20 records with the bounded point moved.** With the library queue back at
`1 000`, `records handed to the buffer: 20 of 20 written; values unaccounted for: none`. The buffer
kept 4 (`seq=37..40`), threw away 16, and every one of the 16 is named by sequence in its own
decision line. Nothing about the plant changed between section 3 and section 4; only *where the data
was bounded* changed, and the report went from silent to complete.

**Section 5 is the policy people reach for, with its trap attached.** A `wait` offer parked for
`71.9819 ms` and then stored the record - nothing was lost, and the waiting became a cost inside the
application. Against a consumer that took nothing, the same policy spent its `150 ms` budget,
waited `159.389 ms`, refused, and counted the refusal next to the losses. The warning line attached
to that section is the honest part: a `wait` **on a handler** stops draining the library's queue,
which re-creates section 3's silent loss with better intentions. `wait` belongs between two
application tasks that each have their own bounded thing.

**Section 6 compares the three deciding policies over identical records,** which is the only way to
compare them honestly. `keep_latest` kept `[3004..3007]` and destroyed `[3000..3003]`;
`keep_window` kept `[3000, 3001, 3002, 3007]` and destroyed the middle - `collapsed 4`,
`contiguous=false`; `reject_new` kept the first four and refused the last four. All three are
"correct" for some application and each one is wrong for a different one. The report is what makes
the choice visible after the fact.

**Section 7 shows the spool as a file, not as an object.** 8 records appended into a capacity of 4
left 4 lines in 1220 bytes with `trimmed 4`; the same records against a byte bound of exactly 1220
bytes produced the same 4 survivors, which is the point of having two bounds; `refuse` turned down 7
of 9 writes and removed nothing, and the example asks the file directly whether it changed. The
first line on disk is printed as JSON, because a spool that a human cannot read is a spool nobody
trusts at 3 a.m.

**Section 8 is store-and-forward with both halves measured.** 6 records spilled from the buffer to
the file while the historian was down, the 2 records the historian refused were appended too, so the
file held all 8; after recovery `take()` handed over 8, the historian accepted them in arrival order
(`[3000..3007]`), and the file was 0 bytes - because `take()` is the acknowledgement, not a peek.
The cost is measured too: 35.5392 ms to handle 8 records while the handler was also writing a file,
and the caveat printed with it: `on_lost` runs on the consumer task, so a spool write there is paid
for by the consumer.

**Section 9 does the sizing arithmetic from the run's own measured rate** - 9.95 records/s total,
0.498 records/s per tag, 305 bytes per spooled record - and states what each queue can cover: the
server's 100-per-item queue buys 200.979 s of this outage and dies with the session; the application
buffer buys nothing at all after the process exits; a 300 s outage at this rate is 2 986 records and
about 889 KiB of file. Those are the numbers a deployment argues about, and the section says out
loud that the rate came from this example's own burst, not from a plant.

## 4. Verification

```powershell
uv run python examples/ch15_buffering.py --demo
uv run python examples/ch15_buffering.py --endpoint opc.tcp://127.0.0.1:4840

uv run pytest tests/unit/test_buffer_model.py tests/unit/test_spool_model.py -q --tb=short
uv run pytest tests/integration/test_buffering.py -q --tb=short
```

Both test commands were run, and this is what they printed:

```text
..............................................                           [100%]
46 passed in 0.73s
```

```text
.....                                                                    [100%]
5 passed in 26.91s
```

The integration run needs a server and real notifications, which is why it takes 27 s while the unit
run takes under a second. Its five tests are:

| Test | What it proves |
| --- | --- |
| `test_a_queue_of_one_delivers_only_the_newest_value` | three asked queue sizes (1, 3, 0) arrive as exactly the newest that many of the values the test wrote, and a new monitored item's first notification is the value it was created with |
| `test_the_libraries_own_queue_drops_without_telling_the_application` | with `queue_maxsize=1` and a 50 ms handler, the newest value arrives, some written values never do, and `SubscriptionManager.stats` underreports what was owed - it has no counter that a library-side drop could reach |
| `test_a_buffer_replays_the_same_records_three_ways` | one run of real records through `keep_latest`, `keep_window`, `reject_new`, with the kept values, the counters and `contiguous` asserted per policy |
| `test_a_spool_holds_what_a_replay_needs_and_remembers_its_order` | append 8 records into capacity 5, and the **file**'s line count, `trimmed`, the oldest-first read-back and `take()`'s acknowledgement all agree |
| `test_store_and_forward_replays_a_down_historian_in_arrival_order` | records refused by a down sink end up in the file and come back in arrival order, and the file is empty afterwards |

- **UaExpert**: create two monitored items on the same tag - one with queue size 1 and one with a
  queue size large enough to hold what one publishing interval produces - then write values faster
  than the publishing interval and compare what each item delivers. That is section 2 of this
  chapter's run, done through a GUI, and the spec's queue-of-one sentence predicts it. Then set the
  sampling interval faster than the publishing interval on a slow-publishing subscription and watch
  which values UaExpert is allowed to see.
- **Prosys**: run `examples/ch15_buffering.py --endpoint …` against it. The write-driven sections
  name the tags they need, so on a server without the demo's `BatchTagNNN` tags those sections say
  what they are waiting for while the policy and spool sections work off whatever that server
  reported. Expect the section 2 answers to change: the cap that answers a `queueSize` of 0 is that
  server's own number, and this chapter measured only asyncua's.
- **The `revisedQueueSize` probe**: the example's `AnswerRecorder` exists because asyncua's high
  level create path hands back the `MonitoredItemId` and discards the rest of the
  `CreateMonitoredItems` response. Section 2's `asked queueSize=… answered revisedQueueSize=…` lines
  are read out of the responses the library threw away. It is instrumentation, and it is also the
  gap it exposes: `MonitoredItemHandle.revised_queue_size` is a field the toolkit defines and the
  high level create path leaves empty, so keeping that answer means making the monitored items
  through the service call instead of through the convenience wrapper.
- **Test proof**: `tests/unit/test_buffer_model.py` pins each policy's decision, the counters, the
  two `max_wait` refusals, the waiting tests, and the loss report. `tests/unit/test_spool_model.py`
  asserts against the **file** - its line count, its bytes, its contents - including a torn final
  line counted once as `corrupt_lines`, a record too large for `max_bytes` refused before anything
  is trimmed, and the counters agreeing with what was read back.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Never configuring the client library's queue | a subscription whose own report says `notifications=4` while 40 were owed | set `queue_maxsize` to what one publish can produce, and choose an `overflow` policy that reports |
| Treating a drop as "the plant was fast" | a historian missing values while every counter reads healthy | measure the consumer's cost: this run's 18 lost records came from a 60 ms handler |
| Reading the requested `queueSize` as the queue you have | a `queueSize: 0` in a config file, and a queue of 100 you never agreed to | read `revisedQueueSize` and review the gap, as Chapter 14 reviews a clamped lifetime |
| Believing `discardOldest` because asyncua sends it | a deployment that assumes the server honoured a flag it never sent and never read | send it, and size the queue so the flag decides as little as possible |
| Expecting a trimmed queue to announce itself | a gap in a data series with no `Overflow` flag, no status, no log | assume the gap is invisible and count it yourself |
| A handler that blocks on the publish path | the library queue above it fills, and the loss moves back to where it cannot be counted | `buffering_handler` never blocks; the slow work goes where a drain task can wait |
| `wait` policy on a subscription handler | back-pressure that produces exactly the silent overflow it was meant to prevent | wait between application tasks, with a bounded `max_wait` |
| Choosing `keep_window` without reading `contiguous` | a "complete" last-N-samples view with a hole in the middle | read `contiguous=false` as the warning it is, or use `keep_latest` |
| Bounding a spool by record count only | a file that grows to a size the machine cannot hold while "within capacity" | bound it twice: `capacity` and `max_bytes` |
| Reading a buffer's counters as the truth after a crash | a report of records that were never written anywhere | the spool re-derives its counters from the file; the buffer's memory is not a store |
| Replaying with `read()` instead of `take()` | the same records replayed forever, or a file nobody dares to delete | `read()` asks, `take()` acknowledges |
| `clear()` counted as a loss | a loss total that hides which losses were decisions | `cleared` is reported apart from `lost`, exactly as `trimmed` is apart from `refused` |
| A spool path in a temp directory | a gateway that "survived" a restart with nothing on the other side of it | the capstone makes the path a required decision; a demo may use a temporary directory and an operator may not |

## 6. What's next

Part 5 turns to the side that sets these limits instead of discovering them. Chapter 16 authors an
address space on a server this repository writes, which is where a queue cap, a subscription cap and
a monitored item's `discardOldest` become decisions someone wrote down rather than answers a client
had to guess at. Chapter 17 is the chapter about the object a trimmed notification would have been
flagged in: `StatusCode`, `SourceTimestamp` and `ServerTimestamp` are the data, and this chapter
showed that asyncua's `StatusCode` exposes only the severity bits, so a spec-level `Overflow` flag
has nowhere to land in this client. Chapter 19 puts events on the same monitored item queues, where
the specification's overflow story is different and harsher: an overflowed event queue is meant to
queue an `EventQueueOverflowEventType` rather than quietly drop values.

The toolkit pieces from this chapter are used by every later part: the supervisor decides *when* to
buffer, Chapter 28 reports `BufferStats` and `SpoolStats` next to the subscription counters, Chapter
29 asserts on records instead of sleeping, and the capstone's `spool:` section is the file that has
to be there when the historian is down.
