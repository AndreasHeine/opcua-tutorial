# Chapter 16 - Author an address space, and read back what it promises

*Part 5 · status: built and tested · examples: `examples/ch16_authored_address_space.py` ·
tests: `tests/unit/test_address_space_model.py`, `tests/integration/test_address_space.py`*

## 1. Why this matters

Fifteen chapters read an address space somebody else wrote. Every node id in this repository so
far was discovered, every path was browsed, and every limit was asked about rather than assumed.
Part 5 changes sides: from here on, this repository **writes** an address space, and the gateway
this tutorial ends with will write one under a companion specification.

Writing it is the easy half. The hard half - and the reason this chapter exists - is that a type
is worth nothing to a client unless the client can find it. A server full of variables typed
`BaseDataVariableType` and `PropertyType` is perfectly legal, perfectly readable, and tells a
client only what each value is *made of*:

```text
  Objects folder: Objects: 353 variables, 0 with no type, 17 with a type that says more (95% typed only as a base type)
```

That is the tutorial's own demo server, measured before this chapter writes anything into it.
Zero variables have no type, and 95 % of them have a type that says nothing beyond "a variable"
or "a property". A client looking at `ns=2;s=Pump_01.SetPoint` can learn it is a `Float` with
`CurrentRead|CurrentWrite`. It cannot learn that it is *a setpoint of a pump*, which is the only
fact a scada tool, a historian or a companion-specification client is actually asking for.

So this chapter does two things, in that order:

1. **It authors a model** - an abstract `MachineType`, a `PumpType` under it, a `VariableType` for
   the setpoint, and instances - through a *client session*, the way a commissioning tool does it,
   not the way a script that happens to own the server process does it.
2. **It reads that model back from a session that knows nothing about it** - a second, anonymous,
   ordinary client - because that is the only version of a type contract that ever gets enforced:
   the one a stranger can find.

Everything the chapter then measures is a consequence of those two acts: which writes the toolkit
refuses before a request is sent, which writes the server refuses after it, and what a client can
see of a promise nobody told it about.

## 2. OPC UA theory

### The four things a node says about itself

A client can ask a node four questions, and they are answered by three different mechanisms -
which is why "what is this node?" is usually asked badly:

| Question | Answered by | How a client gets it |
| --- | --- | --- |
| Which node is it? | `NodeId` | the reference that pointed at it, or the path that resolved it |
| What is it called in a path? | `BrowseName` | the browse result; `TranslateBrowsePathsToNodeIds` matches on it |
| What is it called for a human? | `DisplayName` | a read, or the browse result |
| What *is* it? | `NodeClass` **plus** the `HasTypeDefinition` reference | a read for the class, a **browse** for the type |

The last row is the one that surprises people. There is no `TypeDefinition` attribute to read:

```text
ua.AttributeIds has no TypeDefinition: True
  MachineType HasSubtype PumpType: 0;0 nodeid_type=0
  MachineType HasComponent Temperature: 0;63 nodeid_type=0
  AssetModel child Pump_01: 2;'PumpType' nodeid_type=3
  Pump_01 HasComponent SetPoint: 2;'MachineSetPointType' nodeid_type=3
  Pump_01 HasComponent Temperature: 0;63 nodeid_type=0
```

Read from the tutorial server (the first line is asyncua's attribute enum, the rest are
`ReferenceDescription.TypeDefinition` values as the server filled them). The type of a node arrives
*inside the reference that points at it*, never as an attribute of the node itself.

`NodeClass` is a closed set, and it is one of the few places the specification says "no
extensions": "Each Node in the AddressSpace is an instance of one of these NodeClasses. No other
NodeClasses shall be used to define Nodes, and as a result, Clients and Servers are not allowed to
define NodeClasses or extend the definitions of these NodeClasses" (OPC 10000-3 4.4.2). asyncua
exposes exactly that set, plus its `Unspecified` zero used for browse filters:

```text
ua.NodeClass members:
  Unspecified = 0
  Object = 1
  Variable = 2
  Method = 4
  ObjectType = 8
  VariableType = 16
  ReferenceType = 32
  DataType = 64
  View = 128
  AssetModel: read_node_class -> Object (int 1)
  PumpType: read_node_class -> ObjectType (int 8)
  MachineType: read_node_class -> ObjectType (int 8)
  MachineSetPointType: read_node_class -> VariableType (int 16)
  Pump_01: read_node_class -> Object (int 1)
  Pump_01.Temperature: read_node_class -> Variable (int 2)
  Pump_01.AssetTag: read_node_class -> Variable (int 2)
```

Two things are worth noticing in that block. `Pump_01` and `AssetModel` are both `Object`: a folder
and a pump are the same NodeClass, so **the class never tells you whether something is a type or a
thing**. And `PumpType` is `ObjectType` while `MachineSetPointType` is `VariableType`: those are
the two NodeClasses this chapter writes.

It also explains a fix this chapter had to make in the browse side of the toolkit. Printing a node
class in four lowercase letters - which is what a compact browse tree does - gives `obje` for
`Object` **and** `obje` for `ObjectType`, and `vari` for `Variable` **and** `vari` for
`VariableType`. A tree that prints the same four letters for a type and for an instance of that
type is a tree that hides the one distinction Chapter 16 is about, so the two colliding classes are
now named apart (`objt`, `virt`) and a type node prints no type definition at all:

```text
MachineType [objt] ns=2;s=MachineType
  Temperature [vari] ns=2;s=MachineType.Temperature : ns=0;i=63
  Running [vari] ns=2;s=MachineType.Running : ns=0;i=63
  AssetTag [vari] ns=2;s=MachineType.AssetTag : ns=0;i=68
  PumpType [objt] ns=2;s=PumpType
browse stats: nodes=5 references=4 browse=1 next=0 errors=0
```

`MachineType` is a type, `PumpType` is a type under it, and the three lines between them are its
declarations - a type printed as itself, not as a variable. The `ns=0;i=0` that this server reports
for a type node's `TypeDefinition` (first block above) is now read as "no type definition", because
printing it as a NodeId makes a type look as if it has one, and a supertype is not a type
definition.

### A type is a promise: InstanceDeclaration and ModellingRule

An OPC UA type is not a list of fields. It is a set of *declarations*, each carrying a rule that
says what an instance must contain:

> An InstanceDeclaration is an Object, Variable or Method that references a ModellingRule with a
> HasModellingRule Reference and is the TargetNode of a hierarchical Reference from a
> TypeDefinitionNode or another InstanceDeclaration. There shall be no two TypeDefinitionNodes
> referencing the same InstanceDeclaration with a hierarchical Reference, either directly or from
> another InstanceDeclaration of that TypeDefinitionNode, i.e. an InstanceDeclaration belongs to
> exactly one TypeDefinitionNode. (OPC 10000-3 6.2.1)

Two consequences drive everything in this chapter:

- **The declaration is a node.** It lives in the address space, under the type, and can be browsed,
  read, and counted. Every `decl …` line this chapter prints is the server describing a real node.
- **The rule is metadata on that node**, not a keyword in a programming language. A client with no
  source code can read it.

The two rules this toolkit authors are the two the specification states normatively:

> An InstanceDeclaration marked with the ModellingRule Mandatory means that for each existing
> BrowsePath on the instance a similar Node shall exist, but it is not defined whether a new Node
> is created or an existing Node is referenced. (OPC 10000-3 6.4.4.4.1)

> An InstanceDeclaration marked with the ModellingRule Optional means that for each existing
> BrowsePath on the instance a similar Node may exist, but it is not defined whether a new Node is
> created or an existing Node is referenced. (OPC 10000-3 6.4.4.4.2)

*Shall* and *may* - and neither one says whether the instance is a new node or a shared one. That
is why `InstanceReport` compares **BrowsePaths on the instance** against **the names the type
declares**, and prints the difference instead of asserting that a server instantiated a model
"correctly".

The other two standard rules (`MandatoryPlaceholder`, `OptionalPlaceholder`) are read and reported,
and refused at authoring time by `ComponentSpec`: a placeholder means "an instance may add this
later", and an author that creates the node immediately contradicts the rule it just wrote.

### Subtyping: what a subtype inherits, and what it may change

Declarations are inherited, and inheritance is visible in the report:

```text
   decl Temperature: component rule=mandatory data=Double type=BaseDataVariableType (base type) access=0x03 sampling=100 ms inherited from MachineType
```

A subtype does not copy its supertype's declarations into its own namespace; it inherits them, and
an instance of the subtype is checked against the *fully inherited* set. The toolkit marks the
inherited ones, because "which of these seven parts did this type actually declare?" is a question a
reviewer needs answered before a model is changed - deleting a declaration from a supertype changes
every subtype under it.

What a subtype may *change* is also specified, and the rule is asymmetric (OPC 10000-3 6.4.4.2,
Table 21): a `Mandatory` on a supertype may only stay `Mandatory` on a subtype; an `Optional` may
become `Mandatory` or stay `Optional`. `OptionalPlaceholder` may become `MandatoryPlaceholder`. In
one direction only: **a subtype may tighten a promise, never loosen a mandatory one**. A model that
lets a subtype drop a mandatory part of its supertype would make every client that read the
supertype wrong about every instance of the subtype.

