# Chapter 9 - Methods

*Part 2 · status: built and tested · example: `examples/ch09_methods.py` ·
tests: `tests/unit/test_method_model.py`, `tests/integration/test_methods.py`*

## 1. Why this matters

A method is the only OPC UA service that asks the server to **do** something: start a
line, acknowledge an alarm, change a mode. It is also the service most likely to be
written badly, because it looks like a function call: hardcoded NodeId, positional
arguments, and a `Good` status treated as success.

The chapter's rule is the opposite: **read the declaration first, then call by
argument name.** A method you have never seen before can then be called correctly,
because its argument list is part of the address space.

## 2. OPC UA theory

A `Method` node declares its interface as two variables:

- `InputArguments`: a list of `Argument` - `Name`, `DisplayName`, `Type`, `ValueRank`,
  `ArrayDimensions`, `Description`;
- `OutputArguments`: the same shape for the results.

`Call` sends a `CallMethodRequest` - the **parent** NodeId (the object the method
belongs to), the method NodeId, and `InputArguments` as `Variant`s - and answers with a
`CallMethodResult` containing:

| Field | Meaning |
| --- | --- |
| `StatusCode` | whether the *call machinery* worked |
| `InputArgumentResults` | one `StatusCode` per argument, `Good` or a per-argument refusal |
| `OutputArguments` | the method's answers, in the order of `OutputArguments` |

That structure produces the two failure modes a method client must handle:

1. **`Good` with rejected arguments.** The call ran; the server refused one input;
   `InputArgumentResults[1] = BadInvalidArgument`. Nothing throws.
2. **`Good` with a negative answer.** `AcknowledgeAlarm` returns `Accepted = False`.
   The action was refused, and a client that only logs the status code records a success
   that never happened.

Two more facts:

- `Executable` and `UserExecutable` are separate attributes: a method you can **see** is
  not a method you may **run** (Chapter 26 handles the permission side properly).
- `MaxNodesPerMethodCall` bounds how many method calls one `Call` request may carry, so
  batched calls are chunked like reads and writes.

And the discovery trap: on any real server, most methods belong to the **server itself**.
`GetMonitoredItems`, `AddUser`, `DeleteUser`, `Open`, `Close` and friends live in `ns=0`
and are OPC UA's own services. Two of them appear in this tutorial later (Chapter 25 uses
the user management methods), and section 7 of this chapter asks the demo server whether it
can actually answer `GetMonitoredItems` - which it cannot. They are not a plant's operations,
and a client that "finds the methods and calls the first one" will add a user to a server.

### A declaration is not an implementation

Every method node in the address space carries a signature. That is what makes "call a method
you have never seen" possible. It is also what makes a subtler mistake possible: a server can
publish a method it cannot execute. `GetMonitoredItems` sits on the `Server` object of the
standard address space, it is marked executable, and this tutorial's server answers
`BadNothingToDo` when it is called - even for a subscription that is live and has monitored
items in it. Where a *vendor* server implements it, it is the best cross-check a subscription
client has; the only way to know which kind of server you are talking to is to call it once
and read the answer.

## 3. Implementation

`MethodCatalog` reads declarations; `MethodInvoker` calls with named arguments:

```python
from opcua_toolkit import MethodCatalog, MethodInvoker

catalog = MethodCatalog(client, limits=limits)

# One root, one walk: the server decides where its methods live, not this file.
methods = await catalog.discover_under(ua.ObjectIds.ObjectsFolder, max_depth=6, max_nodes=4_000)
plant_methods = [item for item in methods if item.parent_node_id.NamespaceIndex != 0]
for method in plant_methods:
    print(method.signature(), method.executable, method.callable_by_user)

invoker = MethodInvoker(client, limits=limits)
result = await invoker.call(start_line, {"Mode": 7})  # argument names, from the declaration
result.ok  # call machinery said Good
result.rejected_arguments  # names of arguments the server refused, if any
result.output_of("Accepted")  # by name, not by position
```

| API | What it is for |
| --- | --- |
| `discover_under(root, *, max_depth, max_nodes)` | every method below one object, each with its declaration read from the address space |
| `MethodInvoker.call(method, {"Name": value})` | one call, arguments matched against `InputArguments` by name |
| `call_many([(method, arguments), ...])` | several calls, chunked to `MaxNodesPerMethodCall` |
| `CallResult` | status, `InputArgumentResults` and named outputs kept together |

`MethodNotFoundError` is raised **locally** when an argument name is not in the
declaration - the request never leaves the client, because a wrong argument name is a bug
in the caller and the cheapest place to catch it is before the wire.

`call_many` batches calls, chunked to `MaxNodesPerMethodCall`. `CallResult` keeps the
status, the per-argument results and the named outputs together, so a log line can tell a
refusal apart from a failure.

### What the example actually prints

```text
1. every method this server advertises
  137 methods found by browsing, none hardcoded
    132 of them belong to the server itself (ns=0)
    5 belong to an application namespace: the ones an operator means
    StartLine            StartLine(Mode: UInt32) -> Accepted: Boolean
                         parent=ns=2;s=Line1 executable=True callable_by_user=True

4. an argument the server never declared
  MethodNotFoundError: StartLine does not accept argument(s): totally_made_up
  the request never left the client: a wrong argument name is a bug in the caller

5. Good is not the same as success
  AcknowledgeAlarm status=Good (0x00000000) outputs={'Accepted': False}

6. calls are chunked too
  7 calls to StopLine became 2 CallMethod request(s)
  MaxNodesPerMethodCall=5 -> expected 2

7. a method every server declares, and what this one answers
  GetMonitoredItems(SubscriptionId: UInt32) -> ServerHandles: UInt32[], ClientHandles: UInt32[]
    node=ns=0;i=11492 parent=ns=0;i=2253 executable=True callable_by_user=True
  MethodCallError: GetMonitoredItems returned BadNothingToDo
  declared, executable, callable by this user - and nothing implements it:
  a method node is a name and a signature, not a promise that the server can do the work
```

