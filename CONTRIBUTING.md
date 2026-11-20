# Contributing

This tutorial is a codebase that teaches. A contribution is good when it makes a claim
harder to doubt - not when it adds words.

## What this repository is for

An executable, current, production-shaped OPC UA tutorial: asyncua 2.x, Python 3.13, typed,
linted, tested, containerised, and organised as 31 chapters ending in an edge gateway. The
plan and current status live in [docs/ROADMAP.md](docs/ROADMAP.md).

## The rule every chapter has to satisfy

A chapter is **built and tested** only when all six exist:

| Piece | Where | Requirement |
| --- | --- | --- |
| an example | `examples/chNN_snake_case.py` | runs with `--demo`, runs with `--endpoint`, terminates by itself |
| a building block | `src/opcua_toolkit/<module>.py` | importable without the example, reusable by a later chapter |
| unit tests | `tests/unit/test_<area>.py` | the decisions, without a server |
| integration tests | `tests/integration/test_<area>.py` | the protocol claims, against a live asyncua server |
| a chapter page | `docs/chapters/chNN_snake_case.md` | the six sections below |
| roadmap status | `docs/ROADMAP.md`, `examples/README.md`, `docs/chapters/README.md` | says what exists, in the same words everywhere |

A chapter page's six sections, in this order:

1. **Why this matters** - the problem, in production terms.
2. **OPC UA theory** - the specification's side, without this repository's code.
3. **Implementation** - the real class and method names, plus a quoted excerpt of the
   example's actual output.
4. **Verification** - CLI commands, what to check in UaExpert, what to check in Prosys, and
   which tests prove it.
5. **Common mistakes** - a table: mistake, how it shows up, fix.
6. **What's next** - which chapter reuses this building block.

## Adding or changing a chapter

1. **Pick the number and keep it.** Chapter numbers are fixed by the toolkit's docstrings, so
   `Chapter 12` must mean the same thing in the code, the example, the tests, the chapter page
   and the roadmap. If a topic does not fit the plan, say why in the pull request rather than
   renumbering somebody else's chapter.
2. **Build the block before the story.** Write the toolkit piece and its tests first; the
   example is the last artifact, not the first.
3. **Make the demonstration visible.** If a claim needs a server that *refuses*, add a demo
   server capability (`DemoServerConfig` field + a CLI flag) rather than a fake in the
   example. Chapter 10's small subscription caps exist for exactly this reason.
4. **Run the gates** (below) and then run the example with `--endpoint` against Prosys or a
   vendor server. If it only works against the demo server, it is not a chapter yet.
5. **Quote real output** in the chapter page. Paste what the run printed, not what you expect
   it to print. If a number is in the text (514 variables, 5 requests, 2 CallMethod requests),
   it came from a run.
6. **Say what is demo-server-specific.** "asyncua's server ignores `ModifySubscription`" is a
   true sentence about one implementation. Write it as that, and write the general lesson next
   to it.

## Code rules

These are enforced by `ruff` and `mypy` where possible, and by review where not:

- **asyncua is a boundary, not an API.** Toolkit functions accept toolkit types
  (`NodeIdLike`, `WriteItem`, `SubscriptionOptions`) and return toolkit types. Do not leak
  asyncua wire objects into a chapter's data model, and do not call private asyncua APIs
  (`_subscribe`, `_server_params`): if information is missing from a high-level call, ask the
  service directly the way `confirm_subscription()` does.
- **No global state, no module-level clients.** Configuration enters through a constructor or
  a settings object; components are composed, not imported at a distance.
- **Nothing blocks.** No `time.sleep`, no synchronous I/O in an async path. Timeouts go
  through `asyncio.timeout` or an explicit `max_wait`, and waiting on a notification is
  `wait_for_*`, not a sleep.
- **One outcome per requested item.** Any service that answers per item (`Read`, `Write`,
  `Call`, `CreateMonitoredItems`) must keep per-item statuses visible. Collapsing a partial
  failure into one exception is a bug in this repository, even when it is convenient.
- **Errors carry sentences.** `OpcuaToolkitError` subclasses have actionable messages: what
  failed, on which node, with which status code. `ConfigurationError` for bad settings,
  `EndpointNotFoundError` for a discovery answer that does not contain what you asked for,
  `PartialFailureError` when a batch must not be walked past.
