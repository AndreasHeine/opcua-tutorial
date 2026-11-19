"""Variant typing and value coercion.

The single most common OPC UA client bug is a type mismatch: Python ``float``
is 64 bit, but a variable declared as ``Single`` (Float, NodeId 10) expects 32
bit. asyncua then answers ``BadTypeMismatch``. This module encodes the built-in
data types once, so read, write and method chapters can convert safely instead
of guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from asyncua import ua

from opcua_toolkit.errors import ConfigurationError

#: NodeId of every built-in OPC UA data type -> asyncua VariantType.
BUILTIN_DATA_TYPES: dict[int, ua.VariantType] = {
    ua.ObjectIds.Boolean: ua.VariantType.Boolean,
    ua.ObjectIds.SByte: ua.VariantType.SByte,
    ua.ObjectIds.Byte: ua.VariantType.Byte,
    ua.ObjectIds.Int16: ua.VariantType.Int16,
    ua.ObjectIds.UInt16: ua.VariantType.UInt16,
    ua.ObjectIds.Int32: ua.VariantType.Int32,
    ua.ObjectIds.UInt32: ua.VariantType.UInt32,
    ua.ObjectIds.Int64: ua.VariantType.Int64,
    ua.ObjectIds.UInt64: ua.VariantType.UInt64,
    ua.ObjectIds.Float: ua.VariantType.Float,
    ua.ObjectIds.Double: ua.VariantType.Double,
    ua.ObjectIds.String: ua.VariantType.String,
    ua.ObjectIds.DateTime: ua.VariantType.DateTime,
    ua.ObjectIds.Guid: ua.VariantType.Guid,
    ua.ObjectIds.ByteString: ua.VariantType.ByteString,
    ua.ObjectIds.XmlElement: ua.VariantType.XmlElement,
    ua.ObjectIds.NodeId: ua.VariantType.NodeId,
    ua.ObjectIds.ExpandedNodeId: ua.VariantType.ExpandedNodeId,
    ua.ObjectIds.StatusCode: ua.VariantType.StatusCode,
    ua.ObjectIds.QualifiedName: ua.VariantType.QualifiedName,
    ua.ObjectIds.LocalizedText: ua.VariantType.LocalizedText,
}

_INTEGER_RANGES: dict[ua.VariantType, tuple[int, int]] = {
    ua.VariantType.SByte: (-128, 127),
    ua.VariantType.Byte: (0, 255),
    ua.VariantType.Int16: (-32_768, 32_767),
    ua.VariantType.UInt16: (0, 65_535),
    ua.VariantType.Int32: (-2_147_483_648, 2_147_483_647),
    ua.VariantType.UInt32: (0, 4_294_967_295),
    ua.VariantType.Int64: (-9_223_372_036_854_775_808, 9_223_372_036_854_775_807),
    ua.VariantType.UInt64: (0, 18_446_744_073_709_551_615),
}

_FLOAT_LIMITS: dict[ua.VariantType, float] = {ua.VariantType.Float: 3.402_823_5e38}


def variant_type_for_data_type(data_type: ua.NodeId | None) -> ua.VariantType | None:
    """Map a data type NodeId to an asyncua VariantType for built-in types.

    Returns:
        The matching VariantType, or ``None`` for custom or structured types.
    """
    if data_type is None:
        return None
    if data_type.NamespaceIndex != 0:
        return None
    if isinstance(data_type.Identifier, int):
        return BUILTIN_DATA_TYPES.get(data_type.Identifier)
    return None


def data_type_name(data_type: ua.NodeId | None) -> str:
    """Human readable data type name, falling back to the NodeId string."""
    if data_type is None:
        return "BaseDataType"
    variant_type = variant_type_for_data_type(data_type)
    if variant_type is not None:
        return str(variant_type.name)
    if data_type.NamespaceIndex == 0 and isinstance(data_type.Identifier, int):
        return _STD_TYPE_NAMES.get(data_type.Identifier, f"ns=0;i={data_type.Identifier}")
    return f"ns={data_type.NamespaceIndex};i={data_type.Identifier}"


_STD_TYPE_NAMES: dict[int, str] = {
    ua.ObjectIds.BaseDataType: "BaseDataType",
    ua.ObjectIds.Number: "Number",
    ua.ObjectIds.Integer: "Integer",
    ua.ObjectIds.UInteger: "UInteger",
    ua.ObjectIds.Enumeration: "Enumeration",
    ua.ObjectIds.Structure: "Structure",
    ua.ObjectIds.DataValue: "DataValue",
    ua.ObjectIds.DiagnosticInfo: "DiagnosticInfo",
}


def coerce_to_variant_type(value: Any, variant_type: ua.VariantType) -> Any:
    """Convert a Python value so it matches the requested OPC UA type.

    Args:
        value: The Python value to write or pass as a method argument.
        variant_type: The target OPC UA variant type.

    Returns:
        A Python value that asyncua can encode as ``variant_type``.

    Raises:
        ConfigurationError: The value cannot be represented by that type.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        # A ValueRank >= 0 variable carries a one dimensional array. Coercing each
        # element keeps the "BadTypeMismatch means what you sent, not what you meant"
        # promise for array tags too.
        return [coerce_to_variant_type(item, variant_type) for item in value]
    if variant_type in _INTEGER_RANGES:
        try:
            converted = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"{value!r} is not an integer for {variant_type.name}"
            ) from exc
        low, high = _INTEGER_RANGES[variant_type]
        if not low <= converted <= high:
            raise ConfigurationError(
                f"{converted} does not fit in {variant_type.name} ({low}..{high})"
            )
        return converted
    if variant_type in (ua.VariantType.Float, ua.VariantType.Double):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(f"{value!r} is not numeric for {variant_type.name}") from exc
        limit = _FLOAT_LIMITS.get(variant_type)
        if limit is not None and abs(number) > limit:
            raise ConfigurationError(f"{number} does not fit in {variant_type.name}")
        return number
    if variant_type is ua.VariantType.Boolean:
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "on", "yes"}
        return bool(value)
    if variant_type is ua.VariantType.String:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)
    if variant_type is ua.VariantType.ByteString:
        if isinstance(value, str):
            return value.encode("utf-8")
        return bytes(value)
    if variant_type is ua.VariantType.DateTime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ConfigurationError(f"{value!r} is not an ISO 8601 timestamp") from exc
        # An ISO string with an offset is a moment in time: convert it, do not relabel it.
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return value


