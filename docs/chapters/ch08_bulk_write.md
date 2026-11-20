# Chapter 8 - Writing values

*Part 2 · status: built and tested · example: `examples/ch08_bulk_write.py` ·
tests: `tests/unit/test_write_model.py`, `tests/unit/test_values.py`, `tests/integration/test_write.py`*

## 1. Why this matters

A write that does not raise is a write nobody has verified.

Most OPC UA code in the wild is written as if a service call either works or throws.
OPC UA does not work that way: a bulk write answers with **one StatusCode per node**, and
different codes mean different problems needing different responses - one is your bug, one
is the server's policy, one is a missing node, and one is a server that let through
something you thought it would block.

This chapter is where the tutorial's central sentence appears in full: **a server
publishes limits and access rights; enforcing them is a separate decision the server
makes for itself.**

## 2. OPC UA theory

`Write` takes one `WriteValue` per item (NodeId, attribute, and a `DataValue` to store)
and answers with one `StatusCode` per item. `MaxNodesPerWrite` bounds how many items one
request may carry, exactly as `MaxNodesPerRead` does.

The codes worth being able to tell apart:

| StatusCode | What actually happened | Whose problem |
| --- | --- | --- |
| `BadNotWritable` | writing is not permitted for this node/attribute | the server's model - look at `AccessLevel`, not at your value |
| `BadUserAccessDenied` | this **session** is not allowed | your identity or the server's permissions (Chapter 26) |
| `BadTypeMismatch` | the value does not fit the node's declared `DataType` | almost always your code |
| `BadNodeIdUnknown` | the node does not exist | your address, or a server that changed |
| `BadBadStatus` / `BadInvalidArgument` | the value is a legal type but a nonsense value | your validation |
| `BadWriteNothingNew` / vendor-specific refusals | the server rejected the change for its own reasons | ask the vendor |
| `BadTooManyNodesSpecified` | the request was bigger than `MaxNodesPerWrite` | your chunking |

And the trap: **the same refusal can arrive as different codes on different servers**.
Some answer `BadNotWritable`, some answer `BadUserAccessDenied`, and some answer `Good` -
because `AccessLevel` is an attribute the server publishes, and honouring it is a policy
choice that a server is free to make loosely.

A client that relies on the server to refuse a write it should not perform is a client
that writes to the wrong tag as soon as it meets a permissive server.

## 3. Implementation

`BulkWriter` takes `WriteItem`s and returns a `WriteReport` with one `WriteOutcome` per
requested item:

```python
from opcua_toolkit import BulkWriter, WriteItem

writer = BulkWriter(client, limits=limits)  # MaxNodesPerWrite sized chunks
report = await writer.write(
    [
        WriteItem("ns=2;s=Temperature", 21.5),
        WriteItem("ns=2;s=TemperatureHistory_RO", 21.5),
    ]
)
for outcome in report.outcomes:
    print(outcome.node_id_text, outcome.status.name, outcome.note)
report.raise_for_failures()  # only if a failed batch really must stop the run
```

Three deliberate design decisions:

- **A client-side preflight, switchable.** By default the writer checks `AccessLevel`
  before sending and refuses locally (`BadNotWritable` as a verdict). With the check off,
  the request goes out and the server's own code comes back. The example runs both, on
  purpose, so you can see that a *verdict* and a *status code* are not the same thing.
- **Type built from the node, not from the value.** `WriteItem` values go through
  `make_variant` / `coerce_to_variant_type`, which uses the `DataType` and `ValueRank`
  Chapter 6 read. `Setpoint` is `Float`: a plain Python `float` is `Double`, and that is
  the classic `BadTypeMismatch`.
- **Retry only what can be retried.** A transport-shaped failure is retried, up to a
  bounded number of attempts; a refusal is not. `BadUserAccessDenied` retried five times
  is still `BadUserAccessDenied`, and it makes the incident harder to read.

### What the example actually prints

