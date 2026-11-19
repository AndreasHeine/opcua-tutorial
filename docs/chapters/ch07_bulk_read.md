# Chapter 7 - Reading values

*Part 2 · status: built and tested · example: `examples/ch07_bulk_read.py` ·
tests: `tests/unit/test_limits.py`, `tests/integration/test_read.py`*

## 1. Why this matters

Every collector eventually has to read a lot of values, and the difference between 5
requests and 47 decides whether it keeps up with a plant.

"Use a bulk read" is not the lesson, because bulk read is not a switch: a `Read` request
is bounded by the server's `MaxNodesPerRead`, so a bulk read of 500 tags on a server with
a limit of 10 **is** 50 requests. The lesson is that the number is measurable, that it
comes from the server, and that the answer is one status per node - not one status per
request.

## 2. OPC UA theory

`Read` takes a list of `ReadValueId`s - each one a NodeId plus an **attribute** (and an
optional `IndexRange`) - and answers with one `DataValue` per requested item, each with
its own `StatusCode`.

Three consequences:

- **A request is per chunk.** `OperationLimits.MaxNodesPerRead` caps how many
  `ReadValueId`s one request may carry. Sending more is a server-side rejection
  (`BadTooManyNodesSpecified`), not a client-side detail.
- **A batch is not atomic.** The server answers every item it can. A batch with one bad
  NodeId returns 23 `Good`s and one `BadNodeIdUnknown`. Collapsing that into a single
  exception loses the 23 values, and pretending the batch "succeeded" loses the 1 failure.
- **Values and metadata are the same service.** `Read` reads attributes, so `Description`,
  `DisplayName` and `AccessLevel` are read the same way as `Value` - one chunked call
  either way. `ReadAttributes` is a separate service that some clients use and this
  tutorial does not need.

A `DataValue` is not a bare value: it carries `StatusCode`, `SourceTimestamp` and
`ServerTimestamp` (and `SourcePicoseconds` / `ServerPicoseconds`). A value whose status
is `UncertainInitialValue` or `BadSensorFailure` is information, and a collector that
unwraps to a Python number has thrown it away. Chapter 17 is about that in full; Chapter 7
is where the toolkit stops it happening by accident.

## 3. Implementation

`BulkReader` batches reads to the server's own limit and returns one `ReadResult` per
requested node plus `BulkReadStats`:

```python
from opcua_toolkit import BulkReader, OperationLimits

limits = await OperationLimits.read(client)
reader = BulkReader(client, limits=limits)
results, stats = await reader.read_values(nodes)  # 47 nodes, 5 requests
for result in results:
    if not result.ok:
        ...  # result.status is the server's answer
print(stats.summary())
```

| API | What it is for |
| --- | --- |
| `read_values(nodes)` | current `DataValue` of each node, chunked |
| `read_attribute(nodes, attribute)` | the same for any attribute - metadata in bulk |
| `values_of(nodes)` | the values only, once you have decided status is not your problem |
| `read_one_by_one(nodes)` | the naive baseline, present **only** to make its cost visible |
| `BulkReadStats` | nodes, successes, failures, requests, nodes per request, elapsed |

The stats are cross-checked against the real service calls counted on the client's own
session in `tests/conftest.py`, so "requests = 5" is a measured claim and not a guess
about what the library did internally.

### What the example actually prints

```text
1. the nodes come from the server
  targets: 47 variables, found by browsing
  MaxNodesPerRead published by this server: 10
  a correctly chunked read therefore needs 5 request(s)

2. bulk read
  nodes=47 ok=47 failed=0 requests=5 nodes/request=9.4 elapsed=3.9 ms
  requests counted on the session: 5

3. the same values, read one by one
  nodes=47 ok=47 failed=0 requests=47 nodes/request=1.0 elapsed=16.0 ms
  asyncua's Node.read_data_value asks for the value through Read, not ReadAttributes:
  same service, different request shape, and one node per request
  cost ratio: 4.1x the bulk read for the same 47 values

4. a batch is not a promise: per node statuses
  nodes=4 ok=3 failed=1 requests=1 nodes/request=4.0 elapsed=0.4 ms
    ns=2;s=ThisTagDoesNotExist   BadNodeIdUnknown (0x80340000)
  the batch still returned every node it could - a partial failure is data, not an error
```

The ratio (4.1x here) is not the point - the **request count** is. On a loopback
connection 47 reads take 16 ms; on a WAN link to a plant server with 100 ms round trips
the same code costs 4.7 seconds per scan cycle, and that is the point at which a
collector stops being a collector.

## 4. Verification

```powershell
uv run python examples/ch07_bulk_read.py --demo --demo-port 48401
uv run opcua-toolkit read --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature" --node "ns=2;s=Pressure" --compare
uv run opcua-toolkit read --endpoint opc.tcp://127.0.0.1:48401 --batch --json
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401 --max-nodes-per-read 25
```

- **Change the server, not the code**: restart the demo server with
  `--max-nodes-per-read 25` and run the read again. The toolkit's request count changes
  from 5 to 2 with no code change, because the chunk size came from the server. Then run
  with `--no-limits` and see the toolkit's honest answer to a server that publishes
  nothing (`status_note()` says so, and the fallback is conservative).
- **UaExpert**: watch the traffic while the read runs (*Tools → Trace* / the built-in
  packet view in the evaluation tools) or, more simply, read the same 47 NodeIds by hand
  in the *Subscriptions → Items* list and compare values with `--json` output.
- **Prosys**: point the Prosys client at the demo server and read the same nodes. Its
  own server has different `OperationLimits`, so run the toolkit against it too and
  compare request counts.
- **Test proof**: `tests/integration/test_read.py` asserts the chunk count against
  `MaxNodesPerRead`, and that a batch containing a bad NodeId still returns the good
  values with the failure kept as a status code.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Polling one node per request | a collector that falls behind, high server load | `BulkReader`, sized by the server's limit |
| Choosing a chunk size in code | too small (slow) or too large (`BadTooManyNodesSpecified`) | read `OperationLimits`; `LimitsOverrides` only when you have a reason |
| Treating a batch as one success/failure | 1 bad NodeId loses 23 values, or 1 failure is invisible | one outcome per item (`ReadResult.status`) |
| Unwrapping `DataValue` to a Python number | `UncertainInitialValue` becomes a plausible number in a historian | keep status and timestamps (`ReadResult`) |
| Assuming the value's timestamp is the server's | reporting stale data as fresh | compare `SourceTimestamp` and `ServerTimestamp` (Chapter 17) |
| Reading metadata node-by-node at startup | a slow startup that looks like a connection problem | `read_attribute` in chunks |
| Assuming a server with no published limits has none | oversized requests rejected by a vendor server | trust `status_note()`, fall back conservatively |
| Trusting the toolkit's stats alone | a library that batches differently than you assumed | the integration tests count real service calls on the session |

## 6. What's next

Chapter 8 is the same batching problem in the direction that hurts: writing. It is also
where the tutorial's central sentence appears in full - a server *publishes* limits and
rights, and *enforces* something else. Chapter 10 replaces polling entirely with
subscriptions.