137 methods, 132 of them the server's own. That ratio is not a quirk of the demo server -
a server that loads the full standard address space looks like that, and it is why method
discovery has to be scoped to an application namespace and a parent object.

## 4. Verification

```powershell
uv run python examples/ch09_methods.py --demo --demo-port 48401
uv run opcua-toolkit call --endpoint opc.tcp://127.0.0.1:48401 --list-methods --root "ns=2;s=Line1"
uv run opcua-toolkit call --endpoint opc.tcp://127.0.0.1:48401 --method AcknowledgeAlarm --arg AlarmId=no-such-alarm
uv run opcua-toolkit call --endpoint opc.tcp://127.0.0.1:48401 --method GetLineStatus
```

A plain `call` prints one method, its NodeId, the status and the named outputs - it browses to
*find* the declaration but does not print a hundred declarations on the way to
`Accepted = False`. `--list-methods` is the explicit request for the declarations, and it is
scoped by `--root`: from `ns=2;s=Line1` it lists the five operations a line operator means.

What a walk finds also depends on where it starts and how deep it is allowed to go. The CLI's
default (`--root i=85`, the toolkit's default depth) finds **107** methods on this server;
`--root i=2253` (the `Server` object) finds **133**; `ch09_methods.py` asks for `max_depth=6`
from `ObjectsFolder` and finds **137**. Same address space, three answers - which is the
chapter's point in its least flattering form: a discovery result is a function of the question.

- **UaExpert**: right-click `Line1` → *Call Method...*. Pick `AcknowledgeAlarm` and enter an
  unknown alarm id. UaExpert shows `Good` at service level and `Accepted = False` in the
  outputs - the two-line difference the chapter is about. Then open `StartLine` and look at
  its *InputArguments* / *OutputArguments* properties in the *Attributes* view: that is the
  declaration `MethodCatalog` reads.
- **Prosys**: do the same against the demo server, and then against Prosys's own server,
  whose simulation methods are different names with the same declaration mechanism.
- **A declaration is not an implementation.** The standard address space puts
  `GetMonitoredItems` on the `Server` object (`ns=0;i=2253`; `ServerType` is `i=2004`, and
  that object's `ServiceLevel` variable is `i=2267`), declared as
  `GetMonitoredItems(SubscriptionId: UInt32) -> ServerHandles: UInt32[], ClientHandles: UInt32[]`
  and marked executable. Call it against the tutorial's server and it answers `BadNothingToDo` -
  including for a subscription that is live and has monitored items in it. asyncua's server
  publishes the method node and does not implement the service behind it. Ask a vendor server
  the same question and you may get the list: which is exactly why you call it once before
  your architecture starts depending on it.
- **The toolkit's own refusal**: `opcua-toolkit call --endpoint ... --method GetMonitoredItems`
  with no `--arg` never reaches the server. It prints
  `error: call failed: GetMonitoredItems is missing argument 'SubscriptionId'`, because the
  declaration said so. Asking the server to guess an argument is not something a Chapter 9
  client does.
- **Test proof**: `tests/integration/test_methods.py` discovers the demo methods against a
  live server, calls `AddValues` and asserts the typed output, asserts that an unknown
  argument is refused before the request, and asserts the chunking against
  `MaxNodesPerMethodCall`.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Hardcoding a method's NodeId | breaks when the plant redeploys | discover it under its parent object by name |
| Passing positional arguments | silently wrong the day somebody reorders the declaration | pass a dict of names |
| Treating `Good` as success | a refused action logged as done | check `outputs` and `InputArgumentResults` |
| Not checking `UserExecutable` | `BadUserAccessDenied`, or a call that "works" on the demo server only | read both `Executable` and `UserExecutable` |
| Calling the first method you find | `AddUser` on a production server | scope discovery to an application namespace |
| Ignoring `InputArgumentResults` | one argument rejected, the method answered anyway | `CallResult.rejected_arguments` |
| Building untyped variants | `BadTypeMismatch` on the argument | build from the declared `Type` and `ValueRank` |
| Sending 100 calls in one request | `BadTooManyNodesSpecified` | `call_many`, chunked to `MaxNodesPerMethodCall` |
| Assuming a method is idempotent | retried side effects (a second alarm acknowledgement) | do not blind-retry calls; make the method safe or check its output (Chapter 31) |
| Calling `GetMonitoredItems` because the address space has it | `BadNothingToDo`, even for a live subscription with items in it | treat the service as optional server behaviour and cross-check with a second client instead |
| Searching from the wrong root, or not deep enough | a method that clearly exists is reported as "not found" | start from the object that owns it, and raise the depth bound before concluding it is missing |

## 6. What's next

Part 2 ends here: the tutorial can now reach a node, describe it, read it in bulk, write
to it safely and make it do something. Part 3 stops asking the server for values and starts
letting the server report them - Chapter 10 is subscriptions, and `GetMonitoredItems`
(mentioned above) becomes the tool Chapter 11 uses to check the server's own bookkeeping.
