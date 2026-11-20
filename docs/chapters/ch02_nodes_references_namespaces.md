# Chapter 2 - Nodes, references and namespaces

*Part 1 · status: written · no example file: the code for this chapter is Chapter 5*

## 1. Why this matters

Every OPC UA bug in this tutorial's first ten chapters is a naming bug. A value is
reached through a NodeId; a NodeId is only meaningful inside the namespace that
authored it; a namespace index is assigned by a server and can change when that
server is reconfigured.

Learn the three facts in this chapter - what a node is, what a reference is, what a
namespace is - and the two habits that follow from them, and most of the failure modes
in Chapters 4 to 13 stop being surprises.

## 2. OPC UA theory

### Nodes

Everything in an OPC UA server is a node, and a node has exactly one of a small number
of **NodeClasses**:

| NodeClass | Used for | Example |
| --- | --- | --- |
| `Object` | A thing in the plant | `Line1` |
| `Variable` | A value you read, write or monitor | `Temperature` |
| `Method` | A call the server can execute | `StartLine` |
| `ObjectType` | A shape other objects follow | `BaseObjectType` |
| `VariableType` | A shape other variables follow | `BaseDataVariableType` |
| `DataType` | A type a value can have | `Double`, `Boolean`, `UInt32` |
| `ReferenceType` | A kind of connection | `HasComponent` |
| `View` | A saved subset of the address space | a published view |
| `Unspecified` | "any class", used in filters and browse requests | - |

Every node carries the same core attributes, and the ones that matter in practice:

| Attribute | Why it matters |
| --- | --- |
| `NodeId` | The address. Nothing else identifies a node |
| `BrowseName` | A `QualifiedName` (namespace index + name) used to build browse paths. Stable, machine-facing |
| `DisplayName` | A `LocalizedText` for humans. **Never a lookup key** |
| `Description` | Free text. The demo server fills it in so Chapter 6 has something to read |
| `WriteMask` / `UserWriteMask` | Which attributes *can* be written, and which *you* may write |
| `AccessLevel` / `UserAccessLevel` | Current and per-session rights on the value. Chapter 6 |

`BrowseName` versus `DisplayName` is the single most useful distinction in the
address space: paths are built from `BrowseName`, screens are built from
`DisplayName`, and a client that looks up nodes by display name breaks the first time
a server adds a second language.

### References

A reference is typed and **directed**. The same connection has a forward direction and
an inverse direction, and browsing follows one of them.

- `Organizes` - a folder contains something.
- `HasComponent` - an object has a component that is part of it (a tag on a line).
- `HasProperty` - a subtype of `HasComponent` for metadata (engineering units, a
  description). A property is not something you normally subscribe to.
- `HasSubtype` - a type inherits from a type.
- `HasTypeDefinition` - this node was built from that type. Not a hierarchical
  reference, and the main way a companion-specification client recognises a node
  (Chapter 23).
- `HasModellingRule` - how a variable of an object type is instantiated in real
  instances (`Mandatory`, `Optional`). Part of Chapter 21.

Browsing is the art of asking "what is connected to this node, in which direction, of
which type". Getting the direction wrong is the classic browsing mistake: from a child
you must follow the **inverse** of `HasComponent` to reach its parent.

### NodeIds

A NodeId is a pair: a **namespace index** and an **identifier**. The identifier has four
forms:

| Form | Text looks like | Typical use |
| --- | --- | --- |
| Numeric | `ns=0;i=2259` | Standard nodes (`ns=0`), which every server must provide |
| String | `ns=2;s=Temperature` | Vendor and project tags |
| GUID | `ns=2;g=...` | Nodes that must be unique across servers |
| Opaque | `ns=2;o=...` | Server-internal handles |

Two consequences:

1. `ns=0;i=2259` and `ns=2;i=2259` are **different nodes**. The identifier alone is
   never an address.
2. The index is assigned by the server. `ns=0` is always the standard namespace; every
   other index is this server's decision, in the order it registered namespaces. The
   specification also allows a fully-qualified NodeId that names the namespace by URI
   (`nsu=urn:opcua-tutorial:demo;s=Temperature`) instead of by index, and resolving
   that requires the server's namespace array - which is exactly what Chapter 13 reads
   before it uses an index.

Namespaces are listed by the server object (`Server → ServerCapabilities → NamespaceArray`
in a browser), and the array is a fact about *this* server at *this* moment.

## 3. Implementation

Two toolkit modules carry this chapter.

`src/opcua_toolkit/nodeids.py` normalises anything you might write down into a
canonical `asyncua.ua.NodeId`:

