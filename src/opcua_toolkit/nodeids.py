"""NodeId conversion helpers shared by every toolkit module.

OPC UA has four NodeId namespaces (numeric, string, GUID, opaque). asyncua
accepts ``str``, ``int``, :class:`asyncua.ua.NodeId` and :class:`asyncua.Node`
almost everywhere. The toolkit normalises to :class:`asyncua.ua.NodeId` at the
boundary so batching, chunking and hashing are always based on one canonical
type.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from asyncua import Node, ua
from asyncua.ua.uaerrors import UaStringParsingError

from opcua_toolkit.errors import ConfigurationError

#: Anything :func:`to_nodeid` accepts: canonical NodeId text (``ns=2;s=Temperature``),
#: a numeric identifier, or an asyncua :class:`~asyncua.ua.NodeId` / :class:`~asyncua.Node`.
#:
#: The ``Any`` member is deliberate, not laziness. This project does not type check
#: asyncua's own protocol classes (see the mypy overrides in ``pyproject.toml``): they
#: annotate the wire with ``Int32``, ``String`` and ``Boolean`` instead of ``int``,
#: ``str`` and ``bool``, so naming them here would advertise a guarantee mypy cannot
#: actually check. The conversion itself is enforced at runtime by :func:`to_nodeid`.
type NodeIdLike = str | int | Any

T = TypeVar("T")

_NODE_ID_PREFIXES = ("ns=", "i=", "s=", "g=", "b=")


def to_nodeid(value: NodeIdLike) -> ua.NodeId:
    """Convert any supported NodeId representation to :class:`asyncua.ua.NodeId`.

    Args:
        value: ``"ns=2;s=Temperature"``, ``2259``, ``ua.NodeId(...)`` or a ``Node``.

    Returns:
        The canonical NodeId.

    Raises:
        ConfigurationError: The value cannot be interpreted as a NodeId.
    """
    if isinstance(value, Node):
        return value.nodeid
    if isinstance(value, ua.NodeId):
        return value
    if isinstance(value, int):
        return ua.NodeId(value, 0)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ConfigurationError("Empty NodeId string")
        if text.startswith(_NODE_ID_PREFIXES):
            try:
                return ua.NodeId.from_string(text)
            except (ValueError, TypeError, UaStringParsingError) as exc:
                raise ConfigurationError(f"Invalid NodeId string {value!r}") from exc
        if text.isdigit():
            return ua.NodeId(int(text), 0)
        raise ConfigurationError(
            f"NodeId {value!r} must start with 'ns=0;' or be a numeric or string NodeId"
        )
    raise ConfigurationError(f"Cannot convert {type(value).__name__} to a NodeId")


def to_nodeid_list(values: Iterable[NodeIdLike]) -> list[ua.NodeId]:
    """Convert an iterable of NodeId-like values, preserving order."""
    return [to_nodeid(value) for value in values]


def node_ids_equal(left: NodeIdLike, right: NodeIdLike) -> bool:
    """Compare NodeIds including namespace index semantics."""
    return bool(to_nodeid(left) == to_nodeid(right))


def looks_like_node_id_text(value: object) -> bool:
    """Whether a string is NodeId text (``ns=2;s=Temperature``) rather than a bare name.

    Modules that accept *either* a BrowseName or a NodeId need this decision before they
    can pick which one they are looking at, and a bare name like ``PumpType`` must not be
    fed to :func:`to_nodeid` - it would be rejected as an invalid NodeId instead of being
    recognised as the name of something this module authored itself.
    """
    return isinstance(value, str) and value.strip().startswith(_NODE_ID_PREFIXES)


__all__ = [
    "NodeIdLike",
    "looks_like_node_id_text",
    "node_ids_equal",
    "to_nodeid",
    "to_nodeid_list",
]
