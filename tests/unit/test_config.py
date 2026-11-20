"""Chapter 14 tests: a configuration that refuses the mistakes a config file makes.

These tests are the chapter's argument written as assertions. Each one is a mistake somebody
makes to a config file while meaning well, and what the toolkit does about it: refuse the
contradiction, review the number that works and does not mean what its author pictured, and
name - for every value - who actually decided it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opcua_toolkit import SubscriptionRevision
from opcua_toolkit.config import (
    ApplicationConfig,
    ConfigFinding,
    ConfigLayer,
    ConfigReview,
    ResolvedConfig,
    TagSettings,
    discovery_settings,
    load_config,
)
from opcua_toolkit.discovery import EndpointInfo, ServerProfile
from opcua_toolkit.errors import ConfigurationError
from opcua_toolkit.limits import OperationLimits
from opcua_toolkit.settings import ClientSettings

ENDPOINT = "opc.tcp://127.0.0.1:4840"
POLICY_URI = "http://opcfoundation.org/UA/SecurityPolicy#"
BINARY_TRANSPORT = "http://opcfoundation.org/UA/Binary"

#: The configuration file the repository ships, and the starting point of the capstone gateway.
GATEWAY_FILE = Path(__file__).resolve().parents[2] / "examples" / "gateway.yaml"

#: Duration and count fields deliberately left without a unit: both are ratios, and a ratio with
#: a unit attached would be a lie rather than a label.
DIMENSIONLESS: frozenset[str] = frozenset({"client.retry.multiplier", "client.retry.jitter"})


def client(**updates: Any) -> dict[str, Any]:
    """One ``client`` section, with only the endpoint supplied by default."""
    return {"endpoint_url": ENDPOINT, **updates}


def mapping(**sections: Any) -> dict[str, Any]:
    """A configuration mapping written in code, with the nested client sections in place.

    ``subscriptions``, ``retry`` and ``limits`` are client sections and are nested for the test
    author; ``tags`` and ``supervision`` are not, and are passed through unchanged - which is the
    same shape a YAML file has to use.
    """
    nested = {
        key: sections.pop(key) for key in ("subscriptions", "retry", "limits") if key in sections
    }
    sections["client"] = {**client(), **sections.pop("client", {}), **nested}
    return sections


def resolve(**sections: Any) -> ResolvedConfig:
    """Resolve a configuration mapping with the real environment deliberately switched off.

    Every test passes ``environ={}`` because the alternative is a test that changes behaviour
    depending on what a developer's shell happens to export.
    """
    return load_config(mapping(**sections), environ={})


def findings_for(review: ConfigReview, path: str) -> tuple[ConfigFinding, ...]:
    """Every finding a review made about one setting."""
    return tuple(finding for finding in review.findings if finding.path == path)


def finding_for(review: ConfigReview, path: str) -> ConfigFinding:
    """The one finding a review made about one setting, refusing to guess when there is none."""
    found = findings_for(review, path)
    if not found:
        raise AssertionError(
            f"no finding about {path} in {[f.describe() for f in review.findings]}"
        )
    return found[0]


def finding_about(review: ConfigReview, path: str, fragment: str) -> ConfigFinding:
    """The finding about one setting that says a particular thing.

    One setting can be reported more than once: the timer as this configuration wrote it, and
    the answer a server gave to that timer. A test that reads "the" finding has to say which.
    """
    found = [
        finding
        for finding in review.findings
        if finding.path == path and fragment in finding.message
    ]
    if not found:
        listed = [finding.describe() for finding in review.findings]
        raise AssertionError(f"no finding about {path} says {fragment!r}: {listed}")
    return found[0]


def endpoint(
    *,
    policy: str = "None",
    mode: str = "None",
    url: str = ENDPOINT,
    certificate: bool = False,
) -> EndpointInfo:
    """One endpoint as a server publishes it."""
    return EndpointInfo(
        endpoint_url=url,
        security_policy=f"{POLICY_URI}{policy}",
        security_mode=mode,
        security_level=1 if policy != "None" else 0,
        user_identities=("Anonymous",),
        transport_profile=BINARY_TRANSPORT,
        has_server_certificate=certificate,
        application_uri="urn:opcua-tutorial:demo",
    )


def profile(
    *,
    url: str = ENDPOINT,
    endpoints: tuple[EndpointInfo, ...] = (endpoint(),),
    namespaces: tuple[str, ...] = ("http://opcfoundation.org/UA/", "urn:opcua-tutorial:demo"),
    limits: OperationLimits | None = None,
) -> ServerProfile:
    """A server as Chapter 13 discovered it, built by hand so the review can be tested alone."""
    return ServerProfile(
        endpoint_url=url,
        application_uri="urn:opcua-tutorial:demo",
        product_uri="https://github.com/Freedom-Select/opcua-tutorial",
        application_name="chapter 14 test server",
        namespaces=namespaces,
        endpoints=endpoints,
        limits=limits,
        health=None,
    )


def published_limits(**values: int) -> OperationLimits:
    """Limits a server actually published: everything in ``reported`` and nothing else."""
    names = frozenset(values)
    defaults: dict[str, int] = {
        "max_nodes_per_read": 100,
        "max_nodes_per_write": 100,
        "max_nodes_per_browse": 100,
        "max_nodes_per_method_call": 100,
        "max_monitored_items_per_call": 100,
    }
    defaults.update(values)
    return OperationLimits(reported=names, **defaults)


# -------------------------------------------------------------------------------------------
# Every number says what it is
# -------------------------------------------------------------------------------------------


def test_every_configured_number_declares_the_unit_it_counts() -> None:
    """No number in the configuration may be reported bare.

    The report reads the unit off each field's own type, so a duration or a count added to a
    settings model without a unit fails this test instead of quietly printing ``1000`` and
    leaving the reader to guess which ``1000`` it is.
    """
    values = resolve().resolved_values()
    unitless = {
        value.path
        for value in values
        if value.unit is None
        and isinstance(value.value, int | float)
        and not isinstance(value.value, bool)
    }
    assert unitless == DIMENSIONLESS


def test_counts_are_reported_as_what_they_count() -> None:
    """``lifetime_count`` is not seconds, and a report must not let it look like seconds.

    The count counts publishing intervals, which is why the same number is 5 seconds or 50
    minutes depending on a field written somewhere else in the file.
    """
    review = resolve(subscriptions={"publishing_interval": 500, "lifetime_count": 10_000})
    values = {value.path: value.unit for value in review.resolved_values()}
    assert values["client.subscriptions.publishing_interval"] == "ms"
    assert values["client.subscriptions.sampling_interval"] == "ms"
    assert values["client.subscriptions.lifetime_count"] == "publishing intervals"
    assert values["client.subscriptions.max_keepalive_count"] == "publishing intervals"
    assert values["client.subscriptions.queue_size"] == "notifications per monitored item"
    assert (
        values["client.subscriptions.max_notifications_per_publish"] == "notifications per publish"
    )
    assert values["client.session_timeout"] == "s"
    assert values["client.retry.max_delay"] == "s"
    assert values["client.limits.max_nodes_per_read"] == "items in one request"
    assert values["supervision.silent_probe_limit"] == "s"


def test_the_subscription_request_prints_the_counts_and_the_durations_they_add_up_to() -> None:
    """A report that shows ``lifetime=10000`` leaves the multiplication to the reader.

    That multiplication is the whole question the chapter asks, so it is printed.
    """
    request = (
        resolve(
            client={"session_timeout": "60s"},
            subscriptions={"publishing_interval": "500ms", "lifetime_count": 10_000},
        )
        .review()
        .subscription_request
    )
    assert "publishing=500 ms" in request
    assert "lifetime=10000 publishing intervals (5000 s)" in request
    assert "keepalive=90 publishing intervals (45 s)" in request


def test_a_duration_that_does_not_name_its_unit_is_refused() -> None:
    """``session_timeout: 90`` is a number somebody has to guess the unit of at 3 a.m."""
    with pytest.raises(ConfigurationError, match=r"does not say which unit"):
        resolve(client={"session_timeout": "90"})


def test_a_duration_string_is_read_in_the_unit_the_field_means() -> None:
    """A second written in a millisecond field is 2000 of them, and the report says both."""
    resolved = resolve(subscriptions={"publishing_interval": "2s"})
    value = next(
        item
        for item in resolved.resolved_values()
        if item.path == "client.subscriptions.publishing_interval"
    )
    assert resolved.config.subscriptions.publishing_interval == 2000.0
    assert value.shown() == "2000 ms"
    assert "(authored '2s')" in value.describe()


def test_a_bare_number_is_read_in_the_unit_the_field_documents() -> None:
    """Numbers keep the unit the field was written for; only *strings* have to declare one."""
    resolved = resolve(client={"session_timeout": 90}, subscriptions={"publishing_interval": 250})
    assert resolved.config.client.session_timeout == 90.0
    assert resolved.config.subscriptions.publishing_interval == 250.0


def test_a_boolean_is_refused_as_a_duration() -> None:
    """Python accepts ``True`` as the number 1, which is exactly why OPC UA config refuses it."""
    with pytest.raises(ConfigurationError, match="not a duration"):
        resolve(client={"session_timeout": True})


# -------------------------------------------------------------------------------------------
# Who decided each value
# -------------------------------------------------------------------------------------------


def test_the_layers_resolve_in_one_order_that_never_changes() -> None:
    """Defaults, then a server's answer, then the file, then the environment.

    A discovered value filling a gap is useful; a discovered value replacing an operator's
    written number is the failure this chapter exists to prevent.
    """
    resolved = load_config(
        mapping(client={"session_timeout": "20s"}),
        environ={"OPCUA_TUTORIAL_CLIENT__SESSION_TIMEOUT": "30s"},
        derived={"client": {"session_timeout": "10s"}},
        derived_source="probe discovery",
    )
    assert resolved.config.client.session_timeout == 30.0
    origin = resolved.provenance.get("client.session_timeout")
    assert origin is not None
    assert origin.layer is ConfigLayer.ENVIRONMENT
    assert origin.authored == "30s"


def test_a_written_value_beats_a_servers_answer() -> None:
    resolved = load_config(
        mapping(subscriptions={"sampling_interval": 100}),
        environ={},
        derived={"client": {"subscriptions": {"sampling_interval": 2500.0}}},
    )
    assert resolved.config.subscriptions.sampling_interval == 100.0
    assert resolved.review().derived_in_use == ()


def test_the_environment_beats_the_file_and_says_so() -> None:
    resolved = resolve(
        supervision={"silent_probe_limit": "30s"},
    )
    overridden = load_config(
        mapping(supervision={"silent_probe_limit": "30s"}),
        environ={"OPCUA_TUTORIAL_SUPERVISION__SILENT_PROBE_LIMIT": "10s"},
    )
    assert resolved.config.supervision.silent_probe_limit == 30.0
    assert overridden.config.supervision.silent_probe_limit == 10.0
    line = next(
        item.describe()
        for item in overridden.resolved_values()
        if item.path == "supervision.silent_probe_limit"
    )
    assert "10 s (authored '10s')" in line
    assert "[environment: OPCUA_TUTORIAL_* variables]" in line


def test_a_value_nobody_wrote_is_reported_as_a_default() -> None:
    """Not a warning, not invisible: a fact a production review is actually for."""
    review = resolve().review()
    assert review.defaults_in_use
    assert all(value.is_default for value in review.defaults_in_use)
    assert any(
        value.path == "client.subscriptions.max_keepalive_count" for value in review.defaults_in_use
    )


def test_a_secret_authored_in_the_environment_is_not_reported_as_a_file_secret() -> None:
    resolved = load_config(
        mapping(client={"password": "s3cret"}),
        environ={},
    )
    finding = finding_for(resolved.review(), "client.password")
    assert "written in" in finding.message
    assert "s3cret" not in finding.describe()

    from_environment = load_config(
        mapping(),
        environ={"OPCUA_TUTORIAL_CLIENT__PASSWORD": "s3cret"},
    )
    assert findings_for(from_environment.review(), "client.password") == ()


def test_derived_values_are_named_as_derived_and_never_as_decisions() -> None:
    """A server answering a question is not this deployment deciding it."""
    resolved = load_config(
        None,
        environ={},
        derived={"client": {"endpoint_url": ENDPOINT}},
        derived_source="Chapter 13 discovery",
    )
    origin = resolved.provenance.get("client.endpoint_url")
    assert origin is not None
    assert origin.layer is ConfigLayer.DERIVED
    assert not origin.is_decided
    assert [value.path for value in resolved.review().derived_in_use] == ["client.endpoint_url"]


def test_sources_are_named_in_the_order_that_was_read() -> None:
    resolved = load_config(
        mapping(),
        environ={"OPCUA_TUTORIAL_CLIENT__SESSION_NAME": "probe"},
        derived={"client": {"endpoint_url": ENDPOINT}},
        derived_source="discovery probe",
    )
    assert resolved.sources == ("discovery probe", "given mapping", "OPCUA_TUTORIAL_* variables")


def test_discovery_supplies_only_what_a_server_may_answer_for_a_client() -> None:
    """A server's own namespace indices and session timeouts are facts about it, not orders."""
    derived = discovery_settings(profile(limits=published_limits(min_supported_sample_rate=500)))
    assert sorted(path for path in derived["client"]) == ["endpoint_url", "subscriptions"]
    assert "session_timeout" not in derived["client"]
    assert "namespaces" not in derived


