# Chapters

Sixteen chapter pages exist: three written (Part 1) and thirteen built and tested
(Parts 2 to 5). The rest are planned in [../ROADMAP.md](../ROADMAP.md) and will be added
as their parts are built.

A chapter page always has the same six sections:

1. **Why this matters** - the problem the chapter solves.
2. **OPC UA theory** - what the specification says, without the code.
3. **Implementation** - how this repository solves it, with the real class names.
4. **Verification** - how to check the claims with the CLI, with UaExpert, and with Prosys.
5. **Common mistakes** - what goes wrong in real projects, and what it looks like.
6. **What's next** - which chapter reuses this one's building block.

| Chapter | Title | Part | Status | Example |
| --- | --- | --- | --- | --- |
| [01](ch01_what_opcua_actually_is.md) | What OPC UA actually is | 1 | written | - |
| [02](ch02_nodes_references_namespaces.md) | Nodes, references and namespaces | 1 | written | - |
| [03](ch03_how_to_read_this_repository.md) | How to read this repository | 1 | written | - |
| [04](ch04_first_connection.md) | The first connection | 2 | built and tested | `examples/ch04_first_connection.py` |
| [05](ch05_browsing.md) | Browsing | 2 | built and tested | `examples/ch05_browsing.py` |
| [06](ch06_node_metadata.md) | Node metadata | 2 | built and tested | `examples/ch06_node_metadata.py` |
| [07](ch07_bulk_read.md) | Reading values | 2 | built and tested | `examples/ch07_bulk_read.py` |
| [08](ch08_bulk_write.md) | Writing values | 2 | built and tested | `examples/ch08_bulk_write.py` |
| [09](ch09_methods.md) | Methods | 2 | built and tested | `examples/ch09_methods.py` |
| [10](ch10_subscriptions.md) | Subscriptions | 3 | built and tested | `examples/ch10_subscriptions.py` |
| [11](ch11_dispatch.md) | Dispatch | 3 | built and tested | `examples/ch11_dispatch.py` |
| [12](ch12_supervision.md) | Supervision | 3 | built and tested | `examples/ch12_supervision.py` |
| [13](ch13_discovery_driven_configuration.md) | Discovery-driven configuration | 4 | built and tested | `examples/ch13_discovery.py` |
| [14](ch14_configuration.md) | Configuration that refuses mistakes | 4 | built and tested | `examples/ch14_configuration.py`, `examples/gateway.yaml` |
| [15](ch15_queues_and_buffering.md) | Queues, buffering, and the file that survives the outage | 4 | built and tested | `examples/ch15_buffering.py` |
| [16](ch16_authored_address_space.md) | Author an address space, and read back what it promises | 5 | built and tested | `examples/ch16_authored_address_space.py` |

Chapters 17-31 and the capstone are described in the roadmap: the rest of the server side,
companion specifications, security, logging, testing, interfaces and
architecture, ending with the OPC UA edge gateway.

## Running any built chapter

```powershell
uv run python examples/ch10_subscriptions.py --demo
uv run python examples/ch10_subscriptions.py --demo --demo-port 48401   # fixed address, for UaExpert
uv run python examples/ch10_subscriptions.py --endpoint opc.tcp://127.0.0.1:4840
```

Every chapter also has tests. `tests/unit/` proves the decisions; `tests/integration/`
proves them against a live asyncua server that the fixtures start themselves. The
mapping from chapter to test file is in each chapter's Implementation section.
