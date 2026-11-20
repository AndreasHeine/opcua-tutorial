"""Chapter 8 tests: the write report contract.

The report is the part a collector depends on, so its arithmetic is tested
without a server: one outcome per requested item, failures visible, and a
status code that means "ask again" kept separate from one that means "wrong".
"""

from __future__ import annotations

import pytest
from asyncua import ua

from opcua_toolkit.errors import PartialFailureError
from opcua_toolkit.write import (
    RETRYABLE_STATUSES,
    SHRINK_STATUSES,
    WriteItem,
    WriteOutcome,
    WriteReport,
)


def outcome(identifier: str = "Temperature", status: ua.StatusCode | None = None) -> WriteOutcome:
    return WriteOutcome(
        node_id=ua.NodeId(identifier, 2),
        status=status if status is not None else ua.StatusCode(ua.StatusCodes.Good),
    )


def test_write_item_accepts_a_plain_string_and_still_exposes_canonical_text() -> None:
    item = WriteItem(node_id=ua.NodeId("BatchTag001", 2), value=11)
    assert item.node_id_text == "ns=2;s=BatchTag001"


def test_an_uncertain_write_counts_as_accepted_because_the_server_applied_it() -> None:
    accepted = outcome(status=ua.StatusCode(ua.StatusCodes.UncertainLastUsableValue))
    assert accepted.ok
    assert not outcome(status=ua.StatusCode(ua.StatusCodes.BadTypeMismatch)).ok


def test_a_report_with_no_outcomes_is_not_a_successful_report() -> None:
    # The invariant: requested must equal outcomes. A gap means values went missing.
    report = WriteReport(requested=3)
    assert report.succeeded == 0
    assert report.failed == 0
    assert report.unaccounted == 3
    assert "MISSING_OUTCOMES=3" in report.summary()


def test_a_complete_report_has_no_missing_outcomes() -> None:
    report = WriteReport(requested=2, outcomes=[outcome("A"), outcome("B")])
    assert report.unaccounted == 0
    assert "MISSING_OUTCOMES" not in report.summary()
    assert "items=2 outcomes=2 ok=2" in report.summary()


def test_failures_are_listed_in_the_order_they_were_requested() -> None:
    report = WriteReport(
        requested=3,
        outcomes=[
            outcome("A"),
            outcome("B", ua.StatusCode(ua.StatusCodes.BadNotWritable)),
            outcome("C", ua.StatusCode(ua.StatusCodes.BadTypeMismatch)),
        ],
    )
    assert report.succeeded == 1
    assert report.failed == 2
    assert [failed.node_id_text for failed in report.failures()] == [
        "ns=2;s=B",
        "ns=2;s=C",
    ]


def test_raise_for_failures_names_the_nodes_that_failed() -> None:
    report = WriteReport(
        requested=2,
        outcomes=[
            outcome("A"),
            outcome("Vibration", ua.StatusCode(ua.StatusCodes.BadNotWritable)),
        ],
    )
    with pytest.raises(PartialFailureError, match="ns=2;s=Vibration=BadNotWritable") as caught:
        report.raise_for_failures()
    assert caught.value.failures
    assert report.raise_for_failures is not None


def test_raise_for_failures_returns_the_same_report_when_everything_worked() -> None:
    report = WriteReport(requested=1, outcomes=[outcome()])
    assert report.raise_for_failures() is report


def test_retryable_status_codes_are_the_ones_where_trying_again_can_help() -> None:
    assert ua.StatusCodes.BadTooManyOperations in RETRYABLE_STATUSES
    assert ua.StatusCodes.BadServerTooBusy in RETRYABLE_STATUSES
    # These are the caller's fault: retrying cannot change the outcome.
    assert ua.StatusCodes.BadNotWritable not in RETRYABLE_STATUSES
    assert ua.StatusCodes.BadTypeMismatch not in RETRYABLE_STATUSES
    assert ua.StatusCodes.BadNodeIdUnknown not in RETRYABLE_STATUSES


def test_only_the_limit_status_code_may_be_answered_by_shrinking_a_chunk() -> None:
    assert {ua.StatusCodes.BadTooManyOperations} == SHRINK_STATUSES
    assert SHRINK_STATUSES <= RETRYABLE_STATUSES


def test_outcome_as_dict_is_json_safe() -> None:
    encoded = outcome("Setpoint", ua.StatusCode(ua.StatusCodes.BadTypeMismatch)).as_dict()
    assert encoded["node_id"] == "ns=2;s=Setpoint"
    assert encoded["status"] == "BadTypeMismatch"
    assert encoded["attempts"] == 1