`HasSubtype` is the other half of the picture, and its asymmetry is measured in the CLI block
above: browsing `MachineType` shows `PumpType`, and the server also reports the inverse, because
"is a subtype of" is one fact stored in two directions.

### HasTypeDefinition: the reference that says what a node *is*

> OPC UA Servers shall provide type definitions for Objects and Variables. The HasTypeDefinition
> Reference shall be used to link an instance with its type definition represented by a
> TypeDefinitionNode. (OPC 10000-3 4.6.1)

and, for each NodeClass (OPC 10000-3 5.5.1 and 5.6.2): each `Object` is the SourceNode of exactly
one `HasTypeDefinition` pointing at an `ObjectType`, each `Variable` exactly one pointing at a
`VariableType`. An instance's type is not a hint and not a name convention: it is one required
reference, and OPC 10000-3 6.4.3 requires it to keep pointing at the same type.

That is what the CLI prints as the `: ns=…` suffix:

```text
AssetModel [obje] ns=2;s=AssetModel
  Pump_01 [obje] ns=2;s=Pump_01 : ns=2;s=PumpType
    Pressure [vari] ns=2;s=Pump_01.Pressure : ns=0;i=63
    Vibration [vari] ns=2;s=Pump_01.Vibration : ns=0;i=63
    SetPoint [vari] ns=2;s=Pump_01.SetPoint : ns=2;s=MachineSetPointType
    MaintenanceNote [vari] ns=2;s=Pump_01.MaintenanceNote : ns=0;i=68
    …
browse stats: nodes=17 references=16 browse=2 next=0 errors=0
```

`AssetModel` is a folder - an `Object` - and it is the *root* of that printed tree, which is why it
carries no `: …` suffix while its children do. The reason is the same fact again: a node's type
arrives inside the reference that pointed at it, and the starting node of a tree has no such
reference in hand. Point the same command at the folder above it - `browse --node ns=0;i=85
--depth 1`, with its four-line `namespaces:` header left out - and the suffix appears:

```text
Objects [obje] ns=0;i=85
  Locations [obje] ns=0;i=31915 : ns=0;i=61
  Server [obje] ns=0;i=2253 : ns=0;i=2004
  Aliases [obje] ns=0;i=23470 : ns=0;i=23456
  ProductionLine [obje] ns=2;s=ProductionLine : ns=0;i=61
  AssetModel [obje] ns=2;s=AssetModel : ns=0;i=61
browse stats: nodes=6 references=5 browse=1 next=0 errors=0
```

`ns=0;i=61` is `FolderType`, and the same `AssetModel` node that printed without a suffix one
command earlier prints with one here. Nothing about the node changed; what changed is whether a
reference reached it. A client that reads "no type shown" as "no type" is reading its own tool's
starting point instead of the server - and that is precisely the mistake the `ns=0;i=0` case above
exists to prevent.

`Pump_01` names its type. Six of its seven parts name `BaseDataVariableType` or `PropertyType`:
the two types that say nothing. The seventh, `SetPoint`, names `ns=2;s=MachineSetPointType`, the
type this tutorial wrote.

### Abstract types: a type you may not instance is still useful

> TRUE it is an abstract ObjectType, i.e. no Objects of this type shall exist, only Objects of its
> subtypes. (OPC 10000-3 5.5.2, Table 12)

`MachineType` in this tutorial is abstract. It holds the declarations every machine shares -
`Temperature`, `Running`, `AssetTag` - and no instance of it may exist. A client that finds
`MachineType` and tries to browse "its instances" finds nothing, and that is the model working as
designed: the shared part of a hierarchy has no existence of its own.

The toolkit refuses to create such an instance *before* the server is asked, and the refusal quotes
the clause it is enforcing:

```text
  instantiate MachineType, which is abstract refused: AddressSpaceError: MachineType (ns=2;s=MachineType) is abstract: OPC 10000-3 5.5.2 allows Objects of its subtypes only, so this instance was refused before the server was asked
```

The reason to refuse locally is not politeness. An abstract type is a *type-level* fact: the
address space would happily accept an `AddNodes` call for it, and the resulting model would be one
that every conformant client could reject. A toolkit that knows the rule and does not apply it
produces servers that are wrong in a way only a third party will discover.

### VariableType: giving a component a type of its own

A `VariableType` is where a Variable's *meaning* lives. Its defining attributes (OPC 10000-3 5.6.5,
Table 14) are `DataType` (required, and required to be a valid `DataType` node), `ValueRank`
(required), `ArrayDimensions`, `IsAbstract`, and a default `Value`; it subtype-references through
`HasSubtype`, and it may declare its own `HasProperty` / `HasComponent` children.

Two facts matter here:

- `BaseDataVariableType` and `PropertyType` are `VariableType` nodes too - they are just the two the
  standard supplies, and they carry no meaning beyond "a data variable" and "a property".
- A component's type is *separable* from its data type. `SetPoint` is a `Float` (that is its
  `DataType`) and a `MachineSetPointType` (that is its type definition). The second is the fact that
  survives a data type change.

Measured on this server, the tutorial's own `VariableType` sits in the standard hierarchy next to
the standard's own variable types - one Browse:

```text
BaseDataVariableType [virt] ns=0;i=63
  DataTypeDescriptionType [virt] ns=0;i=69
  DataTypeDictionaryType [virt] ns=0;i=72
  …
  PubSubDiagnosticsCounterType [virt] ns=0;i=19725
  ReferenceDescriptionVariableType [virt] ns=0;i=32657
  MachineSetPointType [virt] ns=2;s=MachineSetPointType
browse stats: nodes=36 references=35 browse=1 next=0 errors=0
```

A type you wrote is *not* kept in a separate corner of the address space. It hangs under the same
`BaseDataVariableType` as the standard's own types, and a client that browses subtypes finds it
there. That is what makes a companion-specification client possible (Part 6): the specification's
types are reachable through the standard hierarchy, not through a document.

asyncua's server, however, does not create a component with its own `VariableType` in one step: it
creates the declaration as a `BaseDataVariableType` and the toolkit then repoints the reference.
The report says so out loud rather than hiding it:

```text
   note: ns=2;s=FanType.Speed: the declaration was typed BaseDataVariableType; that reference now points at FanSpeedType (ns=2;s=FanSpeedType)
   note: ns=2;s=FanType.Speed: HasComponent was removed and added again so that ns=2;s=FanType also reports FanSpeedType for this declaration
```

Two `AddNodes`/`AddReferences` calls, then a `DeleteReferences`-and-`AddReferences` pair, because
the *browse result* - what a client actually sees - carries the type from the reference, and a
stale reference means every client reads the wrong type. That is the difference between "the node
has the right attributes" and "the address space says the right thing", and only the second one is
observable by a client.

### AccessLevel and UserAccessLevel: the write permission a client can see

`AccessLevel` and `UserAccessLevel` are the only write permission a Variable publishes. Their bit
definitions (OPC 10000-3 8.57) are bit 0 `CurrentRead` - "Indicates if the current value of the
Variable is available" - and bit 1 `CurrentWrite` - "Indicates if the current value is writeable".
So `0x01` is read-only, `0x03` is read and write, and the difference is one bit that decides
whether a client's write request has any chance.

Measured on the authored nodes, before this chapter's example writes a single value:

```text
  MachineType.Temperature: AccessLevel=0x03 UserAccessLevel=0x03 WriteMask=0x00 UserWriteMask=0x00 MinimumSamplingInterval=0x64
  MachineType.AssetTag: AccessLevel=0x01 UserAccessLevel=0x01 WriteMask=0x00 UserWriteMask=0x00 MinimumSamplingInterval=0x00
  PumpType.SetPoint: AccessLevel=0x03 UserAccessLevel=0x03 WriteMask=0x00 UserWriteMask=0x00 MinimumSamplingInterval=0xFA
  PumpType.Vibration: AccessLevel=0x01 UserAccessLevel=0x01 WriteMask=0x00 UserWriteMask=0x00 MinimumSamplingInterval=0x00
```

Declarations carry `AccessLevel` too, and an instance inherits it: `Pump_01.SetPoint` reports the
same `0x03` and the same `MinimumSamplingInterval` as `PumpType.SetPoint`, because the author wrote
the promise once, on the type.

`WriteMask` and `UserWriteMask` are a different thing entirely, and mixing them up is one of the
most common mistakes in this area. They are a *bit mask of which attributes* may be written
(OPC 10000-3 8.60, Table 44), not a permission on the value, and `UserWriteMask` "can only further
restrict the WriteMask Attribute" (OPC 10000-3 5.2.8). Both are `0x00` on every node this chapter
authors - which is what the demo server reports for every node it authors, and what a *server that
does not implement attribute writes* reports as well. `WriteMask = 0x00` therefore tells you
nothing about whether `Value` is writable: for that question, only `AccessLevel` and
`UserAccessLevel` answer.

### MinimumSamplingInterval: the one sampling promise a server publishes

Chapter 10 to Chapter 12 measured sampling from the *client* side: a requested interval, a revised
interval, and sometimes no answer at all. On the Variable side the specification defines the
attribute a server publishes instead (OPC 10000-3 5.6.2, Table 13):

> A MinimumSamplingInterval of 0 indicates that the Server is to monitor the item continuously. A
> MinimumSamplingInterval of -1 means indeterminate.

