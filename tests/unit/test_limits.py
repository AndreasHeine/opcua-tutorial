"""Chapter 7 tests: operation limits and chunking.

The server side of these tests lives in ``tests/integration/test_read.py`` and
``test_write.py``: here we test the arithmetic that keeps a request legal.
"""

from __future__ import annotations

import pytest

from opcua_toolkit.errors import OperationLimitError
from opcua_toolkit.limits import (
    DEFAULT_MAX_NODES_PER_BROWSE,
    DEFAULT_MAX_NODES_PER_CALL,
    LIMIT_PROPERTY_IDS,
    OperationLimits,
    chunked,
)


def test_chunked_splits_into_the_sizes_a_server_allows() -> None:
    chunks = [list(chunk) for chunk in chunked(list(range(24)), 10)]
    assert chunks == [
        list(range(0, 10)),
        list(range(10, 20)),
        list(range(20, 24)),
    ]


def test_chunked_never_reorders_or_loses_an_item() -> None:
    items = list(range(101))
    flat = [item for chunk in chunked(items, 7) for item in chunk]
    assert flat == items


def test_chunked_produces_nothing_for_nothing() -> None:
    assert list(chunked([], 10)) == []


def test_chunked_refuses_a_zero_or_negative_chunk_size() -> None:
    # A limit of 0 is not "unlimited", it is a configuration bug we want loudly.
    with pytest.raises(OperationLimitError, match="must be > 0"):
        list(chunked([1, 2, 3], 0))
    with pytest.raises(OperationLimitError):
        list(chunked([1, 2, 3], -5))


def test_chunk_size_larger_than_the_input_yields_one_chunk() -> None:
    assert [list(chunk) for chunk in chunked([1, 2], 1_000)] == [[1, 2]]


def test_limits_fall_back_to_conservative_defaults() -> None:
    limits = OperationLimits()
    assert limits.max_nodes_per_read == DEFAULT_MAX_NODES_PER_CALL
    assert limits.max_nodes_per_browse == DEFAULT_MAX_NODES_PER_BROWSE
    assert limits.reported == frozenset()
    assert len(limits.unresolved()) == len(LIMIT_PROPERTY_IDS)
    assert "server omitted" in limits.status_note()


def test_with_overrides_applies_only_valid_known_overrides() -> None:
    limits = OperationLimits().with_overrides(
        {
            "max_nodes_per_read": 5,
            "max_nodes_per_write": None,
            "max_nodes_per_browse": 0,
            "max_nodes_per_mars": 3,
        }
    )
    assert limits.max_nodes_per_read == 5
    assert limits.max_nodes_per_write == DEFAULT_MAX_NODES_PER_CALL
    assert limits.max_nodes_per_browse == DEFAULT_MAX_NODES_PER_BROWSE
    assert not hasattr(limits, "max_nodes_per_mars")


def test_with_overrides_without_changes_returns_the_same_values() -> None:
    original = OperationLimits(max_nodes_per_read=11)
    same = original.with_overrides({})
    assert same.max_nodes_per_read == 11


def test_limit_property_ids_cover_every_operation_limit() -> None:
    assert set(LIMIT_PROPERTY_IDS) == {
        "max_nodes_per_read",
        "max_nodes_per_write",
        "max_nodes_per_method_call",
        "max_nodes_per_browse",
        "max_nodes_per_register_nodes",
        "max_nodes_per_translate_browse_paths",
        "max_nodes_per_node_management",
        "max_monitored_items_per_call",
    }
    assert all(isinstance(identifier, int) for identifier in LIMIT_PROPERTY_IDS.values())


def test_describe_and_status_note_are_useful_without_being_noisy() -> None:
    limits = OperationLimits(
        max_nodes_per_read=10,
        max_nodes_per_write=10,
        max_nodes_per_browse=10,
        max_nodes_per_method_call=5,
        max_nodes_per_register_nodes=100,
        max_nodes_per_translate_browse_paths=100,
        max_nodes_per_node_management=100,
        max_monitored_items_per_call=10,
        min_supported_sample_rate=50.0,
        reported=frozenset(LIMIT_PROPERTY_IDS),
    )
    described = limits.describe()
    assert "max_nodes_per_read: 10" in described
    assert "min_supported_sample_rate: 50 ms" in described
    assert "50.0 ms" not in described, "50 ms is not a float in a report"
    assert "not published" not in described
    assert limits.status_note() == "server published all operation limits"


def test_unresolved_names_are_stable_across_runs() -> None:
    limits = OperationLimits(reported=frozenset({"max_nodes_per_read", "max_nodes_per_write"}))
    unresolved = limits.unresolved()
    assert unresolved == tuple(sorted(unresolved))
    assert "max_nodes_per_read" not in unresolved
    assert "max_monitored_items_per_call" in unresolved
