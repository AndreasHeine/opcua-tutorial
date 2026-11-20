"""Address-space models (Chapter 16).

A server can answer two different questions about a node: *what is this value* and
*what kind of thing is this node*. The second is not optional. OPC 10000-3, 4.6.1:

    OPC UA Servers shall provide type definitions for Objects and Variables. The
    HasTypeDefinition Reference shall be used to link an instance with its type
    definition represented by a TypeDefinitionNode. Type definitions are required;
    however, OPC 10000-5 defines a BaseObjectType, a PropertyType, and a
    BaseDataVariableType so a Server can use such a base type if no more specialised
    type information is available. Objects and Variables inherit the Attributes
    specified by their TypeDefinitionNode (see 6.4 for details).

"Everything is a ``BaseDataVariableType``" is therefore legal - and it is also the
address space a client cannot read, because nothing in it says that this object is a
pump, that a pump always has a temperature, or which of its parts a client may write.
OPC 10000-5, 6.2 says it as plainly as it can about the base object type:

    Servers should avoid using this ObjectType and use a more specific type, if
    possible.

The models in this module are the author's side of that. A type is declared once, with
its components and properties, and every instance is then checked against what was
declared.

Three specification facts these models are built around:

* **InstanceDeclarations** (OPC 10000-3, 3.1.10) are the nodes a complex type uses to
  describe its own structure. They live under the type, not under the instance, and
  OPC 10000-3, 4.6.2 spells out what that buys: a type "could define a default value
  for ``SP``, that is, each instance of ``AI_BLK_TYPE`` would have a Variable ``SP``
  initially set to this value".
* **ModellingRules** (3.1.12, 6.4.4.4) say what happens to a declaration when the type
  is instantiated. ``Mandatory`` (6.4.4.4.1) means every instance has it, ``Optional``
  (6.4.4.4.2) means an instance may have it, and OPC 10000-3, 6.4.2 is explicit about
  the third case: "Nodes referenced by the TypeDefinitionNode without a ModellingRule
  do not appear in the instance."
* **Attributes are inherited** by instances (4.6.1, 6.4.2), which is why access rights
  and data types are declared once here instead of on every instance.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any

from asyncua import ua
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    field_validator,
    model_validator,
)

from opcua_toolkit.errors import ConfigurationError, format_node_id
from opcua_toolkit.nodeids import NodeIdLike, looks_like_node_id_text, to_nodeid
from opcua_toolkit.settings import MillisecondsOrNone
from opcua_toolkit.values import data_type_name

__all__ = [
    "BASE_TYPE_DEFINITION_IDS",
    "DEFAULT_DATA_TYPE_ID",
    "ComponentKind",
    "ComponentSpec",
    "DataTypeName",
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

#: The local part of a BrowseName: letters, digits and underscores, not starting with a
#: digit. That is the shape OPC 10000-3, 5.8.2 asks for ("the BrowseName for all
#: DataTypes should be Strings that start with a letter and contain only letters,
#: digits or the underscore"), and it is the shape every BrowsePath in this tutorial
#: depends on. Dots and colons are refused for a second, practical reason: asyncua names
#: the components of a string-NodeId instance ``<instance>.<browse name>``, so a BrowseName
#: containing a dot makes the NodeIds a deployment has to remember ambiguous.
_BROWSE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: The data types a declared component may name, as (spec spelling, namespace-0 id).
#: The spelling is the specification's, and it matters twice over: ``Float`` is the
#: 32-bit type (``Single`` in most languages) and ``Double`` is the 64-bit one, which is
#: what a Python ``float`` is.
_DATA_TYPES: tuple[tuple[str, int], ...] = (
    ("Boolean", ua.ObjectIds.Boolean),
    ("SByte", ua.ObjectIds.SByte),
    ("Byte", ua.ObjectIds.Byte),
    ("Int16", ua.ObjectIds.Int16),
    ("UInt16", ua.ObjectIds.UInt16),
    ("Int32", ua.ObjectIds.Int32),
    ("UInt32", ua.ObjectIds.UInt32),
    ("Int64", ua.ObjectIds.Int64),
    ("UInt64", ua.ObjectIds.UInt64),
    ("Float", ua.ObjectIds.Float),
    ("Double", ua.ObjectIds.Double),
    ("String", ua.ObjectIds.String),
    ("DateTime", ua.ObjectIds.DateTime),
    ("Duration", ua.ObjectIds.Duration),
    ("Guid", ua.ObjectIds.Guid),
    ("ByteString", ua.ObjectIds.ByteString),
    ("LocalizedText", ua.ObjectIds.LocalizedText),
    ("NodeId", ua.ObjectIds.NodeId),
    ("StatusCode", ua.ObjectIds.StatusCode),
    ("BaseDataType", ua.ObjectIds.BaseDataType),
)

#: Lower-case spelling -> namespace-0 id, so a configuration file may write ``double``.
_DATA_TYPE_IDS: dict[str, int] = {name.lower(): identifier for name, identifier in _DATA_TYPES}

#: The VariableType a DataVariable's declaration is typed with, and the one a Property's
#: is typed with (OPC 10000-5, 7.4: "This VariableType is the base VariableType for
#: VariableTypes of DataVariables, and all other VariableTypes of DataVariables shall
#: either directly or indirectly inherit from it").
BASE_VARIABLE_TYPE_ID = ua.NodeId(ua.ObjectIds.BaseVariableType)
BASE_DATA_VARIABLE_TYPE_ID = ua.NodeId(ua.ObjectIds.BaseDataVariableType)
PROPERTY_TYPE_ID = ua.NodeId(ua.ObjectIds.PropertyType)
BASE_OBJECT_TYPE_ID = ua.NodeId(ua.ObjectIds.BaseObjectType)

#: The TypeDefinitionNodes that tell a client nothing beyond "this is a variable" or "this is
#: an object". OPC 10000-3, 4.6.1 lets a server fall back on them "if no more specialised
#: type information is available", and OPC 10000-5, 6.2 says a Server "should avoid using
#: this ObjectType and use a more specific type, if possible" - so they are the answer to
#: "how much of this address space is typed", and never the interesting part of it.
BASE_TYPE_DEFINITION_IDS: frozenset[ua.NodeId] = frozenset(
    {
        BASE_OBJECT_TYPE_ID,
        BASE_VARIABLE_TYPE_ID,
        BASE_DATA_VARIABLE_TYPE_ID,
        PROPERTY_TYPE_ID,
    }
)


@lru_cache(maxsize=1)
def _namespace0_names() -> dict[int, str]:
    """Every namespace-0 NodeId asyncua knows a name for, read out of asyncua's own table.

    ``ua.ObjectIds`` is a flat class holding 14772 integer attributes - one per standard node -
    and, measured on the ``.venv``'s asyncua 2.1.0, its values are unique: 14772 attributes,
    14772 distinct ids, none shared. So the table can be read backwards and used to name a
    TypeDefinition this tutorial has never seen. That is what makes a coverage report readable:
    the diagnostic Variables every OPC UA server carries are typed ``ServerStatusType``,
    ``SessionDiagnosticsArrayType`` and ``PubSubDiagnosticsCounterType``, and a table printing
    ``ns=0;i=2138`` for them is a table nobody can read with the schema closed.

    The names starting with an underscore are skipped, and that is not tidiness. Python adds a
    ``__firstlineno__`` attribute to every class body, and on this Python its value is 4 - the
    same integer as ``Int16`` - so a table built straight from ``vars(ua.ObjectIds)`` maps the
    ``Int16`` data type onto a line number.
    """
    table: dict[int, str] = {}
    for attribute, value in vars(ua.ObjectIds).items():
        if attribute.startswith("_") or not isinstance(value, int):
            continue
        table[value] = attribute
    return table


def type_definition_name(node_id: ua.NodeId | None) -> str:
    """Readable name of a TypeDefinition, or its NodeId text when it is not a standard node.

    Every namespace-0 node asyncua's own table names is named here - ``BaseDataVariableType``,
    ``PropertyType``, ``ServerStatusType``, and the standard data types. A type from another
    namespace reports its own NodeId, because the honest answer about a companion
    specification's type this tutorial has not browsed is "I know its identifier, not its name".
    """
    if node_id is None:
        return "nothing (no HasTypeDefinition)"
    if node_id.NamespaceIndex == 0 and isinstance(node_id.Identifier, int):
        named = _namespace0_names().get(node_id.Identifier)
        if named is not None:
            return named
        return data_type_name(node_id)
    return format_node_id(node_id)


def sampling_interval_text(interval: float | None) -> str:
    """``MinimumSamplingInterval`` in the words OPC 10000-3, 5.6.2 attaches to the number.

    A bare ``0 ms`` in a report reads like "no sampling", and that is the opposite of what the
    specification says a 0 means: "A MinimumSamplingInterval of 0 indicates that the Server is
    to monitor the item continuously. A MinimumSamplingInterval of -1 means indeterminate."
    asyncua's server leaves every Variable it creates at 0 unless an author sets the attribute,
    so the honest report for the parts of a model nobody constrained is "continuous", not a
    number a reader has to look up. ``None`` is kept separate: it means this toolkit could not
    read the attribute at all, which is a different fact from the server saying 0.
    """
    if interval is None:
        return "not readable"
    if interval == 0.0:
        return "continuous (0 ms)"
    if interval < 0.0:
        return "indeterminate"
    return f"{interval:g} ms"


def _node_id_text(node_id: ua.NodeId | None) -> str | None:
    """A NodeId as text a report prints, and a config file could write back.

    ``str(ua.NodeId(...))`` is the dataclass repr: ``NodeId(Identifier='PumpType',
    NamespaceIndex=2, ...)``. That is not NodeId text, so a JSON report built with it cannot
    be read back by :func:`~opcua_toolkit.nodeids.to_nodeid`, which makes it useless as the
    audit trail a deployment keeps. ``ns=2;s=PumpType`` is what the specification writes and
    what every other report in this toolkit prints.
    """
    return None if node_id is None else format_node_id(node_id)


class ModellingRule(StrEnum):
    """What a type promises about one of its declarations when it is instantiated.

    The two rules OPC 10000-3, 6.4.4.4.1 and 6.4.4.4.2 define. The placeholder rules
    (6.4.4.4.4 and 6.4.4.4.5) describe structure a type expects an *instance* to add on
    its own, which is a different contract to write and a different one to check, so
    this toolkit does not emit them silently.
    """

    MANDATORY = "mandatory"
    OPTIONAL = "optional"
    MANDATORY_PLACEHOLDER = "mandatory_placeholder"
    OPTIONAL_PLACEHOLDER = "optional_placeholder"

    @property
    def node_id(self) -> ua.NodeId:
        """The ModellingRule node a declaration points at with ``HasModellingRule``."""
        identifiers = {
            ModellingRule.MANDATORY: ua.ObjectIds.ModellingRule_Mandatory,
            ModellingRule.OPTIONAL: ua.ObjectIds.ModellingRule_Optional,
            ModellingRule.MANDATORY_PLACEHOLDER: ua.ObjectIds.ModellingRule_MandatoryPlaceholder,
            ModellingRule.OPTIONAL_PLACEHOLDER: ua.ObjectIds.ModellingRule_OptionalPlaceholder,
        }
        return ua.NodeId(identifiers[self], 0)

    @property
    def spec_clause(self) -> str:
        """The subclause that defines this rule, for a report a human can go and read."""
        clauses = {
            ModellingRule.MANDATORY: "6.4.4.4.1 Mandatory",
            ModellingRule.OPTIONAL: "6.4.4.4.2 Optional",
            ModellingRule.MANDATORY_PLACEHOLDER: "6.4.4.4.5 MandatoryPlaceholder",
            ModellingRule.OPTIONAL_PLACEHOLDER: "6.4.4.4.4 OptionalPlaceholder",
        }
        return clauses[self]

    @property
    def is_placeholder(self) -> bool:
        """True for the two rules that describe structure an instance adds itself."""
        return self in (ModellingRule.MANDATORY_PLACEHOLDER, ModellingRule.OPTIONAL_PLACEHOLDER)

    @property
    def must_be_born_with_it(self) -> bool:
        """``True`` for Mandatory: an instance without this component is not conformant."""
        return self is ModellingRule.MANDATORY

    @property
    def promise(self) -> str:
        """This rule in one sentence, in the specification's own terms."""
        if self is ModellingRule.MANDATORY:
            return "each existing BrowsePath on the instance shall have a similar Node"
        if self is ModellingRule.OPTIONAL:
            return "each existing BrowsePath on the instance may have a similar Node"
        if self is ModellingRule.MANDATORY_PLACEHOLDER:
            return "each instance shall provide at least one Node of the declared TypeDefinition"
        return "instances are expected to add Nodes of the declared TypeDefinition"