`0` is not "no sampling" and it is not "unsampled" - it is *continuous*. `0x64` in the block above
is 100 ms, `0xFA` is 250 ms, and `0x00` is continuous. The toolkit renders the three cases in the
specification's own words, because a report that prints `sampling=0 ms` invites a reader to
conclude that nothing is sampled:

```text
      part SetPoint: ns=2;s=Pump_01.SetPoint component type=ns=2;s=MachineSetPointType data=Float access=0x03 sampling=250 ms value=60.0
      part Pressure: ns=2;s=Pump_01.Pressure component type=BaseDataVariableType data=Double access=0x03 sampling=continuous (0 ms) value=6.4
```

A `MinimumSamplingInterval` is a promise about the *server's* willingness to sample, and it is the
server's number, not the client's request. A monitored item still gets its own revised sampling
interval (Chapter 10); what this attribute adds is the server's own floor, published in the address
space where any client can read it before it ever creates a subscription.

### What this server actually enforces (measured, not assumed)

Everything above is a promise. Enforcement is a server decision, and asyncua's is small, specific,
and worth knowing exactly. Its address-space service (`asyncua/server/address_space.py`) does this
for a session whose user role is not `Admin`:

- a `Write` of any attribute except `Value` is refused outright;
- a `Write` of `Value` additionally requires the `CurrentWrite` bit in **both** `AccessLevel` and
  `UserAccessLevel`;
- `AddNodes`, `AddReferences`, `DeleteReferences` and `DeleteNodes` are refused before anything
  else is examined - the role check in `AddNodes` even runs before the duplicate-NodeId check, so
  an unauthorised session sees a permission problem where an authorised one would see
  `BadNodeIdExists`.

Measured, against the authored model, from a session with no user at all:

```text
  anonymous AddNodes refused: AddressSpaceError: the server refused to create AnonType at ns=2;s=AnonType: User does not have permission to perform the requested operation.(BadUserAccessDenied)
  anonymous add_object refused: BadUserAccessDenied: User does not have permission to perform the requested operation.(BadUserAccessDenied)

  -- writes against authored AccessLevel --
  write Pump_01.AssetTag refused: User does not have permission to perform the requested operation.(BadUserAccessDenied)
  write Pump_01.Temperature: ACCEPTED (61.5 -> 99.0)
  write Pump_01.Running refused: User does not have permission to perform the requested operation.(BadUserAccessDenied)
  write Pump_01.Vibration refused: User does not have permission to perform the requested operation.(BadUserAccessDenied)
  write Pump_01.SetPoint refused: The value supplied for the attribute is not of the same type as the attribute"s value.(BadTypeMismatch)
  anonymous write AccessLevel refused: User does not have permission to perform the requested operation.(BadUserAccessDenied)
  admin write AssetTag (read-only by contract): ACCEPTED: 'changed by admin'
  admin write on a missing node: BadNodeIdUnknown: The node id refers to a node that does not exist in the server address space.(BadNodeIdUnknown)
```

Read that block as a whole and three rules fall out:

1. **An author needs a role.** Not just for `AddNodes`: because `AddReferences` and
   `DeleteReferences` are Admin-only in this server, an author cannot even hang its own
   declarations. Any real authoring - Part 6's companion-specification gateway included - needs a
   user the server trusts, which is why this chapter's authoring half runs on a session with a
   username and the reading half runs on a session without one.
2. **A read-only contract is a bit, not a type.** `AssetTag`, `Running` and `Vibration` are refused
   for the anonymous session and accepted for an `Admin` one. The contract is enforced *through the
   user's role*, and an Admin is not bound by it.
3. **A write can be refused twice for different reasons.** `Pump_01.SetPoint` was refused with
   `BadTypeMismatch` even though it is writable: the probe offered a `Double` where the variable
   declares a `Float`. Permission first, wire type second - and the toolkit's own
   `write_component_value` exists so that the second refusal is never the interesting one.

`BadTypeMismatch` is a wire-type refusal, and the server logs it in the same words:

```text
2026-11-20 06:55:52 | WARNING  | asyncua.server.address_space | Write refused: Variant: Variant(Value=2000, VariantType=<VariantType.Int64: 8>, Dimensions=None, is_array=False) with type 8 does not have expected type: 6
2026-11-20 06:55:52 | WARNING  | asyncua.server.address_space | Write refused: Variant: Variant(Value=61.0, VariantType=<VariantType.Double: 11>, Dimensions=None, is_array=False) with type 11 does not have expected type: 10
```

Python has one `int` and one `float`. OPC UA has `Int16`, `Int32`, `Int64`, `Float`, `Double` (and
variant type numbers 6 and 8, 10 and 11 in those lines). A Python `int` literal of 2000 becomes an
`Int64`, a Python `61.0` becomes a `Double`, and both are refused by a server that declared `Int32`
and `Float`. Chapter 8 measured this from the client side; here it is measured against a model this
repository wrote itself, which is the situation where it actually bites.

## 3. Implementation

Three modules, one deliberate split: **what a model says**, **how it is read**, **how it is
written**. Nothing in the model module talks to a server; nothing in the reader module writes;
nothing in the author module guesses a name.

### `src/opcua_toolkit/server/model.py` - the vocabulary

Pure data. No client, no node, no I/O - and that is why all 64 of this chapter's unit tests run
without a server: they are exactly this module's rules, checked with no connection anywhere in the
file.

| Name | What it is |
| --- | --- |
| `ObjectTypeSpec` | one Object type to author: name, supertype, description, components, properties, `abstract` |
| `VariableTypeSpec` | one VariableType to author, hanging under a component or another VariableType |
| `ComponentSpec` | one declaration: name, data type, initial value, `VariableAccess`, `ModellingRule`, optional `VariableTypeSpec` |
| `InstanceSpec` | one instance to create: name, type name, `include_optional`, initial `values` |
| `TypeReport` | what the server says one Object type declares: declarations, inherited flag, its own `VariableType` nodes, notes |
| `InstanceReport` | what an instance actually has, checked against what its type declares: `missing_names`, `extra_names`, `optional_absent`, `conforms_to_type` |
| `DeclarationReport` / `InstanceComponent` | one declaration on the type / one component on the instance, with `access_mask`, `min_sampling_interval`, `data_type`, `type_definition_name`, and the component's `value` |
| `TypeCoverage` | a subtree counted by type definition: how many variables, how many with no type, how many typed only as a base type |
| `ModellingRule`, `VariableAccess` | the two vocabularies the spec uses, modelled once |

Two rules are deliberately singletons in this module, because a chapter that reports "95 % typed
only as a base type" has to be able to say which types it means:

- `BASE_TYPE_DEFINITION_IDS` - `BaseObjectType`, `BaseVariableType`, `BaseDataVariableType`,
  `PropertyType` and their `NodeId`s - is the one place the list exists. `TypeCoverage`,
  `TypeReport.specialised_variable_types` and the `<- says nothing about the node` mark all read
  it. A second copy of that list would be a number that could drift away from the label printed
  beside it.
- `type_definition_name` turns a namespace-0 `NodeId` into the name the standard gives it through a
  reverse index over the attributes of `ua.ObjectIds`, built once by `_namespace0_names` and cached
  with `@lru_cache`, so `ns=0;i=63` prints as `BaseDataVariableType` and `ns=0;i=19725` as
  `PubSubDiagnosticsCounterType` instead of as numbers a reader has to look up. The index skips
  dunder names for a reason that was measured before writing it: on Python 3.13 every class body
  carries `__firstlineno__`, and its value here is `4` - the same integer as `ua.ObjectIds.Int16`. A
  reverse table built without skipping those names would name a data type after a line number, so
  `test_type_definition_name_names_a_type_this_module_was_never_told_about` pins both halves of that
  fact, and `test_the_base_types_a_report_calls_base_types_are_the_four_the_specification_gives`
  pins the list above.

Every node id this module prints goes through `format_node_id`, which is why every line in this
chapter reads `ns=2;s=PumpType` and none of them reads the Python repr of a node id object. The
second is a debugging view of an object; the first is the same node as the address space names it,
and it is the form UaExpert, Prosys and a companion specification use.

### `src/opcua_toolkit/server/reader.py` - reading what a server says about itself

`AddressSpaceReader` takes a `Client`, a `Server` or a `Node`, anchors at the Objects folder, and
answers six questions:

| Method | Question it answers |
| --- | --- |
| `read_type(...)` | what does this Object type declare, including the parts it inherited, and which `VariableType` nodes are its own |
| `describe_instance(...)` | what does this instance actually have, checked against the type its own `HasTypeDefinition` names - or against `expected_type`, a type you supply |
| `find_type(...)` | is there an Object type with this BrowseName, found by walking `HasSubtype` from `BaseObjectType` |
| `variable_type_report(...)` | what does one `VariableType` say: `DataType`, `ValueRank`, its supertype |
| `components_of(...)` | the components and properties one instance carries, with their type, data type, access mask and sampling promise |
| `coverage(...)` | how much of a subtree is typed with something that means something |

