"""Reading what a server's types promise (Chapter 16).

Authoring a type is half the job. The other half is what a client can do with the type
once it exists, and none of it needs permission to write:

* read the **declarations** a type carries - its own, and the ones it inherits, which is
  the "fully-inherited InstanceDeclarationHierarchy" of OPC 10000-3, 6.3.3.2 and the
  hierarchy an instance is really built from;
* **check one instance** against the type its own ``HasTypeDefinition`` names, which is
  how a client finds out that a server's "pump" objects do not all have a temperature
  *before* someone writes a historian that assumes they do;
* measure **type coverage**: how much of a subtree has a TypeDefinition at all, and how
  much of it has only ``BaseDataVariableType`` - legal under OPC 10000-3, 4.6.1, and the
  type that says nothing about the node.

Nothing here writes, so it works against a server this process does not own and cannot
change, which is the situation Part 6 puts it in.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from asyncua import Client, Node, Server, ua
from asyncua.ua.uaerrors import UaError

from opcua_toolkit.errors import AddressSpaceError, ConfigurationError, format_node_id
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.server.model import (
    BASE_OBJECT_TYPE_ID,
    BASE_TYPE_DEFINITION_IDS,
    BASE_VARIABLE_TYPE_ID,
    ComponentKind,
    DeclarationReport,
    InstanceComponent,
    InstanceReport,
    ModellingRule,
    TypeCoverage,
    TypeReport,
    TypeUsage,
    VariableTypeReport,
    type_definition_name,
)
from opcua_toolkit.values import data_type_name

__all__ = ["DEFAULT_MAX_NODES", "AddressSpaceReader"]

_logger = logging.getLogger(__name__)

#: How many nodes a coverage walk visits before it stops and admits it stopped. A subtree
#: that needs more than this is not a plant, it is an unbounded graph, and a report that
#: silently truncates is worse than one that says how far it looked.
DEFAULT_MAX_NODES = 2_000

#: How many type nodes :meth:`AddressSpaceReader.find_type` visits when a name is looked up
#: through the ``HasSubtype`` hierarchy. The standard OPC UA type hierarchy is large, and a
#: name lookup is a convenience, not a service: 400 browses is enough to walk every type a
#: plant is likely to define, and past that the answer is "give me the NodeId".
DEFAULT_TYPE_SEARCH_BROWSERS = 400

#: The four standard ModellingRule nodes, as the ids OPC 10000-5 gives them. The
#: placeholders are here because a reader must be able to name what it sees, even though
#: this toolkit never authors them (OPC 10000-3, 6.4.4.4.4 and 6.4.4.4.5 describe
#: structure a type expects an *instance* to add, which is a different contract).
_MODELLING_RULE_IDS: dict[int, ModellingRule] = {
    ua.ObjectIds.ModellingRule_Mandatory: ModellingRule.MANDATORY,
    ua.ObjectIds.ModellingRule_Optional: ModellingRule.OPTIONAL,
    ua.ObjectIds.ModellingRule_OptionalPlaceholder: ModellingRule.OPTIONAL_PLACEHOLDER,
    ua.ObjectIds.ModellingRule_MandatoryPlaceholder: ModellingRule.MANDATORY_PLACEHOLDER,
}

#: The TypeDefinitionNodes that tell a client nothing beyond "this is a Variable" or
#: "this is an Object": OPC 10000-5's three base types, which OPC 10000-3, 4.6.1 allows a
#: server to fall back on "if no more specialised type information is available".
_BASE_TYPE_IDS: frozenset[ua.NodeId] = BASE_TYPE_DEFINITION_IDS

#: Attributes that only exist on a Variable, and so are only read on Variable declarations.
_VARIABLE_ATTRIBUTES: tuple[ua.AttributeIds, ...] = (
    ua.AttributeIds.DataType,
    ua.AttributeIds.ValueRank,
    ua.AttributeIds.AccessLevel,
    ua.AttributeIds.UserAccessLevel,
    ua.AttributeIds.MinimumSamplingInterval,
    ua.AttributeIds.Value,
)

#: What a VariableType node is asked for. ``AccessLevel`` and ``Value`` are left out on
#: purpose: this report answers what a VariableType *declares* about the variables typed
#: with it, and the two attributes that describe what a server lets one caller do with one
#: node are not part of that answer.
_VARIABLE_TYPE_ATTRIBUTES: tuple[ua.AttributeIds, ...] = (
    ua.AttributeIds.DataType,
    ua.AttributeIds.ValueRank,
)


def _anchor_node(handle: Node | Server | Client) -> Node:
    """The node a read or an authoring run starts from.

    ``Objects`` is the choice because it is the one folder every server publishes and
    every role is allowed to browse, and because a namespace's own folder is a child of
    it: starting here means a reader never has to know a namespace index to start looking.
    """
    if isinstance(handle, Node):
        return handle
    if isinstance(handle, Server):
        return handle.nodes.objects
    return handle.get_node(ua.NodeId(ua.ObjectIds.ObjectsFolder, 0))


class AddressSpaceReader:
    """Read what a server's types promise, and check instances against them.

    Args:
        handle: Where to browse from: an asyncua :class:`~asyncua.Client`, a
            :class:`asyncua.Server`, or any :class:`asyncua.Node` in the server being read.
            A client or a server starts the read at ``Objects``, which is the only folder a
            client is guaranteed to be allowed to browse. Only browsing and reading are used.
    """

    def __init__(self, handle: Node | Server | Client) -> None:
        self._anchor: Node = _anchor_node(handle)

    @property
    def anchor(self) -> Node:
        """The node this reader started from."""
        return self._anchor

    def node_for(self, node_id: NodeIdLike) -> Node:
        """One node of the server being read, as an asyncua :class:`~asyncua.Node`."""
        return Node(self._anchor.session, to_nodeid(node_id))

    async def read_type(self, type_id: NodeIdLike) -> TypeReport:
        """Read the InstanceDeclarations one object type carries.

        Args:
            type_id: The ObjectType to read.

        Returns:
            A :class:`~opcua_toolkit.server.model.TypeReport` whose declarations include
            the inherited ones, each marked with the type it came from. A type that
            declares nothing is reported as declaring nothing: that is the finding, not an
            oversight.

        Raises:
            AddressSpaceError: The node is not an ObjectType, so it carries no
                InstanceDeclarations to read.
        """
        node = self.node_for(type_id)
        node_class = await _node_class(node)
        if node_class is not ua.NodeClass.ObjectType:
            raise AddressSpaceError(
                f"{format_node_id(node.nodeid)} is a {_class_text(node_class)}, not an ObjectType: "
                "InstanceDeclarations live on a type, and this node is not one"
            )
        browse_name = await node.read_browse_name()
        abstract = bool(await _read_attribute(node, ua.AttributeIds.IsAbstract, default=False))
        supertype_id = await _supertype_id(node)

        declarations: list[DeclarationReport] = []
        own_count = 0
        # Nearest wins: a subtype that re-declares a name of its supertype overrides it, which
        # is also how asyncua's instantiation picks the declaration to copy ("prefer the
        # 'lowest' one in object hierarchy", asyncua/common/instantiate_util.py, 121-122).
        seen_names: set[str] = set()
        for level, (level_node, level_name) in enumerate(await self._type_hierarchy(node)):
            inherited_from = None if level == 0 else level_name
            for description in await level_node.get_children_descriptions():
                if description.NodeClass not in (
                    ua.NodeClass.Variable,
                    ua.NodeClass.Object,
                    ua.NodeClass.Method,
                ):
                    continue
                if str(description.BrowseName.Name) in seen_names:
                    continue
                seen_names.add(str(description.BrowseName.Name))
                declarations.append(
                    await self._describe_declaration(description, level_node, inherited_from)
                )
                if level == 0:
                    own_count += 1

        variable_types: list[VariableTypeReport] = []
        seen_type_ids: set[ua.NodeId] = set()
        for declaration in declarations:
            # The VariableTypes the declarations are actually typed with, read from the
            # server rather than remembered from a request: this is the list that tells a
            # client whether the type it is looking at says anything.
            type_id = declaration.type_definition_id
            if type_id is None or type_id in _BASE_TYPE_IDS or type_id in seen_type_ids:
                continue
            seen_type_ids.add(type_id)
            variable_types.append(await self.variable_type_report(type_id))

        return TypeReport(
            node_id=node.nodeid,
            browse_name=str(browse_name.Name),
            abstract=abstract,
            declared_abstract=abstract,
            supertype_id=supertype_id
            if supertype_id is not None
            else ua.NodeId(ua.ObjectIds.BaseObjectType, 0),
            declarations=tuple(declarations),
            variable_types=tuple(variable_types),
            own_declaration_count=own_count,
        )

    async def variable_type_report(self, type_id: NodeIdLike) -> VariableTypeReport:
        """What one VariableType node says, read from the server.

        Args:
            type_id: The VariableType to read.

        Returns:
            A :class:`~opcua_toolkit.server.model.VariableTypeReport`: its ``DataType``,
            its ``ValueRank`` and the VariableType it is a subtype of. ``reused`` is True
            for a reader, which never creates anything.

        Raises:
            AddressSpaceError: The node is not a VariableType.
        """
        node = self.node_for(type_id)
        node_class = await _node_class(node)
        if node_class is not ua.NodeClass.VariableType:
            what = _class_text(node_class)
            raise AddressSpaceError(
                f"{format_node_id(node.nodeid)} is a {what}, not a VariableType: "
                "DataType and ValueRank are what a VariableType carries"
            )
        browse_name = await node.read_browse_name()
        attributes = await _read_attributes(node, _VARIABLE_TYPE_ATTRIBUTES)
        data_type = _node_id_value(attributes.get(ua.AttributeIds.DataType))
        supertype_id = await _supertype_id(node)
        return VariableTypeReport(
            node_id=node.nodeid,
            browse_name=str(browse_name.Name),
            data_type=data_type_name(data_type) if data_type is not None else "none",
            value_rank=_int_value(attributes.get(ua.AttributeIds.ValueRank), default=0),
            supertype_id=supertype_id if supertype_id is not None else BASE_VARIABLE_TYPE_ID,
            reused=True,
        )

    async def find_type(
        self, name: str, *, max_browses: int = DEFAULT_TYPE_SEARCH_BROWSERS
    ) -> TypeReport:
        """One ObjectType, found by BrowseName instead of by NodeId.

        Args:
            name: The BrowseName to look for, matched on its local part exactly. A name that
                differs only in case is not accepted quietly: it is named in the error,
                because "MachineType" and "machinetype" are two different BrowseNames and a
                client that settled for one would be reading a type it did not ask for.
            max_browses: How many type nodes to visit before admitting the type was not
                found. The walk starts at ``BaseObjectType`` and follows ``HasSubtype``,
                which is the chain every ObjectSubtype is required to hang on to, so a type
                this search cannot reach is a type no client can reach by name either.

        Returns:
            The :class:`~opcua_toolkit.server.model.TypeReport` of the type with that name,
            including the declarations it inherits.

        Raises:
            AddressSpaceError: No reachable ObjectType has that BrowseName. The message says
                how far the search looked and names any near miss that differs only in case.
        """
        wanted = name.strip()
        if not wanted:
            raise AddressSpaceError("a type search needs a BrowseName to look for")
        queue: list[Node] = [self.node_for(BASE_OBJECT_TYPE_ID)]
        visited = 0
        near: list[str] = []
        lowered = wanted.lower()
        while queue and visited < max_browses:
            current = queue.pop(0)
            visited += 1
            for description in await current.get_children_descriptions(
                refs=ua.ObjectIds.HasSubtype,
                nodeclassmask=ua.NodeClass.ObjectType,
                includesubtypes=False,
            ):
                found = str(description.BrowseName.Name)
                if found == wanted:
                    return await self.read_type(description.NodeId)
                if found.lower() == lowered:
                    near.append(f"{found} at {format_node_id(description.NodeId)}")
                queue.append(Node(self._anchor.session, description.NodeId))
        hint = f" near miss: {', '.join(near[:5])}" if near else ""
        raise AddressSpaceError(
            f"no ObjectType named {wanted!r} below BaseObjectType: searched {visited} type "
            f"node(s){hint}. Pass the type's NodeId if it lives where a subtype walk does not go."
        )

    async def components_of(self, instance: NodeIdLike | Node) -> tuple[InstanceComponent, ...]:
        """Every component and property one instance actually has.

        Args:
            instance: The instance to browse.

        Returns:
            One :class:`~opcua_toolkit.server.model.InstanceComponent` per child found
            through ``HasComponent`` or ``HasProperty``, in browse order. Methods are not
            parts of a data contract and are not reported here - Chapter 18 owns methods.
        """
        node = instance if isinstance(instance, Node) else self.node_for(instance)
        components: list[InstanceComponent] = []
        for description in await node.get_children_descriptions():
            if description.NodeClass != ua.NodeClass.Variable:
                continue
            components.append(await self._describe_component(description, node))
        return tuple(components)

    async def describe_instance(
        self,
        instance: NodeIdLike | Node,
        *,
        expected_type: NodeIdLike | None = None,
    ) -> InstanceReport:
        """One instance, measured against the type it is supposed to be an instance of.

        Args:
            instance: The instance to read.
            expected_type: The type to check against. ``None`` uses the instance's own
                ``HasTypeDefinition``, which is the interesting case: it asks "does this
                node hold everything the type it claims to be an instance of promises?"

        Returns:
            The :class:`~opcua_toolkit.server.model.InstanceReport`, whose
            ``conforms_to_type`` answers the only question that matters here.

        Raises:
            AddressSpaceError: ``expected_type`` names a node that is not an ObjectType,
                or the instance has no ``HasTypeDefinition`` and none was supplied.
        """
        node = instance if isinstance(instance, Node) else self.node_for(instance)
        browse_name = await node.read_browse_name()
        own_type = await node.read_type_definition()
        if expected_type is None:
            if own_type is None:
                raise AddressSpaceError(
                    f"{format_node_id(node.nodeid)} has no HasTypeDefinition reference and no "
                    "expected type was given, so there is no contract to check it against"
                )
            expected_id = own_type
        else:
            expected_id = to_nodeid(expected_type)
        report = await self.read_type(expected_id)
        components = await self.components_of(node)
        present = {component.browse_name for component in components}
        declared = report.declared_names
        mandatory = report.mandatory_names
        return InstanceReport(
            node_id=node.nodeid,
            browse_name=str(browse_name.Name),
            type_definition_id=own_type,
            expected_type_id=expected_id,
            components=components,
            declared_names=declared,
            mandatory_names=mandatory,
            type_definition_matches=own_type == expected_id,
            missing_names=tuple(name for name in mandatory if name not in present),
            optional_absent=tuple(
                name for name in declared if name not in mandatory and name not in present
            ),
            extra_names=tuple(name for name in present if name not in set(declared)),
        )

    async def coverage(
        self,
        start: NodeIdLike | Node | None = None,
        *,
        max_nodes: int = DEFAULT_MAX_NODES,
    ) -> TypeCoverage:
        """Count how much of a subtree has a TypeDefinition that says something.

        Args:
            start: Where to start, usually ``Objects`` or a namespace's folder. ``None``
                starts at the node this reader was given.
            max_nodes: How many nodes to visit before stopping and saying so.

        Returns:
            A :class:`~opcua_toolkit.server.model.TypeCoverage` with one row per distinct
            TypeDefinition. Each child's type is taken from the browse result, so a walk
            of ``n`` nodes costs ``n`` browses and no per-node attribute reads.

        Raises:
            AddressSpaceError: ``max_nodes`` is smaller than 1.
        """
        if max_nodes < 1:
            raise AddressSpaceError(f"max_nodes must be at least 1, got {max_nodes}")
        node = (
            self._anchor
            if start is None
            else (start if isinstance(start, Node) else self.node_for(start))
        )
        start_name = str((await node.read_browse_name()).Name)

        seen: set[str] = {format_node_id(node.nodeid)}
        queue: list[Node] = [node]
        browsed = 0
        variables = 0
        untyped = 0
        counts: Counter[tuple[str, bool]] = Counter()
        type_ids: dict[tuple[str, bool], ua.NodeId | None] = {}
        truncated = False

        while queue:
            if browsed >= max_nodes:
                truncated = True
                break
            current = queue.pop(0)
            browsed += 1
            for description in await current.get_children_descriptions():
                if description.NodeClass == ua.NodeClass.Variable:
                    variables += 1
                    key = _usage_bucket(description.TypeDefinition)
                    counts[key] += 1
                    type_ids.setdefault(key, description.TypeDefinition)
                    if key[0] == "nothing (no HasTypeDefinition)":
                        untyped += 1
                if description.NodeClass in (ua.NodeClass.Object, ua.NodeClass.Variable):
                    child = Node(current.session, description.NodeId)
                    key_text = format_node_id(child.nodeid)
                    if key_text not in seen:
                        seen.add(key_text)
                        queue.append(child)

        usage = tuple(
            TypeUsage(
                type_id=type_ids.get(bucket), type_name=bucket[0], count=count, base_only=bucket[1]
            )
            for bucket, count in counts.most_common()
        )
        report = TypeCoverage(
            start_name=start_name,
            variables=variables,
            untyped_variables=untyped,
            usage=usage,
            browsed_nodes=browsed,
            truncated=truncated,
        )
        _logger.debug("type coverage of %s: %s", start_name, report.summary_line())
        return report

    async def _type_hierarchy(self, node: Node) -> tuple[tuple[Node, str], ...]:
        """This type, then each supertype, nearest first, with a cycle guard.

        OPC 10000-3, 6.3.3.2 builds the hierarchy a type's instances are made from by
        merging the hierarchy of its parent type, which is what makes a subtype of an
        abstract type an honest way to describe a family of assets.
        """
        chain: list[tuple[Node, str]] = []
        seen: set[str] = set()
        current: Node | None = node
        while current is not None:
            key = format_node_id(current.nodeid)
            if key in seen:
                _logger.warning(
                    "supertype chain of %s loops back to %s", format_node_id(node.nodeid), key
                )
                break
            seen.add(key)
            name = str((await current.read_browse_name()).Name)
            chain.append((current, name))
            parent_id = await _supertype_id(current)
            if parent_id is None or parent_id == ua.NodeId(ua.ObjectIds.BaseObjectType, 0):
                break
            current = Node(current.session, parent_id)
        return tuple(chain)

    async def _describe_declaration(
        self, description: ua.ReferenceDescription, parent: Node, inherited_from: str | None
    ) -> DeclarationReport:
        """One InstanceDeclaration, as the parent's browse result and the child's attributes say."""
        child = Node(parent.session, description.NodeId)
        kind = _kind_of(description)
        rule = await _rule_of(child)
        type_definition = description.TypeDefinition or await child.read_type_definition()
        if description.NodeClass == ua.NodeClass.Variable:
            attributes = await _read_attributes(child, _VARIABLE_ATTRIBUTES)
            data_type = _node_id_value(attributes.get(ua.AttributeIds.DataType))
            value_rank = _int_value(attributes.get(ua.AttributeIds.ValueRank), default=0)
            access_mask = _int_value(attributes.get(ua.AttributeIds.AccessLevel), default=0)
            sampling_raw = attributes.get(ua.AttributeIds.MinimumSamplingInterval)
            min_sampling = None if sampling_raw is None else _float_value(sampling_raw, default=0.0)
            initial_value = attributes.get(ua.AttributeIds.Value)
        else:
            data_type = None
            value_rank = 0
            access_mask = 0
            min_sampling = None
            initial_value = None
        return DeclarationReport(
            browse_name=str(description.BrowseName.Name),
            kind=kind,
            node_id=description.NodeId,
            rule=rule,
            type_definition_id=type_definition,
            data_type=data_type_name(data_type) if data_type is not None else "none",
            value_rank=value_rank,
            access_mask=access_mask,
            min_sampling_interval=min_sampling,
            initial_value=initial_value,
            specialised_type=_is_specialised(type_definition, kind),
            inherited_from=inherited_from,
        )

    async def _describe_component(
        self, description: ua.ReferenceDescription, parent: Node
    ) -> InstanceComponent:
        """One component of an instance, with its current value."""
        child = Node(parent.session, description.NodeId)
        type_definition = description.TypeDefinition or await child.read_type_definition()
        attributes = await _read_attributes(child, _VARIABLE_ATTRIBUTES)
        data_type = _node_id_value(attributes.get(ua.AttributeIds.DataType))
        sampling_raw = attributes.get(ua.AttributeIds.MinimumSamplingInterval)
        value_data = attributes.get(ua.AttributeIds.Value)
        return InstanceComponent(
            browse_name=str(description.BrowseName.Name),
            node_id=description.NodeId,
            kind=_kind_of(description),
            type_definition_id=type_definition,
            data_type_id=data_type,
            data_type=data_type_name(data_type) if data_type is not None else "none",
            value_rank=_int_value(attributes.get(ua.AttributeIds.ValueRank), default=0),
            access_mask=_int_value(attributes.get(ua.AttributeIds.AccessLevel), default=0),
            min_sampling_interval=(
                None if sampling_raw is None else _float_value(sampling_raw, default=0.0)
            ),
            value=value_data,
        )


def _kind_of(description: ua.ReferenceDescription) -> ComponentKind:
    """Component or property, from the reference the browse result came through."""
    if description.ReferenceTypeId == ua.NodeId(ua.ObjectIds.HasProperty, 0):
        return ComponentKind.PROPERTY
    return ComponentKind.COMPONENT


def _usage_bucket(type_id: ua.NodeId | None) -> tuple[str, bool]:
    """The coverage table row a TypeDefinition belongs in: ``(name, is_base_only)``."""
    if type_id is None:
        return ("nothing (no HasTypeDefinition)", True)
    base_only = type_id in _BASE_TYPE_IDS
    return (type_definition_name(type_id), base_only)


def _is_specialised(type_id: ua.NodeId | None, kind: ComponentKind) -> bool:
    """Whether a declaration is typed with something the author actually wrote."""
    if type_id is None:
        return False
    return bool(type_id != kind.default_type_definition)


async def _supertype_id(node: Node) -> ua.NodeId | None:
    """The ObjectType this type is a subtype of, or ``None`` when nothing names one."""
    references = await node.get_references(
        refs=ua.ObjectIds.HasSubtype, direction=ua.BrowseDirection.Inverse, includesubtypes=False
    )
    if not references:
        return None
    return references[0].NodeId


async def _rule_of(node: Node) -> ModellingRule | None:
    """The ModellingRule a declaration carries, or ``None`` when it carries none.

    OPC 10000-3, 5.5.1 gives an Object at most one ``HasModellingRule`` reference, and
    asyncua's ``set_modelling_rule`` enforces that by deleting the old one, so the first
    reference found is the only one worth reading.
    """
    references = await node.get_references(
        refs=ua.ObjectIds.HasModellingRule,
        direction=ua.BrowseDirection.Forward,
        includesubtypes=False,
    )
    for reference in references:
        node_id = reference.NodeId
        if node_id.NamespaceIndex == 0 and isinstance(node_id.Identifier, int):
            rule = _MODELLING_RULE_IDS.get(node_id.Identifier)
            if rule is not None:
                return rule
    return None


async def _node_class(node: Node) -> ua.NodeClass | None:
    """The node class of a node, or ``None`` when the server does not know the node.

    The value has to be unwrapped twice before it is a number: asyncua answers an attribute
    read with a :class:`~asyncua.ua.DataValue` whose ``Value`` is a
    :class:`~asyncua.ua.Variant`, and the class itself is only inside that Variant (measured:
    ``DataValue(Value=Variant(Value=<NodeClass.ObjectType: 8>, VariantType=Int32))``).
    ``int()`` on a ``Variant`` raises ``TypeError``, which is what made every authored type look
    like "an unknown node" until this line was fixed.
    """
    raw = await _read_attribute(node, ua.AttributeIds.NodeClass, default=None)
    if raw is None:
        return None
    try:
        return ua.NodeClass(int(raw))
    except (TypeError, ValueError):
        return None


def _class_text(node_class: ua.NodeClass | None) -> str:
    """Readable node class name, including the case where the server knows nothing."""
    return node_class.name if node_class is not None else "unknown node (the server did not answer)"


async def _read_attribute(node: Node, attribute: ua.AttributeIds, *, default: Any) -> Any:
    """One attribute value, or ``default`` when the server has no value for it."""
    data_value = await _read_attribute_value(node, attribute)
    if data_value is None or data_value.Value is None or data_value.Value.Value is None:
        return default
    return data_value.Value.Value


async def _read_attribute_value(node: Node, attribute: ua.AttributeIds) -> ua.DataValue | None:
    """One :class:`asyncua.ua.DataValue`, or ``None`` when the read was refused."""
    try:
        return await node.read_attribute(attribute)
    except UaError:
        return None


async def _read_attributes(
    node: Node, attributes: tuple[ua.AttributeIds, ...]
) -> dict[ua.AttributeIds, Any]:
    """Several attributes of one node, keeping only the ones the server answered.

    A server is allowed to answer individual attributes with a bad StatusCode - a Property
    has no ``AccessLevel`` story to tell, an Object has no ``DataType`` at all - so the
    attributes that came back bad are simply absent from the result.

    The values are unwrapped here, once: ``DataValue.Value`` is a
    :class:`~asyncua.ua.Variant` and the value a report wants is ``DataValue.Value.Value``.
    Every caller of this helper therefore works with plain Python values and must not reach
    for ``.Value`` again.
    """
    try:
        results = await node.read_attributes(list(attributes))
    except UaError:
        return {}
    answers: dict[ua.AttributeIds, Any] = {}
    for attribute, data_value in zip(attributes, results, strict=True):
        if data_value.StatusCode is not None and data_value.StatusCode.is_bad():
            continue
        if data_value.Value is None:
            continue
        answers[attribute] = data_value.Value.Value
    return answers


def _node_id_value(raw: Any) -> ua.NodeId | None:
    """A value that should be a NodeId, or ``None`` when it is not one."""
    if raw is None:
        return None
    try:
        return to_nodeid(raw)
    except (TypeError, ValueError, ConfigurationError, UaError):
        return None


def _int_value(raw: Any, *, default: int) -> int:
    """A value that should be an integer, or ``default``."""
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_value(raw: Any, *, default: float) -> float:
    """A value that should be a float, or ``default``."""
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default
