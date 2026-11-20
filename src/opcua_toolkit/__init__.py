"""OPC UA toolkit: the reusable building blocks the tutorial builds with.

The package is deliberately small and boring at the top level: import the
component you need, each one has exactly one job.

Client side building blocks:

* :class:`ClientSettings` - validated configuration that creates a client.
* :func:`load_config` - a configuration file, an environment layer and a derived
  layer resolved into one :class:`ApplicationConfig`, with provenance for every
  value and a review that refuses contradictions.
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
* :class:`NotificationBuffer` and :class:`JsonlSpool` - what an application keeps while
  nothing downstream is reading, what each one drops, and what each one admits to losing.
* :class:`EndpointDiscovery` - GetEndpoints, FindServers and server profiling.

Server side building blocks:

* :class:`ObjectTypeSpec` and :class:`ComponentSpec` - an address space written as
  declarations: ObjectTypes, their InstanceDeclarations, and the VariableTypes some of
  them are typed with.
* :class:`AddressSpaceAuthor` - writes those declarations into a server, instantiates
  them, and returns what the server now holds rather than what was asked for.
* :class:`AddressSpaceReader` - reads what a server's types promise, checks one instance
  against the type it claims, and measures how much of a subtree has a type at all.
* :class:`ModellingRule` - the four rules OPC 10000-3 defines, and what each one promises.

Tutorial side helper: :class:`DemoServer`, the executable server every client
chapter runs against. With :attr:`DemoServerConfig.authored_model` switched on it also
authors the declared model Chapter 16 teaches, in its own ``AssetModel`` folder.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from opcua_toolkit.browse import BrowseStats, BrowseTree, NamespaceExplorer, NodeRef
from opcua_toolkit.buffer import (
    BufferDecision,
    BufferSettings,
    BufferStats,
    DropPolicy,
    NotificationBuffer,
    buffering_handler,
    offer_many,
)
from opcua_toolkit.config import (
    ApplicationConfig,
    ConfigFinding,
    ConfigLayer,
    ConfigProvenance,
    ConfigReview,
    FindingSeverity,
    ResolvedConfig,
    ResolvedValue,
    SupervisionSettings,
    TagSettings,
    ValueOrigin,
    discovery_settings,
    load_config,
)
from opcua_toolkit.devserver import DemoServer, DemoServerConfig
from opcua_toolkit.diagnostics import JsonFormatter, configure_logging, log_with
from opcua_toolkit.discovery import DiscoveredServer, EndpointDiscovery, EndpointInfo, ServerProfile
from opcua_toolkit.errors import (
    AddressSpaceError,
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
    SpoolError,
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
from opcua_toolkit.server import (
    AddressSpaceAuthor,
    AddressSpaceReader,
    ComponentKind,
    ComponentSpec,
    DeclarationReport,
    InstanceComponent,
    InstanceReport,
    InstanceSpec,
    ModellingRule,
    ObjectTypeSpec,
    TypeCoverage,
    TypeReport,
    TypeUsage,
    VariableAccess,
    VariableRank,
    VariableTypeReport,
    VariableTypeSpec,
    resolve_data_type,
    type_definition_name,
)
from opcua_toolkit.settings import (
    ClientSettings,
    LimitsOverrides,
    RetryPolicy,
    SecuritySettings,
    SubscriptionDefaults,
    format_milliseconds,
    format_number,
    format_seconds,
)
from opcua_toolkit.spool import (
    JsonlSpool,
    SpoolEntry,
    SpoolFullPolicy,
    SpoolSettings,
    SpoolStats,
    SpoolWrite,
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
    "AddressSpaceAuthor",
    "AddressSpaceError",
    "AddressSpaceReader",
    "ApplicationConfig",
    "ArgumentSpec",
    "BrowsePathError",
    "BrowseStats",
    "BrowseTree",
    "BufferDecision",
    "BufferSettings",
    "BufferStats",
    "BulkReadStats",
    "BulkReader",
    "BulkWriter",
    "CallResult",
    "ClientSettings",
    "ComponentKind",
    "ComponentSpec",
    "ConfigFinding",
    "ConfigLayer",
    "ConfigProvenance",
    "ConfigReview",
    "ConfigurationError",
    "ConnectionSetupError",
    "ConnectionStatus",
    "ConnectionSupervisor",
    "DataChangeRecord",
    "DeclarationReport",
    "DemoServer",
    "DemoServerConfig",
    "DiscoveredServer",
    "DropPolicy",
    "EndpointDiscovery",
    "EndpointInfo",
    "EndpointNotFoundError",
    "FindingSeverity",
    "InstanceComponent",
    "InstanceReport",
    "InstanceSpec",
    "JsonFormatter",
    "JsonlSpool",
    "LimitsOverrides",
    "MethodCallError",
    "MethodCatalog",
    "MethodDescriptor",
    "MethodInvoker",
    "MethodNotFoundError",
    "ModellingRule",
    "MonitoredItemHandle",
    "NamespaceExplorer",
    "NodeDescriptor",
    "NodeInspector",
    "NodeNotAccessibleError",
    "NodeRef",
    "NotConnectedError",
    "NotificationBuffer",
    "NotificationCallback",
    "ObjectTypeSpec",
    "OpcuaToolkitError",
    "OperationLimitError",
    "OperationLimits",
    "PartialFailureError",
    "ReadResult",
    "ResolvedConfig",
    "ResolvedValue",
    "RetryPolicy",
    "SecurityConfigurationError",
    "SecuritySettings",
    "ServerHealth",
    "ServerProfile",
    "ServerUnhealthyError",
    "ServiceLevelBand",
    "SpoolEntry",
    "SpoolError",
    "SpoolFullPolicy",
    "SpoolSettings",
    "SpoolStats",
    "SpoolWrite",
    "SubscriptionClosedError",
    "SubscriptionDefaults",
    "SubscriptionManager",
    "SubscriptionOptions",
    "SubscriptionRevision",
    "SubscriptionStats",
    "SupervisionSettings",
    "TagSettings",
    "TypeCoverage",
    "TypeReport",
    "TypeUsage",
    "ValueOrigin",
    "VariableAccess",
    "VariableRank",
    "VariableTypeReport",
    "VariableTypeSpec",
    "WriteItem",
    "WriteOutcome",
    "WriteReport",
    "__version__",
    "as_node_class",
    "as_qualified_name",
    "buffering_handler",
    "chunked",
    "classify_service_level",
    "coerce_to_variant_type",
    "configure_logging",
    "data_type_name",
    "describe_status",
    "discovery_settings",
    "format_milliseconds",
    "format_node_id",
    "format_number",
    "format_seconds",
    "load_config",
    "log_with",
    "make_variant",
    "offer_many",
    "resolve_data_type",
    "type_definition_name",
    "unwrap",
]
