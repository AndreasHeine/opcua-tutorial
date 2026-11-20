"""Chapter 14: configuration that refuses mistakes.

Every chapter so far configured its client inside the example script. This chapter moves
the numbers into a file - :file:`examples/gateway.yaml` - and asks the harder question:
would this file have been wrong, and would anybody have noticed before it ran?

The split a reviewer needs:

* a **refusal** is a contradiction between numbers this configuration itself wrote: a
  keepalive that would arrive after the session carrying it expired, a subscription
  lifetime shorter than the reconnect policy's longest wait, a request timeout at least as
  long as the session that sends the request, a duration that does not say which unit it
  is in. The toolkit refuses to start;
* a **review note** is a number that works and probably does not mean what its author
  pictured: a queue narrower than one publish produces, a supervision window wider than
  the server's own keepalive, a lifetime longer than the session that carries it, a setting
  nobody decided.

Four layers, in one order that never changes - the toolkit's defaults, what a server
answered, what a file says, what the environment says - and a report naming which layer
answered each setting. A file beats a server's answer, and that is worth knowing before it
is worth trusting.

Run it:

    uv run python examples/ch14_configuration.py --demo
    uv run python examples/ch14_configuration.py --endpoint opc.tcp://127.0.0.1:4840

Or through the CLI, which runs the same code path a deployment would:

    uv run opcua-toolkit config --file examples/gateway.yaml --check-server opc.tcp://...
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from _support import bullet, chapter_run, heading
from opcua_toolkit import (
    ConfigReview,
    ConfigurationError,
    DataChangeRecord,
    EndpointDiscovery,
    EndpointNotFoundError,
    NamespaceExplorer,
    ResolvedConfig,
    ServerProfile,
    SubscriptionManager,
    discovery_settings,
    load_config,
)

#: The configuration this chapter reviews. It is a real file in the repository, and it is
#: where the capstone gateway starts.
CONFIG_FILE = Path(__file__).with_name("gateway.yaml")

#: The namespace this tutorial's tags live in. Chapter 13 looks the index up; this file
#: keeps the URI, which is the engineer's decision, and never the index, which is not.
DATA_NAMESPACE = "urn:opcua-tutorial:demo"

#: Settings this deployment must have a written decision for. A server can answer where it
#: lives; it cannot answer which application this is.
DEPLOYMENT_DECISIONS: tuple[str, ...] = ("client.application_uri", "client.session_name")

#: A lifetime far above what any server measured in this tutorial keeps, so that the clamp
#: is a measured number here instead of a warning in a paragraph.
OVER_ASKED_LIFETIME = 30_000

#: Asking for no keepalive at all: legal in the protocol, and the answer is a number the
#: server chose. A client that never reads the answer believes its own request.
NO_KEEPALIVE = 0

#: A keepalive count that, at this publishing interval, outlives the session carrying it.
TOO_LONG_KEEPALIVE = 1_000

#: A lifetime count that is shorter than the reconnect policy's longest wait.
TOO_SHORT_LIFETIME = 4

#: An override more generous than any server measured publishes, which is the one thing an
#: override is not for.
IMPOSSIBLE_LIMIT = 999

#: How long the example stays subscribed: long enough for a notification to arrive, without
#: the chapter turning into a subscription lesson. Chapter 10 is that chapter.
WATCH_SECONDS = 2.0


def client_section(endpoint_url: str, **updates: Any) -> dict[str, Any]:
    """The ``client`` section of a configuration, written in code instead of in a file."""
    section: dict[str, Any] = {
        "endpoint_url": endpoint_url,
        "application_uri": "urn:opcua-tutorial:example",
        "application_name": "opcua-tutorial chapter 14",
        "session_name": "Chapter 14 - written in code",
    }
    section.update(updates)
    return section


def bare_client_section(endpoint_url: str) -> dict[str, Any]:
    """A client section with only what a client cannot do without: an address.

    Section 7 uses this to show the shape of an undecided deployment: everything a server can
    answer for you is answered, everything that belongs to this application is still a default,
    and ``require_decided()`` is what says so out loud.
    """
    return {"endpoint_url": endpoint_url}


def refused_cases(endpoint_url: str) -> tuple[tuple[str, Mapping[str, Any]], ...]:
    """Mistakes a configuration file can contain, each one written out in full.

    None of these are artificial: each is a change somebody makes to a config file while
    meaning well. What this chapter shows is that none of them survives to runtime.
    """
    return (
        (
            "a duration that does not say which unit it is in",
            {"client": client_section(endpoint_url, session_timeout="90")},
        ),
        (
            "a setting name that does not exist",
            {"client": client_section(endpoint_url, session_timeoutte="90s")},
        ),
        ("a section written as if it were a value", {"client": endpoint_url}),
        (
            "a tag that answers where it is twice",
            {
                "client": client_section(endpoint_url),
                "tags": [
                    {
                        "name": "Temperature",
                        "node_id": "ns=2;s=Temperature",
                        "path": "ProductionLine/Line1/Temperature",
                    }
                ],
            },
        ),
        (
            "two tags with one name",
            {
                "client": client_section(endpoint_url),
                "tags": [
                    {"name": "Temperature", "node_id": "ns=2;s=Temperature"},
                    {"name": "Temperature", "node_id": "ns=2;s=Pressure"},
                ],
            },
        ),
        (
            "a typo inside a list entry",
            {
                "client": client_section(endpoint_url),
                "tags": [{"name": "A", "node_is": "ns=2;s=Temperature"}],
            },
        ),
        (
            "a security policy with no mode",
            {"client": client_section(endpoint_url, security={"policy": "Basic256Sha256"})},
        ),
        (
            "a username with no password",
            {"client": client_section(endpoint_url, username="operator")},
        ),
        (
            "a subscription lifetime shorter than the reconnect policy's longest wait",
            {
                "client": client_section(
                    endpoint_url,
                    subscriptions={
                        "publishing_interval": "500ms",
                        "lifetime_count": TOO_SHORT_LIFETIME,
                    },
                    retry={"max_delay": "8s"},
                )
            },
        ),
        (
            "a keepalive that would arrive after the session carrying it expired",
            {
                "client": client_section(
                    endpoint_url,
                    subscriptions={
                        "publishing_interval": "500ms",
                        "max_keepalive_count": TOO_LONG_KEEPALIVE,
                    },
                )
            },
        ),
        (
            "a request timeout at least as long as the session that sends it",
            {"client": client_section(endpoint_url, request_timeout="90s", session_timeout="60s")},
        ),
    )


def show_findings(review: ConfigReview) -> None:
    """Print what a configuration will ask a server for, then what is wrong with asking it."""
    for line in review.describe():
        bullet(line)


def new_findings(config: ResolvedConfig, review: ConfigReview) -> list[str]:
    """Findings this review produced that the configuration's own review did not already have.

    Those are the findings that came from the server rather than from the file: the answer a
    server gave, or a limit it publishes, which no amount of reading the file would have shown.
    """
    known = {finding.describe() for finding in config.review().findings}
    return [finding.describe() for finding in review.findings if finding.describe() not in known]


def data_namespace_index(profile: ServerProfile) -> int:
    """Where this tutorial's tags live on this server, or its own namespace if they do not."""
    try:
        return profile.namespace_index_of(DATA_NAMESPACE)
    except EndpointNotFoundError:
        return profile.application_namespace_index


