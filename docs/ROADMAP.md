# Roadmap

31 chapters in eight parts, and one capstone. Each chapter has one thing to
understand, one thing to build, and one thing to verify with someone else's tool.
Nothing is repeated later: every chapter's output is reused by a later chapter,
and the capstone reuses all of them.

## The plan in one table

| Part | Chapters | Subject | Status |
| --- | --- | --- | --- |
| 1 | 1-3 | Foundations: what OPC UA is, how an address space works, how to read the spec | written |
| 2 | 4-9 | Reading a server: connect, browse, inspect, read, write, call | **built and tested** |
| 3 | 10-12 | Keeping data coming: subscriptions, dispatch, supervision | **built and tested** |
| 4 | 13-15 | Application shapes: discovery-driven config, typed settings, queues | 13 built; 14-15 next |
| 5 | 16-20 | Building a server: address space, data quality, methods, events, history | planned |
| 6 | 21-24 | Companion specifications: reading one, applying one, surviving two | planned |
| 7 | 25-27 | Security: policies and certificates, users and roles, deployment | planned |
| 8 | 28-31 | Production concerns: logging, testing, interfaces out, architecture | planned |
| - | capstone | The OPC UA edge gateway, assembled from Parts 2-8 | planned |

## Part 1 - Foundations

| Chapter | Title | The one thing to understand |
| --- | --- | --- |
| 1 | What OPC UA actually is | It is an information model with services attached, not a transport protocol - and the model is why a client can survive a vendor change |
| 2 | Nodes, references and namespaces | Everything is a node with typed references; a NodeId is an address in a namespace the server can renumber |
| 3 | How to read this repository | Tooling, conventions, how a chapter is structured, how to run an example, and how a claim here is checked |

Building blocks: the environment (uv, ruff, mypy, pytest), the demo server, the CLI.

## Part 2 - Reading a server

| Chapter | Title | The one thing to understand | Example |
| --- | --- | --- | --- |
| 4 | The first connection | A connection is a negotiation: endpoints, session timeouts and limits come back from the server, not from your config | `examples/ch04_first_connection.py` |
| 5 | Browsing | Find your tags by following typed references; a hardcoded NodeId is a bug you have already written | `examples/ch05_browsing.py` |
| 6 | Node metadata | A node publishes whether your application may read or write it - and publishing is not enforcement | `examples/ch06_node_metadata.py` |
| 7 | Reading values | Bulk reading is a number you can measure: chunk size comes from the server's `MaxNodesPerRead` | `examples/ch07_bulk_read.py` |
| 8 | Writing values | "No exception" is not "it worked": one outcome per requested item, and a status code is the answer | `examples/ch08_bulk_write.py` |
| 9 | Methods | A method you have never seen can be called correctly, because its argument list is part of the address space | `examples/ch09_methods.py` |

Building blocks: `discovery`, `browse`, `inspector`, `read`, `write`, `methods`,
`limits`.

## Part 3 - Keeping data coming

| Chapter | Title | The one thing to understand | Example |
| --- | --- | --- | --- |
| 10 | Subscriptions | A monitored item reports a change, and a subscription is only what the server agreed to keep: request, answer, revision | `examples/ch10_subscriptions.py` |
| 11 | Dispatch | One ordered consumer per subscription, many handlers per tag, and every handler's failure counted separately | `examples/ch11_dispatch.py` |
| 12 | Supervision | Healthy, degraded, unusable and disconnected are four different problems; silence is a reason to ask, never a reason to rebuild | `examples/ch12_supervision.py` |

Building blocks: `subscriptions` (manager, model, dispatch), `resilience`.

## Part 4 - Application shapes

| Chapter | Title | The one thing to understand | Status |
| --- | --- | --- | --- |
| 13 | Discovery-driven configuration | Ask the server what it offers, then build the client from that answer instead of from a template | built: `examples/ch13_discovery.py` |
| 14 | Configuration that refuses mistakes | Typed settings, validated bounds, and the difference between a default and a decision | next |
| 15 | Queues and buffering | A gateway that stops collecting must not stop storing: what to buffer, what to drop, and what to admit to | next |

## Part 5 - Building a server

| Chapter | Title | The one thing to understand |
| --- | --- | --- |
| 16 | An address space you authored | Object types, variable types, and why a server that only has base nodes is unreadable |
| 17 | Data quality and timestamps | StatusCode, SourceTimestamp and ServerTimestamp are the data; a value without them is an assumption |
| 18 | Methods and node management | Server-side behaviour, and the difference between a node that exists and one a client may add |
| 19 | Events | A monitored item on an event source is a different service path than a data change, with its own filter and select clause |
| 20 | History | What historization on the server gives you, what it does not, and how to build a historian of your own |

## Part 6 - Companion specifications

| Chapter | Title | The one thing to understand |
| --- | --- | --- |
| 21 | Information model inheritance | How a companion spec extends the base model, and what a client sees when two models overlap |
| 22 | Reading a companion specification | OPC 10000-7 and friends: what is normative, what is a figure, and what a vendor is allowed to omit |
| 23 | Applying one | Building and consuming an address space that follows a companion specification, including the nodes you must not invent |
| 24 | Two models in one server | Vendor extensions next to a companion model: detection, fallback, and the failure mode of "it browsed fine in the demo" |

## Part 7 - Security

| Chapter | Title | The one thing to understand |
| --- | --- | --- |
| 25 | Policies, certificates and trust | Security is a negotiation with material attached: policy, mode, certificate, and the application URI that has to match |
| 26 | Users, roles and permissions | Authentication and authorisation are separate services, and `AccessLevel` is neither of them |
| 27 | Deployment | Ports, containers, announced addresses, reverse proxies and firewalls - what has to be true on the network for a client to connect |

## Part 8 - Production concerns

| Chapter | Title | The one thing to understand |
| --- | --- | --- |
| 28 | Logging and diagnostics | A log line is an interface: what a gateway has to say about limits, revisions, refusals and silence |
| 29 | Testing | Unit tests for decisions, integration tests against a real server, and fault injection for the cases a demo cannot produce |
| 30 | Interfaces out | REST, messaging and health endpoints: exposing an OPC UA client without turning it into a web server |
| 31 | Architecture | The shape of an edge gateway: components, lifetimes, restarts, configuration and what a deployment must survive |

## Capstone - the OPC UA edge gateway

One program that reads a companion-specification address space (Part 6) through a
secured session (Part 7), supervises its subscriptions (Part 3), buffers and
replays what it could not send (Part 4), exposes health and data over an interface
(Chapter 30), is deployable as a container (Chapter 31) and explains itself in logs
(Chapter 28). Every component in it is a chapter's building block; none of them is
written for the capstone.

## What "done" means here

A chapter is done when its example runs against the demo server, its tests pass,
`mypy` and `ruff` are clean, and its claims can be checked with a second client
(UaExpert or Prosys) against the same server. The chapter pages carry a
verification section for exactly that: [docs/VERIFICATION_TOOLING.md](VERIFICATION_TOOLING.md).
