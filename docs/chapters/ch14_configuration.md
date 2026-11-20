# Chapter 14 - Configuration that refuses mistakes

*Part 4 · status: built and tested · examples: `examples/ch14_configuration.py`,
`examples/gateway.yaml` · tests: `tests/unit/test_config.py`,
`tests/integration/test_config_review.py`*

## 1. Why this matters

Every chapter so far put its numbers in the example script. A deployment does not: it has a
file, and a file is edited by somebody who is not the author, usually months later, usually
under time pressure, and usually in a place where a comment is not read.

That makes a configuration file the first artifact in this tutorial that has to be correct
**without its author being present**. The question this chapter is about is the one a reviewer
asks before anything connects: *would this file have been wrong, and would anyone have noticed
before it ran?*

And the answer cannot be "be careful". A file is checked by a program that knows what the
numbers mean. Two facts do the work:

- every duration says which unit it is in, and every count says what it counts;
- the numbers in a file are **requests** to a server, and requests have arithmetic between
  them - a keepalive, a session timeout and a retry policy that disagree with each other are
  not three settings, they are one contradiction written in three places.

A toolkit that only validates each setting on its own validates each line and misses the file.

## 2. OPC UA theory

### A configuration file is a list of requests

Each setting in `examples/gateway.yaml` maps onto a parameter in a service request - or onto
nothing on the wire at all. Knowing which is the difference between a setting a server can
revise and a setting only this application obeys:

| Setting | Where it goes on the wire | Who has the last word |
| --- | --- | --- |
| `client.endpoint_url` | the `EndpointUri` picked out of a `GetEndpoints` answer | the server (Chapter 13) |
| `client.application_uri`, `application_name` | `ApplicationDescription` on `CreateSession` / `FindServers` | the client - and it is what the server's own session list shows |
| `client.session_timeout` | `RequestedSessionTimeout` | **revised by the server** (Chapter 4 measured `3600000 ms` come back as `600000 ms`) |
| `client.subscriptions.publishing_interval` | `RequestedPublishingInterval` | revised |
| `client.subscriptions.lifetime_count` | `RequestedLifetimeCount` | revised, and revised *down* more often than anything else |
| `client.subscriptions.max_keepalive_count` | `RequestedMaxKeepAliveCount` | revised |
| `client.subscriptions.sampling_interval`, `queue_size` | `RequestedSamplingInterval` / `QueueSize` on `CreateMonitoredItems` | revised (and on some servers the answer never reaches the client: Chapter 10) |
| `client.limits.*` | nothing: a chunking rule the client obeys | the server's published `OperationLimits` |
| `client.retry.*` | nothing: the transport layer's own schedule | the client |
| `supervision.*` | nothing at all | the client |

So the numbers split into three kinds - **negotiated** (requested/revised pairs),
**published** (the server's `OperationLimits`), and **application-local** (the client's own
timers). A configuration review that treats them as one flat list of integers cannot tell
which kind it is looking at, and that is exactly the review that passes a broken file.

### Lifetime and keepalive are counted in publishing intervals

`RequestedLifetimeCount` and `RequestedMaxKeepAliveCount` are not durations. OPC UA Part 4
defines them as counts **of publishing intervals**: the lifetime is how many publishing
intervals may pass without the client making a `Publish` request before the server deletes the
subscription; the keepalive count is how many publishing intervals of silence pass before the
server sends an empty `Publish` to say it is still there.

`lifetime_count: 10000` next to `publishing_interval: 500ms` is therefore `5000 s` of
tolerated silence - a number that does not appear anywhere in the file and is the number that
decides whether a reconnect fits inside the lifetime. The toolkit renders both forms in every
report and refuses to print a count without the duration it adds up to, because the count is
the form an author writes and the duration is the form that has a consequence.

### The arithmetic between the requests

Four relationships have to hold at the same time, and none of them is visible in a single
setting:

| Relationship | If it fails |
| --- | --- |
| `keepalive_count × publishing_interval < session_timeout` | the server promises to speak after the session carrying the subscription has already expired |
| `lifetime_count × publishing_interval ≥` the reconnect policy's longest wait | the server deletes the subscription before the client asks again (Chapter 12's outage) |
| `request_timeout < session_timeout` | a request is allowed to wait longer than the session that sends it |
| queue size vs `publishing_interval / sampling_interval` | a queue narrower than what one publish produces, i.e. data the server discards before the client asks |

The first three are **contradictions**: the numbers the file itself wrote do not fit together,
so no server is needed to know the file is wrong. The toolkit refuses them.

The fourth, and things like "this lifetime is longer than the session that carries it", are
**weaknesses**: the configuration runs and probably does not mean what its author pictured. The
toolkit reports those as review notes and *does not* refuse them. That split is the design
decision of this chapter, and it is deliberate in both directions:

- a contradiction that is allowed through becomes an outage at runtime;
- a weakness that is refused becomes a deployment that bypasses the checker - which means the
  checker is no longer in the loop, and the next file is unreviewed.

### Layers, and why the order is not negotiable

A setting can arrive from four places, and the toolkit resolves them in one fixed order:

```
toolkit default  <  what a server answered  <  the file  <  the environment
```

Each value records which layer answered for it, and every report prints that. The order has one
consequence that is easy to get backwards, and `examples/gateway.yaml` is written around it: **a
file outranks a discovery**. A file that writes `endpoint_url` beats the address Chapter 13
discovered - so the moment the server moves, the file is authoritative about a fact it does not
know. That is why this file writes no address at all, and why the chapter starts by loading it
with nothing supplied for the endpoint and showing the refusal.

## 3. Implementation

`src/opcua_toolkit/config.py` is one application's configuration as types.

| Piece | What it is for |
| --- | --- |
| `ApplicationConfig` | the three sections: `client`, `supervision`, `tags` |
| `client.subscriptions` | a `SubscriptionDefaults` model that lives **inside** `ClientSettings`: `ApplicationConfig.subscriptions` is a pass-through to it, so a file writes `client.subscriptions.*` and there is only one place for those timers to live |
| `TagSettings`, `SupervisionSettings` | one tag (name + exactly one locator + optional per-tag timers + `monitored`) and the supervision timers |
| `load_config(source, *, environ=…, derived=…, derived_source=…)` | reads a YAML file *or* a mapping, layers it, refuses unknown names, and returns a `ResolvedConfig` |
| `ResolvedConfig` | the validated settings, plus `resolved_values()` (every value with its layer), `provenance`, `describe()`, `review()`, `check_server(profile)`, `check_subscription(revision)`, `require_startable()`, `require_decided(*paths)` |
| `ConfigReview` | one review: `subscription_request`, `refusals`, `notes`, `findings`, `defaults_in_use`, `derived_in_use`, `is_startable`, `require_startable()`, `describe()`, `as_dict()` |
| `ConfigFinding` | one finding: severity (`refusal` or `review`), the dotted `path`, and a message a human reads |
| `ValueOrigin` / `ResolvedValue` / `ConfigLayer` | who answered for a setting, and whether it is a decision or a default. Secrets (`password`, `private_key_password`) are reported as `<kept out of this output>` |
| `discovery_settings(profile)` | the DERIVED layer from Chapter 13: an endpoint, and a sampling interval the server will honour |
| `config_fields()`, `leaf_fields()`, `section_paths()` | the field map the loader validates against, so "unknown setting" errors can name what exists and suggest what was meant |

Three rules the loader enforces, because they are where real config files go wrong:

**Units are part of the value.** A duration field reads a number in its own documented unit
(`session_timeout: 60` is 60 seconds), but a *string* has to say which unit it means: `60s`,
`500ms`, `0.5s`. A unitless string is refused - `'90'` is not a duration, it is a number
somebody has to guess the meaning of at 3 a.m. Counts carry their unit as a marker, so the
report renders `10 attempts`, `10000 publishing intervals`, `10 notifications per monitored
item`, `5 items in one request`, and `multiplier` / `jitter` are deliberately dimensionless.