```python
from opcua_toolkit import to_nodeid, node_ids_equal

to_nodeid("ns=2;s=Temperature")  # canonical NodeId
to_nodeid("2259")  # ns=0, numeric identifier
node_ids_equal("ns=2;s=Temperature", ua.NodeId("Temperature", 2))
```

`src/opcua_toolkit/browse/` turns references into a structure your application can
hold - `NamespaceExplorer` (browse, browse next, walk, filter, resolve paths),
`NodeRef` (one discovered node), `BrowseTree` and `BrowseStats` (the walk and its
cost). It is Chapter 5.

The demo server's address space is what you browse while learning this:

```
Objects
└─ ProductionLine                  (Object)
   ├─ Line1                        (Object, HasComponent from ProductionLine)
   │  ├─ Temperature               (Variable, Double,  writable)
   │  ├─ Setpoint                  (Variable, Float,   writable)
   │  ├─ Pressure, MotorRpm, BatchProgress, RunState, RecipeName, LastSampledAt
   │  ├─ Vibration                 (Variable, Float,   read only)
   │  ├─ QualityScore_RO           (Variable, Byte,    read only)
   │  ├─ TemperatureHistory_RO     (Variable, Double,  read only)
   │  ├─ SampleArray               (Variable, Double[], ValueRank = 1)
   │  ├─ BatchTag001 … BatchTag024 (Variable, Int32,   writable)
   │  └─ StartLine, StopLine, AddValues, GetLineStatus, AcknowledgeAlarm (Methods)
   └─ Maintenance
      └─ MaintenanceMode, FirmwareVersion, CommissioningCompleted_RO
```

Every name above is a string NodeId in the tutorial namespace
(`urn:opcua-tutorial:demo`, `ns=2;s=Temperature` for a `--demo-port 48401` run), and
each variable is a **variable with `HasComponent`**, not a property - which is what a
plant server does and what makes it subscribable.

Tests: `tests/unit/test_nodeids.py`, `tests/unit/test_browse_model.py` and the live
`tests/integration/test_browse.py`.

## 4. Verification

Start the server, then look at the same address space with someone else's client:

```powershell
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48401 --path "ProductionLine/Line1"
uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48401 --node "ns=2;s=Temperature"
```

A browse path is relative to the node it starts at, and `browse --path` starts at the
Objects folder unless `--node` says otherwise. So the path names what is *under* that
folder: `ProductionLine/Line1`, not `Objects/ProductionLine`. Both spellings work (the
toolkit accepts a path that names the node it starts at, because that is how the address
space is drawn), but the first is the one OPC UA itself uses (OPC 10000-4 5.9.4).

In **UaExpert**: connect, open *Address Space*, and check that

- the Namespace Array shows `urn:opcua-tutorial:demo` at index 2;
- `Temperature` has NodeClass *Variable* and NodeId `ns=2;s=Temperature`;
- its BrowseName is `2:Temperature` and its DisplayName is `Temperature`;
- browsing from `Temperature` **inversely** reaches `Line1`, and its
  *TypeDefinition* is `BaseDataVariableType`;
- `Vibration` is the same class as `Temperature` - the class says nothing about access.

In **Prosys UA Simulation Server**, run the same check on its own simulation server
and notice that its tags are in a *different* namespace index. Same model, different
index: that is the lesson, and no chapter text is needed to learn it.

Details for both tools: [../VERIFICATION_TOOLING.md](../VERIFICATION_TOOLING.md).

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Looking up nodes by `DisplayName` | Works in English, fails on a server with a second language | Use `BrowseName`, and build browse paths |
| Hardcoding `ns=2` | Works against the demo server, fails on site | Chapter 13: read the namespace array first |
| Comparing identifiers without namespaces | Two different nodes look equal | `node_ids_equal` compares the pair |
| Browsing only forward references | A child node has no children, so the walk finds nothing | Follow the inverse to reach the parent |
| Filtering on `NodeClass` and calling it a permission check | A read-only variable is still a `Variable` | Chapter 6: read `AccessLevel` / `UserAccessLevel` |
| Treating a `Property` like a tag | Metadata gets subscribed to and monitored items multiply | Subscribe to components, read properties |
| Ignoring `UserWriteMask` | An attribute write fails although `AccessLevel` looked fine | Read both attributes, Chapter 6 |

## 6. What's next

Chapter 3 is about this repository rather than about OPC UA: how a chapter is written
so that it can be run, and how a claim here is checked. Chapter 4 then opens the first
connection and reports what the server actually is.