`find_type` deserves its own paragraph because it is the answer to the most common authoring
mistake in the wild: **looking a type up by `DisplayName`**. It matches `BrowseName` exactly,
walks `HasSubtype` breadth-first up to `DEFAULT_TYPE_SEARCH_BROWSERS` (400 by default, and the
example's failure prints how deep it actually got: `searched 283 type node(s)`), and when a name
fails it names the near misses - `machinetype` is refused with `MachineType at ns=2;s=MachineType`
in the error text. A lookup that returns "not found" when the address space is holding the node
under a different capitalisation is a lookup that teaches the reader to give up on browsing.

`coverage` is the honest version of "how good is this model". It counts variables from the browse
results, so it costs one Browse per node and no per-node reads, and it separates three facts that
are routinely confused: *no type at all*, *a type that says more than the base*, and *a type that
says nothing*.

### `src/opcua_toolkit/server/authoring.py` - writing it, through a session

`AddressSpaceAuthor` is pointed at a node - and in Chapter 16 that node comes from a **client**, so
every node the chapter creates travels through `AddNodes` and `AddReferences` over a session, the
way a commissioning tool's do. The same class pointed at the in-process `Server` writes directly;
both paths are the same code, and that is the point: a model this repository can author as a client
is a model a plant's own engineering client could author.

| Method | What it does |
| --- | --- |
| `author_object_type(...)` | one type: its `VariableType` nodes first, then the type, then its declarations, then `HasModellingRule` on each |
| `author_model(...)` | a list of types, idempotently, returning one report per type with `reused` set when the node was already there |
| `instantiate(...)` | one instance: mandatory parts always, optional parts when asked for, initial values validated first |
| `instantiate_all(...)` | a list of instances under one parent, one report each |
| `check_instance(...)` | an existing node checked against a type, without creating or writing anything |
| `write_component_value(...)` | one value write that reads the component's `DataType` and writes with the correct wire type |
| `type_of(...)` | the report for a type this author knows - by name, by an earlier report, or by node id |
| `authored_types` | the types this author created or reused in this session, keyed by BrowseName |

Four decisions in this module are worth naming, because each is a refusal of something easier:

**Values are checked against the declarations before the server is asked.** `_check_value_names`
compares an `InstanceSpec`'s `values` with the type's fully inherited declarations, so a value for
a part the instance will not have is refused locally. The measured consequence is that `Fan_03`
never exists at all:

```text
  instantiate Fan_03 with a value for a part it would not have refused: AddressSpaceError: Fan_03 would be an instance of FanType, which gives an instance AssetTag, Running, Speed, Temperature: Vendor is not one of them; Vendor is declared optional and this instance was asked to leave optional parts out
  Fan_03 in the address space: BadNodeIdUnknown
```

A half-written instance is the worst outcome an authoring tool can produce: it is invisible until a
client trips over it, and it is usually invisible to the person who created it. This author's rule
is that a partial model never reaches the server.

**`write_component_value` reads the declaration, then writes.** It asks the component what its
`DataType` is, builds the `Variant` with that wire type, and refuses locally when `AccessLevel` does
not carry `CurrentWrite` - with an escape hatch (`enforce_access_level=False`) for the case where
the caller really is an Admin and really does want to write a read-only node:

```text
  write Fan_01.Vendor (declared read only) refused: AddressSpaceError: Vendor has AccessLevel 0x01, which does not include CurrentWrite: this author declared it read-only, and asyncua's server answers a non-Admin write of such a variable with BadUserAccessDenied. Pass enforce_access_level=False to write it as an Admin.
```

**Re-authoring reuses instead of failing.** `asyncua`'s server answers a duplicate `AddNodes` with
`BadNodeIdExists`, and a commissioning script that fails on its second run is a script that cannot
be re-run. The author looks first, reuses what exists, and says so:

```text
  FanType reused=True
     note: reused the ObjectType FanType already at ns=2;s=FanType instead of adding it again, because asyncua's server answers a duplicate NodeId with BadNodeIdExists
     note: Speed: the declaration already existed at ns=2;s=FanType.Speed
     note: FaultCount: the declaration already existed at ns=2;s=FanType.FaultCount
     note: Vendor: the declaration already existed at ns=2;s=FanType.Vendor
  note: ns=2;s=AssetModel already had a child named Fan_01 at ns=2;s=Fan_01; this author used that node instead of adding a second one
```

**Authoring through a session needs a role, and the example says which shortcut it uses.** The demo
server hands `UserRole.Admin` to the username `admin` and ignores the password. That is this
tutorial's shortcut, and Chapter 25 replaces it with real user management; until then it is written
in the open, in a comment, in the example that uses it.

### The demo server's authored model is opt-in, and off by default

`DemoServerConfig.authored_model` (the `--authored-model` flag of `opcua-tutorial-server`) adds
`MachineType`, `PumpType`, `MachineSetPointType` and two pumps under an `AssetModel` folder:

```text
2026-11-20 06:56:35 | INFO     | opcua_toolkit.server.authoring | authored MachineType (ns=2;s=MachineType) in namespace 2: type MachineType (ns=2;s=MachineType) abstract, no instances: 3 declarations, 3 mandatory, 0 VariableTypes of their own
2026-11-20 06:56:35 | INFO     | opcua_toolkit.server.authoring | authored PumpType (ns=2;s=PumpType) in namespace 2: type PumpType (ns=2;s=PumpType) instantiable: 7 declarations (3 inherited), 5 mandatory, 1 VariableType of their own
…
demo server ready on opc.tcp://127.0.0.1:48456
namespace 2 = urn:opcua-tutorial:demo
tags: 39 methods: 5
authored model: 2 types MachineType, PumpType in ns=2;s=AssetModel
```

Off by default on purpose. Chapter 4 to Chapter 15 quote measurements taken against a demo server
whose variables are *all* base-typed - a queue cap of 100, `MaxNodesPerRead=10`, 24 batch tags,
`MinimumSupportedSampleRate=50 ms`. Authoring types into that server by default would change the
address space those chapters measured, and a tutorial whose earlier numbers silently stop being
true is worse than a tutorial with an extra flag to remember.

### What the example actually prints

One run of `examples/ch16_authored_address_space.py --demo --demo-port 48455`, simulator off, so
every value printed is a value the server authored and not one a running plant moved. stdout
first; the toolkit's and asyncua's own log lines from the same run are quoted separately below.
What is shortened, and nothing else:

- in the section 1 block, the eleven rows inside `…` are the remaining `1 variable(s) typed …`
  rows of that coverage report, and in the section 12 block the seven rows inside `…` are the same
  kind of single-count rows;
- in the section 5 block, the two `…` lines stand for the type report's seven declaration lines and
  the instance report's six part lines, which are the same words the same two reports printed in
  sections 3 and 4;
- in the demo-server start block, the `…` stands for the two `instance Pump_…` lines the author
  logged, asyncua's two endpoint warnings and the toolkit's own `demo server listening on …` line;
- the run's logging block quotes one pair of the two `sending service fault response` /
  `ServiceFault … in response to AddNodesRequest` pairs that the run produced, one pair per refused
  `AddNodes`;
- in the `--endpoint` block in section 4, the `…` stands for that run's eleven `1 variable(s) typed
  …` rows, and the block quotes the two headings with their `-----` rules exactly as printed;
- the browse tree quoted in section 2 and the one in section 4 are the same
  `browse --node ns=2;s=AssetModel --depth 2` result: section 2 leaves out three of `Pump_01`'s seven
  component rows and all eight rows of `Pump_02` and its parts, section 4 leaves out only the seven
  component rows under `Pump_02`;
- every CLI block in this page leaves out the four-line `namespaces:` header the CLI prints before a
  browse or inspect result, except where it is quoted, and each `inspect` table leaves out the
  per-node `summary_line()` the same command prints underneath it;
- the four probe blocks in section 2 came from three short measurement scripts run against a
  demo server started with `--authored-model`, and each quotes a run of the probe as printed:
  the attribute/reference probe (whose eleven rows are quoted as six), the `ua.NodeClass` enum and
  `read_node_class` probe (whose extra lines are asyncua's own endpoint and session-timeout
  warnings), the `AccessLevel`/`UserAccessLevel`/`WriteMask`/`UserWriteMask`/
  `MinimumSamplingInterval` probe (whose rows for `MachineType.AssetTag` and `PumpType.Vibration`
  are quoted, along with `BatchTag001`'s all-zero row mentioned in the text), and the attribute-read
  probe whose `BadAttributeIdInvalid` traceback is quoted as its last line;
- nothing else: every other line below is quoted as printed, including the tool's own framing of its
  refusals and asyncua's `attribute"s` spelling, which is what asyncua's message text contains.

Section 1, before anything is written:

```text
1. The namespaces, and the model folder the server already has
--------------------------------------------------------------
  ns=0: http://opcfoundation.org/UA/
  ns=1: urn:freeopcua:python:server
  ns=2: urn:opcua-tutorial:demo
  MaxNodesPerRead=10
  MaxNodesPerMethodCall=5
  Objects folder: Objects: 353 variables, 0 with no type, 17 with a type that says more (95% typed only as a base type)
      259 variable(s) typed PropertyType  <- says nothing about the node
      77 variable(s) typed BaseDataVariableType  <- says nothing about the node
      8 variable(s) typed PubSubDiagnosticsCounterType
      2 variable(s) typed ns=2;s=MachineSetPointType
      1 variable(s) typed ServerStatusType
      …
  AssetModel is ns=2;s=AssetModel
```

Section 2 reads a model the example did not author - the demo server's own pumps - the way a client
reads any model:

```text
  instance Pump_01 (ns=2;s=Pump_01) typed as ns=2;s=PumpType: 7 of 7 declared parts, missing [], extra [], conformant
     part Pressure: ns=2;s=Pump_01.Pressure component type=BaseDataVariableType data=Double access=0x03 sampling=continuous (0 ms) value=6.4
     part Vibration: ns=2;s=Pump_01.Vibration component type=BaseDataVariableType data=Float access=0x01 sampling=continuous (0 ms) value=1.2000000476837158
     part SetPoint: ns=2;s=Pump_01.SetPoint component type=ns=2;s=MachineSetPointType data=Float access=0x03 sampling=250 ms value=60.0
     part MaintenanceNote: ns=2;s=Pump_01.MaintenanceNote property type=PropertyType data=String access=0x01 sampling=continuous (0 ms) value='bearing check due 2026-12-01'
     part Temperature: ns=2;s=Pump_01.Temperature component type=BaseDataVariableType data=Double access=0x03 sampling=100 ms value=61.5
     part Running: ns=2;s=Pump_01.Running component type=BaseDataVariableType data=Boolean access=0x01 sampling=continuous (0 ms) value=True
     part AssetTag: ns=2;s=Pump_01.AssetTag property type=PropertyType data=String access=0x01 sampling=continuous (0 ms) value='PUMP-01'
  type PumpType (ns=2;s=PumpType) instantiable: 7 declarations (3 inherited), 5 mandatory, 1 VariableType of their own
     decl Pressure: component rule=mandatory data=Double type=BaseDataVariableType (base type) access=0x03 sampling=continuous (0 ms)
     decl Vibration: component rule=optional data=Float type=BaseDataVariableType (base type) access=0x01 sampling=continuous (0 ms)
     decl SetPoint: component rule=mandatory data=Float type=ns=2;s=MachineSetPointType access=0x03 sampling=250 ms
     decl MaintenanceNote: property rule=optional data=String type=PropertyType (base type) access=0x01 sampling=continuous (0 ms)
     decl Temperature: component rule=mandatory data=Double type=BaseDataVariableType (base type) access=0x03 sampling=100 ms inherited from MachineType
     decl Running: component rule=mandatory data=Boolean type=BaseDataVariableType (base type) access=0x01 sampling=continuous (0 ms) inherited from MachineType
     decl AssetTag: property rule=mandatory data=String type=PropertyType (base type) access=0x01 sampling=continuous (0 ms) inherited from MachineType
     variable type: MachineSetPointType (ns=2;s=MachineSetPointType) subtype of BaseDataVariableType: DataType=Float ValueRank=-1
```

The same model read twice is the point: the instance report is what the server has *now*, the type
report is what the server *promised*, and `7 of 7 … conformant` is the comparison, not an
assumption. Then the same section asks for a type by name, three times, and prints the refusals in
full:

```text
  find_type('MachineType') -> ns=2;s=MachineType
  find_type('machinetype') refused: no ObjectType named 'machinetype' below BaseObjectType: searched 283 type node(s) near miss: MachineType at ns=2;s=MachineType. Pass the type's NodeId if it lives where a subtype walk does not go.
  find_type('NoSuchType') refused: no ObjectType named 'NoSuchType' below BaseObjectType: searched 283 type node(s). Pass the type's NodeId if it lives where a subtype walk does not go.
```

Section 3 authors `FanType` over a client session, and section 4 instantiates it twice - once with
its optional parts, once without:

```text
  type FanType (ns=2;s=FanType) instantiable: 6 declarations (3 inherited), 4 mandatory, 1 VariableType of their own
     decl Speed: component rule=mandatory data=Int32 type=ns=2;s=FanSpeedType access=0x03 sampling=continuous (0 ms)
     decl FaultCount: component rule=optional data=UInt32 type=BaseDataVariableType (base type) access=0x01 sampling=continuous (0 ms)
     decl Vendor: property rule=optional data=String type=PropertyType (base type) access=0x01 sampling=continuous (0 ms)
     decl Temperature: component rule=mandatory data=Double type=BaseDataVariableType (base type) access=0x03 sampling=100 ms inherited from MachineType
     …
     variable type: FanSpeedType (ns=2;s=FanSpeedType) subtype of BaseDataVariableType: DataType=Int32 ValueRank=-1
  authored types this session knows: FanType
  instance Fan_01 (ns=2;s=Fan_01) typed as ns=2;s=FanType: 6 of 6 declared parts, missing [], extra [], conformant
     note: instantiated Fan_01 from FanType: 4 mandatory, 2 optional
     part Speed: ns=2;s=Fan_01.Speed component type=ns=2;s=FanSpeedType data=Int32 access=0x03 sampling=continuous (0 ms) value=1800
     part FaultCount: ns=2;s=Fan_01.FaultCount component type=BaseDataVariableType data=UInt32 access=0x01 sampling=continuous (0 ms) value=0
     part Vendor: ns=2;s=Fan_01.Vendor property type=PropertyType data=String access=0x01 sampling=continuous (0 ms) value='unknown'
     …
  instance Fan_02 (ns=2;s=Fan_02) typed as ns=2;s=FanType: 4 of 6 declared parts, missing [], extra [], conformant
     note: instantiated Fan_02 from FanType: 4 mandatory, 0 optional
     part Speed: ns=2;s=Fan_02.Speed component type=ns=2;s=FanSpeedType data=Int32 access=0x03 sampling=continuous (0 ms) value=900
     part Temperature: ns=2;s=Fan_02.Temperature component type=BaseDataVariableType data=Double access=0x03 sampling=100 ms value=21.5
     part Running: ns=2;s=Fan_02.Running component type=BaseDataVariableType data=Boolean access=0x01 sampling=continuous (0 ms) value=False
     part AssetTag: ns=2;s=Fan_02.AssetTag property type=PropertyType data=String access=0x01 sampling=continuous (0 ms) value='UNLABELLED'
  Fan_01 carries 6 parts, Fan_02 carries 4, from 6 the type declares
```

Two instances of one type, and both conformant: `4 of 6` is not drift, it is `Optional` doing what
OPC 10000-3 6.4.4.4.2 says it may do. The instance with its optional parts left out has exactly the
four mandatory parts and nothing else, and the report can say that because it compared the
instance against the inherited declarations rather than against what the code asked for.

Section 5 is the one that makes the rest of the chapter meaningful: the same model, read through a
second session that never authored anything and has no user at all.

```text
  type FanType (ns=2;s=FanType) instantiable: 6 declarations (3 inherited), 4 mandatory, 1 VariableType of their own
     …
  instance Fan_01 (ns=2;s=Fan_01) typed as ns=2;s=FanType: 6 of 6 declared parts, missing [], extra [], conformant
     …
```

The `…` lines are the seven declarations and the six parts, and they are the *same words* the
authoring session printed a moment earlier. A model that only its author can see is not a model;
this one is readable by a session that was told nothing, which is the only test of a type contract
that counts.

Section 6 is where the contract meets the wire. All seven attempts, as the example printed them:

```text
  write Fan_01.Speed 2000 (declared Int32, read and write): 2000
  write Fan_01.Vendor (declared read only) refused: AddressSpaceError: Vendor has AccessLevel 0x01, which does not include CurrentWrite: this author declared it read-only, and asyncua's server answers a non-Admin write of such a variable with BadUserAccessDenied. Pass enforce_access_level=False to write it as an Admin.
  write Fan_02.Speed from a session with no role: 1750
  write Vendor with the toolkit's check off, on a session with no role refused: AddressSpaceError: the server refused the value 'vendor-b' for Vendor (declared String): User does not have permission to perform the requested operation.(BadUserAccessDenied)
  raw write_value(2000) on Fan_01.Speed refused: BadTypeMismatch: The value supplied for the attribute is not of the same type as the attribute"s value.(BadTypeMismatch)
  raw write_value(61.0) on Pump_03.SetPoint, which is a Float refused: BadTypeMismatch: The value supplied for the attribute is not of the same type as the attribute"s value.(BadTypeMismatch)
  write Pump_03.Vibration 3.5 (declared Float, read only) refused: AddressSpaceError: Vibration has AccessLevel 0x01, which does not include CurrentWrite: this author declared it read-only, and asyncua's server answers a non-Admin write of such a variable with BadUserAccessDenied. Pass enforce_access_level=False to write it as an Admin.
```

Four different refusals, and the interesting one is the third line. `Speed` on `Fan_02` was written
by a session with **no user at all** - because the author declared it read and write, and
`AccessLevel`/`UserAccessLevel` both carry `CurrentWrite`, so the server's rule is satisfied by the
anonymous role. The fourth line is the same write, on the same read-only `Vendor`, with the
toolkit's local check switched off and no role to back it up: the server answers exactly what
asyncua's server answers, `BadUserAccessDenied`, and the toolkit wraps it with the value and the
declared data type that were refused. `enforce_access_level=False` is not a way to get a write
through; it is a way to find out what the server thinks.

Sections 7 and 8 are the refusals that never reach a value at all:

```text
  instantiate MachineType, which is abstract refused: AddressSpaceError: MachineType (ns=2;s=MachineType) is abstract: OPC 10000-3 5.5.2 allows Objects of its subtypes only, so this instance was refused before the server was asked
  instantiate Fan_03 with a value for a part it would not have refused: AddressSpaceError: Fan_03 would be an instance of FanType, which gives an instance AssetTag, Running, Speed, Temperature: Vendor is not one of them; Vendor is declared optional and this instance was asked to leave optional parts out
  Fan_03 in the address space: BadNodeIdUnknown
  author a brand new type as an anonymous client refused: AddressSpaceError: the server refused to create GhostType at ns=2;s=GhostType: User does not have permission to perform the requested operation.(BadUserAccessDenied)
  add an Object with asyncua's own helper, as an anonymous client refused: BadUserAccessDenied: User does not have permission to perform the requested operation.(BadUserAccessDenied)
```

The first two refusals cost nothing: no request was sent, no node was created, and the third line
proves it (`BadNodeIdUnknown` from a *different* session looking for `Fan_03`). The last two cost
two service faults, which the server logged, and they are the difference between a tutorial server
that lets a tutorial write into it and a server that does not. Those two refusals are the ones the
server logged as `ServiceFault … in response to AddNodesRequest` in the log block below.

Section 9 re-runs the same authoring on purpose:

```text
  FanType reused=True
     note: reused the ObjectType FanType already at ns=2;s=FanType instead of adding it again, because asyncua's server answers a duplicate NodeId with BadNodeIdExists
     note: Speed: the declaration already existed at ns=2;s=FanType.Speed
     note: FaultCount: the declaration already existed at ns=2;s=FanType.FaultCount
     note: Vendor: the declaration already existed at ns=2;s=FanType.Vendor
  note: ns=2;s=AssetModel already had a child named Fan_01 at ns=2;s=Fan_01; this author used that node instead of adding a second one
```

Section 10 checks an instance against a type it is not - `Fan_02` against `PumpType`, which is what
happens when a client is told the wrong thing about a node:

```text
  instance Fan_02 (ns=2;s=Fan_02) typed as ns=2;s=FanType: 4 of 7 declared parts, missing ['Pressure', 'SetPoint'], extra ['Speed'], NOT conformant
```

`NOT conformant` is a verdict the reader can act on, and it is stated without pretending the
instance is broken: `Fan_02` is a perfectly good instance of `FanType`. It is the *claim being
tested* that is wrong.

Section 11 resolves the model by BrowsePath, which is how a client that was given a path instead of
a NodeId finds it (OPC 10000-4 5.9.4):

```text
  AssetModel/Pump_01/Temperature -> ns=2;s=Pump_01.Temperature
  translate AssetModel/Pump_01/Nope refused: BrowsePathError: TranslateBrowsePathsToNodeIds returned BadNoMatch for path Pump_01, Nope
```

Section 12 re-counts the address space after the authoring:

```text
  Objects folder: Objects: 370 variables, 0 with no type, 20 with a type that says more (95% typed only as a base type)
      264 variable(s) typed PropertyType  <- says nothing about the node
      86 variable(s) typed BaseDataVariableType  <- says nothing about the node
      8 variable(s) typed PubSubDiagnosticsCounterType
      3 variable(s) typed ns=2;s=MachineSetPointType
      2 variable(s) typed ns=2;s=FanSpeedType
      …
  variables counted before / after: 353 -> 370
```

The arithmetic is worth doing by hand, because it is the only number in this chapter that a reader
can verify from the other ones. 17 new variables = `Pump_03`'s 7 + `Fan_01`'s 6 + `Fan_02`'s 4. The
`FanType` **declarations** are not among them, because the authored types hang under
`BaseObjectType` - which is where `find_type` found `MachineType` after searching 283 type nodes -
and this count walks the `Objects` folder. `MachineSetPointType` goes 2 → 3 (`Pump_03.SetPoint`),
`FanSpeedType` 0 → 2 (`Fan_01.Speed`, `Fan_02.Speed`), `PropertyType` +5 (`AssetTag` on the three
new instances, `Vendor` on `Fan_01`, `MaintenanceNote` on `Pump_03`) and
`BaseDataVariableType` +9 (the rest). And the headline percentage does not move: 17 → 20 typed
with something meaningful, 95 % base-typed either way. That is what a real model looks like inside a
server that also carries the standard's own diagnostic variables.

Finally, the same run's logging, which is where the server's side of the story is written:

```text
2026-11-20 06:55:52 | WARNING  | asyncua.client.client | Sending plain-text password
2026-11-20 06:55:52 | INFO     | opcua_toolkit.server.authoring | authored FanType (ns=2;s=FanType) in namespace 2: type FanType (ns=2;s=FanType) instantiable: 6 declarations (3 inherited), 4 mandatory, 1 VariableType of their own
2026-11-20 06:55:52 | WARNING  | asyncua.server.address_space | Write refused: Variant: Variant(Value=2000, VariantType=<VariantType.Int64: 8>, Dimensions=None, is_array=False) with type 8 does not have expected type: 6
2026-11-20 06:55:52 | WARNING  | asyncua.server.address_space | Write refused: Variant: Variant(Value=61.0, VariantType=<VariantType.Double: 11>, Dimensions=None, is_array=False) with type 11 does not have expected type: 10
2026-11-20 06:55:52 | ERROR    | asyncua.server.uaprocessor | sending service fault response: User does not have permission to perform the requested operation. (BadUserAccessDenied)
2026-11-20 06:55:52 | WARNING  | asyncua.client.ua_client.UASocketProtocol | ServiceFault (BadUserAccessDenied, diagnostics: DiagnosticInfo(SymbolicId=None, NamespaceURI=None, Locale=None, LocalizedText=None, AdditionalInfo=None, InnerStatusCode=None, InnerDiagnosticInfo=None)) from server received  in response to AddNodesRequest
```

Five facts sit in those six lines. The author logged the type it wrote at `INFO`, which is what
makes a commissioning run auditable - and in this run it logged `FanType` twice, because section 9
re-runs the same authoring and the reuse is reported the same way as the creation.
`Sending plain-text password` is the demo server's own warning about an unsecured endpoint - the
shortcut Chapter 25 exists to remove. The two
`Write refused` lines are the server's side of the two `BadTypeMismatch` refusals above, logged with
the variant type numbers. And the pair at the end is the *server* reporting a service fault and the
*client* receiving it, which is why the anonymous `AddNodes` refusal appears twice in this chapter's
output: once as the toolkit's sentence about what happened, once as asyncua's own protocol warning.

## 4. Verification

### The chapter's own gates

```powershell
uv run python examples/ch16_authored_address_space.py --demo
uv run python examples/ch16_authored_address_space.py --endpoint opc.tcp://127.0.0.1:48401

uv run pytest tests/unit/test_address_space_model.py -q --tb=short
uv run pytest tests/integration/test_address_space.py -q --tb=short
uv run pytest tests/integration -q --tb=short
```

Pointed at `--endpoint` instead of `--demo`, the authoring half of the example refuses to run and
says why, and everything else still runs: reading a model the example did not author, finding types
by name, checking instances against the types they claim, and counting coverage before and after.
Run against the `--authored-model` server above, that printed:

```text
3. Authoring a type over a client session
-----------------------------------------
  skipped: authoring writes into the server
  --endpoint is somebody else's plant, and this chapter will not write into it

12. What the address space says about itself after the authoring
----------------------------------------------------------------
  Objects folder: Objects: 353 variables, 0 with no type, 17 with a type that says more (95% typed only as a base type)
  …
  variables counted before / after: 353 -> 353
```

`353 -> 353` is the honest result of a run that wrote nothing, and the run exits 0: a chapter about
writing a model should still be runnable against somebody else's server, and it should be obvious in
its own output which parts it refused to do there.

Both test files were run against the code as it stands, and this is what they printed:

```text
................................................................         [100%]
64 passed in 0.09s
```

```text
......................                                                   [100%]
22 passed in 70.44s (0:01:10)
```

And the whole integration suite, which is the gate that says this chapter did not break the
fifteen before it:

```text
........................................................................ [ 38%]
.s...........s.......................................................... [ 77%]
.........................................                                [100%]
=========================== short test summary info ===========================
SKIPPED [1] tests\integration\test_discovery.py:154: demo server does not implement FindServersOnNetwork
SKIPPED [1] tests\integration\test_inspect.py:125: historization is not enabled in this fixture
183 passed, 2 skipped in 569.90s (0:09:29)
```

`179 passed` before this chapter, `183 passed` after it: four new integration tests, two of them
about browse paths starting where they were told to (Chapter 2's correction), and the same two
skips that have been there since Chapter 3.

The unit file is the specification side: what each `ModellingRule` NodeId is and which clause the
report quotes for it, `VariableAccess` as bit masks rather than as enum values, `ValueRank` as the
numbers OPC 10000-3 5.6.5 lists, the `ComponentSpec` refusals (a placeholder rule, a name a browse
path could not express - eight of them, parametrised - a data type the toolkit would have to guess,
a component that both names an existing `VariableType` and asks for one to be authored, a negative
sampling interval, a frozen spec asked to invent a field), the four cases `sampling_interval_text`
renders, the exact wording of a coverage row and the `<- says nothing about the node` mark, and four
`…_is_json_ready` tests that keep every report serialisable for the logging chapters. Two of them
are the regressions this chapter's measurements produced:
`test_type_definition_name_names_a_type_this_module_was_never_told_about`, which asserts both that
`ns=0;i=19725` is named `PubSubDiagnosticsCounterType` and that the `__firstlineno__` / `Int16`
collision above is skipped, and
`test_a_declaration_line_names_the_reference_the_rule_the_type_and_the_access`, which asserts that a
`Float` component typed with the tutorial's own `VariableType` prints `type=ns=2;s=MachineSetPointType`
with **no** `(base type)` mark on it.

The integration file is the round trip, against a `DemoServer` started on a port the OS picked, one
fixture with `authored_model=True` and `simulate=False` and one with neither:

| Test | What it proves |
| --- | --- |
| `test_the_plain_demo_server_keeps_its_authored_namespace_off` | on the default server `authored_types`, `authored_instances`, `authored_folder_id` and `author` are all empty or `None`, and browsing for `AssetModel` raises `BrowsePathError` - the opt-in really is off |
| `test_the_authored_server_reports_what_it_authored` | a server that wrote its own model can say what it wrote: two types, two instances, the folder and the author all in the server's own namespace |
| `test_an_authored_type_reports_the_declarations_it_inherits` | `PumpType` reports seven declarations, of which exactly the three named ones carry `inherited_from == "MachineType"`, with mandatory and optional names split, and nothing declared-but-never-instantiated |
| `test_the_abstract_supertype_is_a_contract_no_instance_of_it_exists` | `MachineType` is abstract, not instantiable, declares the three shared parts, and is `PumpType`'s `supertype_id` |
| `test_an_authored_declaration_carries_its_access_and_sampling` | `AccessLevel` masks, `MinimumSamplingInterval` (`100 ms` on `Temperature`, `250 ms` on `SetPoint`, `0` where the author set nothing), the `ModellingRule` on each declaration, and the initial values - including `Vibration` arriving as the nearest float32 to `2.4` |
| `test_an_authored_variable_type_is_what_its_component_is_typed_with` | the type report's one own `VariableType` is `MachineSetPointType` with `DataType=Float` and scalar `ValueRank`, `SetPoint`'s declaration points at it, and asking `variable_type_report` for an `ObjectType` refuses with `not a VariableType` |
| `test_a_client_finds_a_type_by_name_and_refuses_one_that_only_almost_matches` | `find_type` finds `PumpType` by BrowseName, refuses `machinetype` with the near miss named, refuses an unknown name, and refuses a name that is not a BrowseName |
| `test_an_instance_holds_every_part_its_type_promises` | `Pump_01` read through a client: its own `HasTypeDefinition` is `PumpType`, no missing and no extra names, and the parts carry the access mask, sampling interval, data type and value the type declared |
| `test_two_instances_of_one_type_differ_only_in_their_values` | `Pump_01` and `Pump_02` have identical component names and both conform, and differ only in `AssetTag`, `SetPoint` and `Running` |
| `test_optional_parts_can_be_left_out_and_the_instance_still_conforms` | `include_optional=False` produces exactly the mandatory parts, `optional_absent` lists the rest, and a *second reader* agrees the instance conforms |
| `test_a_value_for_a_part_this_instance_would_not_have_is_refused_first` | a value for `MaintenanceNote` on an instance that would leave optional parts out is refused locally, and no `Pump_04` exists in the folder afterwards |
| `test_an_abstract_type_refuses_an_instance_before_the_server_is_asked` | instantiating `MachineType` is refused with the 5.5.2 clause in the message, and no `Machine_01` exists afterwards |
| `test_the_author_writes_with_the_wire_type_the_component_declares` | writes to a `Float`, a `Double` and a `Float` written from a Python `int` are all accepted and read back as the declared type |
| `test_a_read_only_component_is_refused_locally_before_the_server_could_refuse_it` | the local refusal names `CurrentWrite` and `enforce_access_level=False`, the value is unchanged, and a part the instance does not have is refused with `no component` |
| `test_an_admin_session_can_write_what_a_declaration_closed` | `enforce_access_level=False` on a session that really holds `Admin` writes a read-only property, which is the one case where that flag is honest |
| `test_an_anonymous_session_is_refused_the_same_write_with_the_check_off` | the same write with the toolkit's check off answers `BadUserAccessDenied`: turning the local check off does not turn the server's off |
| `test_the_wrong_wire_type_is_refused_by_the_server` | a `String` written to a `Double` variable is `BadTypeMismatch`, from the server, not from this toolkit |
| `test_an_admin_session_authors_a_type_no_test_wrote` | authoring through a *client* session: `FanType` under a supertype named only by BrowseName, an instance whose `Speed` reads back as `Int32`, and a reader that was not the author finding the same declarations |
| `test_a_second_run_of_the_same_model_reuses_every_node` | re-running `author_model` reuses every node with an "already existed" note, `instantiate_all` adds no second child, and `Pump_01` still has exactly seven components |
| `test_an_anonymous_session_cannot_author_a_type` | `BadUserAccessDenied` from `AddNodes`, with the refused type's name in the message |
| `test_a_deleted_component_shows_up_as_drift_on_the_next_read` | delete `Pump_01.Temperature` and the next read reports `missing_names == ("Temperature",)`, `NOT conformant`, and no component for it |
| `test_coverage_says_how_much_of_the_model_is_typed_as_nothing` | under `AssetModel`: 14 variables, 0 untyped, 2 with a specialised type, 12 typed only as a base type, `MachineSetPointType` used twice, and the walk not truncated |

The delete test is the one to read twice. It is the only test here that models the real failure mode
of a server that has been edited by hand: the type still promises the part, the instance no longer
has it, and nothing in the address space warns anybody. `InstanceReport` is how you find out, and
`conforms_to_type` being `False` on a report a client produced is the whole point of having a report
at all.

### Against somebody else's client

Start the server with the model authored:

```powershell
uv run opcua-tutorial-server --port 48456 --authored-model --no-simulate

uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=AssetModel --depth 2
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=MachineType --depth 1
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=PumpType --depth 1
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=0;i=63 --depth 1
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=0;i=85 --depth 1

uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --path AssetModel/Pump_01/SetPoint
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=AssetModel --path Pump_01/SetPoint
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --path Objects/AssetModel
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=AssetModel --path Pump_01/Nope

uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=PumpType --node ns=2;s=MachineSetPointType
uv run opcua-toolkit inspect --endpoint opc.tcp://127.0.0.1:48456 --node ns=2;s=Pump_01.SetPoint --node ns=2;s=Pump_01.MaintenanceNote --value
```

Against the server above, this is what those commands printed. What is shortened, and nothing
else: the `namespaces:` header each of those commands prints is quoted once and then left out; the
seven `Pump_02` component rows of the first tree and the middle 30 child rows of the
`BaseDataVariableType` tree; and the `----` underline rows of the two `inspect` tables are exactly as
printed.

```text
namespaces:
  [0] http://opcfoundation.org/UA/
  [1] urn:freeopcua:python:server
  [2] urn:opcua-tutorial:demo
AssetModel [obje] ns=2;s=AssetModel
  Pump_01 [obje] ns=2;s=Pump_01 : ns=2;s=PumpType
    Pressure [vari] ns=2;s=Pump_01.Pressure : ns=0;i=63
    Vibration [vari] ns=2;s=Pump_01.Vibration : ns=0;i=63
    SetPoint [vari] ns=2;s=Pump_01.SetPoint : ns=2;s=MachineSetPointType
    MaintenanceNote [vari] ns=2;s=Pump_01.MaintenanceNote : ns=0;i=68
    Temperature [vari] ns=2;s=Pump_01.Temperature : ns=0;i=63
    Running [vari] ns=2;s=Pump_01.Running : ns=0;i=63
    AssetTag [vari] ns=2;s=Pump_01.AssetTag : ns=0;i=68
  Pump_02 [obje] ns=2;s=Pump_02 : ns=2;s=PumpType
    …
browse stats: nodes=17 references=16 browse=2 next=0 errors=0
```

```text
MachineType [objt] ns=2;s=MachineType
  Temperature [vari] ns=2;s=MachineType.Temperature : ns=0;i=63
  Running [vari] ns=2;s=MachineType.Running : ns=0;i=63
  AssetTag [vari] ns=2;s=MachineType.AssetTag : ns=0;i=68
  PumpType [objt] ns=2;s=PumpType
browse stats: nodes=5 references=4 browse=1 next=0 errors=0
```

```text
PumpType [objt] ns=2;s=PumpType
  Pressure [vari] ns=2;s=PumpType.Pressure : ns=0;i=63
  Vibration [vari] ns=2;s=PumpType.Vibration : ns=0;i=63
  SetPoint [vari] ns=2;s=PumpType.SetPoint : ns=2;s=MachineSetPointType
  MaintenanceNote [vari] ns=2;s=PumpType.MaintenanceNote : ns=0;i=68
browse stats: nodes=5 references=4 browse=1 next=0 errors=0
```

```text
BaseDataVariableType [virt] ns=0;i=63
  DataTypeDescriptionType [virt] ns=0;i=69
  DataTypeDictionaryType [virt] ns=0;i=72
  …
  PubSubDiagnosticsCounterType [virt] ns=0;i=19725
  ReferenceDescriptionVariableType [virt] ns=0;i=32657
  MachineSetPointType [virt] ns=2;s=MachineSetPointType
browse stats: nodes=36 references=35 browse=1 next=0 errors=0
```

```text
resolved AssetModel/Pump_01/SetPoint -> ns=2;s=Pump_01.SetPoint
  browse name: SetPoint
  node class : Variable
```

```text
resolved Pump_01/SetPoint -> ns=2;s=Pump_01.SetPoint
  browse name: SetPoint
  node class : Variable
```

```text
resolved Objects/AssetModel -> ns=2;s=AssetModel
  browse name: AssetModel
  node class : Object
```

```text
error: browse failed: No node named 'Nope' under ns=2;s=Pump_01 (segment 2 of 2)
```

```text
NodeId                      Class         BrowseName           DataType      Rank  Access  Historizing
--------------------------  ------------  -------------------  ------------  ----  ------  -----------
ns=2;s=PumpType             ObjectType    PumpType             BaseDataType  -1    none    no
ns=2;s=MachineSetPointType  VariableType  MachineSetPointType  Float         -1    none    no
```

```text
NodeId                          Class     BrowseName       DataType  Rank  Access                    Historizing
------------------------------  --------  ---------------  --------  ----  ------------------------  -----------
ns=2;s=Pump_01.SetPoint         Variable  SetPoint         Float     -1    CurrentRead|CurrentWrite  no
ns=2;s=Pump_01.MaintenanceNote  Variable  MaintenanceNote  String    -1    CurrentRead               no
```

Read the four trees in order and the model this chapter authored is visible without any code:

- `AssetModel` has two Objects, each naming `ns=2;s=PumpType`, and `SetPoint` on both names the
  tutorial's own `VariableType` while its six siblings name the two base types.
- `MachineType`'s children are its three **declarations** plus `PumpType`. Both are `[objt]`, and
  neither carries a `: …` type suffix.
- `PumpType`'s children are **only its own four declarations** - `Pressure`, `Vibration`,
  `SetPoint`, `MaintenanceNote` - because the three it inherited hang under `MachineType`, which is
  why `read_type` has to walk the supertype chain to report seven. A browse shows what a type
  declared; a type report shows what an instance must have.
- `MachineSetPointType` is the last child of `BaseDataVariableType`, in the same list as the
  standard's own 34 variable types under it.
- the `--node ns=0;i=85` command's tree is quoted in section 2, and it is the one that shows the
  same `AssetModel` node carrying `: ns=0;i=61` - `FolderType` - when a browse reached it.

Two things a reader should check by hand in those blocks. The `browse --node ns=2;s=MachineType`
tree shows a **type's declarations** as browsable children - `Temperature`, `Running`, `AssetTag` -
and `PumpType` among them, with `[objt]` on both type nodes and no `: …` type suffix on either. And
`inspect` prints an `ObjectType` with `DataType = BaseDataType` and `Access = none`: an ObjectType
has no `Value`, no `AccessLevel` and no `Historizing`, because those are attributes of the Variable
NodeClass (OPC 10000-3 5.6.2, Table 13). Reading an attribute a NodeClass does not define is not a
"null answer" - asyncua's server answers with a status code:

```text
asyncua.ua.uaerrors._auto.BadAttributeIdInvalid: The attribute is not supported for the specified Node.(BadAttributeIdInvalid)
```

That line came from reading `ValueRank` on an authored `ObjectType`, immediately after the same
probe read `IsAbstract` on the same node and got a `Boolean` back. It is the clearest possible
demonstration that attributes belong to NodeClasses and not to "nodes in general", and it is why
the reader in this chapter reads different attribute sets for a type, a declaration and an instance
instead of asking every node the same five questions.

**In UaExpert**: connect to the server started with `--authored-model`, then check that

- `AssetModel` is under *Objects*, `Pump_01` under it, and `Pump_01`'s NodeId is `ns=2;s=Pump_01`
  while `Pump_01.Temperature`'s is `ns=2;s=Pump_01.Temperature` - the child's NodeId is *not* built
  from the parent's, and a client that assumes it is has already lost;
- `Pump_01`'s *TypeDefinition* in UaExpert's node view is `ns=2;s=PumpType`, and `Pump_01.AssetTag`'s
  is `PropertyType` (`ns=0;i=68`);
- `SetPoint`'s TypeDefinition is `ns=2;s=MachineSetPointType`, and its *DataType* is still `Float` -
  the two facts a `VariableType` and a `DataType` hold separately;
- browsing *Types ▸ BaseObjectType* finds `MachineType`, and `MachineType`'s *IsAbstract* is `true`
  while `PumpType`'s is `false`;
- `FanType` (if the example has been run against this server) appears under `MachineType`, and its
  `Speed` declaration's TypeDefinition is `ns=2;s=FanSpeedType`, not `BaseDataVariableType`;
- the `Fan_01` the example created exists in `AssetModel` with six children and `Fan_02` with four,
  while no `Fan_03` exists anywhere.

**In Prosys UA Simulation Server**: import or model an equivalent `PumpType` with one mandatory and
one optional component, create two instances of it - one with the optional part, one without - and
run this chapter's reader against *that* server with `--endpoint`. The chapter's model code is not
run there (it would write into somebody else's server), and every reading half still works. If a
conformant second server reports `missing []` / `extra []` for the same two instances, the reading
code is reading OPC UA and not reading asyncua.

Details for both tools: [../VERIFICATION_TOOLING.md](../VERIFICATION_TOOLING.md).

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Authoring a model with no types of its own | Every variable reads as `BaseDataVariableType`: 95 % of the demo server's variables say nothing about what they are | Declare `VariableType` nodes for the components whose meaning matters, and check it with `coverage` |
| Reading a node's type with a `Read` | There is no `TypeDefinition` attribute to read, so the request fails or the reader invents a name | Read the `HasTypeDefinition` **reference**, or take the type from the browse result |
| Printing a NodeClass in four letters | A type and an instance of it both print as `obje`, which is the distinction the chapter is about | Name the colliding classes apart, as `BrowseTree.render` now does |
| Typing a component by its data type alone | `SetPoint` is a `Float` everywhere, including in a historian that wanted a *setpoint* | Give it a `VariableType`, and keep the data type question and the meaning question apart |
| Assuming a mandatory part appears on an instance | The instance is missing a part every client expects from the type | Compare with `describe_instance` after authoring, and after any later edit |
| Treating `Optional` as "we decided not to use it" | A subtype or instance that dropped an optional part gets read as broken by a client expecting the type | Report `4 of 6 … conformant` and let the rule be visible |
| Reading `WriteMask = 0x00` as "this node is read-only" | `Value` is writable on every node this chapter authored, and `WriteMask` is `0x00` on all of them | Ask `AccessLevel` and `UserAccessLevel`; `WriteMask` is a mask of *attributes* (OPC 10000-3 8.60) |
| Writing a Python number and hoping | `BadTypeMismatch` from the server, because a literal became `Int64` where the model declared `Int32` | `write_component_value`: read the declared `DataType`, then write |
| Authoring from a session with no role | `BadUserAccessDenied` on `AddNodes`, and on `AddReferences` - so even a type with no children ends up half-built | Give the author a user the server trusts, and treat the role as part of the configuration |
| Failing a re-run of an authoring script | `BadNodeIdExists` on the second run, so the script cannot be re-run after a partial failure | `author_model` reuses and reports `reused=True` with a note per reused node |
| Looking a type up by `DisplayName` | The name matches what a human sees and not what the address space matches on | `find_type`, which matches `BrowseName` and names its near misses |
| Counting a model from the `Objects` folder only | The types themselves, and every declaration under them, are invisible to the count | Decide which subtree the number is about: `coverage(start=…)` says so in the report line |
| Trusting `MinimumSamplingInterval = 0` as "not sampled" | A client concludes nothing is monitored on a node the server said it monitors continuously | Read the spec's wording (OPC 10000-3 5.6.2, Table 13): 0 is continuous, -1 is indeterminate |
| Writing into a production server to prove a reader works | An address space changed by a tutorial, on a plant's server, by a script | Author only against a server this repository owns, and print the skip when it is not one |

## 6. What's next

Chapter 17 stops looking at what a node *is* and starts looking at what a value *carries*: a
`DataValue` is not a number, it is a number with a `StatusCode`, a `SourceTimestamp` and a
`ServerTimestamp`, and the difference between "61.5" and "61.5, read at 06:55:51, from a source that
said it was uncertain" is the difference between a value you can use and a value you cannot. Every
chapter from here to the end of Part 5 - methods, events, historized reads - is built on those
fields, and Chapter 28's logging work is built on the same `DataValue` shape.

Building block this chapter adds, and where this repository uses it again:

- `ObjectTypeSpec` / `VariableTypeSpec` / `ComponentSpec` / `InstanceSpec` - the vocabulary the
  companion-specification chapters (Part 6) write a specification's model with, and the capstone
  gateway authors its own namespace with;
- `AddressSpaceReader` - the conformance check of Part 6: a specification is only real if a client
  can read the types it promises, and `coverage` is how "mostly base-typed" gets measured;
- `AddressSpaceAuthor` - the write side of Part 6 and Part 8's architecture chapter, and the only
  way this tutorial will ever have a model it owns;
- `InstanceReport` and `TypeReport` - the drift detector Chapter 29's tests run against a server
  that has been edited;
- the browse-side fix (`objt` / `virt`, and an empty type definition read as none) - used by every
  chapter that prints a tree, which is most of them.