**Environment variables are one scalar each.** The prefix is `OPCUA_TUTORIAL_` and the shape is
`OPCUA_TUTORIAL_SECTION__SETTING` (`__` nests). Anything else in the prefix is refused, so a
typo in a deployment's environment fails at startup instead of being silently ignored - the
most common way a "the environment overrides the file" story ends with the file still winning.

**A setting name that does not exist is an error, not an ignored line.** Unknown names are
reported with what does exist, including inside list entries (`tags.0.node_is` → *did you mean
'tags.node_id'*), and a section written as if it were a value names its children.

### What the example actually prints

One run of `examples/ch14_configuration.py --demo --demo-port 48432`. What is shortened, and
nothing else:

- the `[file: C:\Users\andre\GitHub\…\gateway.yaml]` origin column, written `…\examples\gateway.yaml`;
- part of the sixteen default lines in sections 2 and 3, and two of section 6's four review notes
  (every count quoted is the count as printed);
- two of section 5's eleven refused cases - "a security policy with no mode" and "a username with
  no password", quoted in the table below the block;
- fourteen of section 9's sixteen notification lines, marked `…`;
- the combined error message in section 8, whose two halves are quoted in full just above it, and
  the same two sentences abbreviated inside section 11's JSON;
- the timestamp and padding columns of the two log lines in section 9, which the toolkit's logging
  prints as `2026-11-19 16:18:57 | WARNING  | asyncua.client.client | …`.

Section 11 is an *excerpt* the example builds from `ConfigReview.as_dict()`: `review_notes` and
`defaults_nobody_decided` are this script's own names for `len(payload["reviews"])` and
`len(payload["defaults"])`, and `payload` is the review of section 8's file - which is why its
sampling interval reads `50 ms`, the value Chapter 13's discovery supplied, where the gateway file
wrote `100 ms`.

Everything else is output as printed, including the `asyncua` warning line the toolkit does not
suppress.

