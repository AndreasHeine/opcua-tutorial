# Chapter 6 - Node metadata

*Part 2 · status: built and tested · example: `examples/ch06_node_metadata.py` ·
tests: `tests/unit/test_inspector_model.py`, `tests/integration/test_inspect.py`*

## 1. Why this matters

Chapter 5 gets you to a node. The next question decides whether your application may
touch it at all - and the answer is not in your code, it is in attributes the server
publishes.

`AccessLevel` is the difference between "this is a setpoint you may write" and "this is a
sensor somebody else owns". `DataType` and `ValueRank` are the difference between a write
that works and `BadTypeMismatch`. `MinimumSamplingInterval` and the server-wide
`MinimumSupportedSampleRate` are the difference between a subscription that reports and
one that silently never fires. `Historizing` is the difference between a history query
that answers and one that does not exist.

A client that reads these attributes once configures itself correctly. A client that
hardcodes assumptions about them gets a working demo and a failed commissioning.

## 2. OPC UA theory

### The attributes that matter for a Variable

| Attribute | Meaning | Used by |
| --- | --- | --- |
| `DataType` | The type of the value, as a NodeId into the type system (`ns=0;i=11` is `Double`) | Chapters 8, 20 |
| `ValueRank` | `-1` scalar, `0` one-dimensional, `1` one-dimensional array of known length... a `1` here means an array | Chapters 8, 20 |
| `ArrayDimensions` | The sizes when `ValueRank` is not `-1` | Chapter 8 |
| `AccessLevel` | What the **node** allows: an `AccessLevelType` bit field | Chapters 6, 8 |
| `UserAccessLevel` | What **this session** is allowed, the same `AccessLevelType` bit field | Chapters 6, 8, 26 |
| `MinimumSamplingInterval` | This node's floor for sampling, in milliseconds | Chapter 10 |
| `Historizing` | Whether the server is recording history for this node at all | Chapter 20 |
| `WriteMask` / `UserWriteMask` | Which attributes may be written, and which you may write | Chapter 26 |
| `BrowseName`, `DisplayName`, `Description` | identity and human text | Chapters 2, 5 |
| `NodeVersion`, `QualifiedNamedPath` | rarely used, but present on vendor nodes | - |

### `AccessLevel` is a bit field, not a flag

`AccessLevel` and `UserAccessLevel` share one data type, `AccessLevelType`, which OPC UA
Part 3 defines as a subtype of `Byte` with an `OptionSetValues` property - the
specification's way of saying "one byte, and every bit of it has a name". All eight bits
are defined there, so a decoder has to know all eight:

| Bit | Mask | Name | What the specification attaches to it |
| --- | --- | --- | --- |
| 0 | `0x01` | `CurrentRead` | the current value is readable - and says whether a current value exists at all |
| 1 | `0x02` | `CurrentWrite` | the current value is writeable - and says whether a current value exists at all |
| 2 | `0x04` | `HistoryRead` | the history of the value is readable, i.e. the server has history for this node |
| 3 | `0x08` | `HistoryWrite` | the history of the value is writeable |
| 4 | `0x10` | `SemanticChange` | set on a *Property* that defines a semantic aspect of its parent Node, and whose value - so that semantic - may change while the server runs |
| 5 | `0x20` | `StatusWrite` | the current `StatusCode` is writeable: cleared means only `Good` may be written, set means any status code |
| 6 | `0x40` | `TimestampWrite` | the current `SourceTimestamp` is writeable: cleared means only a null timestamp, set means any timestamp |
| 7 | `0x80` | `Reserved` | reserved for future use, and **shall always be zero** |

Two things come out of that table.

The first is that history appears twice, in two different attributes. `HistoryRead` /
`HistoryWrite` (bits 2 and 3) say whether the history of the value is readable or
writeable on this node - and, per the same table, whether that history is available from
this server at all. `Historizing` is a separate attribute: it says the server's job
includes recording this node's value into history. A client that reads one and assumes the
other asks a server for history it never agreed to keep.

The second is that bits 4 to 6 are not decoration. `StatusWrite` and `TimestampWrite` are
what decide whether a server accepts a value *together with the status code and timestamp
you measured* - a data-quality question for a historian or a store-and-forward gateway,
invisible to a dashboard. `SemanticChange` marks a Property whose value changes what its
parent node means, which is why it appears on type metadata rather than on plant tags.