# -------------------------------------------------------------------------------------------
# Refusals a configuration file cannot get past
# -------------------------------------------------------------------------------------------


def test_an_unknown_setting_is_refused_with_the_setting_it_was_probably_meant_to_be() -> None:
    """A key nobody reads is a setting that was silently not applied."""
    with pytest.raises(
        ConfigurationError, match=r"session_timeoutte.*did you mean 'client\.session_timeout'"
    ):
        resolve(client={"session_timeoutte": "90s"})


def test_a_section_written_as_a_value_lists_the_settings_that_belong_under_it() -> None:
    with pytest.raises(ConfigurationError, match="is a section, not a value"):
        load_config({"client": ENDPOINT}, environ={})


def test_a_typo_inside_a_list_entry_is_suggested_against_the_entry_model() -> None:
    """pydantic reports ``tags.0.node_is``; a config author needs ``tags.node_id``."""
    with pytest.raises(ConfigurationError, match=r"tags\.0\.node_is.*did you mean 'tags\.node_id'"):
        resolve(tags=[{"name": "A", "node_is": "ns=2;s=A"}])


def test_a_tag_that_answers_where_it_is_twice_is_refused() -> None:
    """One locator is a decision; two locators mean one of them is decoration."""
    with pytest.raises(ConfigurationError, match="both node_id and path"):
        resolve(tags=[{"name": "A", "node_id": "ns=2;s=A", "path": "Line1/A"}])


