"""Chapter 14 integration tests: a configuration review against a live server.

``tests/unit/test_config.py`` proves what a configuration file says about *itself*: the
layer order, provenance, unit parsing, and every contradiction a file can write without a
server. Those need no network, and they are fast.

This file proves the half of Chapter 14 that no validator can do offline. Three findings
exist only because a server is there to disagree with:

* a tag in a namespace this server does not publish;
* a client-side limit override more generous than the limit this server publishes;
* a subscription lifetime the server cut - which is a *runtime* answer, and the reason
  :meth:`ResolvedConfig.check_subscription` takes a :class:`~opcua_toolkit.SubscriptionRevision`
  rather than a settings object.

The rule this file follows is the chapter's own: a **refusal** is a contradiction, so the test
asserts the refusal *and* that the same configuration written carefully is startable. Asserting
only the refusal would pass a checker that refuses everything.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from opcua_toolkit import (
    ClientSettings,
    ConfigFinding,
    ConfigReview,
    ConfigurationError,
    EndpointDiscovery,
    FindingSeverity,
    ResolvedConfig,
    ServerProfile,
    SubscriptionManager,
    discovery_settings,
    load_config,
)
from opcua_toolkit.cli import EXIT_ERROR, EXIT_OK, main

pytestmark = pytest.mark.integration

#: The tutorial's own deployment file, loaded by path so that this test reviews the artifact
#: a reader is told to copy rather than a mapping invented here.
GATEWAY_FILE = Path(__file__).parents[2] / "examples" / "gateway.yaml"

#: A tag written with a namespace index no server in this tutorial publishes.
TAG_IN_A_NAMESPACE_THAT_DOES_NOT_EXIST = "ns=99;s=Temperature"

#: A per-request size no server measured here publishes, so it can only be a guess.
IMPOSSIBLE_LIMIT = 999

#: Narrower than what the demo server publishes (10), and therefore allowed.
CAREFUL_LIMIT = 5

#: Publishing intervals so wide that this server's own cap must cut them, which is the point
#: of the last two tests: the file is internally consistent and the server still says no.
OVER_ASKED_LIFETIME = 100_000
FAST_PUBLISHING = "50ms"

#: A reconnect wait the file's own numbers allow (the lifetime it wrote is 5000 s at 50 ms)
#: and the lifetime this server answers with (1000 intervals) cannot cover.
RECONNECT_WAIT = "4000s"

#: The tag these tests monitor. Chapter 5 and Chapter 13 both derive it; naming it here is
#: honest because this file is about the demo server, and the alternative is a browse call
#: that has nothing to do with what is under test.
TEMPERATURE = "ns=2;s=Temperature"


def client_section(endpoint: str, **updates: Any) -> dict[str, Any]:
    """A ``client`` section written in code, so a test changes one setting and nothing else."""
    section: dict[str, Any] = {
        "endpoint_url": endpoint,
        "application_uri": "urn:opcua-tutorial:config-review-test",
        "application_name": "opcua-tutorial config review test",
        "session_name": "Chapter 14 - config review",
    }
    section.update(updates)
    return section


@pytest.fixture
async def profile(demo_server: Any) -> ServerProfile:
    """The live server's profile: its namespaces, its published limits, its endpoints."""
    discovery = EndpointDiscovery(ClientSettings(endpoint_url=demo_server.endpoint))
    return await discovery.profile()


def resolve(mapping: Mapping[str, Any], profile: ServerProfile, **kwargs: Any) -> ResolvedConfig:
    """Load a configuration the way a deployment does: with what the server answered as DERIVED."""
    return load_config(
        mapping,
        environ={},
        derived=discovery_settings(profile),
        derived_source="the live demo server",
        **kwargs,
    )


def finding_about(review: ConfigReview, path: str, fragment: str) -> ConfigFinding:
    """One finding about one setting, so a failure names the setting rather than a list."""
    matches = [
        found for found in review.findings if found.path == path and fragment in found.message
    ]
    if not matches:
        message = "\n".join(found.describe() for found in review.findings)
        raise AssertionError(f"no finding about {path} containing {fragment!r}:\n{message}")
    return matches[0]


