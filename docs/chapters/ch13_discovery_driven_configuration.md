# Chapter 13 - Discovery-driven configuration

*Part 4 · status: built and tested · example: `examples/ch13_discovery.py` ·
tests: `tests/unit/test_discovery_model.py`, `tests/integration/test_discovery.py`,
`tests/integration/test_cli.py`*

## 1. Why this matters

Every number Chapters 4 to 12 depended on was already known to the server: which endpoint
to use, which security policies it offers, which namespace the tags live in, what sample
rate it will actually honour, whether it is in maintenance right now.

A client that reads those facts survives a server move, a namespace renumbering and a
policy change. A client that hardcodes them is written against **one** server, and it
finds out during commissioning.

This chapter turns the reading into a startup step: discover, resolve, configure - and print
the configuration that was derived, so a human can check it.

## 2. OPC UA theory

### The discovery services

| Service | Needs a session? | Answer |
| --- | --- | --- |
| `GetEndpoints` | no | one `EndpointDescription` per endpoint: `EndpointUri`, `TransportProfileUri`, `SecurityMode`, `SecurityPolicyUri`, `UserIdentityTokens` |
| `FindServers` | no | `ApplicationDescription`s this server knows about (itself, and any it is configured to report) |
| `FindServersOnNetwork` | no, and needs a **Local Discovery Server** | multicast/mDNS-style list of servers on the network |
| `GetTrustList` and friends | yes, and security-specific | Chapter 25 |