def test_a_tag_with_no_locator_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="needs node_id or path"):
        resolve(tags=[{"name": "A"}])


def test_two_tags_with_one_name_are_refused() -> None:
    """Dispatch keys, buffers and logs all use the name, so the tags could not be told apart."""
    with pytest.raises(ConfigurationError, match="appears twice"):
        resolve(tags=[{"name": "A", "node_id": "ns=2;s=A"}, {"name": "A", "node_id": "ns=2;s=B"}])


def test_one_node_configured_twice_is_refused() -> None:
    """Two monitored items on one node is two copies of every notification it produces."""
    with pytest.raises(ConfigurationError, match="configured twice"):
        resolve(
            tags=[
                {"name": "A", "node_id": "ns=2;s=Temperature"},
                {"name": "B", "node_id": "ns=2;s=Temperature"},
            ]
        )


def test_an_endpoint_that_is_required_and_supplied_by_nothing_is_named_as_such() -> None:
    """The gateway file is deliberately not runnable on its own, and the message says why."""
    with pytest.raises(ConfigurationError, match=r"client\.endpoint_url: required"):
        load_config(GATEWAY_FILE, environ={})


def test_a_malformed_environment_name_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ConfigurationError, match="OPCUA_TUTORIAL_SECTION__SETTING"):
        load_config(mapping(), environ={"OPCUA_TUTORIAL_QUEUE_SIZE__": "4"})


