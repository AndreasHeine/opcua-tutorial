"""Chapter 10 tests: monitored item options, records and the counters.

Pure model tests: no server, no event loop, nothing that can be slow or flaky.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from asyncua import ua

from opcua_toolkit.settings import SubscriptionDefaults
from opcua_toolkit.subscriptions.model import (
    DEFAULT_SUBSCRIPTION_KEY,
    DataChangeRecord,
    MonitoredItemHandle,
    SubscriptionOptions,
    SubscriptionStats,
)


def make_record(**overrides: object) -> DataChangeRecord:
    fields: dict[str, object] = {
        "node_id": ua.NodeId("Temperature", 2),
        "value": 21.5,
        "status_code": ua.StatusCode(0),
        "source_timestamp": datetime(2026, 5, 4, 9, 0, 0, tzinfo=UTC),
        "server_timestamp": datetime(2026, 5, 4, 9, 0, 1, tzinfo=UTC),
        "client_handle": 41,
        "key": DEFAULT_SUBSCRIPTION_KEY,
        "sequence": 7,
    }
    fields.update(overrides)
    return DataChangeRecord(**fields)  # type: ignore[arg-type]


def test_a_value_that_arrives_with_a_bad_status_is_not_good() -> None:
    """A number in the payload does not mean the reading can be trusted.

    asyncua unwraps the value for applications, and a ``Bad`` status with a stale
    value still in hand is exactly how a historian learns to lie.
    """
    record = make_record(status_code=ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown))
    assert record.value == 21.5
    assert not record.is_good


def test_a_good_status_with_no_value_is_not_good_either() -> None:
    assert not make_record(value=None).is_good


def test_a_record_serialises_for_logging_without_leaking_protocol_objects() -> None:
    payload = make_record().as_dict()
    encoded = json.dumps(payload)
    assert '"ns=2;s=Temperature"' in encoded
    assert payload["status"] == "Good"
    assert payload["client_handle"] == 41
    assert payload["sequence"] == 7
    assert payload["source_timestamp"].startswith("2026-05-04T09:00:00")


def test_options_inherit_the_subscription_defaults_when_unset() -> None:
    defaults = SubscriptionDefaults(
        publishing_interval=500.0, sampling_interval=100.0, queue_size=10
    )
    resolved = SubscriptionOptions().resolved(defaults)
    assert resolved.sampling_interval == 100.0
    assert resolved.queue_size == 10
    assert resolved.monitoring_mode is ua.MonitoringMode.Reporting
    assert resolved.attribute is ua.AttributeIds.Value


def test_options_keep_explicit_values_and_do_not_copy_the_defaults_blindly() -> None:
    defaults = SubscriptionDefaults(sampling_interval=100.0, queue_size=10)
    resolved = SubscriptionOptions(
        sampling_interval=0.0, queue_size=1, monitoring_mode=ua.MonitoringMode.Sampling
    ).resolved(defaults)
    assert resolved.sampling_interval == 0.0
    assert resolved.queue_size == 1
    assert resolved.monitoring_mode is ua.MonitoringMode.Sampling


def test_options_can_monitor_another_attribute_although_value_is_the_default() -> None:
    # Monitoring metadata instead of the value is a real (if rare) use case:
    # watching when a tag's Description changes.
    resolved = SubscriptionOptions(attribute=ua.AttributeIds.Description).resolved(
        SubscriptionDefaults()
    )
    assert resolved.attribute is ua.AttributeIds.Description
    assert resolved.as_dict()["attribute"] == "Description"
    assert resolved.as_dict()["monitoring_mode"] == "Reporting"

    default = SubscriptionOptions().resolved(SubscriptionDefaults())
    assert default.as_dict()["attribute"] == "Value"


def make_handle(**overrides: object) -> MonitoredItemHandle:
    options: SubscriptionOptions = overrides.pop("options", SubscriptionOptions())  # type: ignore[assignment]
    fields: dict[str, object] = {
        "node_id": ua.NodeId("Temperature", 2),
        "monitored_item_id": 301,
        "key": "process-values",
        "options": options,
        "requested_sampling_interval": 100.0,
    }
    fields.update(overrides)
    return MonitoredItemHandle(**fields)  # type: ignore[arg-type]


def test_handle_names_the_id_the_server_assigned_because_that_is_what_delete_takes() -> None:
    """ModifyMonitoredItems and DeleteMonitoredItems address the MonitoredItemId.

    The number a notification carries is the ClientHandle, which the client chose.
    Keeping them under one name is how a Chapter 10 client ends up deleting the
    wrong item, so the model names the one it is used for.
    """
    handle = make_handle()
    assert handle.as_dict()["monitored_item_id"] == 301
    assert "handle" not in handle.as_dict()


def test_handle_reports_the_revised_interval_the_server_gave_back() -> None:
    revised = make_handle(revised_sampling_interval=125.0, revised_queue_size=2)
    assert revised.as_dict()["sampling_interval"] == 125.0
    assert revised.as_dict()["queue_size"] == 2

    unmodified = make_handle()
    assert unmodified.as_dict()["sampling_interval"] == 100.0
    assert unmodified.as_dict()["queue_size"] is None


def test_handle_uses_the_same_node_id_text_the_dispatcher_uses() -> None:
    assert make_handle().node_id_text == "ns=2;s=Temperature"


def test_stats_summary_shows_the_mode_a_subscription_is_in() -> None:
    reporting = SubscriptionStats(
        subscription_id=79, publishing_interval=250.0, monitored_items=3, notifications=12
    )
    assert "subscription=79" in reporting.summary()
    assert "mode=reporting" in reporting.summary()

    paused = SubscriptionStats(subscription_id=79, paused=True)
    assert "mode=paused" in paused.summary()


def test_stats_count_failed_items_separately_from_errors() -> None:
    stats = SubscriptionStats(monitored_items=8, failed_items=2, errors=1, recreations=1)
    assert "failed=2" in stats.summary()
    assert "recreations=1" in stats.summary()


def test_stats_sort_notifications_by_the_status_code_the_server_sent() -> None:
    """Counting notifications is not the same as knowing whether they were usable.

    A plant with stuck sensors produces a notification per sample and delivers no
    information. The three counters are what make that visible in one log line.
    """
    stats = SubscriptionStats()
    stats.record_outcome(ua.StatusCode(0))
    stats.record_outcome(ua.StatusCode(ua.StatusCodes.UncertainLastUsableValue))
    stats.record_outcome(ua.StatusCode(ua.StatusCodes.BadNodeIdUnknown))

    assert (stats.good_values, stats.uncertain_values, stats.bad_values) == (1, 1, 1)
    assert stats.notifications == 3
    assert "good=1 uncertain=1 bad=1" in stats.summary()


def test_stats_separate_a_paused_subscription_from_an_unmonitored_one() -> None:
    """``suppressed`` is what the client refused, ``stray`` is what arrived too late."""
    stats = SubscriptionStats(suppressed=4, stray_notifications=2)
    assert "suppressed=4" in stats.summary()
    assert "stray=2" in stats.summary()
