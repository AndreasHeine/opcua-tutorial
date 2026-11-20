"""Chapter 8 tests: Variant typing, coercion and value formatting."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from asyncua import ua

from opcua_toolkit.errors import ConfigurationError
from opcua_toolkit.values import (
    BUILTIN_DATA_TYPES,
    coerce_to_variant_type,
    data_type_name,
    format_value,
    make_variant,
    unwrap,
    variant_type_for_data_type,
)


def test_builtin_data_types_cover_the_types_a_plant_actually_uses() -> None:
    for object_id in (
        ua.ObjectIds.Boolean,
        ua.ObjectIds.Int32,
        ua.ObjectIds.UInt32,
        ua.ObjectIds.Int64,
        ua.ObjectIds.Float,
        ua.ObjectIds.Double,
        ua.ObjectIds.String,
        ua.ObjectIds.DateTime,
    ):
        assert object_id in BUILTIN_DATA_TYPES


def test_variant_type_for_data_type_maps_namespace_zero_builtins() -> None:
    assert variant_type_for_data_type(ua.NodeId(ua.ObjectIds.UInt32, 0)) is ua.VariantType.UInt32
    assert variant_type_for_data_type(ua.NodeId(ua.ObjectIds.Double, 0)) is ua.VariantType.Double


def test_variant_type_for_data_type_refuses_custom_types_and_missing_types() -> None:
    # A companion spec data type lives in the model namespace, not in namespace 0.
    assert variant_type_for_data_type(ua.NodeId(42, 3)) is None
    assert variant_type_for_data_type(ua.NodeId("Machine", 3)) is None
    assert variant_type_for_data_type(None) is None


def test_data_type_name_is_readable_for_builtins_abstractions_and_custom_types() -> None:
    assert data_type_name(ua.NodeId(ua.ObjectIds.Int32, 0)) == "Int32"
    assert data_type_name(ua.NodeId(ua.ObjectIds.BaseDataType, 0)) == "BaseDataType"
    assert data_type_name(None) == "BaseDataType"
    assert data_type_name(ua.NodeId(9999, 0)) == "ns=0;i=9999"
    assert data_type_name(ua.NodeId(7, 2)) == "ns=2;i=7"


@pytest.mark.parametrize(
    ("variant_type", "value", "expected"),
    [
        (ua.VariantType.Int32, 42, 42),
        (ua.VariantType.UInt32, "17", 17),
        (ua.VariantType.Float, 21.5, 21.5),
        (ua.VariantType.Double, "3.25", 3.25),
        (ua.VariantType.Boolean, "on", True),
        (ua.VariantType.Boolean, "off", False),
        (ua.VariantType.Boolean, 1, True),
        (ua.VariantType.String, b"line1", "line1"),
        (ua.VariantType.String, 12, "12"),
        (ua.VariantType.ByteString, "line1", b"line1"),
        (ua.VariantType.Int32, None, None),
    ],
)
def test_coerce_to_variant_type_accepts_what_a_plant_sends(
    variant_type: ua.VariantType, value: object, expected: object
) -> None:
    assert coerce_to_variant_type(value, variant_type) == expected


def test_coerce_to_variant_type_respects_integer_ranges() -> None:
    with pytest.raises(ConfigurationError, match="does not fit in UInt32"):
        coerce_to_variant_type(4_294_967_296, ua.VariantType.UInt32)
    with pytest.raises(ConfigurationError, match="does not fit in Int16"):
        coerce_to_variant_type(-32_769, ua.VariantType.Int16)
    with pytest.raises(ConfigurationError, match="does not fit in Byte"):
        coerce_to_variant_type(256, ua.VariantType.Byte)


def test_coerce_to_variant_type_reports_values_that_are_not_numbers() -> None:
    with pytest.raises(ConfigurationError, match="not an integer"):
        coerce_to_variant_type("not a number", ua.VariantType.Int32)
    with pytest.raises(ConfigurationError, match="not numeric"):
        coerce_to_variant_type("not a number", ua.VariantType.Float)


def test_coerce_to_variant_type_catches_the_float_precision_trap() -> None:
    # 1e39 fits a Double but overflows a Single: asyncua would answer BadTypeMismatch.
    with pytest.raises(ConfigurationError, match="does not fit in Float"):
        coerce_to_variant_type(1e39, ua.VariantType.Float)
    assert coerce_to_variant_type(1e30, ua.VariantType.Float) == 1e30


def test_coerce_to_variant_type_normalises_timestamps_to_utc() -> None:
    naive = datetime(2026, 3, 1, 12, 0, 0)  # noqa: DTZ001 - deliberately naive input
    converted = coerce_to_variant_type(naive, ua.VariantType.DateTime)
    assert converted.tzinfo is not None
    assert converted.utcoffset().total_seconds() == 0

    from_text = coerce_to_variant_type("2026-03-01T12:00:00+01:00", ua.VariantType.DateTime)
    assert from_text == naive.replace(tzinfo=UTC).astimezone(UTC).replace(hour=11)

    with pytest.raises(ConfigurationError, match="ISO 8601"):
        coerce_to_variant_type("last tuesday", ua.VariantType.DateTime)


def test_make_variant_pins_the_type_instead_of_guessing_it() -> None:
    guessed = make_variant(7)
    pinned = make_variant(7, variant_type=ua.VariantType.UInt32)

    # This is the lesson: asyncua's default for a Python int is a 64 bit Int64.
    assert guessed.VariantType is ua.VariantType.Int64
    assert pinned.VariantType is ua.VariantType.UInt32
    assert pinned.Value == 7


def test_make_variant_coerces_before_wrapping() -> None:
    variant = make_variant("21.5", variant_type=ua.VariantType.Float)
    assert variant.VariantType is ua.VariantType.Float
    assert variant.Value == 21.5


def test_make_variant_reports_a_value_that_cannot_be_pinned() -> None:
    with pytest.raises(ConfigurationError):
        make_variant("hot", variant_type=ua.VariantType.Int32)


def test_unwrap_reaches_through_a_variant_but_leaves_plain_values_alone() -> None:
    assert unwrap(ua.Variant(11, ua.VariantType.Int32)) == 11
    assert unwrap(11) == 11
    assert unwrap(None) is None


def test_format_value_keeps_arrays_readable() -> None:
    assert format_value([1, 2, 3]) == "[1, 2, 3] (3 items)"
    big = format_value(list(range(1, 1_001)))
    assert "1, 2, 3, 4, 5, ..." in big
    assert "(1000 items)" in big
    assert len(big) < 200


def test_format_value_prints_binary_as_hex() -> None:
    assert format_value(b"\x01\x02\x03") == "0x010203 (3 bytes)"


def test_format_value_truncates_long_text() -> None:
    text = format_value("x" * 500, max_length=40)
    assert text.endswith("...")
    assert len(text) <= 40