def test_an_environment_value_cannot_carry_a_list() -> None:
    """The environment is one scalar per setting; a tag list in a variable is a second language."""
    with pytest.raises(ConfigurationError, match="must be one scalar"):
        load_config(mapping(), environ={"OPCUA_TUTORIAL_TAGS": '["A", "B"]'})


def test_a_name_inside_the_prefix_that_no_setting_defines_is_refused() -> None:
    """Reading only the known names is what stops a typo in a deployment looking like a setting."""
    with pytest.raises(ConfigurationError, match="not_a_setting"):
        load_config(mapping(), environ={"OPCUA_TUTORIAL_NOT_A_SETTING": "1"})


def test_variables_outside_the_prefix_are_none_of_this_configurations_business() -> None:
    resolved = load_config(mapping(), environ={"PATH": "/usr/bin", "OPCUA_OTHER_TOOL__X": "1"})
    assert resolved.config.client.endpoint_url == ENDPOINT


def test_a_yaml_file_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    broken = tmp_path / "gateway.yaml"
    broken.write_text("- Temperature\n- Pressure\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must contain a mapping"):
        load_config(broken, environ={})


def test_a_yaml_file_written_in_the_shape_the_chapter_documents(tmp_path: Path) -> None:
    """The documented shape is the shape the loader reads, units and nested sections included."""
    written = tmp_path / "gateway.yaml"
    written.write_text(
        "client:\n"
        f"  endpoint_url: {ENDPOINT}\n"
        "  session_timeout: 90s\n"
        "  subscriptions:\n"
        "    publishing_interval: 250ms\n"
        "    lifetime_count: 1200\n"
        "supervision:\n"
        "  silent_probe_limit: 30s\n"
        "tags:\n"
        "  - name: Temperature\n"
        "    path: ProductionLine/Line1/Temperature\n",
        encoding="utf-8",
    )
    resolved = load_config(written, environ={})
    assert resolved.config.client.session_timeout == 90.0
    assert resolved.config.subscriptions.publishing_interval == 250.0
    assert resolved.config.tags[0].path == "ProductionLine/Line1/Temperature"
    assert str(written) in resolved.describe()[0]


# -------------------------------------------------------------------------------------------
# Refusals: two configured numbers that cannot both be true
# -------------------------------------------------------------------------------------------


def test_a_subscription_lifetime_shorter_than_the_reconnect_wait_is_a_contradiction() -> None:
    """The server deletes the subscription before the client comes back to ask for it."""
    review = resolve(
        subscriptions={"publishing_interval": "500ms", "lifetime_count": 4},
        retry={"max_delay": "8s"},
    ).review()
    finding = finding_for(review, "client.subscriptions.lifetime_count")
    assert finding.severity.value == "refusal"
    assert "2 s" in finding.message
    assert "8 s" in finding.message
    assert not review.is_startable


