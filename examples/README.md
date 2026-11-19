# Examples

One runnable script per chapter. Each script prints what it did and why, so the
output is the lesson; the test suite is the proof. Nothing here is a snippet you
have to paste into a file: every example is checked by `ruff`, `mypy` and the
same import rules the toolkit itself follows.

The chapter numbers are the ones used in `docs/ROADMAP.md` and in the toolkit
docstrings, so a script, a test and a chapter page all say the same number.

## Part 2 - reading a server (Chapters 4-9)

| Chapter | Example | What it proves |
| --- | --- | --- |
| 4 | `ch04_first_connection.py` | A connection that reports what the server actually is |
| 5 | `ch05_browsing.py` | Finding your tags by browsing instead of hardcoding NodeIds |
| 6 | `ch06_node_metadata.py` | Whether your application may touch this node at all |
| 7 | `ch07_bulk_read.py` | Bulk reads are a number you can measure, not a trick |
| 8 | `ch08_bulk_write.py` | "No exception" is not "it worked" |
| 9 | `ch09_methods.py` | Calling a method you have never seen before |

## Part 3 - keeping data coming (Chapters 10-12)

| Chapter | Example | What it proves |
| --- | --- | --- |
| 10 | `ch10_subscriptions.py` | The server's answer about a subscription, not the client's request |
| 11 | `ch11_dispatch.py` | One ordered consumer, many handlers |
| 12 | `ch12_supervision.py` | Healthy, degraded, unusable and disconnected are four problems |

## Part 4 - building an application (Chapter 13 onwards)

| Chapter | Example | What it proves |
| --- | --- | --- |
| 13 | `ch13_discovery.py` | Ask the server before you configure the client |

Chapters 14 and beyond are added as their parts are built; the numbering above is
the numbering those files will use.

## Running one

Every example can start the tutorial's own demo server, so it runs on a machine
with no OPC UA server installed:

```powershell
uv run python examples/ch07_bulk_read.py --demo
```

`--demo-port` pins the server to one address, which is what you want while
UaExpert or Prosys is pointed at it:

```powershell
uv run python examples/ch10_subscriptions.py --demo --demo-port 48401
```

`--endpoint` runs the same script against a real server instead, and the
demo-only steps are skipped rather than faked:

```powershell
uv run python examples/ch05_browsing.py --endpoint opc.tcp://127.0.0.1:4840
```

## The shared harness

`_support.py` is not part of the toolkit and is not importable from outside
`examples/`. It holds the three things every chapter script needs: the argument
handling above, a demo server lifecycle (`chapter_run`, `restart_demo_server`),
and the small print helpers (`heading`, `bullet`).

Because it is shared, an example never hides a server it started: a script that
needs demo-only behaviour asks for it explicitly with `run.uses_demo_server` and
`run.demo_server`, which is why the same script works unchanged against a server
it does not control.