#: The two ModellingRules this toolkit authors. The placeholder rules describe structure a
#: type expects its *instances* to add themselves (OPC 10000-3, 6.4.4.4.4 and 6.4.4.4.5):
#: this module reads and reports them, but refuses to author them, because an author who
#: writes one gets a declaration that is instantiated like an ordinary one, and a reviewer
#: who reads it expects something else entirely.
_AUTHORABLE_RULES: tuple[ModellingRule, ...] = (ModellingRule.MANDATORY, ModellingRule.OPTIONAL)


class ComponentKind(StrEnum):
    """How a component hangs off its object: composition, or information about it.

    ``HasComponent`` is a part of the thing - the temperature of a pump. ``HasProperty``
    is metadata *about* the thing - its tag name, its unit. OPC 10000-3, 4.5.2 gives
    Properties one rule a server and an author both need to respect: "the BrowseName of
    a Property shall be unique in the context of the Node containing the Properties".
    """

    COMPONENT = "component"
    PROPERTY = "property"

    @property
    def reference_type_id(self) -> ua.NodeId:
        """The hierarchical reference this kind of declaration is hung on."""
        if self is ComponentKind.COMPONENT:
            return ua.NodeId(ua.ObjectIds.HasComponent, 0)
        return ua.NodeId(ua.ObjectIds.HasProperty, 0)

    @property
    def base_variable_type_id(self) -> ua.NodeId:
        """The VariableType a specialised VariableType of this kind must subtype.

        OPC 10000-5 defines ``BaseDataVariableType`` (i=63) as the base of every
        VariableType of a DataVariable and ``PropertyType`` (i=68) as the one used
        whenever a Property has no more concrete type - so a specialised type has to
        subtype the right one, and "which one" is decided by the reference, not by taste.
        """
        if self is ComponentKind.COMPONENT:
            return BASE_DATA_VARIABLE_TYPE_ID
        return PROPERTY_TYPE_ID

    @property
    def default_type_definition(self) -> ua.NodeId:
        """The TypeDefinition a declaration of this kind gets when nothing was declared."""
        return self.base_variable_type_id