Bit 7 has no name to decode. `AccessRights(0x80).describe()` prints `none`: the toolkit
does not invent a permission for a bit the specification says shall be zero.

So "read only" means `CurrentRead` set and `CurrentWrite` clear - and `AccessLevel == 3`
is a comparison that works on the two bits everyone uses and misreads the six bits a real
server may also set. The toolkit's `AccessRights` decodes the named bits once and prints
them in specification order, so a node with every defined bit set reports:

```text
CurrentRead|CurrentWrite|HistoryRead|HistoryWrite|SemanticChange|StatusWrite|TimestampWrite
```

`AccessLevel` and `UserAccessLevel` are **different attributes**: the first is what the
node permits, the second is what your session is permitted. They are frequently equal
and dangerously different on a real server. Neither one is authorisation in the
security sense - Chapter 26 is about that.

### The type definition

Following the `HasTypeDefinition` reference tells you which variable type a node was
built from (`BaseDataVariableType`, or a companion-specification type). On a plain
server it is uninteresting; on a companion-specification server it is how a client
recognises a node it is allowed to understand (Chapter 23).

## 3. Implementation

`NodeInspector` reads the attributes in batches (it is a `Read` service, so it obeys
`MaxNodesPerRead`) and returns a `NodeDescriptor`:

```python
from opcua_toolkit import NamespaceExplorer, NodeInspector

descriptor = await NodeInspector(client).inspect("ns=2;s=Temperature")
descriptor.data_type_text  # 'Double'
descriptor.is_array  # False
descriptor.access_rights.describe()  # 'CurrentRead|CurrentWrite'
descriptor.readable, descriptor.writable, descriptor.historizing
descriptor.type_definition_text  # 'BaseDataVariableType'
```

`inspect_many` inspects a list in chunked reads. A node the server does not have raises
`NodeNotAccessibleError`, which **keeps the status code** - because "missing" and
"unreadable" are different problems with different fixes.

`AccessRights` exposes one property per named bit - `current_read`, `current_write`,
`history_read`, `history_write`, `semantic_change`, `status_write`, `timestamp_write` - so
the table in section 2 is exactly what the code reads. The demo server reports
`Temperature` as `CurrentRead|CurrentWrite`, which is the whole report: the five other
named bits are clear, and that is the fact a client needs before it decides to send its
own `SourceTimestamp` or `StatusCode` along with a value.

`NodeDescriptor.summary_line()` and `.as_dict()` are what a log line and a JSON payload
use later (Chapter 28, Chapter 30).

### What the example actually prints

```text
2. one Read per attribute, one line per node
  ns=2;s=Temperature           Variable  Temperature              Double       rank=-1  access=CurrentRead|CurrentWrite
  ns=2;s=Setpoint              Variable  Setpoint                 Float        rank=-1  access=CurrentRead|CurrentWrite
  ns=2;s=Vibration             Variable  Vibration                Float        rank=-1  access=CurrentRead
  ns=2;s=SampleArray           Variable  SampleArray              Double       rank=1   access=CurrentRead|CurrentWrite
  ns=2;i=1                     Variable  InputArguments           Argument     rank=0   access=CurrentRead

3. the same server holds tags you may write and tags you may not
  writable     : 33 nodes
  not writable : 7 nodes -> Vibration, QualityScore_RO, TemperatureHistory_RO, InputArguments, OutputArguments
  InputArguments and OutputArguments are method declaration variables living in the same
  namespace: Chapter 9 reads them as signatures, not as tags

4. everything one node can tell you
  access level  : CurrentRead|CurrentWrite
  user access   : CurrentRead|CurrentWrite
    the five other named bits of the same byte:
    history_read=False history_write=False semantic_change=False
    status_write=False timestamp_write=False
  min sampling  : 0.0 ms from this node
    0 does not mean 'as fast as you like': the server-wide MinimumSupportedSampleRate
    is the other floor, and Chapter 10 is where the two meet
  historizing   : False (Chapter 20 needs this to be true)

5. a node the server does not have
  NodeNotAccessibleError: Node ns=2;s=ThisTagDoesNotExist: BadNodeIdUnknown (0x80340000)
```

