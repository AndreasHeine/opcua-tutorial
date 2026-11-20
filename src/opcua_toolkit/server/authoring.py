"""Writing an address space on a server (Chapter 16).

Authoring is three separate jobs, and doing them in the wrong order is how a server ends
up with an address space that browses fine and means nothing:

1. write the types (VariableTypes first, then the ObjectType);
2. hang the InstanceDeclarations off the type, each with its ModellingRule - OPC 10000-3,
   6.4.2: "Nodes referenced by the TypeDefinitionNode without a ModellingRule do not
   appear in the instance";
3. fix the one thing the library gets wrong on the way in. asyncua's ``add_variable`` and
   ``add_property`` hard-code the TypeDefinition of every node they create
   (``asyncua/common/manage_nodes.py``, 317-322: ``PropertyType`` for a property,
   ``BaseDataVariableType`` for everything else), so a declaration written with them is
   typed with the base type and the VariableType authored in step 1 is never used. This
   module replaces that reference and records that it did, in the report.

Everything here is idempotent, because a gateway that authors its own address space at
startup runs more than once. asyncua's own server answers an ``AddNodes`` request for a
NodeId it already has with ``BadNodeIdExists`` (``asyncua/server/address_space.py``,
294-296), so "works until the first restart" is not a design. The author looks each node
up by NodeId before asking for it and reports reuse.

The author works through an asyncua :class:`~asyncua.Node`, so the same code drives a
server this process owns or a remote server that allows node management - in asyncua's
own server that means an Admin session (``asyncua/server/address_space.py``, 281-283
refuses every ``AddNodes`` from any other role), which is Chapter 18's subject.

Every report this module returns is read back from the server rather than assembled from
the request, so what a caller sees is what the address space now holds.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from typing import Any

from asyncua import Client, Node, Server, ua
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from opcua_toolkit.errors import AddressSpaceError, ConfigurationError, format_node_id
from opcua_toolkit.nodeids import NodeIdLike, looks_like_node_id_text, to_nodeid
from opcua_toolkit.server.model import (
    BASE_OBJECT_TYPE_ID,
    ComponentKind,
    ComponentSpec,
    InstanceReport,
    InstanceSpec,
    ModellingRule,
    ObjectTypeSpec,
    TypeReport,
    VariableTypeReport,
    VariableTypeSpec,
    type_definition_name,
)
from opcua_toolkit.server.reader import AddressSpaceReader, _anchor_node
from opcua_toolkit.values import (
    coerce_to_variant_type,
    data_type_name,
    make_variant,
    variant_type_for_data_type,
)

__all__ = ["AddressSpaceAuthor"]

_logger = logging.getLogger(__name__)

#: The value a declaration carries when the author did not give it one. A declaration with
#: no value is legal and every instance of it then starts empty; a declaration with a value
#: is copied into every instance (OPC 10000-3, 6.4.2), which is how two identical pumps are
#: born reporting the same reading.
_DEFAULT_VALUES: dict[int, Any] = {
    ua.ObjectIds.Boolean: False,
    ua.ObjectIds.SByte: 0,
    ua.ObjectIds.Byte: 0,
    ua.ObjectIds.Int16: 0,
    ua.ObjectIds.UInt16: 0,
    ua.ObjectIds.Int32: 0,
    ua.ObjectIds.UInt32: 0,
    ua.ObjectIds.Int64: 0,
    ua.ObjectIds.UInt64: 0,
    ua.ObjectIds.Float: 0.0,
    ua.ObjectIds.Double: 0.0,
    ua.ObjectIds.String: "",
}


class AddressSpaceAuthor:
    """Write declared types and instances into a server, and report what it now holds.

    Args:
        handle: Where to write: an asyncua :class:`~asyncua.Client`, a
            :class:`asyncua.Server`, or any :class:`asyncua.Node` belonging to the target
            server. A client or a server starts at ``Objects``. Which of the three works
            for *authoring* is a server decision, not a toolkit one: asyncua's own server
            answers ``AddNodes`` from a session whose role is not ``Admin`` with
            ``BadUserAccessDenied`` (``asyncua/server/address_space.py``, 281-283), so a
            client author must hold that role. Reading never needs it.
        namespace_index: The namespace the authored nodes are created in. Nothing is
            inferred from ``handle``: the node a caller starts from is usually in namespace
            0, and the namespace to write in is a decision, not a fact.

    Raises:
        AddressSpaceError: ``namespace_index`` is negative, which no server accepts.
    """

    def __init__(self, handle: Node | Server | Client, *, namespace_index: int) -> None:
        if namespace_index < 0:
            raise AddressSpaceError(f"namespace_index must be 0 or greater, got {namespace_index}")
        self._index = namespace_index
        self._anchor: Node = _anchor_node(handle)
        self._reader = AddressSpaceReader(self._anchor)
        self._session = self._anchor.session
        self._types: dict[str, TypeReport] = {}
        self._variable_types: dict[str, VariableTypeReport] = {}

    @property
    def namespace_index(self) -> int:
        """The namespace this author creates nodes in."""
        return self._index

    @property
    def anchor(self) -> Node:
        """The node this author started from, exposed for callers that need it."""
        return self._anchor

    @property
    def reader(self) -> AddressSpaceReader:
        """The reader that produced every report this author returned."""
        return self._reader

    @property
    def authored_types(self) -> dict[str, TypeReport]:
        """Copy of the object types this author has written or reused, by BrowseName."""
        return dict(self._types)

    def node_id(self, identifier: NodeIdLike | None, *, fallback_for: str) -> ua.NodeId:
        """The NodeId one authored node will be created with.

        Args:
            identifier: A bare identifier (``"Pump_01"``), a full NodeId text
                (``"ns=2;s=Pump_01"``), a :class:`asyncua.ua.NodeId`, or ``None`` to let
                this author derive a string NodeId from the BrowseName.
            fallback_for: The name used when ``identifier`` is ``None``, so the generated
                identifier is readable in a log.

        Returns:
            A NodeId in this author's namespace.

        Raises:
            ConfigurationError: A NodeId was given in another namespace. Writing a node
                into a namespace the caller did not ask for is how one chapter's nodes end
                up in another chapter's folder.
        """
        if identifier is None:
            return ua.NodeId(str(fallback_for), self._index)
        if isinstance(identifier, str) and not looks_like_node_id_text(identifier):
            return ua.NodeId(identifier, self._index)
        resolved = to_nodeid(identifier)
        if resolved.NamespaceIndex != self._index:
            raise ConfigurationError(
                f"NodeId {format_node_id(resolved)} is in namespace {resolved.NamespaceIndex}, "
                f"but this author writes namespace {self._index}"
            )
        return resolved

    async def author_model(self, specs: Iterable[ObjectTypeSpec]) -> tuple[TypeReport, ...]:
        """Author a whole model, in the order it was written.

        Args:
            specs: Object types to write. A type whose ``supertype`` is a BrowseName uses
                the type authored earlier in the same call, which is what lets a model be
                declared as one flat, readable tuple.

        Returns:
            One :class:`~opcua_toolkit.server.model.TypeReport` per spec, read back from
            the server.

        Raises:
            AddressSpaceError: A supertype name was not authored earlier in the same call,
                or the server refused a node.
        """
        reports = [await self.author_object_type(spec) for spec in specs]
        return tuple(reports)

    async def author_object_type(self, spec: ObjectTypeSpec) -> TypeReport:
        """Write one object type, the VariableTypes it needs, and its InstanceDeclarations.

        Args:
            spec: The declared type.

        Returns:
            A :class:`~opcua_toolkit.server.model.TypeReport` read back from the server,
            including every correction this module had to make to get there.

        Raises:
            AddressSpaceError: The supertype is not an ObjectType, or the server refused a
                node for a reason other than "it already exists".
        """
        supertype_id, supertype_node = await self._resolve_supertype(spec.supertype)
        notes: list[str] = []

        type_node_id = self.node_id(None, fallback_for=spec.name)
        type_node, type_reused = await self._node_or_add(
            type_node_id,
            ua.QualifiedName(spec.name, self._index),
            supertype_node.add_object_type,
        )
        if type_reused:
            notes.append(
                f"reused the ObjectType {spec.name} already at {format_node_id(type_node.nodeid)} "
                "instead of adding it again, because asyncua's server answers a duplicate "
                "NodeId with BadNodeIdExists"
            )

        abstract_now = bool(
            await _read_attribute(type_node, ua.AttributeIds.IsAbstract, default=False)
        )
        if spec.abstract and not abstract_now:
            # asyncua's _create_object_type always writes IsAbstract=False
            # (asyncua/common/manage_nodes.py, 292), so an abstract type has to be made
            # abstract after it exists. OPC 10000-3, 5.5.2 is what the attribute means:
            # no Objects of this type shall exist, only Objects of its subtypes.
            await _write_attribute(
                type_node,
                ua.AttributeIds.IsAbstract,
                value=True,
                variant_type=ua.VariantType.Boolean,
            )
            notes.append(
                f"{spec.name}: asyncua created the ObjectType with IsAbstract=False "
                "and this author set it to True"
            )
        elif spec.abstract is False and abstract_now:
            notes.append(
                f"{spec.name}: the server already had this type as an abstract type "
                "and this author left it abstract"
            )

        variable_types: list[VariableTypeReport] = []
        for declaration_spec, kind in spec.declarations:
            variable_type = await self._author_variable_type(declaration_spec, kind)
            variable_types.append(variable_type)
            notes.extend(
                await self._author_declaration(type_node, declaration_spec, kind, variable_type)
            )

        if spec.description:
            await _write_description(type_node, spec.description)

        report = await self._reader.read_type(type_node.nodeid)
        declared_rules = {
            declaration.name: declaration.rule for declaration, _kind in spec.declarations
        }
        report = dataclasses.replace(
            report,
            declarations=tuple(
                dataclasses.replace(
                    declaration, declared_rule=declared_rules.get(declaration.browse_name)
                )
                for declaration in report.declarations
            ),
            declared_abstract=spec.abstract,
            variable_types=_with_reuse_flags(report.variable_types, variable_types),
            notes=tuple(notes),
            reused=type_reused,
            supertype_id=supertype_id,
        )
        self._types[spec.name] = report
        _logger.info(
            "authored %s (%s) in namespace %d: %s",
            spec.name,
            format_node_id(type_node.nodeid),
            self._index,
            report.summary_line(),
        )
        return report

    async def instantiate(
        self,
        spec: InstanceSpec,
        *,
        type_ref: str | TypeReport | NodeIdLike | None = None,
        parent: NodeIdLike | Node | None = None,
    ) -> InstanceReport:
        """Create one instance of an authored type and measure it against that type.

        Args:
            spec: The instance, with the values that make it this instance.
            type_ref: The type to instantiate: a BrowseName authored by this author, a
                :class:`~opcua_toolkit.server.model.TypeReport`, or any ObjectType NodeId.
                ``None`` uses ``spec.type_name``.
            parent: The folder or object to hang the instance on. ``None`` means
                ``Objects``. Under a ``FolderType`` parent asyncua attaches with
                ``Organizes``, under anything else with ``HasComponent``
                (``asyncua/common/manage_nodes.py``, 230-233), which is why the same
                instance browses differently depending on where it was put.

        Returns:
            An :class:`~opcua_toolkit.server.model.InstanceReport`: the components the
            instance really has, and the ones its own type promised and did not deliver.

        Raises:
            AddressSpaceError: The type is abstract, its name was never authored, or the
                server refused the instance or one of its values.
        """
        wanted: str | TypeReport | NodeIdLike = type_ref if type_ref is not None else spec.type_name
        type_report = await self._resolve_type(wanted)
        if not type_report.instantiable:
            raise AddressSpaceError(
                f"{type_report.browse_name} ({format_node_id(type_report.node_id)}) is abstract: "
                "OPC 10000-3 5.5.2 allows Objects of its subtypes only, so this instance "
                "was refused before the server was asked"
            )

        self._check_value_names(spec, type_report)

        parent_node = await self._resolve_parent(parent)
        instance_node_id = self.node_id(spec.node_id, fallback_for=spec.name)
        notes: list[str] = []
        existing = await _child_by_browse_name(parent_node, spec.name)
        if existing is not None:
            instance_node = existing
            notes.append(
                f"{format_node_id(parent_node.nodeid)} already had a child named {spec.name} at "
                f"{format_node_id(instance_node.nodeid)}; this author used that node "
                "instead of adding a second one"
            )
        else:
            try:
                instance_node = await parent_node.add_object(
                    instance_node_id,
                    ua.QualifiedName(spec.name, self._index),
                    objecttype=type_report.node_id,
                    instantiate_optional=spec.include_optional,
                )
            except (UaStatusCodeError, UaError) as exc:
                raise AddressSpaceError(
                    f"the server refused to instantiate {spec.name} from "
                    f"{type_report.browse_name}: {exc}"
                ) from exc
            optional_count = len(type_report.optional_names) if spec.include_optional else 0
            notes.append(
                f"instantiated {spec.name} from {type_report.browse_name}: "
                f"{len(type_report.mandatory_names)} mandatory, {optional_count} optional"
            )

        if spec.description:
            await _write_description(instance_node, spec.description)
        await self._apply_values(instance_node, spec)

        report = await self._reader.describe_instance(
            instance_node, expected_type=type_report.node_id
        )
        report = dataclasses.replace(report, notes=tuple(notes))
        _logger.info(
            "instance %s (%s): %s",
            spec.name,
            format_node_id(instance_node.nodeid),
            report.summary_line(),
        )
        return report

    async def instantiate_all(
        self,
        specs: Iterable[InstanceSpec],
        *,
        parent: NodeIdLike | Node | None = None,
    ) -> tuple[InstanceReport, ...]:
        """Instantiate a declared list of instances, in order.

        Args:
            specs: The instances to create, each naming its own type.
            parent: Where to put them.

        Returns:
            One :class:`~opcua_toolkit.server.model.InstanceReport` per spec.
        """
        return tuple([await self.instantiate(spec, parent=parent) for spec in specs])

    async def check_instance(
        self,
        instance: NodeIdLike | Node,
        *,
        expected_type: NodeIdLike | None = None,
    ) -> InstanceReport:
        """Check any instance against any type, without having authored either.

        Same call as :meth:`opcua_toolkit.server.reader.AddressSpaceReader.describe_instance`,
        exposed here so a review can author and audit through one object.
        """
        return await self._reader.describe_instance(instance, expected_type=expected_type)

    async def write_component_value(
        self,
        instance: NodeIdLike | Node,
        component_name: str,
        value: Any,
        *,
        enforce_access_level: bool = True,
    ) -> Any:
        """Write one component of one instance the way its own declaration says to.

        Two measured rules decide whether a write reaches an authored variable, and this is
        the call that gets both right instead of finding out from the server:

        * the value has to arrive as the data type the component declares. asyncua's
          ``write_value(900)`` picks ``Int64`` for a Python ``int`` and ``Double`` for a
          Python ``float``, and a variable authored as ``Int32`` or ``Float`` answers
          ``BadTypeMismatch``. The wire type is read off the component here.
        * ``AccessLevel`` is enforced by asyncua's server for every role but ``Admin``: a
          write of the ``Value`` attribute needs the ``CurrentWrite`` bit in both
          ``AccessLevel`` and ``UserAccessLevel``, and every other attribute of a node is
          closed to a non-Admin outright (``asyncua/server/address_space.py``, 98-113).

        Args:
            instance: The instance, by NodeId or as a node.
            component_name: BrowseName of the component to write.
            value: The value, as a Python value or a :class:`asyncua.ua.Variant`.
            enforce_access_level: Set False to let a session that really is ``Admin`` write
                a component this author declared read-only, which is the one thing the
                server's own check lets an Admin do.

        Returns:
            The value as the server now holds it.

        Raises:
            AddressSpaceError: The instance has no such component, the contract says the
                component is read-only, or the server refused the value.
        """
        components = {
            component.browse_name: component
            for component in await self._reader.components_of(instance)
        }
        component = components.get(component_name)
        if component is None:
            raise AddressSpaceError(
                f"this instance has no component {component_name!r}: it has "
                f"{', '.join(sorted(components)) or 'no components at all'}"
            )
        if enforce_access_level and not component.is_writable:
            raise AddressSpaceError(
                f"{component_name} has AccessLevel 0x{component.access_mask:02X}, which does not "
                "include CurrentWrite: this author declared it read-only, and asyncua's server "
                "answers a non-Admin write of such a variable with BadUserAccessDenied. Pass "
                "enforce_access_level=False to write it as an Admin."
            )
        node = Node(self._session, component.node_id)
        try:
            await node.write_value(
                make_variant(value, variant_type=_wire_type_for(component.data_type_id))
            )
        except UaError as exc:
            raise AddressSpaceError(
                f"the server refused the value {value!r} for {component_name} "
                f"(declared {component.data_type}): {exc}"
            ) from exc
        return await node.read_value()

    async def type_of(self, type_ref: str | TypeReport | NodeIdLike) -> TypeReport:
        """The declarations one object type carries, read from the server.

        Raises:
            AddressSpaceError: The name was not authored by this author and is not a
                NodeId, or the node is not an ObjectType.
        """
        return await self._resolve_type(type_ref)

    async def _resolve_type(self, type_ref: str | TypeReport | NodeIdLike) -> TypeReport:
        """A type reference of any shape, resolved to what the server now has."""
        if isinstance(type_ref, TypeReport):
            return type_ref
        if isinstance(type_ref, str) and not looks_like_node_id_text(type_ref):
            known = self._types.get(type_ref)
            if known is not None:
                return known
            try:
                return await self._reader.find_type(type_ref)
            except AddressSpaceError as exc:
                named = ", ".join(sorted(self._types)) or "nothing yet"
                raise AddressSpaceError(
                    f"{type_ref!r} is not a type this author has written: it has authored {named}, "
                    f"and the server does not have that BrowseName either ({exc})"
                ) from exc
        node_id = to_nodeid(type_ref)
        return await self._reader.read_type(node_id)

    async def _resolve_supertype(
        self, supertype: str | NodeIdLike | None
    ) -> tuple[ua.NodeId, Node]:
        """The type the new ObjectType will hang a ``HasSubtype`` reference off."""
        if supertype is None:
            return BASE_OBJECT_TYPE_ID, Node(self._session, BASE_OBJECT_TYPE_ID)
        if isinstance(supertype, str) and not looks_like_node_id_text(supertype):
            known = self._types.get(supertype)
            if known is not None:
                return known.node_id, Node(self._session, known.node_id)
            # The name may belong to the server rather than to this author: a client that
            # subtypes a type some other process published has to find it the only way a
            # client can - by browsing the type hierarchy for that BrowseName.
            try:
                found = await self._reader.find_type(supertype)
            except AddressSpaceError as exc:
                named = ", ".join(sorted(self._types)) or "nothing yet"
                raise AddressSpaceError(
                    f"{exc} This author has authored {named}: a model is authored in "
                    "dependency order, or the supertype is given as a NodeId."
                ) from exc
            return found.node_id, Node(self._session, found.node_id)
        node_id = to_nodeid(supertype)
        node = Node(self._session, node_id)
        node_class = await _node_class(node)
        if node_class is not ua.NodeClass.ObjectType:
            raise AddressSpaceError(
                f"supertype {format_node_id(node_id)} is {_class_text(node_class)}, not an "
                "ObjectType: a type is created with HasSubtype from another type"
            )
        return node_id, node

    async def _author_variable_type(
        self, spec: ComponentSpec, kind: ComponentKind
    ) -> VariableTypeReport:
        """Create the VariableType one declaration is typed with, or reuse it."""
        declared = spec.variable_type
        if declared is None:
            if spec.type_definition is not None:
                existing_id = spec.type_definition
                name = type_definition_name(existing_id)
                self._variable_types.setdefault(
                    name,
                    VariableTypeReport(
                        node_id=existing_id,
                        browse_name=name,
                        data_type=data_type_name(
                            await _variable_data_type(Node(self._session, existing_id))
                        ),
                        value_rank=0,
                        supertype_id=kind.base_variable_type_id,
                        reused=True,
                    ),
                )
                return self._variable_types[name]
            base_id = kind.default_type_definition
            return VariableTypeReport(
                node_id=base_id,
                browse_name=type_definition_name(base_id),
                data_type=spec.data_type_text,
                value_rank=spec.rank.value_rank,
                supertype_id=base_id,
                reused=True,
            )

        base_id = await self._variable_type_supertype(declared, kind)
        variable_node_id = self.node_id(None, fallback_for=declared.name)
        variable_node, reused = await self._node_or_add(
            variable_node_id,
            ua.QualifiedName(declared.name, self._index),
            lambda node_id, browse_name: Node(self._session, base_id).add_variable_type(
                node_id, browse_name, declared.data_type
            ),
        )
        await _write_attribute(
            variable_node,
            ua.AttributeIds.ValueRank,
            value=declared.rank.value_rank,
            variant_type=ua.VariantType.Int32,
        )
        data_type_id = await _variable_data_type(variable_node)
        if data_type_id is not None and data_type_id != declared.data_type:
            # A type whose DataType is not what was declared is a lie in the report, so it
            # is corrected here and the correction is recorded instead of hidden.
            await _write_attribute(
                variable_node,
                ua.AttributeIds.DataType,
                value=declared.data_type,
                variant_type=ua.VariantType.NodeId,
            )
            _logger.warning(
                "%s: DataType read back as %s and was corrected to %s",
                declared.name,
                data_type_name(data_type_id),
                declared.data_type_text,
            )
        if declared.description:
            await _write_description(variable_node, declared.description)
        # Read the finished VariableType back from the server instead of describing it from
        # the request, with one exception this module knows and a reader cannot: whether the
        # node was created now or already existed.
        read_back = await self._reader.variable_type_report(variable_node.nodeid)
        report = dataclasses.replace(read_back, reused=reused)
        self._variable_types[declared.name] = report
        return report

    async def _variable_type_supertype(
        self, declared: VariableTypeSpec, kind: ComponentKind
    ) -> ua.NodeId:
        """The VariableType a declared VariableType must subtype.

        OPC 10000-5, 7.4: ``BaseDataVariableType`` "is the base VariableType for
        VariableTypes of DataVariables, and all other VariableTypes of DataVariables shall
        either directly or indirectly inherit from it" - and a Property's VariableType has
        to subtype ``PropertyType`` instead. A component is not allowed to pick its own
        grandparent by accident.
        """
        if declared.supertype is None:
            return kind.base_variable_type_id
        node_id = to_nodeid(declared.supertype)
        node = Node(self._session, node_id)
        node_class = await _node_class(node)
        if node_class is not ua.NodeClass.VariableType:
            raise AddressSpaceError(
                f"variable type {declared.name} asks for supertype {format_node_id(node_id)}, "
                f"which is {_class_text(node_class)} and not a VariableType"
            )
        return node_id

    async def _author_declaration(
        self,
        type_node: Node,
        spec: ComponentSpec,
        kind: ComponentKind,
        variable_type: VariableTypeReport,
    ) -> tuple[str, ...]:
        """Hang one InstanceDeclaration on a type, and type it with what was declared.

        Returns:
            The notes a reviewer should see: what was reused, and what this module had to
            correct because the library did not do what was asked. Empty when the library
            cooperated.
        """
        node_id = self.node_id(None, fallback_for=f"{type_node.nodeid.Identifier}.{spec.name}")
        existing = await _child_by_browse_name(type_node, spec.name)
        notes: list[str] = []
        if existing is not None:
            declaration_node = existing
            notes.append(
                f"{spec.name}: the declaration already existed at "
                f"{format_node_id(declaration_node.nodeid)}"
            )
        else:
            value, variant_type = _declaration_value(spec)
            try:
                if kind is ComponentKind.COMPONENT:
                    declaration_node = await type_node.add_variable(
                        node_id,
                        ua.QualifiedName(spec.name, self._index),
                        value,
                        varianttype=variant_type,
                        datatype=spec.data_type,
                    )
                else:
                    declaration_node = await type_node.add_property(
                        node_id,
                        ua.QualifiedName(spec.name, self._index),
                        value,
                        varianttype=variant_type,
                        datatype=spec.data_type,
                    )
            except (UaStatusCodeError, UaError) as exc:
                raise AddressSpaceError(
                    f"the server refused to declare {spec.name} on "
                    f"{format_node_id(type_node.nodeid)}: {exc}"
                ) from exc

        correction = await self._retrofit_type_definition(declaration_node, variable_type)
        if correction is not None:
            notes.append(correction)
            notes.extend(
                await self._rehang_declaration(type_node, declaration_node, kind, variable_type)
            )
        await _write_attribute(
            declaration_node,
            ua.AttributeIds.ValueRank,
            value=spec.rank.value_rank,
            variant_type=ua.VariantType.Int32,
        )
        await _write_attribute(
            declaration_node,
            ua.AttributeIds.AccessLevel,
            value=spec.access.mask,
            variant_type=ua.VariantType.Byte,
        )
        await _write_attribute(
            declaration_node,
            ua.AttributeIds.UserAccessLevel,
            value=spec.access.mask,
            variant_type=ua.VariantType.Byte,
        )
        if spec.min_sampling_interval is not None:
            await _write_attribute(
                declaration_node,
                ua.AttributeIds.MinimumSamplingInterval,
                value=float(spec.min_sampling_interval),
                variant_type=ua.VariantType.Double,
            )
        if spec.description:
            await _write_description(declaration_node, spec.description)
        # OPC 10000-3, 5.5.1: an Object "can point to at most one ModellingRule Object
        # using a HasModellingRule Reference", and asyncua's set_modelling_rule enforces
        # that by deleting whatever rule was there before.
        await declaration_node.set_modelling_rule(mandatory=spec.rule is ModellingRule.MANDATORY)
        return tuple(notes)

    async def _retrofit_type_definition(
        self, declaration_node: Node, variable_type: VariableTypeReport
    ) -> str | None:
        """Replace the TypeDefinition asyncua chose with the one that was declared."""
        declared_id = await declaration_node.read_type_definition()
        if declared_id == variable_type.node_id:
            return None
        note = (
            f"{format_node_id(declaration_node.nodeid)}: the declaration was typed "
            f"{type_definition_name(declared_id)}; that reference now points at "
            f"{variable_type.browse_name} ({format_node_id(variable_type.node_id)})"
        )
        if declared_id is not None:
            try:
                await declaration_node.delete_reference(
                    declared_id, ua.ObjectIds.HasTypeDefinition, forward=True, bidirectional=True
                )
            except UaStatusCodeError as exc:
                # OPC 10000-3, 5.6.2 gives every Variable exactly one HasTypeDefinition, so
                # a reference we could not delete is a reference the server does not have.
                _logger.debug(
                    "no HasTypeDefinition reference to remove on %s: %s", declared_id, exc
                )
        try:
            await declaration_node.add_reference(
                variable_type.node_id,
                ua.ObjectIds.HasTypeDefinition,
                forward=True,
                bidirectional=True,
            )
        except (UaStatusCodeError, UaError) as exc:
            raise AddressSpaceError(
                f"the server refused to type {format_node_id(declaration_node.nodeid)} with "
                f"{variable_type.browse_name}: {exc}"
            ) from exc
        return note

    def _check_value_names(self, spec: InstanceSpec, type_report: TypeReport) -> None:
        """Refuse a value for a component this instance is not going to have.

        Asked before the server is touched, because an instance created and then abandoned
        mid-write is worse than a request that never happened: the half-written instance is
        left in the address space for the next person to find.
        """
        allowed = set(type_report.mandatory_names)
        if spec.include_optional:
            allowed.update(type_report.optional_names)
        unknown = sorted(set(spec.values) - allowed)
        if not unknown:
            return
        dropped = ""
        left_out = sorted(set(unknown) & set(type_report.optional_names))
        if left_out and not spec.include_optional:
            dropped = (
                f"; {', '.join(left_out)} is declared optional and this instance was asked to "
                "leave optional parts out"
            )
        raise AddressSpaceError(
            f"{spec.name} would be an instance of {type_report.browse_name}, which gives an "
            f"instance {', '.join(sorted(allowed)) or 'no components at all'}: "
            f"{', '.join(unknown)} is not one of them{dropped}"
        )

    async def _apply_values(self, instance_node: Node, spec: InstanceSpec) -> None:
        """Write the values that make one instance different from its siblings."""
        if not spec.values:
            return
        components = {
            component.browse_name: component
            for component in await self._reader.components_of(instance_node)
        }
        for name, value in spec.values.items():
            component = components.get(name)
            if component is None:
                raise AddressSpaceError(
                    f"instance {spec.name} has no component {name!r}: this instance has "
                    f"{', '.join(sorted(components)) or 'no components at all'}. A value for a "
                    "component the type does not declare is drift the author refuses to write."
                )
            variant_type = _wire_type_for(component.data_type_id)
            try:
                await Node(self._session, component.node_id).write_value(
                    make_variant(value, variant_type=variant_type)
                )
            except UaError as exc:
                raise AddressSpaceError(
                    f"the server refused the value {value!r} for {spec.name}.{name} "
                    f"(declared {component.data_type}): {exc}"
                ) from exc

    async def _rehang_declaration(
        self,
        type_node: Node,
        declaration_node: Node,
        kind: ComponentKind,
        variable_type: VariableTypeReport,
    ) -> tuple[str, ...]:
        """Make the type's own browse result report the TypeDefinition that was declared.

        Two records answer "what is this node typed with", and asyncua's server writes them
        at different moments:

        * the declaration's forward ``HasTypeDefinition`` reference, which the retrofit
          points at the VariableType that was declared;
        * the *parent's* record of that child, whose ``TypeDefinition`` was copied out of the
          AddNodesItem when the child was created (``asyncua/server/address_space.py``,
          381-390: ``desc.TypeDefinition = item.TypeDefinition``).

        A browse result is built from the parent's record, and so is the child list an
        instantiation walks - which is why correcting only the first one leaves every instance
        typed with the base type. Re-adding the hierarchical reference makes the server
        rebuild that record from the declaration's current ``HasTypeDefinition``
        (``asyncua/server/address_space.py``, 494-500). The re-hung declaration comes last in
        browse order afterwards, which is what it then looks like in a report and in UaExpert.
        """
        if kind is ComponentKind.PROPERTY:
            reference_id, reference_name = ua.ObjectIds.HasProperty, "HasProperty"
        else:
            reference_id, reference_name = ua.ObjectIds.HasComponent, "HasComponent"
        try:
            await type_node.delete_reference(
                declaration_node.nodeid, reference_id, forward=True, bidirectional=True
            )
        except (UaStatusCodeError, UaError) as exc:
            raise AddressSpaceError(
                f"the server refused to remove {reference_name} from "
                f"{format_node_id(type_node.nodeid)} to {format_node_id(declaration_node.nodeid)}, "
                f"so the type would keep reporting {variable_type.browse_name} as nothing: {exc}"
            ) from exc
        try:
            await type_node.add_reference(
                declaration_node.nodeid, reference_id, forward=True, bidirectional=True
            )
        except (UaStatusCodeError, UaError) as exc:
            raise AddressSpaceError(
                f"the server refused to re-hang {format_node_id(declaration_node.nodeid)} on "
                f"{format_node_id(type_node.nodeid)} after its TypeDefinition was corrected: {exc}"
            ) from exc
        return (
            f"{format_node_id(declaration_node.nodeid)}: {reference_name} was removed and added "
            f"again so that {format_node_id(type_node.nodeid)} also reports "
            f"{variable_type.browse_name} for this declaration",
        )

    async def _resolve_parent(self, parent: NodeIdLike | Node | None) -> Node:
        """The node instances are attached to."""
        if parent is None:
            return Node(self._session, ua.ObjectIds.ObjectsFolder)
        if isinstance(parent, Node):
            return parent
        return Node(self._session, to_nodeid(parent))

    async def _node_or_add(
        self,
        node_id: ua.NodeId,
        browse_name: ua.QualifiedName,
        creator: Any,
    ) -> tuple[Node, bool]:
        """The node this NodeId already names, or a new one.

        Returns:
            The node, and whether it already existed rather than being created now.
        """
        node = Node(self._session, node_id)
        node_class = await _node_class(node)
        if node_class is not None:
            return node, True
        try:
            created = await creator(node_id, browse_name)
        except (UaStatusCodeError, UaError) as exc:
            # The most common reason a server says no is the role the session has: asyncua's
            # own address space hands AddNodes to an Admin and answers every other role with
            # BadUserAccessDenied (asyncua/server/address_space.py, 281-283).
            raise AddressSpaceError(
                f"the server refused to create {browse_name.Name} at "
                f"{format_node_id(node_id)}: {exc}"
            ) from exc
        return created, False


def _with_reuse_flags(
    read: Iterable[VariableTypeReport], authored: Iterable[VariableTypeReport]
) -> tuple[VariableTypeReport, ...]:
    """The VariableTypes as the server has them, plus what this author knows about their origin.

    The list comes from the reader, because that is the one that looked at the address space;
    the author adds the one fact a reader cannot see from a node: whether the node was created
    by this run or was already there.
    """
    origin = {report.node_id: report.reused for report in authored}
    return tuple(
        dataclasses.replace(item, reused=origin[item.node_id]) if item.node_id in origin else item
        for item in read
    )


def _wire_type_for(data_type_id: ua.NodeId | None) -> ua.VariantType | None:
    """The wire type a value of this data type must be written as, or ``None``.

    ``None`` means "this module may not guess": a data type from another namespace has no
    wire type this toolkit knows, and inventing one is how a server ends up holding a
    Double in a variable that declared something else.
    """
    if data_type_id is None:
        return None
    return variant_type_for_data_type(data_type_id)


def _declaration_value(spec: ComponentSpec) -> tuple[Any, ua.VariantType | None]:
    """The value a declaration is created with, and the wire type that value needs.

    The wire type is always passed explicitly. Left to guess, asyncua derives the declared
    data type from whatever Python object happens to be there - which is how a 32-bit
    ``Float`` silently turns into a 64-bit ``Double`` in a *type definition*, so every
    instance inherits the mistake. Chapter 8's write bug, moved one level earlier.
    """
    data_type = to_nodeid(spec.data_type)
    variant_type = _wire_type_for(data_type)
    identifier = data_type.Identifier if data_type.NamespaceIndex == 0 else None
    if spec.initial is None:
        default = _DEFAULT_VALUES.get(identifier) if isinstance(identifier, int) else None
        return default, variant_type
    if variant_type is None:
        return spec.initial, None
    return coerce_to_variant_type(spec.initial, variant_type), variant_type


async def _variable_data_type(node: Node) -> ua.NodeId | None:
    """The ``DataType`` attribute of a VariableType or Variable, as a NodeId."""
    raw = await _read_attribute(node, ua.AttributeIds.DataType, default=None)
    if raw is None:
        return None
    try:
        return to_nodeid(raw)
    except (ConfigurationError, UaError, TypeError, ValueError):
        return None


async def _read_attribute(node: Node, attribute: ua.AttributeIds, *, default: Any) -> Any:
    """One attribute value, or ``default`` when the server has no answer."""
    try:
        result = await node.read_attribute(attribute)
    except UaError:
        return default
    if result is None or result.Value is None or result.Value.Value is None:
        return default
    return result.Value.Value


async def _node_class(node: Node) -> ua.NodeClass | None:
    """The node class of a node, or ``None`` when the server does not know it."""
    raw = await _read_attribute(node, ua.AttributeIds.NodeClass, default=None)
    if raw is None:
        return None
    try:
        return ua.NodeClass(int(raw))
    except (TypeError, ValueError):
        return None


def _class_text(node_class: ua.NodeClass | None) -> str:
    """Readable node class name, including the case where the server knows nothing."""
    return (
        node_class.name if node_class is not None else "an unknown node (the server did not answer)"
    )


async def _write_attribute(
    node: Node, attribute: ua.AttributeIds, value: Any, variant_type: ua.VariantType
) -> None:
    """Write one attribute, with the wire type the specification gives that attribute.

    The explicit variant type is not decoration: OPC 10000-3, 5.6.2 declares
    ``AccessLevel`` as a Byte and ``ValueRank`` as an Int32, and asyncua's server refuses a
    Python ``int`` that arrives as Int64.
    """
    try:
        await node.write_attribute(
            attribute,
            ua.DataValue(ua.Variant(value, variant_type)),
        )
    except UaError as exc:
        name = getattr(attribute, "name", None) or str(attribute)
        raise AddressSpaceError(
            f"the server refused to write {name} on {format_node_id(node.nodeid)}: {exc}"
        ) from exc


async def _write_description(node: Node, text: str) -> None:
    """Write the human text a client will read in a browse."""
    try:
        await node.write_attribute(
            ua.AttributeIds.Description,
            ua.DataValue(ua.Variant(ua.LocalizedText(Text=text), ua.VariantType.LocalizedText)),
        )
    except UaError as exc:
        raise AddressSpaceError(
            f"the server refused the Description of {format_node_id(node.nodeid)}: {exc}"
        ) from exc


async def _child_by_browse_name(parent: Node, browse_name: str) -> Node | None:
    """The child of ``parent`` with this BrowseName, if it has one."""
    for description in await parent.get_children_descriptions():
        if str(description.BrowseName.Name) == browse_name:
            return Node(parent.session, description.NodeId)
    return None