class VariableAccess(StrEnum):
    """The access rights a declared component carries.

    ``AccessLevel`` is a Variable attribute (OPC 10000-3, 5.6.2), so it belongs on the
    declaration and on every instance - which is the difference between declaring that a
    setpoint is writable and hoping a client can guess it. Chapter 6 showed that
    publishing a right is not the same as enforcing one; this is the author side of the
    same fact.
    """

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"

    @property
    def mask(self) -> int:
        """The ``AccessLevel`` / ``UserAccessLevel`` bit mask this name means.

        ``ua.AccessLevel`` members are bit *numbers*, not masks: ``CurrentRead.value`` is 0
        and ``CurrentWrite.value`` is 1, while the mask the server stores has ``1`` in the
        read bit and ``2`` in the write bit. asyncua's own ``.mask`` property does that
        shift, which is why it is used here instead of the value. Writing the values instead
        of the mask produced ``AccessLevel=0`` - a variable no client may read.
        """
        read = int(ua.AccessLevel.CurrentRead.mask)
        if self is VariableAccess.READ_ONLY:
            return read
        return read | int(ua.AccessLevel.CurrentWrite.mask)

    @property
    def description(self) -> str:
        """The words a report prints for this access right."""
        return "read only" if self is VariableAccess.READ_ONLY else "read and write"


class VariableRank(StrEnum):
    """Scalar or one-dimensional array, as a declared intent.

    ``ValueRank`` is an Int32 with a handful of legal numbers, listed in OPC 10000-3,
    5.6.5: ``Scalar`` (-1), ``OneDimension`` (1), ``Any`` (-2) among them. The three this
    toolkit writes cover almost every authored type; anything with more dimensions is a
    decision a chapter should make on purpose, so it is reported, never written here.
    """

    SCALAR = "scalar"
    ONE_DIMENSION = "one_dimension"
    ANY = "any"

    @property
    def value_rank(self) -> int:
        """The numeric ``ValueRank`` this name means."""
        if self is VariableRank.SCALAR:
            return int(ua.ValueRank.Scalar)
        if self is VariableRank.ONE_DIMENSION:
            return int(ua.ValueRank.OneDimension)
        return int(ua.ValueRank.Any)

    @property
    def description(self) -> str:
        """The words a report prints for this rank."""
        if self is VariableRank.SCALAR:
            return "one value"
        if self is VariableRank.ONE_DIMENSION:
            return "one dimensional array"
        return "scalar or array"


def resolve_data_type(value: str | int | ua.NodeId) -> ua.NodeId:
    """Turn a data type name or NodeId into a NodeId a server understands.

    Args:
        value: ``"Double"``, ``"double"``, ``11``, ``"ns=0;i=11"`` or a
            :class:`asyncua.ua.NodeId`.

    Returns:
        The data type NodeId. A name is resolved in namespace 0; any other NodeId is
        passed through, because a custom data type is a legitimate thing to declare and
        this module is not the one that decides what is legal.

    Raises:
        ConfigurationError: The name is not one of the specification's data types and the
            value is not a NodeId.
    """
    if isinstance(value, str) and not value.isdigit() and not _looks_like_node_id(value):
        key = value.strip().lower()
        identifier = _DATA_TYPE_IDS.get(key)
        if identifier is not None:
            return ua.NodeId(identifier, 0)
        names = ", ".join(name for name, _identifier in _DATA_TYPES)
        raise ConfigurationError(
            f"data type {value!r} is not one of: {names}. "
            "Pass a NodeId instead if the type is a custom one."
        )
    return to_nodeid(value)


def _looks_like_node_id(value: str) -> bool:
    """Whether a string is NodeId text rather than the name of a data type."""
    return looks_like_node_id_text(value)


def _written_data_type(value: Any) -> ua.NodeId:
    """Resolve whatever a spec wrote in a data type field to a NodeId.

    A ``ValueError`` rather than the :class:`~opcua_toolkit.errors.ConfigurationError`
    :func:`resolve_data_type` raises, for the reason ``settings.py`` gives for its duration
    fields: a ``ValueError`` inside a validator becomes one readable line in a pydantic
    error report, while anything else escapes the validation machinery. The message itself
    is kept, because naming the data types that *are* legal is the useful part.
    """
    try:
        return resolve_data_type(value)
    except ConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def _written_data_type_text(value: ua.NodeId) -> str:
    """How a data type leaves the model: ``ns=0;i=11``, the text a config file must write back.

    The other half of the ``DataTypeName`` annotation: pydantic has no way to serialise a
    ``ua.NodeId``, so ``model_dump()`` would fail on a spec that never did anything wrong. The
    obvious fix - ``str`` - produces the dataclass repr, which is not NodeId text and cannot be
    read back by :func:`~opcua_toolkit.nodeids.to_nodeid`, so the same text every other report
    in this toolkit prints is what is written here.
    """
    return format_node_id(value)