Four things in that output are worth dwelling on:

- **`Setpoint` is `Float`, `Temperature` is `Double`.** They read the same on a screen and
  behave differently on a write. Chapter 8 shows the failure.
- **Not every variable is a tag.** `InputArguments` and `OutputArguments` are variables too,
  in the same namespace as the plant tags, and they are method declarations. A client that
  says "subscribe to every Variable in ns=2" subscribes to method metadata.
- **The demo server sets only the two bits everyone looks at.** On `Temperature` the other
  five named bits are all clear, which is a normal thing for a plain server to publish - and
  is precisely why a client has to read them instead of assuming that a node you may write a
  value to is also a node you may write a timestamp or a status code to.
- **A node-level `MinimumSamplingInterval` of 0 is not a promise of speed.** The server-wide
  `MinimumSupportedSampleRate` (this server publishes 50 ms) is the other floor, and neither
  one is necessarily *enforced* - Chapter 10 measures that.

## 4. Verification

```powershell
uv run python examples/ch06_node_metadata.py --demo --demo-port 48401
uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature" --value
uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Vibration" --node "ns=2;s=Setpoint"
```

- **UaExpert**: select `Temperature`, open the *Attributes* tab, and check `DataType`,
  `ValueRank`, `AccessLevel`, `UserAccessLevel`, `MinimumSamplingInterval`, `Historizing`
  against the example's output. Read `AccessLevel` as the named bits from section 2 rather
  than as one number: the toolkit reports `CurrentRead|CurrentWrite` on `Temperature` and
  `CurrentRead` on `Vibration`, so the bytes this server publishes are 3 and 1, and every
  other named bit is clear on it. Then select `Vibration` and try
  *Write Value* - the server's rejection is the fact this chapter is about, and it is
  visible in UaExpert's own error dialog (`BadNotWritable` or `BadUserAccessDenied`
  depending on the server). Compare `TemperatureHistory_RO` (same type as `Temperature`,
  different rights) to see why rights cannot be inferred from type.
- **Prosys UA Simulation Server**: its tags have their own `AccessLevel` settings and a
  different `MinimumSupportedSampleRate`. Run the example against it with `--endpoint` and
  watch the output change while the code does not.
- **Test proof**: `tests/integration/test_inspect.py` inspects real nodes, asserts the
  writable/read-only split, and asserts that a missing node reports `BadNodeIdUnknown`
  rather than a generic failure.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Assuming every variable is a tag | subscriptions to `InputArguments`, `OutputArguments`, EU-range properties | filter by path and purpose, not by `NodeClass` |
| Treating `AccessLevel` as what your code decided | a write rejected on a server that was never consulted | read `AccessLevel` *and* `UserAccessLevel` |
| Comparing `AccessLevel == 3` instead of testing bits | a node that also sets `HistoryRead` or `TimestampWrite` is misclassified | test named bits with `AccessRights` |
| Sending a value with its own timestamp or status code without checking bits 5 and 6 | the client is asking a node to accept quality that node never offered | check `StatusWrite` and `TimestampWrite` first, and decide what your client does when they are clear |
| Treating `HistoryRead` as "this node is being historized" | a history request aimed at a node whose `Historizing` is false | read both: the bits for rights over history, `Historizing` for what the server records |
| Assuming `DataType` matches what the UI shows | a `Float` tag that a write sends as `Double` | build the variant from the declared type (Chapter 8) |
| Ignoring `ValueRank` | an array tag written as a scalar, or read as one | `descriptor.is_array` before building a value |
| Believing `MinimumSamplingInterval = 0` means "as fast as you like" | a monitored item that never reports | two floors exist; Chapter 10 negotiates |
| Running a history query without checking `Historizing` | "history is broken" when it was never enabled | check the attribute first (Chapter 20) |
| Treating a `BadNodeIdUnknown` as a permission problem | an operator sent to the wrong machine | keep the status code, `NodeNotAccessibleError` does |

## 6. What's next

Chapter 7 reads the values of the nodes this chapter described, in batches sized by the
limits from Chapter 4. Chapter 8 writes them and reads the server's per-item answer.
Chapter 9 reads method declarations, which are the same attributes seen from a `Method`
node instead of a `Variable` one.