def test_a_keepalive_that_outlives_the_session_carrying_it_is_a_contradiction() -> None:
    """A subscription lives inside a session, so its keepalive cannot outlive that session."""
    review = resolve(
        client={"session_timeout": "60s"},
        subscriptions={"publishing_interval": "500ms", "max_keepalive_count": 1_000},
    ).review()
    finding = finding_for(review, "client.subscriptions.max_keepalive_count")
    assert finding.severity.value == "refusal"
    assert "500 s" in finding.message
    assert "60 s" in finding.message


def test_a_request_timeout_as_long_as_the_session_sending_it_is_a_contradiction() -> None:
    review = resolve(client={"request_timeout": "90s", "session_timeout": "60s"}).review()
    assert finding_for(review, "client.request_timeout").severity.value == "refusal"


def test_a_watchdog_slower_than_the_session_is_a_review_and_not_a_refusal() -> None:
    """Legal, late, and worth saying out loud - which is the difference this chapter insists on."""
    review = resolve(client={"session_timeout": "10s", "watchdog_interval": "10s"}).review()
    finding = finding_for(review, "client.watchdog_interval")
    assert finding.severity.value == "review"
    assert review.is_startable


def test_a_lifetime_longer_than_the_session_it_lives_in_is_reviewed() -> None:
    """On a session loss the lifetime buys nothing: the subscription goes with the session."""
    review = resolve(
        client={"session_timeout": "60s"},
        subscriptions={"publishing_interval": "500ms", "lifetime_count": 10_000},
    ).review()
    finding = finding_for(review, "client.subscriptions.lifetime_count")
    assert finding.severity.value == "review"
    assert "5000 s" in finding.message


def test_refusals_are_ordered_before_review_notes() -> None:
    """A reader who acts in order acts on what matters."""
    review = resolve(
        client={"request_timeout": "90s", "session_timeout": "60s"},
        subscriptions={"publishing_interval": "500ms", "max_keepalive_count": 1_000},
    ).review()
    assert len(review.refusals) >= 2
    assert review.findings[: len(review.refusals)] == review.refusals
    with pytest.raises(ConfigurationError, match="contradicts itself") as captured:
        review.require_startable()
    assert "client.request_timeout" in str(captured.value)
    assert "client.subscriptions.max_keepalive_count" in str(captured.value)


def test_a_startable_review_raises_nothing() -> None:
    resolve(
        subscriptions={"publishing_interval": "500ms", "lifetime_count": 100}
    ).require_startable()


# -------------------------------------------------------------------------------------------
# Reviews: numbers that work and do not mean what their author pictured
# -------------------------------------------------------------------------------------------


def test_a_queue_narrower_than_the_sampling_it_configures_is_reviewed_with_its_own_arithmetic() -> (
    None
):
    review = resolve(
        subscriptions={"publishing_interval": "500ms", "sampling_interval": "10ms", "queue_size": 2}
    ).review()
    finding = finding_for(review, "client.subscriptions.queue_size")
    assert "50 changes per publish" in finding.message
    assert "the queue holds 2" in finding.message
    assert "48 of them are discarded" in finding.message


def test_a_queue_size_of_zero_asks_for_the_servers_default_queue() -> None:
    review = resolve(subscriptions={"queue_size": 0}).review()
    message = finding_for(review, "client.subscriptions.queue_size").message
    assert "server's default queue" in message
    assert "only readable from its answer" in message


def test_sampling_slower_than_publishing_is_reviewed_as_the_data_rate_it_is_not() -> None:
    review = resolve(
        subscriptions={"publishing_interval": "100ms", "sampling_interval": "1s"}
    ).review()
    finding = finding_for(review, "client.subscriptions.sampling_interval")
    assert "most publishes carry nothing new" in finding.message


def test_a_publishing_interval_faster_than_any_server_here_publishes_at_is_reviewed() -> None:
    review = resolve(subscriptions={"publishing_interval": "5ms"}).review()
    finding = finding_for(review, "client.subscriptions.publishing_interval")
    assert "5 ms between publishes" in finding.message
    assert "expect the server to revise it" in finding.message


def test_notifications_the_tags_can_produce_are_measured_against_one_publish_response() -> None:
    tags = [{"name": f"T{index}", "node_id": f"ns=2;s=T{index}"} for index in range(12)]
    review = resolve(tags=tags, subscriptions={"max_notifications_per_publish": 5}).review()
    finding = finding_for(review, "client.subscriptions.max_notifications_per_publish")
    assert "12 monitored item(s) can produce 120 notifications" in finding.message