#: A data type the way a spec writes it - ``"Double"``, ``11``, ``"ns=0;i=11"``, or a
#: :class:`asyncua.ua.NodeId` - and always stored as a NodeId.
#:
#: The annotation does not simply say ``str | int | ua.NodeId``, and that is not a stylistic
#: choice. asyncua's ``NodeId`` is a dataclass whose own fields are typed with the wire
#: classes (its ``Identifier`` is ``ua.Int32 | ua.String | ua.Guid | ua.ByteString``), and
#: pydantic refuses to build a schema for ``ua.Int32``: a field annotated with ``ua.NodeId``
#: fails at class creation, far away from the spec that caused it. A plain validator tells
#: pydantic "the conversion is this function" and never asks it to understand asyncua's
#: wire types - which is the same boundary :mod:`opcua_toolkit.nodeids` keeps everywhere
#: else in the toolkit.
DataTypeName = Annotated[
    ua.NodeId,
    PlainValidator(_written_data_type),
    PlainSerializer(_written_data_type_text, return_type=str),
]

#: The data type a component carries when the author did not name one. ``Double`` because
#: that is what a Python ``float`` is, and a silent 32/64 bit surprise is the one data type
#: mistake this toolkit refuses to make twice (Chapter 8 wrote that lesson as a
#: ``BadTypeMismatch``).
DEFAULT_DATA_TYPE_ID = ua.NodeId(ua.ObjectIds.Double, 0)


def _validated_browse_name(value: str, *, what: str) -> str:
    """Reject a BrowseName a client could not build a BrowsePath from."""
    name = value.strip()
    if not name:
        raise ValueError(f"{what} must not be empty")
    if not _BROWSE_NAME.match(name):
        raise ValueError(
            f"{what} {value!r} must start with a letter or an underscore and contain only "
            "letters, digits and underscores: a BrowseName with a '.', a ':' or a leading "
            "digit cannot be used as a BrowsePath segment"
        )
    return name