async def main() -> int:
    """Resolve one configuration file, review it, then run a subscription with its numbers."""
    async with chapter_run(
        "resolve a configuration file, review it, and subscribe with the numbers it chose",
        session_name="Chapter 14 - configuration",
    ) as run:
        endpoint = run.endpoint
        derived = {"client": {"endpoint_url": endpoint}}
        derived_here = "the server this example started"

        heading("1. a file with no endpoint in it is not a runnable configuration")
        bullet(f"loading {CONFIG_FILE.name} with nothing supplied for the endpoint:")
        try:
            load_config(CONFIG_FILE, environ={})
        except ConfigurationError as exc:
            bullet(str(exc))
            bullet(
                "the file deliberately writes no address: Chapter 13 discovers one, and a file "
                "that writes an address is a file that goes stale the day the server moves"
            )

        heading("2. the file, plus what this server answered")
        config = load_config(CONFIG_FILE, environ={}, derived=derived, derived_source=derived_here)
        for line in config.describe():
            bullet(line)

        heading("3. what this file's own numbers add up to")
        show_findings(config.review())
        bullet(
            f"{len(config.provenance)} value(s) authored, {len(config.config.tags)} tag(s) named:"
        )
        for tag in config.config.tags:
            state = "monitored" if tag.monitored else "kept unmonitored"
            where = tag.node_id if tag.node_id is not None else f"path {tag.path}"
            bullet(f"  {tag.name}: {where}  [{state}]")

        heading("4. the environment overrides the file, and only for one scalar")
        overridden = load_config(
            CONFIG_FILE,
            environ={
                "OPCUA_TUTORIAL_CLIENT__SUBSCRIPTIONS__PUBLISHING_INTERVAL": "1s",
                "OPCUA_TUTORIAL_SUPERVISION__POLL_INTERVAL": "10s",
            },
            derived=derived,
            derived_source=derived_here,
        )
        for value in overridden.resolved_values():
            if value.path.endswith("publishing_interval") or value.path.endswith("poll_interval"):
                bullet(value.describe())
        for name, raw in (
            ("OPCUA_TUTORIAL_QUEUE_SIZE__", "4"),
            ("OPCUA_TUTORIAL_TAGS", '["Temperature", "Pressure"]'),
            ("OPCUA_TUTORIAL_NOT_A_SETTING", "1"),
        ):
            try:
                load_config(CONFIG_FILE, environ={name: raw}, derived=derived)
            except ConfigurationError as exc:
                bullet(f"{name}: {exc}")

        heading("5. what a configuration file cannot get away with")
        for label, mapping in refused_cases(endpoint):
            try:
                refused = load_config(mapping, environ={})
                refused.require_startable()
            except ConfigurationError as exc:
                bullet(f"{label}:")
                for part in str(exc).split("; "):
                    bullet(f"  {part}")
            else:
                bullet(f"{label}: accepted - this one turns out not to be a contradiction")

        heading("6. what a running server adds to the review")
        profile = await EndpointDiscovery(run.settings).profile()
        discovered = load_config(
            CONFIG_FILE,
            environ={},
            derived=discovery_settings(profile),
            derived_source="Chapter 13 discovery",
        )
        show_findings(discovered.check_server(profile))

        heading("7. decisions this deployment still owes")
        try:
            discovered.require_decided(*DEPLOYMENT_DECISIONS)
            bullet(f"this file decides: {', '.join(DEPLOYMENT_DECISIONS)}")
        except ConfigurationError as exc:
            bullet(str(exc))
        defaults_only = load_config(
            {"client": bare_client_section(endpoint)},
            environ={},
            derived=discovery_settings(profile),
        )
        try:
            defaults_only.require_decided(*DEPLOYMENT_DECISIONS)
        except ConfigurationError as exc:
            bullet(f"a client built from defaults only: {exc}")
            bullet(
                "the server answered the questions it can answer, and these two are not among "
                "them: a discovered value is not a decision"
            )
        else:  # pragma: no cover - the example would be lying if this branch ran
            bullet("unexpected: a configuration with no written decisions passed require_decided()")

        heading("8. a file that argues with this server")
        stubborn = load_config(
            {
                "client": client_section(endpoint, limits={"max_nodes_per_read": IMPOSSIBLE_LIMIT}),
                "tags": [{"name": "Temperature", "node_id": "ns=99;s=Temperature"}],
            },
            environ={},
            derived=discovery_settings(profile),
        )
        stubborn_review = stubborn.check_server(profile)
        for finding in stubborn_review.refusals:
            bullet(finding.describe())
        try:
            stubborn_review.require_startable()
        except ConfigurationError as exc:
            bullet("and the command that would have started it:")
            bullet(f"  {exc}")

        heading("9. running the file: monitored items from a configuration")
        settings = discovered.config.client
        async with run.open_client(settings=settings) as client:
            limits = (await run.read_limits(client)).with_overrides(settings.limits.as_dict())
            bullet("the limits this gateway will actually use (server published, file narrowed):")
            for row in limits.describe().splitlines():
                bullet(row)
            explorer = NamespaceExplorer(client, limits=limits)
            index = data_namespace_index(profile)
            monitored = [tag for tag in discovered.config.tags if tag.monitored]
            nodes: list[str] = []
            for tag in monitored:
                if tag.node_id is not None:
                    nodes.append(tag.node_id)
                elif tag.path is not None:
                    found = await explorer.resolve_path_with_translate(
                        tuple(tag.path.split("/")), namespace_index=index
                    )
                    nodes.append(found.node_id_text)
                    bullet(f"{tag.name}: {tag.path} -> {found.node_id_text}")
            manager = SubscriptionManager(
                client, limits=limits, defaults=discovered.config.subscriptions
            )

            async def report(record: DataChangeRecord) -> None:
                bullet(f"notification {record.node_id_text} = {record.value}")

            manager.register_handler(report)
            handles = await manager.subscribe(nodes, key="gateway")
            configured = len(discovered.config.tags)
            bullet(
                f"{len(handles)} monitored item(s) created from {configured} tag(s) configured "
                f"({configured - len(monitored)} kept unmonitored)"
            )
            await asyncio.sleep(WATCH_SECONDS)
            revision = await manager.confirm_subscription()
            bullet(revision.summary())
            changed = new_findings(discovered, discovered.check_subscription(revision))
            if changed:
                bullet("what this server answered that this file did not already know:")
                for line in changed:
                    bullet(line)
            else:
                bullet("this server answered every timer exactly as this file asked for it")
            bullet(manager.stats.summary())
            await manager.stop()

        heading("10. what a server answers when a file asks for more than it keeps")
        overasked = load_config(
            {
                "client": client_section(
                    endpoint,
                    subscriptions={
                        "publishing_interval": "500ms",
                        "lifetime_count": OVER_ASKED_LIFETIME,
                        "max_keepalive_count": NO_KEEPALIVE,
                    },
                )
            },
            environ={},
            derived=discovery_settings(profile),
        )
        overasked.require_startable()
        bullet(overasked.review().subscription_request)
        async with run.open_client(settings=overasked.config.client) as client:
            limits = await run.read_limits(client)
            manager = SubscriptionManager(
                client, limits=limits, defaults=overasked.config.subscriptions
            )
            await manager.subscribe(["ns=2;s=Temperature"], key="probe")
            revision = await manager.confirm_subscription()
            bullet(revision.summary())
            for line in new_findings(overasked, overasked.check_subscription(revision)):
                bullet(line)
            await manager.stop()

        heading("11. the same review as data, for CI")
        payload = stubborn_review.as_dict()
        excerpt = {
            "subscription_request": payload["subscription_request"],
            "startable": payload["startable"],
            "refusals": payload["refusals"],
            "review_notes": len(payload["reviews"]),
            "defaults_nobody_decided": len(payload["defaults"]),
        }
        print(json.dumps(excerpt, indent=2, ensure_ascii=False))

        heading("building block")
        bullet("ApplicationConfig / TagSettings / SupervisionSettings: one application's settings")
        bullet("load_config: defaults < derived < file < environment, provenance for every value")
        bullet("ConfigReview: refusals and review notes, require_startable(), require_decided()")
        bullet(
            "check_server(profile) / check_subscription(revision): the same review, measured live"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