The commonly confused pair is the last two. `FindServers` is a call to a server;
`FindServersOnNetwork` is a call to a **Local Discovery Server**, which most vendor servers
are not. And the refusal does not arrive as a clean "no such service": against the tutorial's
demo server it arrives as `BadUserAccessDenied` (which the toolkit reports as "server does not
support `FindServersOnNetwork`"), and against a real LDS with an identity that is not allowed
to use it, it arrives as **the same code**. Two different causes, one status code - which is
precisely why people conclude that "discovery is broken" on a perfectly healthy server.

### What an endpoint description actually tells you

| Field | What it says | What it does not say |
| --- | --- | --- |
| `SecurityPolicyUri` | the algorithm family (`http://opcfoundation.org/UA/SecurityPolicy#Basic256Sha256`, `#None`) | whether it is a good idea |
| `SecurityMode` | `None`, `Sign` or `SignAndEncrypt` | that the transport is encrypted at all if mode is `None` |
| `TransportProfileUri` | the transport and message shape (`http://opcfoundation.org/UA-binaryprotocol-...` → `uatcp-uasc-uabinary`) | anything about protection: "binary" is not "secure" |
| `UserIdentityTokens` | which identity types this endpoint accepts (`Anonymous`, `UserName`, `Certificate`, `IssuedToken`) | whether your user exists (Chapter 26) |
| `EndpointUri` | the URL the server calls itself | **not** a fact about your network - see Chapter 31 |

A security *policy* and a *transport profile* are two different facts, and a client that
checks only one of them can end up on an unsecured endpoint while congratulating itself on
using OPC UA.

### Namespaces are facts about a running server

The namespace array always starts with `http://opcfoundation.org/UA/` at index 0, and the
specification reserves index 1 for the server's own application URI. After that, indices are
the server's own decision and its own order. **Nothing says your tags live there.** On the
demo server the application URI is `urn:freeopcua:python:server` at `ns=1`, and the tags are
in `urn:opcua-tutorial:demo` at `ns=2`. A client that assumed `ns=1` is written in the wrong
file, and no amount of local testing against this one server will tell it.

The robust rule: look a namespace **up by URI** and use the index the server reports today.

## 3. Implementation

`EndpointDiscovery` performs the calls; `ServerProfile` is the answer as data:

```python
from pathlib import Path

from opcua_toolkit import ClientSettings, EndpointDiscovery

discovery = EndpointDiscovery(ClientSettings(endpoint_url="opc.tcp://127.0.0.1:48401"))
profile = await discovery.profile()  # endpoints + identity + namespaces + limits + health

endpoint = profile.best_endpoint()  # ranks what actually exists, secure or not
index = profile.namespace_index_of("urn:opcua-tutorial:demo")  # the index the server reports today
settings = profile.security_settings_for(
    endpoint.policy_short_name,
    endpoint.security_mode_name,
    certificate=Path("certs/client.pem"),  # required for any policy that is not None
    private_key=Path("certs/client.key"),
)
```

| API | What it gives you |
| --- | --- |
| `list_endpoints()` | `EndpointInfo`s: `policy_short_name`, `security_mode_name`, `transport_profile_name`, `is_unsecured`, `supports_identity(...)`, `preferred` |
| `profile()` | `ServerProfile`: application info, namespaces, endpoints, `limits`, `supports_security`, `secured_endpoints`, `describe()` |
| `namespace_index_of(uri_fragment)` | the index the **server** reports now, or `EndpointNotFoundError` listing what it does publish |
| `endpoint_for(policy, mode)` / `best_endpoint()` | a specific endpoint, or the strongest available - where "best" means best of what exists |
| `security_settings_for(policy, mode, *, certificate, private_key)` | `SecuritySettings` for an endpoint the server really publishes. Ask for a secured policy without a certificate and it raises `SecurityConfigurationError` naming what is missing, instead of failing in a handshake |
| `find_servers()` / `find_servers_on_network()` | `DiscoveredServer`s; the second one raises a typed error when the server is not an LDS |
| `health()` | `ServerHealth` before any session exists |
| `server_node_texts()` | the `Server` node's own text, for a diagnostic report |

`best_endpoint()` is deliberately named "best available": it ranks what a server actually
publishes. On a server with only unsecured endpoints, the best endpoint **is unsecured**, and
the profile says so in words (`security : NO - unsecured endpoints only`). A function that
returned something called "secure" here would be a lie with a certificate-shaped API.

The mirror of that rule is `security_settings_for`: a policy this client cannot speak is
**refused, not rounded down**. A server is free to publish `Basic128Rsa15` or a policy this
toolkit does not implement, and the quiet alternative - connect to that endpoint with `None`
and log a warning - is the exact failure this chapter exists to prevent. So the answer is a
`SecurityConfigurationError` that names both the policy the server offered and the ones the
client can speak.

### What the example actually prints

```text
1. what the server offers, before any session exists
  1 endpoint(s) published
  opc.tcp://127.0.0.1:64463    None    unsecured    level=0    identities=Anonymous,Certificate,UserName
    transport : opc.tcp (profile uatcp-uasc-uabinary, tcp=True)
    security  : mode=None policy=None level=0 certificate=no

3. namespaces are discovered, not remembered
  ns=0  http://opcfoundation.org/UA/
  ns=1  urn:freeopcua:python:server
  ns=2  urn:opcua-tutorial:demo
  the tags this tutorial uses live in 'urn:opcua-tutorial:demo' -> ns=2
  looking up a URI that is not there: no namespace matching 'urn:not-a-server-this-client-talks-to';
  server publishes: http://opcfoundation.org/UA/, urn:freeopcua:python:server, urn:opcua-tutorial:demo

4. the resolved client configuration
  best endpoint: ... policy=None mode=None level=0 identities=Anonymous, Certificate, UserName
  and the strongest endpoint this server has is an unsecured one: best_endpoint ranks what exists,
  so 'best available' is not 'secure'
  endpoint=opc.tcp://127.0.0.1:64463 ... security=unsecured (policy None, mode None) auth=anonymous

5. one request reaches a tag, using the discovered namespace
  ProductionLine/Line1/Temperature -> ns=2;s=Temperature
  the ns=2 came from the NamespaceArray, not from this source file

7. what the discovery services are, and are not
  FindServersOnNetwork: server does not support FindServersOnNetwork: User does not have permission
  to perform the requested operation.(BadUserAccessDenied)

8. a URL that is not an endpoint
  pydantic rejected it before anything connected: Value error, endpoint_url must start with 'opc.tcp://'
```

Section 6 prints the discovered profile as a YAML document - the file Chapter 14 loads
instead of a source file full of constants, with the limits left as `null` because an
override is a human decision and not a discovered value.

Section 8 is the cheapest safety net in the whole tutorial: `ClientSettings` refuses a URL
that is not `opc.tcp://...` **before anything connects**, so the failure is a one-line
configuration error instead of a stack trace from inside the transport.

## 4. Verification

```powershell
uv run python examples/ch13_discovery.py --demo --demo-port 48401
uv run opcua-toolkit discover --endpoint opc.tcp://127.0.0.1:48401
uv run opcua-toolkit discover --find-servers --json --endpoint opc.tcp://127.0.0.1:48401
uv run python examples/ch13_discovery.py --endpoint opc.tcp://127.0.0.1:4840   # a Prosys or vendor server
```

- **UaExpert**: its server *Settings* dialog shows the endpoint list, the security policy and
  mode, and the identity tokens for each - the same table the example prints. Its
  *Server Namespaces* view is the namespace array; check that the tags live in a namespace
  other than the server's application namespace.
- **Prosys**: point `ch13_discovery.py` at Prosys's own simulation server. Its published
  limits, policies and namespace indices differ, and the example's derived configuration
  changes with them while its code does not. That difference is the test.
- **Prove the LDS point**: run the same `FindServersOnNetwork` from UaExpert against the demo
  server and read its error. Then, if you have the OPC Foundation Local Discovery Server
  installed, point the toolkit at `opc.tcp://localhost:4840` and see the same call answer a
  server list. Two different services, two different servers.
- **Test proof**: `tests/integration/test_discovery.py` asserts `GetEndpoints` works without a
  session, the unsecured profile wording, namespace lookup (and its error listing what the
  server does publish), limits reading, `FindServers`'s `ApplicationType`, and that a wrong
  port fails at the transport rather than inside the protocol stack.

## 5. Common mistakes

| Mistake | How it shows up | Fix |
| --- | --- | --- |
| Hardcoding `ns=2` | works here, fails on site | `namespace_index_of(uri)`, or browse (Chapter 5) |
| Assuming `ns=1` holds the tags | the server's own plumbing is treated as plant data | look namespaces up by URI |
| Confusing `FindServers` with `FindServersOnNetwork` | "discovery is broken" on a healthy vendor server | one is a server call, the other needs an LDS |
| Believing an announced `EndpointUri` is your network address | a client connecting to a hostname it cannot resolve | Chapter 31: the port mapping is the fact, the announcement is an answer |
| Choosing a policy the endpoint does not offer | a session that never activates | derive `SecuritySettings` from the endpoint you found |
| Letting an unsupported policy be quietly replaced | the endpoint you asked for is not the connection you got | refuse it: `security_settings_for` names what the server offers and what this client speaks |
| Treating "best endpoint" as "secure endpoint" | a production client on a `None` policy | read `supports_security` / `secured_endpoints`, and fail loudly when there is nothing secure |
| Reading identity tokens and assuming a user exists | `BadIdentityTokenRejected` later | identity is Chapter 26 |
| Skipping health before connecting | a large subscription created against a server in `Paused` | `discovery.health()` first, then decide |
| Doing discovery once at build time instead of at startup | a config file that describes last year's server | discover at startup, cache the result for the session |

## 6. What's next

Chapter 14 makes the configuration this chapter derives **typed and validated**: settings
that refuse the mistakes a config file makes, and the difference between a default and a
decision. Chapter 15 gives the application somewhere to put data while supervision says
"not right now". The secured version of this same startup is Chapter 25.