```text
1. a file with no endpoint in it is not a runnable configuration
  loading gateway.yaml with nothing supplied for the endpoint:
  configuration is invalid: client.endpoint_url: required, and nothing supplies it
  the file deliberately writes no address: Chapter 13 discovers one, and a file
  that writes an address is a file that goes stale the day the server moves

2. the file, plus what this server answered
  configuration from: the server this example started, C:\Users\andre\GitHub\opcua-tutorial\examples\gateway.yaml
  19 value(s) decided by somebody:
    client.endpoint_url = opc.tcp://127.0.0.1:48432  [derived: the server this example started]
    client.application_uri = urn:opcua-tutorial:gateway  [file: …\examples\gateway.yaml]
    client.session_timeout = 60 s (authored '60s')  [file: …\examples\gateway.yaml]
    client.subscriptions.publishing_interval = 500 ms (authored '500ms')  [file: …\examples\gateway.yaml]
    client.subscriptions.lifetime_count = 10000 publishing intervals  [file: …\examples\gateway.yaml]
    client.limits.max_nodes_per_read = 5 items in one request  [file: …\examples\gateway.yaml]
    tags = 3 tag(s): Temperature, Pressure, MotorRpm  [file: …\examples\gateway.yaml]
  16 value(s) left to defaults:
    client.max_concurrent_requests = 20 requests in flight  [default: the toolkit's default]
    client.password = <kept out of this output>  [default: the toolkit's default]
    client.subscriptions.max_keepalive_count = not set  [default: the toolkit's default]
    client.limits.max_nodes_per_browse = not set  [default: the toolkit's default]

3. what this file's own numbers add up to
  this client will ask for: publishing=500 ms sampling=100 ms queue=10 notifications per monitored
  item lifetime=10000 publishing intervals (5000 s) keepalive=90 publishing intervals (45 s)
  per_publish=10000 notifications per publish
  refusals: 0  reviews: 2
  review: client.subscriptions.lifetime_count: this subscription outlives a stopped publish request
  by 5000 s, but its session times out after 60 s: on a session loss the lifetime buys nothing,
  because the subscription goes with it
  review: supervision.silent_probe_limit: the supervisor asks about silence after 30 s; this
  server's keepalive would speak after 45 s. The client asks first, which costs one cheap request
  per window and does not wait for the server's permission
  19 value(s) authored, 3 tag(s) named:
    Temperature: path ProductionLine/Line1/Temperature  [monitored]
    Pressure: path ProductionLine/Line1/Pressure  [monitored]
    MotorRpm: path ProductionLine/Line1/MotorRpm  [kept unmonitored]

4. the environment overrides the file, and only for one scalar
  client.subscriptions.publishing_interval = 1000 ms (authored '1s')  [environment: OPCUA_TUTORIAL_* variables]
  supervision.poll_interval = 10 s (authored '10s')  [environment: OPCUA_TUTORIAL_* variables]
  OPCUA_TUTORIAL_QUEUE_SIZE__: OPCUA_TUTORIAL_QUEUE_SIZE__: expected OPCUA_TUTORIAL_SECTION__SETTING, e.g. OPCUA_TUTORIAL_CLIENT__SESSION_TIMEOUT
  OPCUA_TUTORIAL_TAGS: OPCUA_TUTORIAL_TAGS: an environment value must be one scalar; lists and sections belong in the configuration file
  OPCUA_TUTORIAL_NOT_A_SETTING: unknown setting(s): not_a_setting (environment: OPCUA_TUTORIAL_* variables)

5. what a configuration file cannot get away with
  a duration that does not say which unit it is in:
    configuration is invalid: client.session_timeout: '90' does not say which unit it is in:
    write 90s, or name the unit you mean (ms, s)
  a setting name that does not exist:
    unknown setting(s): client.session_timeoutte (file: given mapping) - did you mean 'client.session_timeout'?
  a section written as if it were a value:
    unknown setting(s): client (file: given mapping) is a section, not a value: write the settings
    below it (application_name, application_uri, endpoint_url, …)
  a tag that answers where it is twice:
    configuration is invalid: tags.0: tag 'Temperature' has both node_id and path: choose one. A node_id says 'this node, in this namespace'
    a path says 'find the node'. Answering both questions means one of them is decoration
  two tags with one name:
    tag name 'Temperature' appears twice: dispatch keys, buffers and logs all use it, so two tags
    with one name cannot be told apart
  a typo inside a list entry:
    configuration is invalid: tags.0.node_is: unknown setting - did you mean 'tags.node_id'
  a security policy with no mode:
    policy=Basic256Sha256 mode=None requires certificate and private_key
  a username with no password:
    username set without password
  a subscription lifetime shorter than the reconnect policy's longest wait:
    configuration contradicts itself: client.subscriptions.lifetime_count: this subscription may
    survive 2 s without a publish request, while the reconnect policy waits up to 8 s before asking
    again: the server deletes the subscription before the client comes back
  a keepalive that would arrive after the session carrying it expired:
    configuration contradicts itself: client.subscriptions.max_keepalive_count: the server is asked to speak after 500 s of silence, but the session carrying this subscription times out after 60 s: the keepalive would arrive after the subscription had gone with the session
  a request timeout at least as long as the session that sends it:
    configuration contradicts itself: client.request_timeout: a request may wait 90 s for an answer,
    which is at least as long as the 60 s session that sends it

6. what a running server adds to the review
  refusals: 0  reviews: 4
  review: client.security: this configuration is unsecured and this server publishes no secured
  endpoint either. Chapter 25 makes that a decision with certificate material behind it instead of
  a default nobody noticed
  review: client.subscriptions.sampling_interval: this configuration writes 100 ms while this
  server's published floor is 50 ms. The written value wins - a decision outranks a discovery -
  and the gap is 50 ms of rate this deployment is not using, which is worth a decision rather
  than a default

7. decisions this deployment still owes
  this file decides: client.application_uri, client.session_name
  a client built from defaults only: these settings must be decided by this deployment and are not:
  client.application_uri, client.session_name
  the server answered the questions it can answer, and these two are not among them: a discovered
  value is not a decision

8. a file that argues with this server
  refusal: tags.Temperature.node_id: this tag is in namespace 99 and this server publishes 3
  namespace(s): http://opcfoundation.org/UA/, urn:freeopcua:python:server,
  urn:opcua-tutorial:demo. A node cannot be monitored in a namespace the server does not have
  refusal: client.limits.max_nodes_per_read: the configuration allows 999 items in one request
  while this server publishes 10. An override exists to be more careful than the server, not to
  imagine that it is more generous
  and the command that would have started it:
    configuration contradicts itself: tags.Temperature.node_id: …; client.limits.max_nodes_per_read: …

9. running the file: monitored items from a configuration
  the limits this gateway will actually use (server published, file narrowed):
  max_monitored_items_per_call: 10
  max_nodes_per_browse: 10
  max_nodes_per_method_call: 5
  max_nodes_per_node_management: 100
  max_nodes_per_read: 5
  max_nodes_per_register_nodes: 100
  max_nodes_per_translate_browse_paths: 100
  max_nodes_per_write: 10
  min_supported_sample_rate: 50 ms
  Temperature: ProductionLine/Line1/Temperature -> ns=2;s=Temperature
  Pressure: ProductionLine/Line1/Pressure -> ns=2;s=Pressure
  WARNING asyncua.client.client: Revised values returned differ from subscription values: CreateSubscriptionResult(SubscriptionId=78, RevisedPublishingInterval=500.0, RevisedLifetimeCount=1000, RevisedMaxKeepAliveCount=90)
  INFO opcua_toolkit.subscriptions.manager: subscription 78 created with 500 ms publishing interval
  2 monitored item(s) created from 3 tag(s) configured (1 kept unmonitored)
  notification ns=2;s=Temperature = 22.36591385739083
  notification ns=2;s=Pressure = 4.447392469046055
  …   (14 further notification lines; the report below counts 16 of them)
  ModifySubscription: asked publishing=500 ms lifetime=10000 (5000 s) keepalive=90 (45 s); revised publishing=500 ms lifetime=1000 (500 s) keepalive=90 (45 s)
  what this server answered that this file did not already know:
  review: client.subscriptions.lifetime_count: this client asked for 10000 publishing intervals
  (5000 s) and this server answered 1000 (500 s). The answer is the lifetime that runs, and it is
  the number a reconnect has to fit inside
  subscription=78 publishing=500 ms items=2 failed=0 notifications=16 good=16 uncertain=0 bad=0
  suppressed=0 stray=0 errors=0 recreations=0 status=none mode=reporting

10. what a server answers when a file asks for more than it keeps
  publishing=500 ms sampling=50 ms queue=10 notifications per monitored item lifetime=30000 publishing intervals (15000 s) keepalive=0 publishing intervals (0 s) per_publish=10000 notifications per publish
  ModifySubscription: asked publishing=500 ms lifetime=30000 (15000 s) keepalive=0 (0 s); revised publishing=500 ms lifetime=1000 (500 s) keepalive=100 (50 s)
  review: client.subscriptions.lifetime_count: this client asked for 30000 publishing intervals (15000 s) and this server answered 1000 (500 s). The answer is the lifetime that runs, and it is the number a reconnect has to fit inside
  review: client.subscriptions.max_keepalive_count: this client asked for 0 publishing intervals
  (0 s) of silence and this server answered 100 (50 s); with a silence window of 60 s, this server
  speaks before the supervisor asks about the silence

11. the same review as data, for CI
{
  "subscription_request": "publishing=500 ms sampling=50 ms queue=10 notifications per monitored item lifetime=10000 publishing intervals (5000 s) keepalive=90 publishing intervals (45 s) per_publish=10000 notifications per publish",
  "startable": false,
  "refusals": [
    {
      "severity": "refusal",
      "path": "tags.Temperature.node_id",
      "message": "this tag is in namespace 99 and this server publishes 3 namespace(s): …"
    },
    {
      "severity": "refusal",
      "path": "client.limits.max_nodes_per_read",
      "message": "the configuration allows 999 items in one request while this server publishes 10. …"
    }
  ],
  "review_notes": 4,
  "defaults_nobody_decided": 28
}
```