- **Keyword-only where a mistake is possible.** `*, force=False`, `*, key=None`,
  `*, with_limits=True`. A boolean flag in a positional call is a bug waiting to happen.
- **Docstrings describe behaviour, not the signature.** Google style (`Args`, `Returns`,
  `Raises`), and the interesting sentences are the ones about what a server may do instead.
- **Print in the toolkit only where printing is the product** (`cli.py`, `devserver.py`);
  everywhere else logs through the module's own logger.

## Test rules

- A **unit test** proves a decision: classification, chunking, ordering, refusal, a message.
  It uses a stub, and it runs in milliseconds.
- An **integration test** proves a protocol claim against a live asyncua server, marked
  `pytestmark = pytest.mark.integration`. Servers use `port=0`, so parallel runs never
  collide, and assertions about an *announced* address use the `free_port()` fixture.
- **Assert against measurements, not internals.** The fixtures count real service calls on the
  client's session (`tests/conftest.py`), so "5 requests" is a measurement. Counting on the
  `uaclient` facade instead records zero for the calls a subscription makes - the fixtures do
  the counting where a subscription can be seen.
- **Test the failure you are teaching.** A missing node, a read-only tag, a wrong type, a
  subscription the server deleted, a probe that answers a status code instead of an `OSError`.
- **No sleeps as synchronisation.** Use the manager's `wait_for_*`, `wait_idle`, or an explicit
  timeout. A test that sleeps is a test that passes on the author's machine.
- Do not test asyncua. Test what this repository concluded from asyncua's behaviour, and say
  which server that behaviour belongs to.

## Documentation rules

- A claim is either **runnable**, **measured** or **marked as one implementation's behaviour**.
  Anything else belongs in a discussion, not in a chapter.
- Keep lines under about 100 characters in code and in tables, so a chapter is readable in a
  terminal and on GitHub without horizontal scrolling.
- Update every index when a chapter lands: `docs/ROADMAP.md`, `docs/chapters/README.md`,
  `examples/README.md`, and the status sentence in `README.md`.
- Do not add a chapter that implies a feature exists. If Part 6 is not built, the roadmap says
  "planned", and the chapters say nothing about it except what is coming.

## The gates

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests examples
uv run pytest tests/unit -q
uv run pytest tests/integration -q --tb=short
```

Or let the formatter fix your own work first:

```powershell
uv run ruff format .
uv run ruff check --fix .
```

CI runs the same commands on Linux and Windows, plus a container build and a run
([.github/workflows/ci.yml](../.github/workflows/ci.yml)). A pull request that is red in CI
is not reviewed.

## Pull requests

- One chapter, one topic, one pull request. A tooling change that a chapter needs is its own
  commit inside it.
- Describe what was **measured**, not only what was written: "the demo server answered
  `BadSubscriptionIdInvalid` to `ModifySubscription`, so the manager rebuilds instead of
  pretending" is a reviewable sentence.
- Show the output you got. Commands and their real output belong in the pull request.
- Do not reformat files you did not change, and do not rename public API without migrating the
  examples and the tests in the same commit.
- Dependencies: adding one needs a reason a standard library or asyncua cannot serve.
  `uv add <package>` updates `pyproject.toml` and `uv.lock` together; commit both.

## Reporting problems

Bug reports are most useful with: the chapter and file, the exact command, what the toolkit
printed, what UaExpert or Prosys showed for the same server, your OS and Python version, and
the asyncua version (`uv pip show asyncua`).

If a chapter's text and the example's output disagree, that is a bug in the text. If the
toolkit and a second client disagree about the protocol, that is a bug in the toolkit or in the
chapter's claim - either way, report it.

## Licence and ownership

Everything contributed is under the MIT licence in [LICENSE](../LICENSE). This repository began
as [AndreasHeine/opcua-tutorial](https://github.com/AndreasHeine/opcua-tutorial); the rewrite
keeps the subject and the licence, and the copyright line names the contributors collectively.
If that line has to name somebody specific, that is a repository-owner decision - raise an
issue rather than editing it in a pull request.
