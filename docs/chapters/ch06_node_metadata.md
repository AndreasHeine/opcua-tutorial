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
| `AccessLevel` | What the **node** allows, as a bit field | Chapters 6, 8 |
| `UserAccessLevel` | What **this session** is allowed, same bit field | Chapters 6, 8, 26 |
| `MinimumSamplingInterval` | This node's floor for sampling, in milliseconds | Chapter 10 |
| `Historizing` | Whether the server is recording history for this node at all | Chapter 20 |
| `WriteMask` / `UserWriteMask` | Which attributes may be written, and which you may write | Chapter 26 |
| `BrowseName`, `DisplayName`, `Description` | identity and human text | Chapters 2, 5 |
| `NodeVersion`, `QualifiedNamedPath` | rarely used, but present on vendor nodes | - |

### `AccessLevel` is a bit field, not a flag

| Bit | Name | Means |
| --- | --- | --- |
| `0x01` | `CurrentRead` | the current value may be read |
| `0x02` | `CurrentWrite` | the current value may be written |
| `0x04` | `Annotations` | annotations may be written |
| `0x08` | `WriteAlways` | may be written even when not `CurrentWrite` |
| `0x10` | `AutoWrite` | the server writes it itself |
| `0x20` | `InsertOrgData` | original data is kept on writes |
| `0x40` | `Historically` | history read/write is allowed |

So "read only" means `CurrentRead` set and `CurrentWrite` clear - and the toolkit's
`AccessRights` turns the integer into named facts instead of leaving every caller to do
bit arithmetic in a hurry.

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
  InputArguments and OutputArguments are method declaration variables living in the same
  namespace: Chapter 9 reads them as signatures, not as tags

4. everything one node can tell you
  min sampling  : 0.0 ms from this node
    0 does not mean 'as fast as you like': the server-wide MinimumSupportedSampleRate
    is the other floor, and Chapter 10 is where the two meet
  historizing   : False (Chapter 20 needs this to be true)

5. a node the server does not have
  NodeNotAccessibleError: Node ns=2;s=ThisTagDoesNotExist: BadNodeIdUnknown (0x80340000)
```

Three things in that output are worth dwelling on:

- **`Setpoint` is `Float`, `Temperature` is `Double`.** They read the same on a screen and
  behave differently on a write. Chapter 8 shows the failure.
- **Not every variable is a tag.** `InputArguments` and `OutputArguments` are variables too,
  in the same namespace as the plant tags, and they are method declarations. A client that
  says "subscribe to every Variable in ns=2" subscribes to method metadata.
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
  against the example's output. Then select `Vibration` and try
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
| Comparing `AccessLevel == 3` instead of testing bits | a node with `Historically` or `WriteAlways` set is misclassified | use `AccessRights` |
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