def make_variant(value: Any, *, variant_type: ua.VariantType | None = None) -> ua.Variant:
    """Build a :class:`asyncua.ua.Variant`, coercing when the type is known.

    Never rely on asyncua guessing the type: a Python ``int`` written to a
    Double node works, but a ``float`` written to a Single node does not.
    """
    if variant_type is None:
        return ua.Variant(value)
    return ua.Variant(coerce_to_variant_type(value, variant_type), variant_type)


def as_node_class(value: Any) -> ua.NodeClass:
    """Coerce the Int32 a server actually sends into a :class:`asyncua.ua.NodeClass`.

    ``NodeClass`` is an enumeration in the specification, but on the wire it is an
    Int32, and asyncua hands back whichever the server sent. A client that only
    accepts the enum reports every node it has read as ``Unspecified`` - the most
    common way a browse result turns into a wall of identical rows.
    """
    if isinstance(value, ua.NodeClass):
        return value
    try:
        return ua.NodeClass(int(value))
    except (TypeError, ValueError):
        return ua.NodeClass.Unspecified


def as_qualified_name(value: Any, *, default_namespace: int = 0) -> ua.QualifiedName:
    """Coerce a server value into a :class:`asyncua.ua.QualifiedName`.

    The argument order is ``QualifiedName(Name, NamespaceIndex)``. Getting it the
    other way round is not a silent bug: asyncua detects it, logs a warning, and
    then raises ``ValueError`` from its own constructor.
    """
    if isinstance(value, ua.QualifiedName):
        return value
    name = getattr(value, "Name", None)
    if name is None:
        name = "" if value is None else str(value)
    namespace = getattr(value, "NamespaceIndex", None)
    index = default_namespace if namespace is None else int(namespace)
    return ua.QualifiedName(str(name), index)


def unwrap(value: Any) -> Any:
    """Return the plain Python value behind a ``Variant``.

    asyncua models three different things and beginners mix them up constantly:
    ``DataValue`` (value + status + timestamps), ``Variant`` (value + type) and
    the raw Python value. Toolkit read paths always hand back unwrapped values
    together with the status code, never a bare Variant.
    """
    if isinstance(value, ua.Variant):
        return value.Value
    return value


#: How many array elements :func:`format_value` shows before collapsing the rest.
MAX_PREVIEW_ITEMS = 5


def format_value(value: Any, *, max_length: int = 60) -> str:
    """Format a value for terminal output without dumping huge arrays."""
    if isinstance(value, (bytes, bytearray)):
        preview = value[: max_length - 4].hex()
        suffix = "..." if len(value) > max_length - 4 else ""
        return f"0x{preview}{suffix} ({len(value)} bytes)"
    if isinstance(value, (list, tuple)):
        preview_items = list(value)[:MAX_PREVIEW_ITEMS]
        head = ", ".join(format_value(item, max_length=20) for item in preview_items)
        extra = ", ..." if len(value) > MAX_PREVIEW_ITEMS else ""
        return f"[{head}{extra}] ({len(value)} items)"
    text = str(value)
    if len(text) > max_length:
        return f"{text[: max_length - 3]}..."
    return text


__all__ = [
    "BUILTIN_DATA_TYPES",
    "MAX_PREVIEW_ITEMS",
    "as_node_class",
    "as_qualified_name",
    "coerce_to_variant_type",
    "data_type_name",
    "format_value",
    "make_variant",
    "unwrap",
    "variant_type_for_data_type",
]