def test_a_tag_kept_unmonitored_does_not_count_towards_the_publish_capacity() -> None:
    tags = [
        {"name": f"T{index}", "node_id": f"ns=2;s=T{index}", "monitored": False}
        for index in range(12)
    ]
    review = resolve(tags=tags, subscriptions={"max_notifications_per_publish": 5}).review()
    assert findings_for(review, "client.subscriptions.max_notifications_per_publish") == ()


def test_one_retry_attempt_is_reviewed_as_a_decision_to_fail_fast() -> None:
    review = resolve(retry={"max_attempts": 1}).review()
    assert "nothing is retried" in finding_for(review, "client.retry.max_attempts").message


def test_a_supervision_window_shorter_than_the_servers_keepalive_is_reviewed_as_a_question() -> (
    None
):
    """Asking early costs one cheap request; it does not wait for the server's permission."""
    review = resolve(
        client={"session_timeout": "60s"}, supervision={"silent_probe_limit": "10s"}
    ).review()
    finding = finding_for(review, "supervision.silent_probe_limit")
    assert "The client asks first" in finding.message


def test_a_supervision_window_wider_than_the_servers_keepalive_names_the_gap_it_waits_in() -> None:
    review = resolve(
        client={"session_timeout": "60s"}, supervision={"silent_probe_limit": "120s"}
    ).review()
    finding = finding_for(review, "supervision.silent_probe_limit")
    assert "45 s of silence" in finding.message
    assert "75 s of that window" in finding.message


def test_a_supervision_window_exactly_matching_the_keepalive_is_reviewed_as_the_cheapest_case() -> (
    None
):
    review = resolve(
        client={"session_timeout": "60s"}, supervision={"silent_probe_limit": "45s"}
    ).review()
    assert (
        "exactly this server's keepalive"
        in finding_for(review, "supervision.silent_probe_limit").message
    )


def test_silence_that_is_never_questioned_is_reviewed_as_a_way_to_miss_a_server_side_delete() -> (
    None
):
    review = resolve(supervision={"silent_probe_limit": None}).review()
    assert (
        "silence is never questioned"
        in finding_for(review, "supervision.silent_probe_limit").message
    )


def test_a_supervision_poll_wider_than_the_window_it_watches_is_reviewed() -> None:
    review = resolve(supervision={"poll_interval": "120s", "silent_probe_limit": "30s"}).review()
    assert "exceeded by a whole poll" in finding_for(review, "supervision.poll_interval").message


# -------------------------------------------------------------------------------------------
# What a running server adds to the review
# -------------------------------------------------------------------------------------------


def test_a_tag_in_a_namespace_this_server_does_not_publish_is_refused() -> None:
    review = resolve(tags=[{"name": "A", "node_id": "ns=99;s=A"}]).check_server(profile())
    finding = finding_for(review, "tags.A.node_id")
    assert finding.severity.value == "refusal"
    assert "namespace 99" in finding.message


def test_an_override_more_generous_than_a_published_limit_is_refused() -> None:
    """An override exists to be more careful than the server, never to imagine it is generous."""
    server = profile(limits=published_limits(max_nodes_per_read=10))
    review = resolve(limits={"max_nodes_per_read": 999}).check_server(server)
    assert finding_for(review, "client.limits.max_nodes_per_read").severity.value == "refusal"


def test_a_conservative_override_is_accepted_and_a_refusal_says_it_was_a_decision() -> None:
    server = profile(limits=published_limits(max_nodes_per_read=10))
    review = resolve(limits={"max_nodes_per_read": 5}).check_server(server)
    assert findings_for(review, "client.limits.max_nodes_per_read") == ()
    review.require_startable()


def test_a_limit_the_server_did_not_publish_is_compared_against_nothing() -> None:
    """Refusing an override for disagreeing with an assumed default is the chapter's own error."""
    server = profile(limits=published_limits(max_nodes_per_read=10))
    review = resolve(limits={"max_nodes_per_write": 5_000}).check_server(server)
    assert findings_for(review, "client.limits.max_nodes_per_write") == ()
    listing = finding_about(review, "client.limits", "does not publish")
    assert "max_nodes_per_write" in listing.message
    assert listing.severity.value == "review"


def test_a_server_that_publishes_no_limits_at_all_is_reviewed_as_an_assumption() -> None:
    review = resolve().check_server(profile(limits=None))
    assert "published no operation limits at all" in finding_for(review, "client.limits").message


def test_an_unsecured_configuration_against_a_secured_only_server_is_refused() -> None:
    server = profile(
        endpoints=(endpoint(policy="Basic256Sha256", mode="SignAndEncrypt", certificate=True),)
    )
    review = resolve().check_server(server)
    finding = finding_for(review, "client.security")
    assert finding.severity.value == "refusal"
    assert "server does not offer None/None" in finding.message