async def test_meeting_the_server_adds_findings_the_file_could_not_produce(profile) -> None:
    """``check_server`` is the configuration's own review plus what only a server can say.

    The gateway file is valid on its own terms: zero refusals. Against this server it gains
    findings - a published sample-rate floor the file's sampling interval sits above, limits
    the server does not publish, the endpoint it left to discovery. A review that compared a
    file with a schema would report the same file unchanged, which is the failure mode this
    test exists to make impossible.
    """
    resolved = load_config(GATEWAY_FILE, environ={}, derived=discovery_settings(profile))
    file_review = resolved.review()
    server_review = resolved.check_server(profile)

    assert file_review.refusals == ()
    assert server_review.is_startable

    # Nothing the file already said is lost, and the ordering rule survives the merge.
    known = {found.describe() for found in file_review.findings}
    merged = {found.describe() for found in server_review.findings}
    assert known <= merged
    assert server_review.refusals == ()

    floor = finding_about(
        server_review, "client.subscriptions.sampling_interval", "published floor"
    )
    assert floor.severity is FindingSeverity.REVIEW
    assert "The written value wins" in floor.message

    # At least one finding exists only because a server was met, and it is about a number
    # the file never mentions: what this server publishes.
    added = merged - known
    assert added, "meeting a server produced no finding the file could not produce"
    assert any("publishes" in finding for finding in added)


async def test_a_tag_in_a_namespace_this_server_does_not_publish_is_refused(profile) -> None:
    """A NodeId is a promise about a server, and this is the promise this server breaks.

    The configuration is not malformed: ``ns=99;s=Temperature`` is a valid NodeId. What makes
    it a refusal is that this server publishes three namespaces and 99 is not one of them, so
    no monitored item can ever exist. Chapter 13's rule - write a path, look the index up - is
    what keeps this refusal out of a real file.
    """
    resolved = resolve(
        {
            "client": client_section(profile.endpoint_url),
            "tags": [{"name": "Temperature", "node_id": TAG_IN_A_NAMESPACE_THAT_DOES_NOT_EXIST}],
        },
        profile,
    )
    review = resolved.check_server(profile)

    refusal = finding_about(review, "tags.Temperature.node_id", "namespace 99")
    assert refusal.severity is FindingSeverity.REFUSAL
    assert "namespace the server does not have" in refusal.message
    assert not review.is_startable

    with pytest.raises(ConfigurationError) as raised:
        review.require_startable()
    assert "tags.Temperature.node_id" in str(raised.value)

    # The same tag named by a path is startable: a path defers the index to the server.
    by_path = resolve(
        {
            "client": client_section(profile.endpoint_url),
            "tags": [{"name": "Temperature", "path": "ProductionLine/Line1/Temperature"}],
        },
        profile,
    )
    assert by_path.check_server(profile).is_startable


async def test_an_override_more_generous_than_this_server_publishes_is_refused(profile) -> None:
    """Overrides narrow. This is the one rule an override exists under.

    ``max_nodes_per_read`` published by this server is a real number read from the server, and
    the configuration claims it may put 999 nodes in one Read request. Refusing that is not
    politeness: a client that believes its own override sends a request the server answers in
    pieces or rejects outright, and the failure appears as a partial read hours later.
    """
    published = (profile.limits.max_nodes_per_read if profile.limits else None) or 0
    assert published < IMPOSSIBLE_LIMIT

    overruled = resolve(
        {
            "client": client_section(
                profile.endpoint_url, limits={"max_nodes_per_read": IMPOSSIBLE_LIMIT}
            )
        },
        profile,
    )
    review = overruled.check_server(profile)
    refusal = finding_about(
        review, "client.limits.max_nodes_per_read", "more careful than the server"
    )
    assert refusal.severity is FindingSeverity.REFUSAL
    assert f"while this server publishes {published}" in refusal.message

    with pytest.raises(ConfigurationError):
        review.require_startable()

    # The same setting, narrowed: startable, and no finding about it at all.
    narrower = resolve(
        {
            "client": client_section(
                profile.endpoint_url, limits={"max_nodes_per_read": CAREFUL_LIMIT}
            )
        },
        profile,
    )
    narrow_review = narrower.check_server(profile)
    assert narrow_review.is_startable
    assert not [
        found
        for found in narrow_review.findings
        if found.path == "client.limits.max_nodes_per_read"
    ]


async def test_check_subscription_reports_the_lifetime_this_server_actually_grants(
    client, limits, profile, demo_server
) -> None:
    """A file with no refusals at all still gets its subscription cut.

    The configuration asked for 10 000 publishing intervals of lifetime and the server answered
    1000, which is the cap this server applies and publishes nowhere in its address space. The
    finding is a review note, not a refusal - the deployment runs - and it is the number a
    reconnect has to fit inside. Chapter 10 measured the cap; this test is what Chapter 14 does
    with it.
    """
    resolved = resolve(
        {
            "client": client_section(
                profile.endpoint_url, subscriptions={"publishing_interval": "500ms"}
            )
        },
        profile,
    )
    assert resolved.review().is_startable
    assert resolved.check_server(profile).is_startable

    manager = SubscriptionManager(client, limits=limits, defaults=resolved.config.subscriptions)
    try:
        await manager.subscribe([TEMPERATURE], key="review")
        revision = await manager.confirm_subscription()
        review = resolved.check_subscription(revision)

        granted = revision.revised_lifetime_count
        assert granted is not None
        assert granted == demo_server.subscription_caps["max_lifetime_count"]
        assert granted < revision.requested_lifetime_count

        note = finding_about(review, "client.subscriptions.lifetime_count", "this server answered")
        assert note.severity is FindingSeverity.REVIEW
        assert "the number a reconnect has to fit inside" in note.message
        assert review.is_startable, (
            "a server moving a number it is entitled to move is not a refusal"
        )
    finally:
        await manager.stop()