class VariableTypeSpec(BaseModel):
    """A VariableType to author, when the base type is not specific enough.

    This is the shape of OPC 10000-3, 4.6.1's own example: a setpoint parameter used by
    many objects, each of them pointing at one common ``SetPoint`` VariableType so a
    client can recognise the definition without reading it from the server.

    Attributes:
        name: BrowseName of the VariableType.
        data_type: The specification's data type name, or any data type NodeId.
        rank: ``ValueRank``: scalar, one dimensional array, or "any".
        supertype: The VariableType to subtype. ``None`` picks the base type that matches
            the kind of component this type will be used for.
        description: Human text written into ``Description``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    data_type: DataTypeName = DEFAULT_DATA_TYPE_ID
    rank: VariableRank = VariableRank.SCALAR
    supertype: NodeIdLike | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _browse_name_is_legal(cls, value: str) -> str:
        """Reject a BrowseName a server would reject."""
        return _validated_browse_name(value, what="variable type name")

    @property
    def data_type_text(self) -> str:
        """The data type as a word, for reports."""
        return data_type_name(self.data_type)


class ComponentSpec(BaseModel):
    """One InstanceDeclaration: the part a type promises, and what it carries.

    Attributes:
        name: BrowseName of the declaration, and of the component every instance gets.
            OPC 10000-3, 4.6.4: "Instances based on InstanceDeclarations shall always
            keep the same BrowseName as the InstanceDeclaration they are derived from."
        data_type: The specification's data type name, or any data type NodeId.
        rank: ``ValueRank`` of the component.
        initial: The value the declaration carries. OPC 10000-3, 6.4.2: instances "inherit
            the initial values for the Attributes that they have in common with the
            TypeDefinitionNode", so **every instance starts with this value** - which is
            exactly how two pumps end up born reporting the same temperature.
        access: The ``AccessLevel`` and ``UserAccessLevel`` the component carries.
        rule: The ModellingRule the declaration carries. A declaration with no rule is not
            instantiated at all, so this is not a field you may leave unset by accident.
        variable_type: A VariableType to author for this component, which the declaration
            is then typed with. ``None`` keeps the base type for this kind of component.
        type_definition: An existing VariableType (an authored one, or a companion
            specification's) to type the declaration with instead of authoring one.
        min_sampling_interval: ``MinimumSamplingInterval`` in milliseconds, or ``None``
            when the type does not claim a fastest sampling rate.
        description: Human text written into ``Description``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    data_type: DataTypeName = DEFAULT_DATA_TYPE_ID
    rank: VariableRank = VariableRank.SCALAR
    initial: Any = None
    access: VariableAccess = VariableAccess.READ_ONLY
    rule: ModellingRule = ModellingRule.MANDATORY
    variable_type: VariableTypeSpec | None = None
    type_definition: NodeIdLike | None = None
    min_sampling_interval: MillisecondsOrNone = Field(default=None, gt=0.0)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _browse_name_is_legal(cls, value: str) -> str:
        """Reject a component name a BrowsePath could not express."""
        return _validated_browse_name(value, what="component name")

    @field_validator("type_definition")
    @classmethod
    def _type_definition_is_a_node_id(cls, value: NodeIdLike | None) -> ua.NodeId | None:
        """Normalise an existing VariableType to a NodeId."""
        return None if value is None else to_nodeid(value)

    @model_validator(mode="after")
    def _only_one_way_to_type_it(self) -> ComponentSpec:
        """Refuse a component that says both "author a type" and "use this type"."""
        if self.rule not in _AUTHORABLE_RULES:
            raise ValueError(
                f"component {self.name} asks for the {self.rule.spec_clause} ModellingRule, "
                "which this toolkit reads but does not author: a placeholder rule describes "
                "Nodes an instance adds itself, and an author writing one does not get that"
            )
        if self.variable_type is not None and self.type_definition is not None:
            raise ValueError(
                f"component {self.name} declares both variable_type "
                f"{self.variable_type.name!r} and type_definition; pick one"
            )
        if self.variable_type is not None and self.variable_type.name == self.name:
            raise ValueError(
                f"component {self.name} would author a VariableType with its own BrowseName: "
                "a client browsing the type cannot tell the component from its type"
            )
        return self

    @property
    def data_type_text(self) -> str:
        """The data type as a word, for reports."""
        return data_type_name(self.data_type)


class ObjectTypeSpec(BaseModel):
    """One object type: its declarations, and whether it may ever be instantiated.

    Attributes:
        name: BrowseName of the ObjectType.
        components: Declarations hung on ``HasComponent``.
        properties: Declarations hung on ``HasProperty``.
        supertype: The ObjectType to subtype - a BrowseName authored earlier in the same
            model, or any ObjectType NodeId. ``None`` means ``BaseObjectType``.
        abstract: ``IsAbstract``. OPC 10000-3, 5.5.2: for ``TRUE`` "no Objects of this
            type shall exist, only Objects of its subtypes" - so an abstract type is a
            contract and a concrete type is a thing.
        description: Human text written into ``Description``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    components: tuple[ComponentSpec, ...] = ()
    properties: tuple[ComponentSpec, ...] = ()
    supertype: str | NodeIdLike | None = None
    abstract: bool = False
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _browse_name_is_legal(cls, value: str) -> str:
        """Reject an ObjectType BrowseName a server would reject."""
        return _validated_browse_name(value, what="object type name")

    @model_validator(mode="after")
    def _declares_something_useful(self) -> ObjectTypeSpec:
        """Refuse a type that promises nothing, or promises it twice.

        An ObjectType with no InstanceDeclarations browses fine and tells a client
        nothing: that is the same unreadable address space this chapter exists to fix,
        written by someone who believed they had fixed it. Duplicate names are refused
        for the reason OPC 10000-3, 4.6.4 gives: "A TypeDefinitionNode or an
        InstanceDeclaration shall never reference two Nodes having the same BrowseName
        using forward hierarchical References."
        """
        if not self.components and not self.properties:
            raise ValueError(
                f"object type {self.name} declares no components and no properties; "
                "a type with no InstanceDeclarations tells a client nothing"
            )
        names = [declaration.name for declaration, _kind in self.declarations]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(
                f"object type {self.name} declares {', '.join(duplicates)} more than once; "
                "the BrowseNames of the declarations of one type must be distinct"
            )
        # OPC 10000-3, 5.2.4: "The string-part of the BrowseName is case sensitive. That
        # is, Clients shall consider them case sensitive. Servers are allowed to handle
        # BrowseNames passed in Service requests as case insensitive." A pair of names
        # that differ only by case is legal on paper and a mystery in a running system.
        lowered: dict[str, list[str]] = {}
        for name in names:
            lowered.setdefault(name.lower(), []).append(name)
        collisions = sorted(group for group in lowered.values() if len(group) > 1)
        if collisions:
            joined = "; ".join(" or ".join(group) for group in collisions)
            raise ValueError(
                f"object type {self.name} declares {joined}, which differ only by case: "
                "a client treats BrowseNames as case sensitive and a server may not"
            )
        return self

    @property
    def declarations(self) -> tuple[tuple[ComponentSpec, ComponentKind], ...]:
        """Every declaration, with the kind of reference it hangs on."""
        components = [(declaration, ComponentKind.COMPONENT) for declaration in self.components]
        properties = [(declaration, ComponentKind.PROPERTY) for declaration in self.properties]
        return tuple(components + properties)

    @property
    def declared_names(self) -> tuple[str, ...]:
        """BrowseNames this type promises, in declaration order."""
        return tuple(declaration.name for declaration, _kind in self.declarations)

    @property
    def mandatory_names(self) -> tuple[str, ...]:
        """BrowseNames every instance of this type must have."""
        return tuple(
            declaration.name
            for declaration, _kind in self.declarations
            if declaration.rule is ModellingRule.MANDATORY
        )

    @property
    def optional_names(self) -> tuple[str, ...]:
        """BrowseNames an instance may have."""
        return tuple(
            declaration.name
            for declaration, _kind in self.declarations
            if declaration.rule is ModellingRule.OPTIONAL
        )

    @property
    def supertype_name(self) -> str | None:
        """The supertype as a name, when it was written as one."""
        if isinstance(self.supertype, str) and not _looks_like_node_id(self.supertype):
            return self.supertype
        return None

    def declaration_for(self, browse_name: str) -> ComponentSpec | None:
        """One declaration by BrowseName, or ``None`` when this type does not have one."""
        for declaration, _kind in self.declarations:
            if declaration.name == browse_name:
                return declaration
        return None


class InstanceSpec(BaseModel):
    """One instance of an authored type, and the values that make it this instance.

    Attributes:
        name: BrowseName of the instance.
        type_name: BrowseName of the type this instance is an instance of, as authored by
            :class:`~opcua_toolkit.server.authoring.AddressSpaceAuthor`. Keeping it here is
            what lets a whole deployment model be declared as one flat, readable tuple.
        node_id: NodeId to create it with. ``None`` lets the author derive a string NodeId
            from the BrowseName. A string like ``"Pump_01"`` makes the children
            predictable, because asyncua names them ``Pump_01.Temperature``.
        values: Component BrowseName to value, applied after instantiation.
        include_optional: ``False`` instantiates only the mandatory declarations, which is
            the difference between "a client may look for it" and "a client must find it".
        description: Human text written into ``Description``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type_name: str
    node_id: NodeIdLike | None = None
    values: Mapping[str, Any] = Field(default_factory=dict)
    include_optional: bool = True
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _browse_name_is_legal(cls, value: str) -> str:
        """Reject an instance BrowseName a BrowsePath could not express."""
        return _validated_browse_name(value, what="instance name")

    @field_validator("values", mode="before")
    @classmethod
    def _values_is_a_mapping(cls, value: Any) -> Any:
        """Accept a dict, and refuse anything else pretending to be one."""
        if value is None:
            return {}
        if isinstance(value, Mapping):
            return dict(value)
        raise ValueError("values must be a mapping of component BrowseName to value")


@dataclass(frozen=True, slots=True)
class VariableTypeReport:
    """A VariableType a server now has, as the server has it.

    Attributes:
        node_id: The VariableType's NodeId.
        browse_name: Its BrowseName.
        data_type: The ``DataType`` it carries, as a name where one is known.
        value_rank: The numeric ``ValueRank`` it carries.
        supertype_id: The VariableType it is a subtype of.
        reused: True when an existing VariableType with the same BrowseName was used
            instead of adding one, which is what makes a gateway restart safe.
    """

    node_id: ua.NodeId
    browse_name: str
    data_type: str
    value_rank: int
    supertype_id: ua.NodeId
    reused: bool = False

    def summary_line(self) -> str:
        """One report line: what this type says about every variable typed with it."""
        parent = type_definition_name(self.supertype_id)
        return (
            f"{self.browse_name} ({format_node_id(self.node_id)}) subtype of {parent}: "
            f"DataType={self.data_type} ValueRank={self.value_rank}"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this report."""
        return {
            "node_id": _node_id_text(self.node_id),
            "browse_name": self.browse_name,
            "data_type": self.data_type,
            "value_rank": self.value_rank,
            "supertype_id": _node_id_text(self.supertype_id),
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class DeclarationReport:
    """One InstanceDeclaration, with the rule that decides how instances get it.

    Attributes:
        browse_name: The declaration's BrowseName.
        kind: Component or property: ``HasComponent`` or ``HasProperty``.
        node_id: The declaration's NodeId.
        rule: The ModellingRule the server reports for it, or ``None`` when it has none -
            the state in which OPC 10000-3, 6.4.2 says it never appears in an instance.
        declared_rule: The rule the author asked for, kept next to the answer so a
            difference between the two is visible instead of theoretical.
        type_definition_id: What the declaration is typed with.
        type_definition_name: That NodeId as a readable name.
        specialised_type: True when the declaration is typed with something other than the
            base VariableType for its kind, i.e. the author actually wrote a type.
        data_type: The declaration's ``DataType`` as a name where one is known.
        value_rank: The declaration's numeric ``ValueRank``.
        access_mask: The declaration's ``AccessLevel``.
        min_sampling_interval: Its ``MinimumSamplingInterval`` in milliseconds, or ``None``.
        initial_value: The value the declaration carries, which every instance inherits.
        inherited_from: The type this declaration was read from, when it came from a
            supertype rather than from the type being reported.
    """

    browse_name: str
    kind: ComponentKind
    node_id: ua.NodeId
    rule: ModellingRule | None
    type_definition_id: ua.NodeId | None
    data_type: str
    value_rank: int
    access_mask: int
    min_sampling_interval: float | None
    initial_value: Any = None
    declared_rule: ModellingRule | None = None
    specialised_type: bool = False
    inherited_from: str | None = None

    @property
    def type_definition_name(self) -> str:
        """This declaration's TypeDefinition as a readable name."""
        return type_definition_name(self.type_definition_id)

    @property
    def will_be_instantiated(self) -> bool:
        """False when the server reports no ModellingRule, so instantiation skips it."""
        return self.rule is not None

    @property
    def is_mandatory(self) -> bool:
        """True when the server says every instance of this type has this component."""
        return self.rule is not None and self.rule.must_be_born_with_it

    @property
    def rule_matches_declaration(self) -> bool:
        """False when the author asked for one rule and the server says another."""
        return self.declared_rule is None or self.declared_rule is self.rule

    @property
    def is_readable(self) -> bool:
        """Whether this declaration's own ``AccessLevel`` offers ``CurrentRead``.

        The mask, not the enum value: ``ua.AccessLevel.CurrentRead.value`` is the *bit number*
        0, and the stored attribute has 1 set in that bit. Testing against the value made
        every variable look unreadable.
        """
        return bool(self.access_mask & int(ua.AccessLevel.CurrentRead.mask))

    @property
    def is_writable(self) -> bool:
        """Whether this declaration's own ``AccessLevel`` offers ``CurrentWrite``."""
        return bool(self.access_mask & int(ua.AccessLevel.CurrentWrite.mask))

    def summary_line(self) -> str:
        """One report line: what the type promises, and what the server really says."""
        rule_text = "no modelling rule" if self.rule is None else self.rule.value
        typed = self.type_definition_name
        if not self.specialised_type:
            typed = f"{typed} (base type)"
        inherited = "" if self.inherited_from is None else f" inherited from {self.inherited_from}"
        return (
            f"{self.browse_name}: {self.kind.value} rule={rule_text} data={self.data_type} "
            f"type={typed} access={self.access_mask:#04x} "
            f"sampling={sampling_interval_text(self.min_sampling_interval)}{inherited}"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this declaration."""
        return {
            "browse_name": self.browse_name,
            "kind": self.kind.value,
            "node_id": _node_id_text(self.node_id),
            "rule": self.rule.value if self.rule is not None else None,
            "declared_rule": self.declared_rule.value if self.declared_rule is not None else None,
            "type_definition_id": _node_id_text(self.type_definition_id),
            "type_definition_name": self.type_definition_name,
            "specialised_type": self.specialised_type,
            "data_type": self.data_type,
            "value_rank": self.value_rank,
            "access_mask": self.access_mask,
            "min_sampling_interval": self.min_sampling_interval,
            "initial_value": self.initial_value,
            "inherited_from": self.inherited_from,
        }


@dataclass(frozen=True, slots=True)
class TypeReport:
    """What one object type now is, in the server that has it.

    Attributes:
        node_id: The ObjectType's NodeId.
        browse_name: Its BrowseName.
        abstract: The ``IsAbstract`` the server reports.
        declared_abstract: What was asked for, kept beside the answer.
        supertype_id: The ObjectType it was created as a subtype of.
        declarations: The InstanceDeclarations this type has, including the ones it
            inherits - the "fully-inherited InstanceDeclarationHierarchy" of OPC 10000-3,
            6.3.3.2, which is what an instance is actually built from.
        own_declaration_count: Declarations hanging on this type itself, not inherited.
        variable_types: The VariableTypes the declarations of this type are typed with, base
            types included. :attr:`specialised_variable_types` is the same list without them.
        notes: Corrections the author had to make on the way in, in the words used when
            logging them.
        reused: True when an existing ObjectType with this BrowseName was used.
    """

    node_id: ua.NodeId
    browse_name: str
    abstract: bool
    supertype_id: ua.NodeId
    declarations: tuple[DeclarationReport, ...]
    declared_abstract: bool = False
    variable_types: tuple[VariableTypeReport, ...] = ()
    notes: tuple[str, ...] = ()
    reused: bool = False
    own_declaration_count: int = 0

    @property
    def declared_names(self) -> tuple[str, ...]:
        """BrowseNames this type promises an instance, inherited ones included."""
        return tuple(declaration.browse_name for declaration in self.declarations)

    @property
    def mandatory_names(self) -> tuple[str, ...]:
        """BrowseNames the server says every instance of this type has."""
        return tuple(
            declaration.browse_name for declaration in self.declarations if declaration.is_mandatory
        )

    @property
    def optional_names(self) -> tuple[str, ...]:
        """BrowseNames an instance may have."""
        return tuple(
            declaration.browse_name
            for declaration in self.declarations
            if declaration.rule is ModellingRule.OPTIONAL
        )

    @property
    def instantiable(self) -> bool:
        """False when the server refuses instances of this type: ``IsAbstract``.

        OPC 10000-3, 5.5.2 for an abstract ObjectType: "no Objects of this type shall
        exist, only Objects of its subtypes".
        """
        return not self.abstract

    @property
    def never_instantiated(self) -> tuple[DeclarationReport, ...]:
        """Declarations no instantiation will ever create, because they have no rule."""
        return tuple(
            declaration for declaration in self.declarations if not declaration.will_be_instantiated
        )

    @property
    def unspecialised_declarations(self) -> tuple[DeclarationReport, ...]:
        """Declarations typed with a base VariableType rather than an authored one."""
        return tuple(
            declaration for declaration in self.declarations if not declaration.specialised_type
        )

    @property
    def declares_nothing(self) -> bool:
        """True when this type carries no InstanceDeclarations at all.

        A type with no declarations is legal - ``BaseObjectType`` is one - and it is the
        exact thing OPC 10000-5, 6.2 tells a Server not to lean on: "Servers should avoid
        using this ObjectType and use a more specific type, if possible."
        """
        return not self.declarations

    def declaration(self, browse_name: str) -> DeclarationReport | None:
        """One declaration by BrowseName, or ``None`` when this type has no such promise."""
        for declaration in self.declarations:
            if declaration.browse_name == browse_name:
                return declaration
        return None

    @property
    def specialised_variable_types(self) -> tuple[VariableTypeReport, ...]:
        """The VariableTypes its declarations are typed with, base types left out.

        A report that counts ``BaseDataVariableType`` as a type the author wrote is the
        number that makes a review agree with the address space it is looking at, and this is
        the number that does not.
        """
        return tuple(
            variable_type
            for variable_type in self.variable_types
            if variable_type.node_id not in BASE_TYPE_DEFINITION_IDS
        )

    def summary_line(self) -> str:
        """One report line for the type as a whole."""
        if self.declares_nothing:
            return (
                f"type {self.browse_name} ({format_node_id(self.node_id)}) declares nothing: "
                "a client learns no structure from it"
            )
        state = "abstract, no instances" if self.abstract else "instantiable"
        inherited = len(self.declarations) - self.own_declaration_count
        inherited_text = "" if inherited == 0 else f" ({inherited} inherited)"
        skipped = len(self.never_instantiated)
        skipped_text = "" if skipped == 0 else f", {skipped} without a modelling rule"
        total = len(self.declarations)
        mandatory = len(self.mandatory_names)
        types = len(self.specialised_variable_types)
        type_text = "VariableType of their own" if types == 1 else "VariableTypes of their own"
        return (
            f"type {self.browse_name} ({format_node_id(self.node_id)}) "
            f"{state}: {total} declarations{inherited_text}, "
            f"{mandatory} mandatory, {types} {type_text}{skipped_text}"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this type."""
        return {
            "node_id": _node_id_text(self.node_id),
            "browse_name": self.browse_name,
            "abstract": self.abstract,
            "declared_abstract": self.declared_abstract,
            "supertype_id": _node_id_text(self.supertype_id),
            "instantiable": self.instantiable,
            "own_declaration_count": self.own_declaration_count,
            "declarations": [declaration.as_dict() for declaration in self.declarations],
            "variable_types": [variable_type.as_dict() for variable_type in self.variable_types],
            "notes": list(self.notes),
            "reused": self.reused,
        }


@dataclass(frozen=True, slots=True)
class InstanceComponent:
    """One component an instance actually has, read back from the server.

    Attributes:
        browse_name: The component's BrowseName.
        node_id: Its NodeId, which for a string-NodeId instance is
            ``<instance>.<browse name>``.
        kind: The reference it was found through.
        type_definition_id: What the component's ``HasTypeDefinition`` says.
        type_definition_name: That NodeId as a readable name.
        data_type_id: The component's ``DataType`` NodeId, which is what tells a writer
            which wire type a value for this component needs.
        data_type: The component's ``DataType`` as a name where one is known.
        value_rank: The component's numeric ``ValueRank``.
        access_mask: The component's ``AccessLevel``.
        min_sampling_interval: Its ``MinimumSamplingInterval`` in milliseconds, or ``None``.
        value: The current value, unwrapped, or ``None`` when the read failed.
    """

    browse_name: str
    node_id: ua.NodeId
    kind: ComponentKind
    type_definition_id: ua.NodeId | None
    data_type_id: ua.NodeId | None
    data_type: str
    value_rank: int
    access_mask: int
    min_sampling_interval: float | None
    value: Any = None

    @property
    def type_definition_name(self) -> str:
        """This component's TypeDefinition as a readable name."""
        return type_definition_name(self.type_definition_id)

    @property
    def is_readable(self) -> bool:
        """Whether the component's own ``AccessLevel`` offers ``CurrentRead``."""
        return bool(self.access_mask & int(ua.AccessLevel.CurrentRead.mask))

    @property
    def is_writable(self) -> bool:
        """Whether the component's own ``AccessLevel`` offers ``CurrentWrite``."""
        return bool(self.access_mask & int(ua.AccessLevel.CurrentWrite.mask))

    def summary_line(self) -> str:
        """One report line for one component."""
        return (
            f"{self.browse_name}: {format_node_id(self.node_id)} {self.kind.value} "
            f"type={self.type_definition_name} data={self.data_type} "
            f"access={self.access_mask:#04x} "
            f"sampling={sampling_interval_text(self.min_sampling_interval)} value={self.value!r}"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this component."""
        return {
            "browse_name": self.browse_name,
            "node_id": _node_id_text(self.node_id),
            "kind": self.kind.value,
            "type_definition_id": _node_id_text(self.type_definition_id),
            "type_definition_name": self.type_definition_name,
            "data_type_id": _node_id_text(self.data_type_id),
            "data_type": self.data_type,
            "value_rank": self.value_rank,
            "access_mask": self.access_mask,
            "min_sampling_interval": self.min_sampling_interval,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class InstanceReport:
    """An instance, measured against the type the server says it is an instance of.

    Attributes:
        node_id: The instance's NodeId.
        browse_name: Its BrowseName.
        type_definition_id: The target of the instance's own ``HasTypeDefinition``.
        expected_type_id: The type it was checked against.
        type_definition_matches: The answer to "was this node really typed with the type I
            authored", which is the single claim this chapter is about.
        components: Components and properties found by browsing, in browse order.
        declared_names: BrowseNames the type declares, inherited ones included.
        mandatory_names: BrowseNames every instance must have.
        missing_names: Mandatory and absent: a client that assumes them gets
            ``BadBrowseNameNotFound`` from its own BrowsePath instead of a warning.
        optional_absent: Optional declared and absent, which OPC 10000-3, 6.4.4.4.2 allows.
        extra_names: Present but never declared.
        notes: What the author did to this instance, in the words it was reported in.
    """

    node_id: ua.NodeId
    browse_name: str
    type_definition_id: ua.NodeId | None
    expected_type_id: ua.NodeId | None
    components: tuple[InstanceComponent, ...]
    declared_names: tuple[str, ...]
    mandatory_names: tuple[str, ...]
    type_definition_matches: bool = True
    missing_names: tuple[str, ...] = ()
    optional_absent: tuple[str, ...] = ()
    extra_names: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def type_definition_name(self) -> str:
        """The instance's own ``HasTypeDefinition`` as a readable name."""
        return type_definition_name(self.type_definition_id)

    @property
    def conforms_to_type(self) -> bool:
        """True when it is typed with the expected type and every mandatory part is there.

        Extra components do not make an instance non-conformant on their own - OPC 10000-3,
        6.4.4.4.4/6.4.4.4.5 exists exactly for types that expect instances to add parts -
        but :attr:`extra_names` lists them so a reviewer can decide whether someone
        extended the instance or drifted from the type.
        """
        return self.type_definition_matches and not self.missing_names

    @property
    def component_names(self) -> tuple[str, ...]:
        """BrowseNames this instance really has."""
        return tuple(component.browse_name for component in self.components)

    def component(self, browse_name: str) -> InstanceComponent | None:
        """One component by BrowseName, or ``None`` when this instance does not have it."""
        for component in self.components:
            if component.browse_name == browse_name:
                return component
        return None

    def summary_line(self) -> str:
        """One report line for the instance as a whole."""
        state = "conformant" if self.conforms_to_type else "NOT conformant"
        return (
            f"instance {self.browse_name} ({format_node_id(self.node_id)}) "
            f"typed as {self.type_definition_name}: "
            f"{len(self.components)} of {len(self.declared_names)} declared parts, "
            f"missing {list(self.missing_names)}, extra {list(self.extra_names)}, {state}"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this instance."""
        return {
            "node_id": _node_id_text(self.node_id),
            "browse_name": self.browse_name,
            "type_definition_id": _node_id_text(self.type_definition_id),
            "type_definition_name": self.type_definition_name,
            "expected_type_id": _node_id_text(self.expected_type_id),
            "type_definition_matches": self.type_definition_matches,
            "conforms_to_type": self.conforms_to_type,
            "components": [component.as_dict() for component in self.components],
            "declared_names": list(self.declared_names),
            "mandatory_names": list(self.mandatory_names),
            "missing_names": list(self.missing_names),
            "optional_absent": list(self.optional_absent),
            "extra_names": list(self.extra_names),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class TypeUsage:
    """One TypeDefinition, and how many variables in a browsed subtree point at it.

    Attributes:
        type_id: The ``HasTypeDefinition`` target, or ``None`` for "no type at all".
        type_name: Its readable name.
        count: How many variables in the subtree report it.
        base_only: True for a base type such as ``BaseDataVariableType``: legal under
            OPC 10000-3, 4.6.1, and the type that tells a client nothing beyond "this is a
            variable".
    """

    type_id: ua.NodeId | None
    type_name: str
    count: int
    base_only: bool

    def summary_line(self) -> str:
        """One report line for one TypeDefinition's share of a subtree."""
        mark = "  <- says nothing about the node" if self.base_only else ""
        return f"{self.count:>4} variable(s) typed {self.type_name}{mark}"

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this row."""
        return {
            "type_id": _node_id_text(self.type_id),
            "type_name": self.type_name,
            "count": self.count,
            "base_only": self.base_only,
        }


@dataclass(frozen=True, slots=True)
class TypeCoverage:
    """How much of a subtree has a TypeDefinition that says something.

    This is the measurement that turns "our server is unreadable" from an opinion into a
    number a deployment can argue about.

    Attributes:
        start_name: BrowseName of the node the walk started at.
        variables: Variables found in the subtree.
        untyped_variables: Variables with no ``HasTypeDefinition`` at all.
        usage: One :class:`TypeUsage` per distinct TypeDefinition, most used first.
        browsed_nodes: How many nodes were visited, so a report can say what it looked at.
        truncated: True when ``max_nodes`` stopped the walk before it finished, in which
            case every count here is a floor and not a total.
    """

    start_name: str
    variables: int
    untyped_variables: int
    usage: tuple[TypeUsage, ...]
    browsed_nodes: int
    truncated: bool = False

    @property
    def base_only_count(self) -> int:
        """Variables whose only type is a base type."""
        return sum(row.count for row in self.usage if row.base_only)

    @property
    def base_only_share(self) -> float:
        """Fraction of the variables found that are typed only as a base type."""
        if self.variables == 0:
            return 0.0
        return self.base_only_count / self.variables

    @property
    def specialised_variables(self) -> int:
        """Variables whose TypeDefinition says more than "this is a variable"."""
        return self.variables - self.base_only_count

    def usage_for(self, type_name: str) -> TypeUsage | None:
        """One row of the usage table by type name, or ``None`` when nothing used it."""
        for row in self.usage:
            if row.type_name == type_name:
                return row
        return None

    def summary_line(self) -> str:
        """One report line with the number a deployment argues about."""
        stopped = ", truncated" if self.truncated else ""
        specialised = self.specialised_variables
        return (
            f"{self.start_name}: {self.variables} variables, "
            f"{self.untyped_variables} with no type, {specialised} with a type that says more "
            f"({self.base_only_share:.0%} typed only as a base type{stopped})"
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form of this coverage report."""
        return {
            "start_name": self.start_name,
            "variables": self.variables,
            "untyped_variables": self.untyped_variables,
            "base_only_count": self.base_only_count,
            "specialised_variables": self.specialised_variables,
            "base_only_share": self.base_only_share,
            "browsed_nodes": self.browsed_nodes,
            "truncated": self.truncated,
            "usage": [row.as_dict() for row in self.usage],
        }