def test_an_unsecured_configuration_on_an_unsecured_server_is_reviewed_as_a_choice() -> None:
    review = resolve().check_server(profile())
    assert "Chapter 25" in finding_for(review, "client.security").message


def test_sampling_faster_than_a_published_floor_is_reviewed_as_a_number_to_read_back() -> None:
    server = profile(limits=published_limits(min_supported_sample_rate=500))
    review = resolve(subscriptions={"sampling_interval": 100}).check_server(server)
    finding = finding_for(review, "client.subscriptions.sampling_interval")
    assert "sample-rate floor of 500 ms" in finding.message


def test_a_written_sampling_interval_above_the_published_floor_names_the_rate_it_is_not_using() -> (
    None
):
    server = profile(limits=published_limits(min_supported_sample_rate=250))
    review = resolve(subscriptions={"sampling_interval": 1000}).check_server(server)
    finding = finding_about(review, "client.subscriptions.sampling_interval", "written value wins")
    assert "published floor is 250 ms" in finding.message
    assert "750 ms" in finding.message


def test_an_endpoint_written_in_the_file_is_compared_with_what_the_server_announces() -> None:
    server = profile(url="opc.tcp://plant.example:4840")
    review = resolve(client={"endpoint_url": "opc.tcp://127.0.0.1:4840"}).check_server(server)
    assert (
        "announced from opc.tcp://plant.example:4840"
        in finding_for(review, "client.endpoint_url").message
    )


def test_many_monitored_items_are_reviewed_as_the_calls_the_server_splits_them_into() -> None:
    server = profile(limits=published_limits(max_monitored_items_per_call=10))
    tags = [{"name": f"T{index}", "node_id": f"ns=2;s=T{index}"} for index in range(12)]
    review = resolve(tags=tags).check_server(server)
    finding = finding_for(review, "tags")
    assert "splits the request into 2 calls" in finding.message


def test_a_server_profile_is_only_used_for_the_facts_a_server_owns() -> None:
    """A server's answers add findings; they never replace a number this configuration wrote."""
    resolved = resolve(client={"limits": {"max_nodes_per_read": 5}})
    review = resolved.check_server(profile(limits=published_limits(max_nodes_per_read=10_000)))
    assert resolved.config.client.limits.max_nodes_per_read == 5
    assert review.subscription_request == resolved.review().subscription_request
    review.require_startable()


# -------------------------------------------------------------------------------------------
# What a server answers to a subscription
# -------------------------------------------------------------------------------------------


def asked(**updates: Any) -> ResolvedConfig:
    """A configuration with the timers a subscription will be asked for, written down."""
    base: dict[str, Any] = {
        "publishing_interval": 500.0,
        "lifetime_count": 10_000,
        "max_keepalive_count": 90,
    }
    base.update(updates)
    return resolve(
        client={"session_timeout": "60s", "retry": {"max_delay": "30s"}}, subscriptions=base
    )


def test_a_server_that_answered_exactly_as_asked_produces_no_new_finding() -> None:
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90, 500.0, 10_000, 90)
    assert findings_for(
        config.check_subscription(revision), "client.subscriptions.lifetime_count"
    ) == (findings_for(config.review(), "client.subscriptions.lifetime_count"))


def test_a_clamped_lifetime_is_reviewed_with_both_the_request_and_the_answer() -> None:
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90, 500.0, 1_000, 90)
    finding = finding_about(
        config.check_subscription(revision), "client.subscriptions.lifetime_count", "answered"
    )
    assert finding.severity.value == "review"
    assert "asked for 10000 publishing intervals (5000 s)" in finding.message
    assert "answered 1000 (500 s)" in finding.message


def test_a_lifetime_the_server_clamped_under_the_reconnect_wait_becomes_a_refusal() -> None:
    """The contradiction only exists once the server answers, so this check is a separate call."""
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90, 500.0, 8, 90)
    review = config.check_subscription(revision)
    finding = finding_for(review, "client.subscriptions.lifetime_count")
    assert finding.severity.value == "refusal"
    assert "answered 8 (4 s)" in finding.message
    assert "30 s" in finding.message
    with pytest.raises(ConfigurationError, match="contradicts itself"):
        review.require_startable()


def test_a_revised_publishing_interval_relabels_every_count_in_the_configuration() -> None:
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90, 1000.0, 10_000, 90)
    finding = finding_for(
        config.check_subscription(revision), "client.subscriptions.publishing_interval"
    )
    assert "asked for 500 ms and this server publishes every 1000 ms" in finding.message


def test_a_keepalive_the_server_moved_is_measured_against_the_silence_window() -> None:
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90, 500.0, 10_000, 100)
    finding = finding_for(
        config.check_subscription(revision), "client.subscriptions.max_keepalive_count"
    )
    assert "asked for 90 publishing intervals (45 s)" in finding.message
    assert "answered 100 (50 s)" in finding.message


