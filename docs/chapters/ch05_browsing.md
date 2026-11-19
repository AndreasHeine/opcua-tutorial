# Chapter 5 - Browsing

*Part 2 · status: built and tested · example: `examples/ch05_browsing.py` ·
tests: `tests/unit/test_browse_model.py`, `tests/integration/test_browse.py`*

## 1. Why this matters

A hardcoded NodeId is a promise about somebody else's address space. It is the first
thing that breaks when a vendor ships a new build, a customer renames a folder, or a
project moves to a second machine - and it breaks in a way that looks like an OPC UA
bug and is really a naming bug.

Browsing is how a client keeps its promises to itself: it reaches tags **by name**,
through the references the server publishes, and it learns the namespace index from the
server instead of guessing it.

## 2. OPC UA theory

`Browse` is a **reference** service. It answers "what is connected to this node, in this
direction, of these reference types" - it says nothing about values. That is why a
browse result gives you `BrowseName`, `NodeId`, `NodeClass` and `DisplayName`, and never
a value.

Four things in the service are worth knowing before writing a client:

- **Result limits and continuation points.** A server may answer a `Browse` with only
  part of the children and hand back a *continuation point* per node. `BrowseNext`
  drains it, and the continuation point is per-node state the server is entitled to
  expire - a client that ignores it sees an address space that appears to have fewer
  children than it does.
- **`NodeClass` is a server-side filter, not a permission.** Asking for `Variable`
  children filters by node kind. A read-only tag is still a `Variable`.
- **`MaxNodesPerBrowse`** bounds how many starting nodes one `Browse` request may name.
  Walking a big tree is therefore a chunked service, like `Read`.
- **`TranslateBrowsePathsToNodeIds`** resolves a `BrowsePath` - a chain of
  `RelativePath` elements - in one request. Each element is a `BrowseName`, which is a
  **qualified** name: it belongs to a namespace index. Resolve `ProductionLine/Line1/Temperature`
  against the wrong index and the answer is `BadNoMatch`, not "not found, try another
  index".

A `BrowsePath` starts at a node with a known NodeId (`ObjectsFolder`, `i=85`, is the
normal one) and follows **forward hierarchical references**. Path navigation is not
search: it is a sequence of steps you must be able to name.

## 3. Implementation

`NamespaceExplorer` is this chapter's building block, and it is used by every later
chapter that needs a node:

| Method | What it does |
| --- | --- |
| `namespaces()` | the namespace array, as `(index, uri)` pairs |
| `children(node, node_class=..., reference_type=...)` | one level, with the server-side filters applied |
| `walk(root, max_depth=..., max_nodes=...)` | the tree, chunked to the server's `MaxNodesPerBrowse`, returning `BrowseTree` + `BrowseStats` |
| `walk_flat(...)` | the same walk as a flat list of `NodeRef` |
| `find_by_browse_path(["ProductionLine", "Line1"])` | step-by-step resolution by name |
| `resolve_path_with_translate([...], namespace_index=...)` | one `TranslateBrowsePathsToNodeIds` request |
| `describe_node(node, browse_name=...)` | one node, when you know its id |

Continuation points are drained by the explorer, and a continuation point the explorer
stops on is **released** rather than left to expire - `BrowseStats` counts requests,
references, errors and continuation points so a walk's cost is visible instead of
guessed.

### What the example actually prints

```text
1. the namespaces, in the order the server publishes them
  ns=0  http://opcfoundation.org/UA/
  ns=1  urn:freeopcua:python:server
  ns=2  urn:opcua-tutorial:demo
  application namespaces: [1, 2]

3. the same walk as a flat list, keeping only the variables
  variables collected: 514, of which 47 hold this server's data
  Temperature            ns=2;s=Temperature
  ...
  nodes=731 references=730 browse_calls=78 browse_next_calls=0 errors=0
  the walk browsed every level and filtered on the way home: a server-side Variable
  filter here would have stopped at the Objects and found nothing

5. the same path with one TranslateBrowsePathsToNodeIds
  ns=0:Temperature -> TranslateBrowsePathsToNodeIds returned BadNoMatch for path ProductionLine, Line1, Temperature
  resolved in one request -> ns=2;s=Temperature
  ns=2 came from the browse above, not from a constant in this file
```

Two numbers in that output are the lesson:

- **514 variables, 47 of them yours.** The standard namespace is huge and mostly
  plumbing. A client that "collects all variables" on a real server collects the
  specification's own nodes too, and then wonders why its subscription has 3 000 items.
  Filter by namespace, not by node class alone.
- **78 `Browse` calls for 731 references**, because the explorer chunks each request to
  the server's published `MaxNodesPerBrowse = 10`. In this run no continuation point had
  to be drained (`browse_next_calls=0`): the demo server answered each browse in full.
  The explorer handles `BrowseNext` when a server does split an answer - which a big
  vendor server will.

And the deliberate failure: asking `TranslateBrowsePathsToNodeIds` for the path in
`ns=0` returns `BadNoMatch`, because a `BrowseName` belongs to a namespace. The example
reads the index from the browse it just did and asks again - one request, correct answer.

## 4. Verification

```powershell
uv run python examples/ch05_browsing.py --demo --demo-port 48401
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48401 --path "ProductionLine/Line1"
uv run opcua-toolkit browse --endpoint opc.tcp://127.0.0.1:48401 --flat --depth 3
```

- **UaExpert**: expand *Objects* and confirm `ProductionLine` and `Line1` are objects and
  that the tags below them are variables with NodeIds like `ns=2;s=MotorRpm`. Right-click
  `Temperature` → *Browse Children* and *Browse Parents*: the parent step is the inverse
  of `HasComponent`, which is the direction a client has to ask for explicitly. Then use
  *Address Space* filters to browse **only** `Variable` children of `Line1` and confirm
  you get 36 - the same number the example prints - and that the read-only tags are in
  the list.
- **Prosys UA Simulation Server**: run the same scripts against its server
  (`--endpoint opc.tcp://127.0.0.1:4840`). Its address space has a different layout and a
  different namespace index; the example's output changes and its code does not. That is
  the point of browsing.
- **Test proof**: `tests/integration/test_browse.py` asserts the namespace array, the
  chunked walk, the `NodeClass` filter, path resolution, and that a missing path raises
  `BrowsePathError` naming the path that failed.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Hardcoding NodeIds in a config file | works until the vendor renames a folder | resolve paths by name, once, at startup |
| Filtering on `NodeClass` and expecting permissions | read-only tags appear in the list | `NodeClass` says kind; Chapter 6 says rights |
| Filtering on `Variable` **at the top level** | the walk returns the objects and stops, "no variables found" | walk every level, filter on the way home (as `walk_flat` does) |
| Guessing the namespace index in a browse path | `BadNoMatch` for a path that clearly exists | read the namespace array first |
| Ignoring continuation points | an address space that seems to have 10 children per folder | `BrowseNext`, and release what you will not read |
| Treating browse as a value service | browsing 500 nodes, then reading them one by one | browse for structure, then bulk `Read` (Chapter 7) |
| Browsing the whole tree on a big server, repeatedly | slow startup, server browse pressure, timeouts | cache the resolved NodeIds, keep `max_nodes` bounds, re-browse on demand |
| Using `DisplayName` for lookups | breaks when a server publishes a second language | use `BrowseName` |

## 6. What's next

Browsing gets you **to** a node. Chapter 6 asks the node what it is: data type, array
rank, access rights, sampling floor, and whether history is switched on. Chapter 7 then
reads the values you found in batches sized by the limits you read in Chapter 4.
