# Chapter 1 - What OPC UA actually is

*Part 1 · status: written · no example file: this chapter is the vocabulary the code uses*

## 1. Why this matters

Most OPC UA tutorials teach a transport. They show a client, a URL and a value, and
the reader comes away thinking OPC UA is a way to move numbers between machines.

It is not. OPC UA is an **information model with services attached to it**. The
transport is an implementation detail of one of several mappings, and it changes:
binary over TCP today, MQTT and HTTPS bindings in the same specification. What does
not change is the model: nodes, typed references, data types, and the services that
read and browse them.

That distinction is the whole reason an application survives a vendor change. A
client that was written against one vendor's register layout breaks when the register
names change. A client that was written against an information model - that finds
its tags by browsing, that reads what a node says about itself, that asks the server
what it is allowed to do - keeps working when the machine, the vendor and the
transport all change.

Every later chapter in this tutorial is an argument for that position, with numbers
attached.

## 2. OPC UA theory

### The two halves

OPC UA is specified as a family of documents (OPC 10000-x, at
[reference.opcfoundation.org](https://reference.opcfoundation.org/)):

- an **information model**: what a server's address space is made of, which base
  types exist, which nodes a conforming server must provide (Parts 3, 7 and 8);
- a set of **services**: request/response operations a client may perform on that
  model (Part 4);
- **transport mappings** that carry those services (Part 6 for binary over TCP and
  HTTPS, and separate mappings for MQTT and UDP);
- **security** as its own subject (Part 2), which is not the same thing as transport.

The services this tutorial uses constantly are a short list:

| Service | What it is for | Chapter |
| --- | --- | --- |
| `GetEndpoints` | what this address offers, before a session exists | 4, 13 |
| `FindServers` | which applications a server knows about | 13 |
| `CreateSession` / `ActivateSession` | the conversation a client is allowed to have | 4 |
| `Browse` / `BrowseNext` | which nodes are connected to which | 5 |
| `TranslateBrowsePathsToNodeIds` | resolve a path you know by name | 5, 13 |
| `Read` / `Write` | attribute access, one status per requested item | 6, 7, 8 |
| `Call` | ask the server to do something | 9 |
| `CreateSubscription`, `CreateMonitoredItems`, `Publish` | server-side reporting | 10-12 |
| `ReadHistoryRawModified` | what the value was earlier | 20 |

### Why a model, and not a register map

A register-based protocol hands you addresses. OPC UA hands you **nodes with typed
references and declared data types**, and a namespace URI to identify who authored
them. Three consequences follow, and every one of them is a later chapter:

- a node can be found **by name**, because it has a `BrowseName` and a path
  (Chapter 5);
- a node can be asked **what it is**, because it declares `DataType`, `ValueRank`,
  `AccessLevel` and its type definition (Chapter 6);
- a server can declare **how much it will answer per request**, because
  `OperationLimits` are part of the model (Chapter 7).

A client built on those three facts is a client that survives.

### What OPC UA is not

- It is not OPC Classic (OPC DA/HA/AE, DCOM, Windows-only). OPC UA is a different
  specification that can *wrap* classic data sources, but it is not them.
- It is not a fieldbus or a controller protocol. It runs above those systems, on
  hosts that can run TCP and TLS.
- It is not a historian or a data bus. It has history *access* services, and it has
  a Pub Sub variant (Part 14), but storing data is your application's job (Chapter 20).
- It is not secure by default. An unsecured endpoint is a legal, common, first-run
  configuration. Chapters 25-27 are about turning that into a deployment.

### The vocabulary used by the rest of this tutorial

| Term | Means |
| --- | --- |
| Address space | The nodes a server exposes, and the references between them |
| Node | One addressable thing: an object, a variable, a method, a type, a reference |
| NodeClass | Which kind of node this is (`Object`, `Variable`, `Method`, `ObjectType`, ...) |
| Reference | A typed, directed connection between two nodes |
| Namespace | A set of nodes with an author, identified by URI and by a per-server index |
| Session | A client's authorised conversation with a server, inside a secure channel |
| Endpoint | One address + security policy + security mode + transport profile |
| Monitored item | A server-side subscription to one node's changes |
| StatusCode | The server's answer, per requested item, not per request |
| Profile | A named subset of the specification a product claims to implement |

## 3. Implementation

This tutorial uses **asyncua 2.x**, an asyncio implementation of the client and the
server side of Parts 4, 6, 8 and parts of the base information model. It is a real
OPC UA stack: it speaks binary over TCP, negotiates endpoints, and builds sessions.

asyncua gives you the protocol. It does not give you:

- awareness of `OperationLimits` (it will happily send 1 000 NodeIds);
- reading of the revised numbers a server answers a subscription with;
- a health or state signal your application or a dashboard can act on;
- an error type per failure mode;
- a decision about whether a reconnect is worth having.

`src/opcua_toolkit/` is those missing pieces, one class per concept, each reusable on
its own. The toolkit is what every chapter builds with, and it is importable as the
`opcua_toolkit` package - see [../GETTING_STARTED.md](../GETTING_STARTED.md).

The demo server (`src/opcua_toolkit/devserver.py`, runnable as
`opcua-tutorial-server`) is a real asyncua server with a plant-shaped address space:
`Objects/ProductionLine/Line1` with process tags, a `Maintenance` object, five
methods, a 24-tag bulk block, deliberately small `OperationLimits`, and deliberately
small private subscription caps. It exists so that every claim in this tutorial can
be demonstrated instead of asserted.

## 4. Verification

Start the server and look at it with three independent tools:

```powershell
uv run opcua-tutorial-server --host 127.0.0.1 --port 48401
```

```powershell
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48401
```

`discover` prints what the server says about itself: the endpoint you reached, its
security policy and mode, the namespace array, `ServerState`, `ServiceLevel` and the
limits it published. That output *is* the information model talking.

In **UaExpert** or the **Prosys UA Simulation Server** client, connect to the same
URL and check the same facts by hand: the namespace array under *Server → Namespaces*,
the tree under *ProductionLine*, and the NodeId of any tag. See
[../VERIFICATION_TOOLING.md](../VERIFICATION_TOOLING.md) for the exact clicks.

The point of doing it twice is the point of the chapter: a claim about OPC UA is a
claim about what a server answers, so it is checked with a second client.

## 5. Common mistakes

| Mistake | How it shows up | The fix in this tutorial |
| --- | --- | --- |
| Treating OPC UA as "a way to read registers" | Every tag is a hardcoded address in a config file | Chapter 5: browse to the tags by name |
| Assuming the namespace index is stable | `ns=2;s=Temperature` works in the demo, fails on site | Chapter 13: discover the index from the server |
| Assuming one URL is one server | Two servers behind one host and port, or a load balancer | Chapter 13 and Chapter 31 |
| Treating the endpoint URL as a fact about the network | Copying an announced `EndpointUrl` out of `GetEndpoints` | Chapter 31: an announcement is an answer to a question |
| Thinking security is automatic | An unsecured endpoint in a production deployment | Chapters 25-27 |
| Believing "no exception" means "it worked" | A bulk write that failed 20 of 24 items silently | Chapters 8 and 12 |
| Using OPC UA to poll because subscriptions are hard | 500 Reads a second, then a mystery outage under load | Chapters 10-12 |

## 6. What's next

Chapter 2 is the model itself: nodes, references and namespaces, because you cannot
browse what you cannot name. Chapter 3 explains how this repository is built so that
every following chapter can be run rather than read.