```text
3. one batch, three different refusals
  items=3 outcomes=3 ok=0 failed=3 skipped=2 requests=0 retried=0 shrinks=0 elapsed=1.6 ms
    ns=2;s=TemperatureHistory_RO BadNotWritable (0x803B0000) attempts=1 note=AccessLevel does not allow CurrentWrite (bits=0x01)
    ns=2;s=ThisTagDoesNotExist   BadNodeIdUnknown (0x80340000) attempts=1 note=node not readable, access level unknown
    ns=2;s=Temperature           BadTypeMismatch (0x80740000) attempts=1 note='not a number' is not numeric for Double
  three items, three status codes, and not one exception

4. the same item with the client-side check turned off
    ns=2;s=TemperatureHistory_RO BadUserAccessDenied (0x801F0000) attempts=1 note=BadUserAccessDenied is not retryable
  the client's preflight said BadNotWritable; this server said BadUserAccessDenied (0x801F0000)
  a verdict and a status code are not the same thing, so a report keeps the code and the note instead of a boolean

5. a collector must not walk past a bad batch
  PartialFailureError: bulk write failed for 3 of 3 items: ns=2;s=TemperatureHistory_RO=BadNotWritable, ...

6. what the refused writes did not change
  ns=2;s=TemperatureHistory_RO 21.5
  ns=2;s=Temperature           21.5
  a refused write leaves the node as it was: only the report changed
```

The last section is the one people skip, and it is the safety argument for the whole
chapter: a refused write is inert. A batch that fails and is *reported* leaves the plant
as it was; a batch that fails and is *not examined* leaves an operator believing values
were sent.

## 4. Verification

```powershell
uv run python examples/ch08_bulk_write.py --demo --demo-port 48401
uv run opcua-toolkit write --endpoint opc.tcp://127.0.0.1:48401 --set "ns=2;s=Temperature=23.5"
uv run opcua-toolkit write --endpoint opc.tcp://127.0.0.1:48401 --set "ns=2;s=Vibration=9.9"
uv run opcua-toolkit read --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Vibration"
```

- **Write from UaExpert as well.** In UaExpert, right-click `Vibration` → *Write Value*.
  The error dialog is the server's own answer, and comparing it with the toolkit's line is
  the whole lesson: the same attempt, the same refusal, two independent implementations
  agreeing. Then write `Temperature` from UaExpert and see it arrive in the toolkit's
  Chapter 10 subscription.
- **Prosys**: connect to the demo server and attempt the same two writes. Also try it
  against Prosys's own simulation server, where rights and codes are configured
  differently - that is where "the same refusal arrives as a different code" stops being
  a theory.
- **Test proof**: `tests/integration/test_write.py` asserts the per-item codes against a
  live server (read-only node, missing node, type mismatch), that chunking matches
  `MaxNodesPerWrite`, and that a refused write leaves the node's value unchanged.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Treating "no exception" as success | 20 of 24 writes refused, nobody notices | read the `WriteReport`; `raise_for_failures()` when it must stop |
| Relying on the server to block a write | a permissive server accepts a write to a read-only tag | keep the preflight on, or make your own permission model explicit |
| Sending a Python `float` to a `Float`/`Single` tag | `BadTypeMismatch` | build the variant from the declared `DataType` |
| Calling `BadNodeIdUnknown` "not writable" | an engineer checks permissions on the wrong machine | keep and print the code (`WriteOutcome.status` + `note`) |
| Retrying refusals | five attempts against a permission that will not change | retry transport-shaped failures only |
| Writing attributes you have no right to (`Description`, `AccessLevel`) | `BadNotWritable` on an attribute, not on the value | check `WriteMask` / `UserWriteMask` (Chapter 26) |
| Writing without reading first, on a controller-owned tag | a value overwritten by the PLC two seconds later | write-verify-read, or don't write tags you do not own (Chapter 31) |
| Ignoring `IndexRange` for array tags | writing a whole array when one element was meant | `WriteItem` supports ranges; Chapter 20 uses the same idea |

## 6. What's next

Chapter 9 leaves the attribute world behind and calls a method - the one OPC UA service
that asks the server to *do* something, with a declaration you can read before you call
it. Chapter 10 then stops writing values to make things happen and starts reading the
server's own reporting.
