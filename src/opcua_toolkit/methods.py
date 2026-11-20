"""Generic method discovery and invocation.

Chapter 9's rule: never hardcode a method call. A real plant exposes methods
with names and data types that the client must *discover* - `Start`, `Stop`,
`Reset`, `Acknowledge` - and then invoke by argument name. This module reads
``InputArguments``/``OutputArguments`` from the server, builds typed variants
from Python values, and reports ``InputArgumentResults`` per argument so a
rejected argument is obvious instead of a bare ``BadInvalidArgument``.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from asyncua import Client, ua
from asyncua.ua.uaerrors import UaError, UaStatusCodeError

from opcua_toolkit.browse.explorer import NamespaceExplorer
from opcua_toolkit.browse.model import NodeRef
from opcua_toolkit.errors import (
    ConfigurationError,
    MethodCallError,
    MethodNotFoundError,
    format_node_id,
)
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.values import (
    coerce_to_variant_type,
    data_type_name,
    format_value,
    unwrap,
    variant_type_for_data_type,
)

_HAS_METHOD_ARGUMENTS = {"InputArguments", "OutputArguments"}


@dataclasses.dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """One declared method argument as the server publishes it."""

    name: str
    data_type: ua.NodeId
    data_type_name: str
    value_rank: int
    array_dimensions: tuple[int, ...]
    description: str = ""

    @property
    def is_array(self) -> bool:
        """True for one or two dimensional arrays."""
        return self.value_rank >= 0

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "name": self.name,
            "data_type": self.data_type_name,
            "value_rank": self.value_rank,
            "array_dimensions": list(self.array_dimensions),
            "description": self.description,
        }

    def signature(self) -> str:
        """Compact signature fragment, e.g. ``speed: UInt32``."""
        suffix = "[]" if self.is_array else ""
        return f"{self.name}: {self.data_type_name}{suffix}"


@dataclasses.dataclass(frozen=True, slots=True)
class MethodDescriptor:
    """A discoverable method with its parent, arguments and executability."""

    node_id: ua.NodeId
    parent_node_id: ua.NodeId
    browse_name: str
    display_name: str
    inputs: tuple[ArgumentSpec, ...]
    outputs: tuple[ArgumentSpec, ...]
    executable: bool
    user_executable: bool

    @property
    def callable_by_user(self) -> bool:
        """True when both Executable and UserExecutable are set."""
        return self.executable and self.user_executable

    @property
    def node_id_text(self) -> str:
        """Canonical NodeId text."""
        return format_node_id(self.node_id)

    @property
    def parent_node_id_text(self) -> str:
        """The object that owns this method, as ``ns=...;...`` text.

        A method is always a component of something. Knowing *what* - a line, a
        pump, a batch - is what lets a generic client call the right instance
        instead of the first one it bumped into while browsing.
        """
        return format_node_id(self.parent_node_id)

    def signature(self) -> str:
        """Readable signature, e.g. ``Add(a: UInt32, b: UInt32) -> UInt32``."""
        inputs = ", ".join(spec.signature() for spec in self.inputs)
        outputs = ", ".join(spec.signature() for spec in self.outputs) or "None"
        return f"{self.browse_name}({inputs}) -> {outputs}"

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "node_id": self.node_id_text,
            "parent": self.parent_node_id_text,
            "browse_name": self.browse_name,
            "display_name": self.display_name,
            "executable": self.executable,
            "user_executable": self.user_executable,
            "inputs": [spec.as_dict() for spec in self.inputs],
            "outputs": [spec.as_dict() for spec in self.outputs],
        }


@dataclasses.dataclass(frozen=True, slots=True)
class CallResult:
    """Outcome of one method call, including per-argument verdicts."""

    method: MethodDescriptor
    status: ua.StatusCode
    outputs: dict[str, Any]
    input_argument_results: tuple[ua.StatusCode, ...]
    elapsed_ms: float

    @property
    def ok(self) -> bool:
        """True when the method itself returned Good."""
        return not self.status.is_bad()

    @property
    def rejected_arguments(self) -> list[str]:
        """Names of arguments the server rejected."""
        rejected: list[str] = []
        for index, status in enumerate(self.input_argument_results):
            if status.is_bad() and index < len(self.method.inputs):
                rejected.append(self.method.inputs[index].name)
        return rejected

    def output_of(self, name: str, default: Any = None) -> Any:
        """Return one output value by its declared name."""
        return self.outputs.get(name, default)

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "method": self.method.browse_name,
            "status": self.status.name,
            "outputs": {key: format_value(value) for key, value in self.outputs.items()},
            "input_argument_results": [status.name for status in self.input_argument_results],
            "rejected_arguments": self.rejected_arguments,
            "elapsed_ms": round(self.elapsed_ms, 3),
        }


class MethodCatalog:
    """Discover methods without knowing NodeIds or vendor conventions."""

    def __init__(
        self,
        client: Client,
        *,
        limits: OperationLimits | None = None,
        explorer: NamespaceExplorer | None = None,
    ) -> None:
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()
        self._explorer = explorer or NamespaceExplorer(client, limits=self._limits)

    async def discover(self, parent: NodeIdLike) -> list[MethodDescriptor]:
        """Discover the methods that are components of one node."""
        methods = await self._methods_of([to_nodeid(parent)])
        return await self._describe(methods)

    async def discover_under(
        self,
        root: NodeIdLike,
        *,
        max_depth: int = 4,
        max_nodes: int = 2_000,
    ) -> list[MethodDescriptor]:
        """Discover every method below ``root`` with a small number of requests.

        First pass: batched Browse finds the containers (Objects). Second pass:
        batched Browse finds their methods. Third pass: bulk Reads fetch the
        executability flags and the argument declarations. Cost stays
        logarithmic in tree size instead of one request per node.
        """
        tree, _ = await self._explorer.walk(
            root,
            max_depth=max_depth,
            max_nodes=max_nodes,
            node_class=ua.NodeClass.Object,
        )
        containers = [to_nodeid(root)]
        containers.extend(ref.node_id for ref in tree.walk())
        methods = await self._methods_of(containers)
        return await self._describe(methods)

    async def _methods_of(self, parents: Sequence[ua.NodeId]) -> list[tuple[ua.NodeId, NodeRef]]:
        """Batched browse for Method nodes, remembering which parent they belong to.

        The reference type is left unfiltered on purpose. Servers attach methods with
        ``HasComponent``, others with a subtype of it, and asyncua attaches the
        argument declaration variables with ``HasProperty`` - a chain that a strict
        ``HasComponent`` browse does not follow. The NodeClass mask is the filter that
        actually holds across servers.
        """
        found: list[tuple[ua.NodeId, NodeRef]] = []
        for chunk in chunked(list(parents), self._limits.max_nodes_per_browse):
            descriptions = []
            for parent in chunk:
                description = ua.BrowseDescription()
                description.NodeId = parent
                description.BrowseDirection = ua.BrowseDirection.Forward
                description.ReferenceTypeId = ua.NodeId(0, 0)
                description.IncludeSubtypes = True
                description.NodeClassMask = ua.NodeClass.Method
                description.ResultMask = ua.BrowseResultMask.All
                descriptions.append(description)
            params = ua.BrowseParameters()
            params.View = ua.ViewDescription()
            params.RequestedMaxReferencesPerNode = 0
            params.NodesToBrowse = descriptions
            results = await self._client.uaclient.browse(params)
            for parent, result in zip(chunk, results, strict=True):
                if result.StatusCode.is_bad():
                    continue
                found.extend(
                    (parent, NodeRef.from_description(reference)) for reference in result.References
                )
        return found

    async def _describe(
        self, methods: Sequence[tuple[ua.NodeId, NodeRef]]
    ) -> list[MethodDescriptor]:
        """Fetch executability flags and argument declarations for many methods."""
        if not methods:
            return []
        method_ids = [ref.node_id for _, ref in methods]

        executable = await self._read_attribute(method_ids, ua.AttributeIds.Executable)
        user_executable = await self._read_attribute(method_ids, ua.AttributeIds.UserExecutable)

        argument_nodes = await self._argument_nodes(method_ids)
        flat: list[tuple[int, str, ua.NodeId]] = [
            (method_position, role, node_id)
            for method_position, nodes in enumerate(argument_nodes)
            for role, node_id in nodes
        ]
        argument_values = (
            await self._read_attribute([node_id for _, _, node_id in flat], ua.AttributeIds.Value)
            if flat
            else []
        )
        data_type_names = await self._resolve_data_type_names(argument_values)
        by_method: dict[int, list[tuple[str, int]]] = {}
        for value_index, (method_position, role, _) in enumerate(flat):
            by_method.setdefault(method_position, []).append((role, value_index))

        descriptors: list[MethodDescriptor] = []
        for position, (parent, ref) in enumerate(methods):
            inputs: list[ArgumentSpec] = []
            outputs: list[ArgumentSpec] = []
            for role, value_index in by_method.get(position, []):
                if value_index >= len(argument_values):
                    continue
                raw = unwrap(argument_values[value_index].Value)
                if not isinstance(raw, list):
                    continue
                target = inputs if role == "InputArguments" else outputs
                for argument_index, argument in enumerate(raw):
                    target.append(
                        _argument_spec(
                            argument,
                            data_type_names,
                            role=role,
                            position=argument_index,
                        )
                    )
            descriptors.append(
                MethodDescriptor(
                    node_id=ref.node_id,
                    parent_node_id=parent,
                    browse_name=ref.name,
                    display_name=ref.display_name or ref.name,
                    inputs=tuple(inputs),
                    outputs=tuple(outputs),
                    executable=bool(unwrap(executable[position].Value))
                    if position < len(executable)
                    else False,
                    user_executable=bool(unwrap(user_executable[position].Value))
                    if position < len(user_executable)
                    else False,
                )
            )
        return descriptors

    async def _argument_nodes(
        self, method_ids: Sequence[ua.NodeId]
    ) -> list[list[tuple[str, ua.NodeId]]]:
        """Find InputArguments/OutputArguments nodes, keeping their declared role."""
        per_method: list[list[tuple[str, ua.NodeId]]] = [[] for _ in method_ids]
        for positions in chunked(list(range(len(method_ids))), self._limits.max_nodes_per_browse):
            descriptions = []
            for position in positions:
                description = ua.BrowseDescription()
                description.NodeId = method_ids[position]
                description.BrowseDirection = ua.BrowseDirection.Forward
                description.ReferenceTypeId = ua.NodeId(0, 0)
                description.IncludeSubtypes = True
                description.NodeClassMask = ua.NodeClass.Variable
                description.ResultMask = ua.BrowseResultMask.All
                descriptions.append(description)
            params = ua.BrowseParameters()
            params.View = ua.ViewDescription()
            params.RequestedMaxReferencesPerNode = 0
            params.NodesToBrowse = descriptions
            results = await self._client.uaclient.browse(params)
            for position, result in zip(positions, results, strict=True):
                if result.StatusCode.is_bad():
                    continue
                for reference in result.References:
                    role = str(reference.BrowseName.Name)
                    if role in _HAS_METHOD_ARGUMENTS:
                        per_method[position].append(
                            (
                                role,
                                ua.NodeId(
                                    reference.NodeId.Identifier,
                                    int(reference.NodeId.NamespaceIndex or 0),
                                ),
                            )
                        )
        return per_method

    async def _read_attribute(
        self, node_ids: Sequence[ua.NodeId], attribute: ua.AttributeIds
    ) -> list[ua.DataValue]:
        collected: list[ua.DataValue] = []
        for chunk in chunked(list(node_ids), self._limits.max_nodes_per_read):
            collected.extend(await self._client.uaclient.read_attributes(list(chunk), attribute))
        return collected

    async def _resolve_data_type_names(self, values: Sequence[ua.DataValue]) -> dict[str, str]:
        """Resolve unique argument data types in one Read call."""
        unique: dict[str, ua.NodeId] = {}
        for data_value in values:
            raw = unwrap(data_value.Value)
            if isinstance(raw, list):
                for argument in raw:
                    data_type = getattr(argument, "DataType", None)
                    if isinstance(data_type, ua.NodeId):
                        unique[format_node_id(data_type)] = data_type
        if not unique:
            return {}
        node_ids = list(unique.values())
        resolved: dict[str, str] = {}
        for chunk in chunked(node_ids, self._limits.max_nodes_per_read):
            results = await self._client.uaclient.read_attributes(
                list(chunk), ua.AttributeIds.BrowseName
            )
            for node_id, data_value in zip(chunk, results, strict=True):
                raw = unwrap(data_value.Value)
                resolved[format_node_id(node_id)] = str(
                    getattr(raw, "Name", None) or data_type_name(node_id)
                )
        return resolved


class MethodInvoker:
    """Call discovered methods with Python values, matched by argument name."""

    def __init__(
        self,
        client: Client,
        *,
        limits: OperationLimits | None = None,
        raise_on_error: bool = True,
    ) -> None:
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()
        self._raise_on_error = raise_on_error

    async def call(
        self,
        method: MethodDescriptor,
        arguments: Mapping[str, Any] | Sequence[Any] = (),
    ) -> CallResult:
        """Call one method.

        Args:
            method: A descriptor from :class:`MethodCatalog`.
            arguments: Keyword values matched to the declared argument names, or
                a positional sequence in declaration order.

        Raises:
            MethodNotFoundError: A required argument is missing or unknown.
            MethodCallError: The method returned a bad status and
                ``raise_on_error`` is set.
        """
        results = await self.call_many([(method, arguments)])
        return results[0]

    async def call_many(
        self,
        calls: Iterable[tuple[MethodDescriptor, Mapping[str, Any] | Sequence[Any]]],
    ) -> list[CallResult]:
        """Call many methods, chunked to ``MaxNodesPerMethodCall``."""
        call_list = list(calls)
        results: list[CallResult] = []
        for chunk in chunked(call_list, self._limits.max_nodes_per_method_call):
            requests: list[ua.CallMethodRequest] = []
            descriptors: list[MethodDescriptor] = []
            for method, arguments in chunk:
                try:
                    variants = _build_arguments(method, arguments)
                except ConfigurationError as exc:
                    raise MethodNotFoundError(str(exc)) from exc
                request = ua.CallMethodRequest(
                    ObjectId=method.parent_node_id,
                    MethodId=method.node_id,
                    InputArguments=variants,
                )
                requests.append(request)
                descriptors.append(method)
            started = time.monotonic()
            try:
                raw_results = await self._client.uaclient.call(requests)
            except (UaStatusCodeError, UaError) as exc:
                status = _status_from_exception(exc)
                elapsed = (time.monotonic() - started) * 1000.0
                results.extend(
                    CallResult(
                        method=method,
                        status=status,
                        outputs={},
                        input_argument_results=(),
                        elapsed_ms=elapsed,
                    )
                    for method in descriptors
                )
                if self._raise_on_error:
                    raise MethodCallError(
                        f"call request failed: {status.name}", status_code=status
                    ) from exc
                continue
            elapsed_total = (time.monotonic() - started) * 1000.0
            per_call = elapsed_total / max(1, len(descriptors))
            for method, raw in zip(descriptors, raw_results, strict=True):
                result = CallResult(
                    method=method,
                    status=raw.StatusCode,
                    outputs=_map_outputs(method, raw.OutputArguments),
                    input_argument_results=tuple(raw.InputArgumentResults),
                    elapsed_ms=per_call,
                )
                results.append(result)
                if self._raise_on_error and raw.StatusCode.is_bad():
                    raise MethodCallError(
                        f"{method.browse_name} returned {raw.StatusCode.name}"
                        + (
                            f" (rejected arguments: {', '.join(result.rejected_arguments)})"
                            if result.rejected_arguments
                            else ""
                        ),
                        status_code=raw.StatusCode,
                        input_argument_results=list(raw.InputArgumentResults),
                    )
        return results


def _build_arguments(
    method: MethodDescriptor, arguments: Mapping[str, Any] | Sequence[Any]
) -> list[ua.Variant]:
    """Convert Python values into typed variants in declaration order."""
    if isinstance(arguments, Mapping):
        unknown = set(arguments) - {spec.name for spec in method.inputs}
        if unknown:
            raise ConfigurationError(
                f"{method.browse_name} does not accept argument(s): {', '.join(sorted(unknown))}"
            )
        values: list[Any] = []
        for spec in method.inputs:
            if spec.name not in arguments:
                raise ConfigurationError(f"{method.browse_name} is missing argument {spec.name!r}")
            values.append(arguments[spec.name])
    else:
        values = list(arguments)
        if len(values) != len(method.inputs):
            raise ConfigurationError(
                f"{method.browse_name} expects {len(method.inputs)} positional arguments, "
                f"got {len(values)}"
            )

    variants: list[ua.Variant] = []
    for spec, value in zip(method.inputs, values, strict=True):
        variants.append(_variant_for(spec, value))
    return variants


def _variant_for(spec: ArgumentSpec, value: Any) -> ua.Variant:
    variant_type = variant_type_for_data_type(spec.data_type)
    if variant_type is None or spec.is_array:
        return ua.Variant(value)
    return ua.Variant(coerce_to_variant_type(value, variant_type), variant_type)


def _map_outputs(method: MethodDescriptor, outputs: Sequence[ua.Variant]) -> dict[str, Any]:
    """Name the method outputs using the declaration order."""
    named: dict[str, Any] = {}
    for index, variant in enumerate(outputs):
        name = method.outputs[index].name if index < len(method.outputs) else f"output_{index}"
        named[name] = unwrap(variant)
    return named


def _argument_spec(
    argument: Any,
    data_type_names: Mapping[str, str],
    *,
    role: str,
    position: int,
) -> ArgumentSpec:
    """Turn one server-side ``Argument`` into something a caller can use.

    Many real servers declare argument *types* but leave ``Name`` empty. Those
    arguments are still callable positionally, so we give them a stable fallback
    name instead of pretending they do not exist.
    """
    data_type = getattr(argument, "DataType", None) or ua.NodeId(ua.ObjectIds.BaseDataType, 0)
    key = format_node_id(data_type)
    declared = str(getattr(argument, "Name", "") or "").strip()
    fallback = f"{'input' if role == 'InputArguments' else 'output'}_{position + 1}"
    return ArgumentSpec(
        name=declared or fallback,
        data_type=data_type,
        data_type_name=data_type_names.get(key) or data_type_name(data_type),
        value_rank=int(getattr(argument, "ValueRank", -1) or -1),
        array_dimensions=tuple(
            int(size) for size in (getattr(argument, "ArrayDimensions", None) or ())
        ),
        description=str(getattr(getattr(argument, "Description", None), "Text", "") or ""),
    )


def _status_from_exception(exc: BaseException) -> ua.StatusCode:
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return ua.StatusCode(code)
    return ua.StatusCode(ua.StatusCodes.BadCommunicationError)


__all__ = [
    "ArgumentSpec",
    "CallResult",
    "MethodCatalog",
    "MethodDescriptor",
    "MethodInvoker",
]
