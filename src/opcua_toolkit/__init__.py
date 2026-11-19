"""OPC UA toolkit: the reusable building blocks the tutorial builds with.

The package is deliberately small and boring at the top level: import the
component you need, each one has exactly one job.

Client side building blocks:

* :class:`ClientSettings` - validated configuration that creates a client.
* :class:`OperationLimits` - what the server allows per request, plus
  :func:`chunked`.
* :class:`NamespaceExplorer` - Browse, BrowseNext, TranslateBrowsePathsToNodeIds.
* :class:`NodeInspector` - the attributes that describe a node.
* :class:`BulkReader` / :class:`BulkWriter` - batched IO that keeps every
  StatusCode.
* :class:`MethodCatalog` / :class:`MethodInvoker` - generic method discovery and
  typed invocation.
* :class:`SubscriptionManager` - monitored items, dispatch, pause and rebuild.
* :class:`ConnectionSupervisor` - health aware reconnect handling.
* :class:`EndpointDiscovery` - GetEndpoints, FindServers and server profiling.

Tutorial side helper: :class:`DemoServer`, the executable server every client
chapter runs against.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from opcua_toolkit.browse import BrowseStats, BrowseTree, NamespaceExplorer, NodeRef
from opcua_toolkit.devserver import DemoServer, DemoServerConfig
from opcua_toolkit.diagnostics import JsonFormatter, configure_logging, log_with
from opcua_toolkit.discovery import DiscoveredServer, EndpointDiscovery, EndpointInfo, ServerProfile
from opcua_toolkit.errors import (
    BrowsePathError,
    ConfigurationError,
    ConnectionSetupError,
    EndpointNotFoundError,
    MethodCallError,
    MethodNotFoundError,
    NodeNotAccessibleError,
    NotConnectedError,
    OpcuaToolkitError,
    OperationLimitError,
    PartialFailureError,
    SecurityConfigurationError,
    ServerUnhealthyError,
    SubscriptionClosedError,
    describe_status,
    format_node_id,
)
from opcua_toolkit.health import ServerHealth, ServiceLevelBand, classify_service_level
from opcua_toolkit.inspector import AccessRights, NodeDescriptor, NodeInspector
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.methods import (
    ArgumentSpec,
    CallResult,
    MethodCatalog,
    MethodDescriptor,
    MethodInvoker,
)
from opcua_toolkit.read import BulkReader, BulkReadStats, ReadResult
from opcua_toolkit.resilience import ConnectionStatus, ConnectionSupervisor
from opcua_toolkit.settings import (
    ClientSettings,
    LimitsOverrides,
    RetryPolicy,
    SecuritySettings,
    SubscriptionDefaults,
)
from opcua_toolkit.subscriptions import (
    DataChangeRecord,
    MonitoredItemHandle,
    NotificationCallback,
    SubscriptionManager,
    SubscriptionOptions,
    SubscriptionRevision,
    SubscriptionStats,
)
from opcua_toolkit.values import (
    as_node_class,
    as_qualified_name,
    coerce_to_variant_type,
    data_type_name,
    make_variant,
    unwrap,
)
from opcua_toolkit.write import BulkWriter, WriteItem, WriteOutcome, WriteReport

try:
    __version__ = version("opcua-tutorial")
except PackageNotFoundError:  # running from a source checkout without the install
    __version__ = "2.0.0"

__all__ = [
    "AccessRights",
    "ArgumentSpec",
    "BrowsePathError",
    "BrowseStats",
    "BrowseTree",
    "BulkReadStats",
    "BulkReader",
    "BulkWriter",
    "CallResult",
    "ClientSettings",
    "ConfigurationError",
    "ConnectionSetupError",
    "ConnectionStatus",
    "ConnectionSupervisor",
    "DataChangeRecord",
    "DemoServer",
    "DemoServerConfig",
    "DiscoveredServer",
    "EndpointDiscovery",
    "EndpointInfo",
    "EndpointNotFoundError",
    "JsonFormatter",
    "LimitsOverrides",
    "MethodCallError",
    "MethodCatalog",
    "MethodDescriptor",
    "MethodInvoker",
    "MethodNotFoundError",
    "MonitoredItemHandle",
    "NamespaceExplorer",
    "NodeDescriptor",
    "NodeInspector",
    "NodeNotAccessibleError",
    "NodeRef",
    "NotConnectedError",
    "NotificationCallback",
    "OpcuaToolkitError",
    "OperationLimitError",
    "OperationLimits",
    "PartialFailureError",
    "ReadResult",
    "RetryPolicy",
    "SecurityConfigurationError",
    "SecuritySettings",
    "ServerHealth",
    "ServerProfile",
    "ServerUnhealthyError",
    "ServiceLevelBand",
    "SubscriptionClosedError",
    "SubscriptionDefaults",
    "SubscriptionManager",
    "SubscriptionOptions",
    "SubscriptionRevision",
    "SubscriptionStats",
    "WriteItem",
    "WriteOutcome",
    "WriteReport",
    "__version__",
    "as_node_class",
    "as_qualified_name",
    "chunked",
    "classify_service_level",
    "coerce_to_variant_type",
    "configure_logging",
    "data_type_name",
    "describe_status",
    "format_node_id",
    "log_with",
    "make_variant",
    "unwrap",
]