async def test_a_lifetime_the_server_cut_below_the_reconnect_wait_is_a_refusal(
    client, limits, profile, demo_server
) -> None:
    """Where a server's answer breaks a promise the file made elsewhere, it becomes a refusal.

    This file is consistent: at 50 ms publishes, 100 000 intervals is 5000 s of tolerated
    silence, and the reconnect policy's longest wait of 4000 s fits inside it. The server caps
    the lifetime at 1000 intervals - 50 s at this interval - and now the reconnect policy is
    longer than the subscription it is recovering. Nothing in the file changed; only the
    server's answer is different, and that answer is what turns a review into a refusal.

    The gate closed at the end of this test is the one on the review the measurement produced,
    not :meth:`ResolvedConfig.require_startable`. That method is the pre-flight gate a
    deployment runs before it has met a server; once a subscription exists, the verdict belongs
    to the ``ConfigReview`` that ``check_subscription`` returned.
    """
    resolved = resolve(
        {
            "client": client_section(
                profile.endpoint_url,
                subscriptions={
                    "publishing_interval": FAST_PUBLISHING,
                    "lifetime_count": OVER_ASKED_LIFETIME,
                },
                retry={"max_delay": RECONNECT_WAIT},
            )
        },
        profile,
    )
    assert resolved.review().is_startable, "the file's own numbers have to be consistent first"

    manager = SubscriptionManager(client, limits=limits, defaults=resolved.config.subscriptions)
    try:
        await manager.subscribe([TEMPERATURE], key="review")
        revision = await manager.confirm_subscription()
        review = resolved.check_subscription(revision)

        assert (
            revision.revised_lifetime_count == demo_server.subscription_caps["max_lifetime_count"]
        )
        refusal = finding_about(
            review, "client.subscriptions.lifetime_count", "this server answered"
        )
        assert refusal.severity is FindingSeverity.REFUSAL
        assert "the server deletes the subscription before the client comes back" in refusal.message
        assert not review.is_startable

        with pytest.raises(ConfigurationError) as raised:
            review.require_startable()
        assert "client.subscriptions.lifetime_count" in str(raised.value)
    finally:
        await manager.stop()


async def test_config_command_reviews_the_file_and_then_the_server(demo_server, capsys) -> None:
    """The CLI runs the same review, in the same order, with a machine-readable result.

    ``opcua-toolkit config`` is what a CI step calls, so what is asserted here is the contract
    a script depends on: JSON alone on stdout, ``--require-decided`` refusing a setting nobody
    wrote, and exactly one ``error:`` sentence on stderr when it does.
    """
    code = await _run_cli(
        "config",
        "--file",
        str(GATEWAY_FILE),
        "--check-server",
        demo_server.endpoint,
        "--json",
    )
    printed = capsys.readouterr().out
    assert code == EXIT_OK

    payload = json.loads(printed)
    review = payload["review"]
    assert review["startable"] is True
    assert review["refusals"] == []
    assert "publishing=500 ms" in review["subscription_request"]

    endpoints = [item for item in payload["settings"] if item["path"] == "client.endpoint_url"]
    assert endpoints, "the file wrote no address: discovery did"
    assert endpoints[0]["layer"] == "derived"

    # A setting nobody decided is a refusal the command reports rather than a default it uses.
    capsys.readouterr()
    code = await _run_cli(
        "config",
        "--file",
        str(GATEWAY_FILE),
        "--check-server",
        demo_server.endpoint,
        "--require-decided",
        "client.password",
    )
    captured = capsys.readouterr()
    assert code == EXIT_ERROR
    errors = [line for line in captured.err.splitlines() if line.startswith("error:")]
    assert len(errors) == 1
    assert "client.password" in errors[0]
    assert "Traceback" not in captured.err

    # A setting the file does decide passes the same gate, which is what makes the gate usable.
    code = await _run_cli(
        "config",
        "--file",
        str(GATEWAY_FILE),
        "--check-server",
        demo_server.endpoint,
        "--require-decided",
        "client.application_uri",
        "--require-decided",
        "client.session_name",
    )
    assert code == EXIT_OK


async def _run_cli(*argv: str) -> int:
    """:func:`opcua_toolkit.cli.main` owns its event loop, so it runs in a thread here."""
    return await asyncio.to_thread(main, list(argv))
