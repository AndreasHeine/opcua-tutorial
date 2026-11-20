"""Address-space building blocks - the Chapter 16 toolkit.

Two objects with opposite jobs and one shared set of models:

* :class:`AddressSpaceAuthor` writes declared types and instances into a server.
* :class:`AddressSpaceReader` reads what a server's types promise and checks an instance
  against the type its own ``HasTypeDefinition`` names.

The author returns reports produced by the reader, so a caller always sees the address
space as the server has it rather than as the request described it.
"""

from opcua_toolkit.server.authoring import AddressSpaceAuthor
from opcua_toolkit.server.model import (
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
from opcua_toolkit.server.reader import DEFAULT_MAX_NODES, AddressSpaceReader

__all__ = [
    "DEFAULT_MAX_NODES",
    "AddressSpaceAuthor",
    "AddressSpaceReader",
    "ComponentKind",
    "ComponentSpec",
    "DeclarationReport",
    "InstanceComponent",
    "InstanceReport",
    "InstanceSpec",
    "ModellingRule",
    "ObjectTypeSpec",
    "TypeCoverage",
    "TypeReport",
    "TypeUsage",
    "VariableAccess",
    "VariableRank",
    "VariableTypeReport",
    "VariableTypeSpec",
    "resolve_data_type",
    "type_definition_name",
]