Read the last two blocks together. Section 9 runs the tutorial's own file: no refusals, and the
server still cut its lifetime from 10 000 publishing intervals to 1 000 - a tenth of what was
asked, in a review that reported no refusal at all. Section 10 asks for three times as much
lifetime and for no keepalive at all, and gets the *same* `1000` back plus a keepalive the client
never requested. Nothing in either file was invalid, so nothing in a schema-based validator would
have said a word: the refusal-free file and the clamped file are indistinguishable until someone
compares the request with the answer. That comparison - `check_subscription(revision)` - is why
`ConfigReview` exists as a separate object instead of being a validator bolted onto the model.

And note the keepalive. The file never writes `max_keepalive_count`, and section 2 says so plainly:
`client.subscriptions.max_keepalive_count = not set [default: the toolkit's default]`. Yet section 3
prints `keepalive=90 publishing intervals (45 s)`, because `SubscriptionDefaults.keepalive_count()`
derives the count this client will request from the `session_timeout: 60s` the file *does* write -
75 % of that session at this publishing interval - which is what the client library has always done
silently. A default is printed as a default even when its value is computed, and that is the
difference between a number this deployment chose and a number it inherited.

## 4. Verification

```powershell
uv run python examples/ch14_configuration.py --demo --demo-port 48432

# the same review a deployment would run:
uv run opcua-toolkit config --file examples/gateway.yaml
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48433
uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://127.0.0.1:48433 --json
```

