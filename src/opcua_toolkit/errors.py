"""Typed exceptions raised by :mod:`opcua_toolkit`.

Every error carries enough context for an operator to act on it: which node,
which status code, which limit was hit. Silent failures are the main source of
mystery behaviour in OPC UA applications, so the toolkit never swallows a bad
:class:`~asyncua.ua.StatusCode` without reporting it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from asyncua.ua import NodeId, StatusCode


class OpcuaToolkitError(RuntimeError):
    """Base class for every error raised by the toolkit."""


class ConfigurationError(OpcuaToolkitError):
    """Settings are incomplete, inconsistent, or invalid."""


class SecurityConfigurationError(ConfigurationError):
    """A security profile was requested that cannot be configured."""


class ConnectionSetupError(OpcuaToolkitError):
    """The connection could not be established or the endpoint was rejected."""


class EndpointNotFoundError(ConnectionSetupError):
    """The server does not publish the endpoint, policy or namespace requested."""


class NotConnectedError(OpcuaToolkitError):
    """An operation was attempted while the client had no usable session."""


class ServerUnhealthyError(OpcuaToolkitError):
    """The server reports a state or service level that makes collection unsafe."""

    def __init__(
        self,
        message: str,
        *,
        state: Any = None,
        service_level: int | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.service_level = service_level


class OperationLimitError(OpcuaToolkitError):
    """A request exceeded a server operation limit and could not be split."""


class BrowsePathError(OpcuaToolkitError):
    """A browse path could not be resolved on the target server."""

    def __init__(self, message: str, *, path: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.path = path


class NodeNotAccessibleError(OpcuaToolkitError):
    """A node exists but is not readable/writable for this session."""

    def __init__(self, message: str, *, node_id: NodeId | None = None) -> None:
        super().__init__(message)
        self.node_id = node_id


class PartialFailureError(OpcuaToolkitError):
    """A bulk operation completed, but some items failed."""

    def __init__(self, message: str, *, failures: list[Any] | None = None) -> None:
        super().__init__(message)
        self.failures: list[Any] = failures or []


class MethodNotFoundError(OpcuaToolkitError):
    """The requested method is not a component of the expected parent node."""


class MethodCallError(OpcuaToolkitError):
    """A method call returned a bad status code."""

    def __init__(
        self,
        message: str,
        *,
        status_code: StatusCode | None = None,
        input_argument_results: list[StatusCode] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.input_argument_results = input_argument_results or []
        self.bad_arguments = [
            index
            for index, code in enumerate(self.input_argument_results)
            if code is not None and code.is_bad()
        ]


class SubscriptionClosedError(OpcuaToolkitError):
    """The subscription was already closed."""


def describe_status(status: StatusCode | None) -> str:
    """Return a stable ``Name (0x........)`` representation for a status code."""
    if status is None:
        return "Unknown"
    name = getattr(status, "name", None) or "Unknown"
    return f"{name} (0x{status.value & 0xFFFFFFFF:08X})"


def format_node_id(node_id: NodeId | Any) -> str:
    """Format a NodeId for humans without leaking implementation details."""
    if node_id is None:
        return "<none>"
    if isinstance(node_id, str):
        return node_id
    identifier = getattr(node_id, "Identifier", node_id)
    namespace = getattr(node_id, "NamespaceIndex", 0)
    if isinstance(identifier, str):
        return f"ns={namespace};s={identifier}"
    return f"ns={namespace};i={identifier}"


__all__ = [
    "BrowsePathError",
    "ConfigurationError",
    "ConnectionSetupError",
    "EndpointNotFoundError",
    "MethodCallError",
    "MethodNotFoundError",
    "NodeNotAccessibleError",
    "NotConnectedError",
    "OpcuaToolkitError",
    "OperationLimitError",
    "PartialFailureError",
    "SecurityConfigurationError",
    "ServerUnhealthyError",
    "SubscriptionClosedError",
    "describe_status",
    "format_node_id",
]