def test_a_keepalive_answered_wider_than_the_supervision_window_is_reviewed_for_the_gap() -> None:
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90, 500.0, 10_000, 400)
    finding = finding_for(
        config.check_subscription(revision), "client.subscriptions.max_keepalive_count"
    )
    assert "200 s" in finding.message


def test_unanswered_subscription_answers_are_reported_as_unanswered() -> None:
    """asyncua's create path throws part of the answer away, and a review must not invent one."""
    config = asked()
    revision = SubscriptionRevision("CreateSubscription", 500.0, 10_000, 90)
    review = config.check_subscription(revision)
    assert findings_for(review, "client.subscriptions.publishing_interval") == ()
    assert "not reported" in revision.summary()


# -------------------------------------------------------------------------------------------
# Decisions an application refuses to run without
# -------------------------------------------------------------------------------------------


def test_a_setting_answered_by_a_default_is_refused_when_the_deployment_must_decide_it() -> None:
    config = resolve()
    with pytest.raises(ConfigurationError, match="must be decided by this deployment"):
        config.require_decided("client.application_uri", "client.session_name")


def test_a_setting_answered_by_a_server_is_not_a_decision_either() -> None:
    config = load_config(
        None,
        environ={},
        derived={"client": {"endpoint_url": ENDPOINT}},
    )
    with pytest.raises(ConfigurationError, match=r"client\.endpoint_url"):
        config.require_decided("client.endpoint_url")


def test_a_setting_written_in_the_environment_counts_as_decided() -> None:
    config = load_config(
        mapping(),
        environ={"OPCUA_TUTORIAL_CLIENT__SESSION_NAME": "plant gateway"},
    )
    config.require_decided("client.session_name")


def test_the_gateway_configuration_the_repository_ships_is_valid_and_startable() -> None:
    """The file a reader is handed is held to the same standard the chapter preaches."""
    resolved = load_config(
        GATEWAY_FILE,
        environ={},
        derived={"client": {"endpoint_url": ENDPOINT}},
        derived_source="Chapter 13 discovery",
    )
    review = resolved.check_server(
        profile(limits=published_limits(max_nodes_per_read=10, max_monitored_items_per_call=10))
    )
    review.require_startable()
    resolved.require_decided("client.application_uri", "client.session_name")
    assert [tag.name for tag in resolved.config.tags] == ["Temperature", "Pressure", "MotorRpm"]
    assert [tag.name for tag in resolved.config.tags if not tag.monitored] == ["MotorRpm"]
    assert resolved.config.subscriptions.publishing_interval == 500.0
    assert resolved.config.client.limits.max_nodes_per_read == 5


def test_a_configuration_reports_its_findings_as_json_a_ci_step_can_act_on() -> None:
    review = resolve(client={"request_timeout": "90s", "session_timeout": "60s"}).review()
    payload = json.loads(json.dumps(review.as_dict()))
    assert payload["startable"] is False
    assert payload["refusals"][0]["path"] == "client.request_timeout"
    assert payload["subscription_request"]
    assert "settings" in resolve().as_dict()


def test_a_review_describes_itself_in_the_order_a_reader_should_act_in() -> None:
    review = resolve(client={"request_timeout": "90s", "session_timeout": "60s"}).review()
    lines = review.describe()
    assert lines[0].startswith("this client will ask for: publishing=500 ms")
    assert lines[1] == "refusals: 1  reviews: 2"
    assert lines[2].startswith("refusal: client.request_timeout:")


def test_two_configurations_that_differ_only_by_provenance_are_the_same_configuration() -> None:
    """Two ways of writing the same number must produce the same client, or the layers are a lie."""
    from_file = load_config(mapping(client={"session_timeout": "90s"}), environ={})
    from_environment = load_config(
        mapping(),
        environ={
            "OPCUA_TUTORIAL_CLIENT__ENDPOINT_URL": ENDPOINT,
            "OPCUA_TUTORIAL_CLIENT__SESSION_TIMEOUT": "90s",
        },
    )
    assert from_file.config == from_environment.config
    assert from_file.provenance.authored() != from_environment.provenance.authored()


def test_an_application_config_built_in_code_and_one_loaded_from_text_agree() -> None:
    """Chapter 14's models are the models every other chapter of this repository already uses."""
    written = ApplicationConfig(
        client=ClientSettings(endpoint_url=ENDPOINT, session_timeout=90.0),
        tags=(TagSettings(name="Temperature", node_id="ns=2;s=Temperature"),),
    )
    loaded = resolve(
        client={"session_timeout": "90s"},
        tags=[{"name": "Temperature", "node_id": "ns=2;s=Temperature"}],
    )
    assert loaded.config == written
    assert loaded.review().subscription_request == written.review().subscription_request