The three commands above were run, and what they printed is quoted below. Their exit codes were
`1`, `0` and `0`.

The first one matters most: the shipped file writes no address, so with nothing to fill that gap
the command refuses, and it refuses **without contacting anything** - no session, no `GetEndpoints`,
no server log line.

```text
$ uv run opcua-toolkit config --file examples/gateway.yaml
error: config failed: configuration is invalid: client.endpoint_url: required, and nothing supplies it
```

Against a demo server on port 48433 the same file resolves, and the review is the report quoted in
section 3 of this chapter plus two findings that only a server can produce (`client.security`,
`client.subscriptions.sampling_interval`): `refusals: 0  reviews: 4`.

### The claim that a contradiction needs no server, measured

To check that sentence rather than assert it, the file was copied with two changes: an
`endpoint_url` written, and `max_keepalive_count: 1000` added under `client.subscriptions`. The same
copy was then reviewed twice - once with no server at all, once with a live one - and the refusal
was identical:

```text
$ opcua-toolkit config --file gateway_overlong_keepalive.yaml          # nothing contacted
this client will ask for: … keepalive=1000 publishing intervals (500 s) …
refusals: 1  reviews: 2
refusal: client.subscriptions.max_keepalive_count: the server is asked to speak after 500 s of
silence, but the session carrying this subscription times out after 60 s: the keepalive would
arrive after the subscription had gone with the session

$ opcua-toolkit config --file gateway_overlong_keepalive.yaml --check-server opc.tcp://127.0.0.1:48434
refusals: 1  reviews: 4
refusal: client.subscriptions.max_keepalive_count: the server is asked to speak after 500 s of
silence, but the session carrying this subscription times out after 60 s: the keepalive would
arrive after the subscription had gone with the session
```

Exit code `1` both times, and stderr exactly one `error:` sentence each time - no traceback, which
is what a CI step needs to make a failure readable. `--json` prints the same review as data when the
file passes: `sources`, `settings`, and a `review` object with `subscription_request`, `startable`,
`refusals`, `reviews`, `derived` and `defaults`. When the review refuses, `--json` prints **nothing**
on stdout: one `error:` sentence on stderr and exit `1`. `--require-decided client.password` is the
third kind of failure, and it says so in the same shape:

```text
error: config failed: these settings must be decided by this deployment and are not: client.password
```

And one measurement worth making before believing the limits half of this chapter: with
`limits.max_nodes_per_read: 999` written into the copy, the review **with no `--check-server`** is
clean - `refusals: 0  reviews: 2`. Against the demo server, which publishes `10`, the same file
reports `refusals: 1` and exits `1`. A checker that never met the server cannot know that an attempt
to raise a limit was a lie, which is the whole reason `check_server` exists as a separate call.

- **UaExpert**: open a subscription with a 500 ms publishing interval and a lifetime far above
  it, then read the revised values UaExpert reports for the same subscription. UaExpert shows the
  revised numbers too; the difference is that the toolkit turns the gap into a sentence with the
  seconds spelled out. Then change the publishing interval in UaExpert's subscription dialog and
  watch the `Publish` frequency, and compare it with what this chapter's file asked for.
- **Prosys**: run the same file against Prosys's server with `--check-server`. Its published
  `OperationLimits` and its `MinimumSupportedSampleRate` are different numbers, so the review
  changes - the limits finding, the sample-rate finding and the chunking note belong to the server
  rather than to the file.
- **Test proof**: `tests/unit/test_config.py` pins the layer order and provenance, unit parsing and
  rendering, every refusal in section 5, the review notes, `require_decided()` and
  `require_startable()`, and the review's JSON payload (`as_dict()`). `tests/integration/test_config_review.py`
  runs the same review against a live server - the findings that depend on a server (an unknown
  namespace, an override more generous than what is published, a clamped lifetime, a keepalive the
  server raised from `0`) and the CLI contract itself are asserted against a real answer rather than
  against a hand-built profile.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Writing an endpoint address in the file | the file wins the argument the day the server moves | keep `endpoint_url` derived (Chapter 13) and let the loader refuse when nothing supplies it |
| A bare number for a duration | `90` means 90 s to the toolkit and 90 min to the next engineer | write the unit (`90s`); the loader refuses a unitless string |
| Reading `lifetime_count` as seconds | a lifetime 20× longer than intended, and a reconnect that does not fit inside it | read the duration the toolkit prints next to the count |
| Tuning one setting and ignoring what it multiplies | keepalive longer than the session, retry longer than the lifetime | let `require_startable()` refuse the combination |
| Treating every warning as fatal | people bypass the checker, and the next file is unreviewed | refuse contradictions, report weaknesses |
| Trusting a schema-validated file | a valid file whose subscription the server cut to a quarter of what was asked | `check_subscription(revision)` after the subscription exists |
| Assuming an override raises a server limit | requests refused in pieces, or one giant request answered `BadTooManyNodesSpecified` | overrides narrow: `check_server` refuses an override above what is published |
| A default nobody chose | 16 settings in section 2 that no one is accountable for | `require_decided()` on the settings that must be decisions |
| Setting a security policy without the certificate material | a session that never activates | the loader refuses a policy with no mode or no key, and `check_server` reports an unsecured choice on a server that offers better |
| Two tags with the same name | dispatch keys, buffers and logs cannot tell them apart | tag names are unique - the model refuses duplicates |
| An environment variable with a typo in its name | "the environment overrides the file" that quietly does nothing | unknown names inside `OPCUA_TUTORIAL_` are refused |
| `monitored: false` read as a mistake | a tag that should be there is deleted from the file instead | a disabled tag is a written decision, and it stays in every report |

## 6. What's next

Chapter 15 gives this configured application somewhere to put data while supervision says "not
right now": a bounded buffer with an explicit drop policy and a spool that survives the process.
Chapter 27 makes `client.security` a decision with `require_decided()` behind it instead of a
review note. The capstone gateway starts from exactly this file: `examples/gateway.yaml` is its
configuration, and every chapter after this one adds a component that reads it.
