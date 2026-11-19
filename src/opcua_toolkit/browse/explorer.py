"""Navigate servers whose NodeIds you do not know.

A client that hardcodes ``ns=4;s=...`` breaks the moment the plant reboots, the
vendor ships a new build, or the customer renames a tag folder. Chapter 5 builds
the habit every later chapter depends on: *discover* the address space at
runtime with Browse, BrowseNext and TranslateBrowsePathsToNodeIds, and only then
read data.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from asyncua import Client, ua

from opcua_toolkit.browse.model import BrowseStats, BrowseTree, NodeRef
from opcua_toolkit.errors import BrowsePathError, NodeNotAccessibleError, format_node_id
from opcua_toolkit.limits import OperationLimits, chunked
from opcua_toolkit.nodeids import NodeIdLike, to_nodeid
from opcua_toolkit.values import as_node_class, as_qualified_name, unwrap

_logger = logging.getLogger(__name__)

_HIERARCHICAL = ua.NodeId(ua.ObjectIds.HierarchicalReferences, 0)
_NO_REFERENCE = ua.NodeId(0, 0)


class NamespaceExplorer:
    """Explore any OPC UA server safely, respecting its browse limits.

    Args:
        client: A connected asyncua client.
        limits: Server operation limits. When omitted, conservative defaults
            apply - the client still chunks, it just assumes less.
    """

    def __init__(self, client: Client, *, limits: OperationLimits | None = None) -> None:
        self._client = client
        self._limits = limits if limits is not None else OperationLimits()

    @property
    def limits(self) -> OperationLimits:
        """The limits this explorer chunks its browse requests to."""
        return self._limits

    async def namespaces(self) -> list[tuple[int, str]]:
        """Return ``(index, uri)`` pairs from the server NamespaceArray."""
        results = await self._client.uaclient.read_attributes(
            [ua.NodeId(ua.ObjectIds.Server_NamespaceArray, 0)], ua.AttributeIds.Value
        )
        value = unwrap(results[0].Value)
        if value is None:
            return []
        return list(enumerate((str(uri) for uri in value), start=0))

    async def children(
        self,
        node: NodeIdLike,
        *,
        reference_type: NodeIdLike | None = _HIERARCHICAL,
        node_class: ua.NodeClass = ua.NodeClass.Unspecified,
        includesubtypes: bool = True,
    ) -> list[NodeRef]:
        """Browse the direct children of one node.

        Args:
            node: Node to browse.
            reference_type: Reference to follow, e.g. ``ua.ObjectIds.Organizes``
                for folders or ``ua.ObjectIds.HasComponent`` for components.
                Defaults to HierarchicalReferences, which covers Organizes,
                HasComponent and HasProperty.
            node_class: Restrict the result to one node class, e.g. ``Variable``.
            includesubtypes: Also follow subtypes of ``reference_type``.
        """
        groups = await self._browse_groups(
            [to_nodeid(node)],
            reference_type=reference_type,
            node_class=node_class,
            includesubtypes=includesubtypes,
            stats=BrowseStats(),
        )
        return groups[0] if groups else []

    async def walk(
        self,
        root: NodeIdLike,
        *,
        max_depth: int = 3,
        max_nodes: int = 5_000,
        reference_type: NodeIdLike | None = _HIERARCHICAL,
        node_class: ua.NodeClass = ua.NodeClass.Unspecified,
    ) -> tuple[BrowseTree, BrowseStats]:
        """Explore a subtree breadth first and return the tree plus its cost.

        Level batching is what makes a namespace scan affordable: one Browse
        call carries up to ``MaxNodesPerBrowse`` nodes instead of one call per
        node. Chapter 7 quantifies the difference.

        Args:
            root: Starting node, typically ``ua.ObjectIds.ObjectsFolder``.
            max_depth: Levels below the root. ``0`` returns only the root;
                negative means unlimited within ``max_nodes``.
            max_nodes: Hard stop so a huge server cannot hang the client.
            reference_type: Reference type to follow.
            node_class: Server-side ``ResultNodeClass`` filter. It prunes the
                traversal: the server only sends children of that class, so a
                walk that asks for ``Variable`` stops at the first Object level
                and finds nothing below it. Use it on a folder you already know
                holds only that class; use :meth:`walk_flat` to collect a class
                from deeper levels.

        Returns:
            ``(tree, stats)`` where ``tree.node`` is the root itself and
            ``stats.nodes_visited`` counts the root plus everything captured.
            ``stats.stopped_reason`` explains an early stop.
        """
        stats = BrowseStats()
        root_id = to_nodeid(root)
        # The root is not part of any Browse result - no reference points at it from
        # here - so its identity comes from one small Read. Without it the tree has no
        # name, and "ProductionLine" disappears from the very report you print.
        tree = BrowseTree(node=await self.describe_node(root_id))
        stats.nodes_visited = 1
        if max_depth == 0:
            stats.stopped_reason = "max_depth=0: only the root was captured"
            return tree, stats
        root_group = await self._browse_groups(
            [root_id],
            reference_type=reference_type,
            node_class=node_class,
            includesubtypes=True,
            stats=stats,
        )
        root_children = root_group[0] if root_group else []
        tree.children.extend(BrowseTree(node=child) for child in root_children)
        stats.nodes_visited += len(root_children)
        if not root_children:
            stats.stopped_reason = f"root {format_node_id(root_id)} has no children"
            return tree, stats

        frontier = tree.children
        depth = 1
        while frontier and (max_depth < 0 or depth < max_depth):
            if stats.nodes_visited >= max_nodes:
                stats.stopped_reason = f"max_nodes={max_nodes} reached"
                break
            branches: list[tuple[BrowseTree, NodeRef]] = []
            for branch in frontier:
                node = branch.node
                if node is not None:
                    branches.append((branch, node))
            batch: list[BrowseTree] = []
            for index_group in chunked(
                list(range(len(branches))), self._limits.max_nodes_per_browse
            ):
                node_ids = [branches[index][1].node_id for index in index_group]
                groups = await self._browse_groups(
                    node_ids,
                    reference_type=reference_type,
                    node_class=node_class,
                    includesubtypes=True,
                    stats=stats,
                )
                for index, children in zip(index_group, groups, strict=True):
                    parent = branches[index][0]
                    for child in children:
                        if stats.nodes_visited >= max_nodes:
                            stats.stopped_reason = f"max_nodes={max_nodes} reached"
                            break
                        child_branch = BrowseTree(node=child)
                        parent.children.append(child_branch)
                        batch.append(child_branch)
                        stats.nodes_visited += 1
            frontier = batch
            depth += 1
        if stats.stopped_reason is None and stats.nodes_visited >= max_nodes:
            stats.stopped_reason = f"max_nodes={max_nodes} reached"
        return tree, stats

    async def walk_flat(
        self,
        root: NodeIdLike,
        *,
        max_nodes: int = 5_000,
        reference_type: NodeIdLike | None = _HIERARCHICAL,
        node_class: ua.NodeClass = ua.NodeClass.Unspecified,
    ) -> tuple[list[NodeRef], BrowseStats]:
        """Flat list of everything below ``root`` - the bulk subscribe use case.

        ``node_class`` decides what the **caller keeps**, not what the server
        sends: this walk always browses every level. That distinction is the
        whole difficulty of a namespace scan. The server-side filter
        (:meth:`children`) makes one browse cheap but prunes the traversal -
        browse ``ObjectsFolder`` for ``Variable`` and the answer is nothing,
        because its children are Objects and the variables you want sit below
        them. Here the levels are walked unfiltered and the class filter runs on
        the way home, which is also why ``stats.nodes_visited`` can be larger
        than the list you get back.
        """
        tree, stats = await self.walk(
            root,
            max_depth=-1,
            max_nodes=max_nodes,
            reference_type=reference_type,
        )
        if node_class == ua.NodeClass.Unspecified:
            return list(tree.walk()), stats
        return [ref for ref in tree.walk() if ref.node_class == node_class], stats

    async def find_by_browse_path(
        self,
        path: Sequence[str],
        *,
        root: NodeIdLike | None = None,
    ) -> NodeRef:
        """Resolve a human readable path such as ``Line1/Temperature``.

        Matching is on BrowseName only, ignoring namespace indexes. That costs
        one Browse per level, but works on servers whose namespace index you do
        not know yet - the situation in Chapter 13 and Chapter 23. When you
        already know the namespaces, use
        :meth:`resolve_path_with_translate`, which needs a single roundtrip.

        Raises:
            BrowsePathError: A segment of the path does not exist.
        """
        if not path:
            raise BrowsePathError("Empty browse path")
        current = to_nodeid(root) if root is not None else ua.NodeId(ua.ObjectIds.ObjectsFolder, 0)
        resolved: NodeRef | None = None
        for index, segment in enumerate(path):
            children = await self.children(current)
            match = next((child for child in children if child.name == segment), None)
            if match is None:
                raise BrowsePathError(
                    f"No node named {segment!r} under {format_node_id(current)} "
                    f"(segment {index + 1} of {len(path)})",
                    path=tuple(path),
                )
            current = match.node_id
            resolved = match
        assert resolved is not None
        return resolved

    async def resolve_path_with_translate(
        self,
        path: Sequence[ua.QualifiedName | str],
        *,
        root: NodeIdLike | None = None,
        namespace_index: int | None = None,
    ) -> NodeRef:
        """Resolve a fully qualified path with one TranslateBrowsePathsToNodeIds call.

        Use this once Chapter 13 has discovered the namespace indexes: it is the
        cheapest way to reach a known path, and the preferred approach in
        companion-specification clients (Chapter 23).

        Args:
            path: :class:`~asyncua.ua.QualifiedName` segments, or plain names paired
                with ``namespace_index``.
            root: Starting node. Defaults to the ObjectsFolder.
            namespace_index: Namespace index of the plain string segments. It defaults
                to the starting node's own index, because a BrowseName is matched
                *including* its namespace: asking for ``ns=0:Temperature`` under an
                ``ns=2`` folder fails even when the tag is right there.

        Raises:
            BrowsePathError: The server could not resolve the path.
        """
        if not path:
            raise BrowsePathError("Empty browse path")
        start = to_nodeid(root) if root is not None else ua.NodeId(ua.ObjectIds.ObjectsFolder, 0)
        index = start.NamespaceIndex if namespace_index is None else namespace_index
        elements = [
            ua.RelativePathElement(
                ReferenceTypeId=_HIERARCHICAL,
                # asyncua's RelativePathElement defaults IsInverse to True, which means
                # "follow the reference backwards from the target". Left alone, it turns a
                # perfectly good path into BadBrowsePathNoMatch. Always say the direction.
                IsInverse=False,
                IncludeSubtypes=True,
                TargetName=(
                    ua.QualifiedName(segment, index) if isinstance(segment, str) else segment
                ),
            )
            for segment in path
        ]
        # RelativePath is a struct with an Elements list, not the list itself: passing a
        # plain list fails in the serializer with an error that says nothing about paths.
        browse_path = ua.BrowsePath(
            StartingNode=start, RelativePath=ua.RelativePath(Elements=elements)
        )
        results = await self._client.uaclient.translate_browsepaths_to_nodeids([browse_path])
        result = results[0]
        target_text = ", ".join(_path_segment_text(segment) for segment in path)
        if result.StatusCode.is_bad() or not result.Targets:
            raise BrowsePathError(
                f"TranslateBrowsePathsToNodeIds returned {result.StatusCode.name} "
                f"for path {target_text}",
                path=tuple(target_text.split(" / ")),
            )
        target = result.Targets[0].TargetId
        node_id = ua.NodeId(target.Identifier, int(target.NamespaceIndex or 0))
        return await self.describe_node(node_id, browse_name=_target_name(path[-1]))

    async def describe_node(self, node: NodeIdLike, *, browse_name: str | None = None) -> NodeRef:
        """Read the identification attributes of a single node in one Read call.

        ``Read`` accepts many ``(node, attribute)`` pairs, so NodeClass, BrowseName
        and DisplayName arrive in a single request. Reading them one by one is how a
        "simple" lookup turns into three roundtrips per node.
        """
        node_id = to_nodeid(node)
        attributes = (
            ua.AttributeIds.NodeClass,
            ua.AttributeIds.BrowseName,
            ua.AttributeIds.DisplayName,
        )
        results = await self._read_pairs([(node_id, attribute) for attribute in attributes])
        node_class_value = unwrap(results[0].Value)
        if results[0].StatusCode is not None and results[0].StatusCode.is_bad():
            raise NodeNotAccessibleError(
                f"Node {format_node_id(node_id)}: {results[0].StatusCode.name}",
                node_id=node_id,
            )
        browse_raw = unwrap(results[1].Value)
        display_raw = unwrap(results[2].Value)
        return NodeRef(
            node_id=node_id,
            browse_name=as_qualified_name(browse_raw, default_namespace=node_id.NamespaceIndex)
            if browse_raw is not None
            else ua.QualifiedName(browse_name or "", node_id.NamespaceIndex),
            display_name=str(getattr(display_raw, "Text", display_raw or "") or browse_name or ""),
            node_class=as_node_class(node_class_value),
            type_definition=None,
            reference_type=None,
        )

    async def _read_pairs(
        self, pairs: Sequence[tuple[ua.NodeId, ua.AttributeIds]]
    ) -> list[ua.DataValue]:
        """Read many ``(node, attribute)`` pairs, chunked to ``MaxNodesPerRead``."""
        collected: list[ua.DataValue] = []
        for chunk in chunked(list(pairs), self._limits.max_nodes_per_read):
            params = ua.ReadParameters()
            for node_id, attribute in chunk:
                item = ua.ReadValueId()
                item.NodeId = node_id
                item.AttributeId = attribute
                item.IndexRange = ""
                params.NodesToRead.append(item)
            collected.extend(await self._client.uaclient.read(params))
        return collected

    async def _browse_groups(
        self,
        node_ids: Sequence[ua.NodeId],
        *,
        reference_type: NodeIdLike | None,
        node_class: ua.NodeClass,
        includesubtypes: bool,
        stats: BrowseStats,
    ) -> list[list[NodeRef]]:
        """Browse many nodes per call and keep results aligned with the input."""
        groups: list[list[NodeRef]] = [[] for _ in node_ids]
        for index_chunk in chunked(list(range(len(node_ids))), self._limits.max_nodes_per_browse):
            descriptions = [
                _browse_description(
                    node_ids[index], reference_type, node_class, includesubtypes=includesubtypes
                )
                for index in index_chunk
            ]
            params = ua.BrowseParameters()
            params.View = ua.ViewDescription()
            params.RequestedMaxReferencesPerNode = 0
            params.NodesToBrowse = descriptions
            results = await self._client.uaclient.browse(params)
            stats.browse_calls += 1
            for position, result in enumerate(results):
                target_index = index_chunk[position] if position < len(index_chunk) else -1
                if result.StatusCode.is_bad():
                    stats.errors += 1
                    _logger.warning(
                        "browse of %s returned %s",
                        format_node_id(node_ids[target_index]) if target_index >= 0 else "?",
                        result.StatusCode.name,
                    )
                    continue
                collected = groups[target_index] if target_index >= 0 else []
                for description in result.References:
                    collected.append(NodeRef.from_description(description))
                    stats.references_seen += 1
                collected.extend(await self._drain_continuations(result, stats))
        return groups

    async def _drain_continuations(
        self, result: ua.BrowseResult, stats: BrowseStats
    ) -> list[NodeRef]:
        """Follow a node's continuation points and release the one we stopped on."""
        refs: list[NodeRef] = []
        point = result.ContinuationPoint
        unreleased: ua.ByteString | None = None
        while point:
            stats.continuation_points += 1
            params = ua.BrowseNextParameters()
            params.ReleaseContinuationPoints = False
            params.ContinuationPoints = [point]
            results = await self._client.uaclient.browse_next(params)
            stats.browse_next_calls += 1
            next_result = results[0]
            if next_result.StatusCode.is_bad():
                stats.errors += 1
                _logger.warning("browse_next returned %s", next_result.StatusCode.name)
                unreleased = point
                break
            refs.extend(
                NodeRef.from_description(description) for description in next_result.References
            )
            stats.references_seen += len(next_result.References)
            point = next_result.ContinuationPoint
        if unreleased is not None:
            release = ua.BrowseNextParameters()
            release.ReleaseContinuationPoints = True
            release.ContinuationPoints = [unreleased]
            await self._client.uaclient.browse_next(release)
        return refs


def _target_name(segment: ua.QualifiedName | str) -> str:
    return segment if isinstance(segment, str) else segment.Name


def _path_segment_text(segment: ua.QualifiedName | str) -> str:
    """``ProductionLine`` or ``2:ProductionLine`` for error messages."""
    if isinstance(segment, str):
        return segment
    return f"{segment.NamespaceIndex}:{segment.Name}"


def _browse_description(
    node_id: ua.NodeId,
    reference_type: NodeIdLike | None,
    node_class: ua.NodeClass,
    *,
    includesubtypes: bool,
) -> ua.BrowseDescription:
    description = ua.BrowseDescription()
    description.NodeId = node_id
    description.BrowseDirection = ua.BrowseDirection.Forward
    description.ReferenceTypeId = to_nodeid(reference_type) if reference_type else _NO_REFERENCE
    description.IncludeSubtypes = includesubtypes
    description.NodeClassMask = node_class
    description.ResultMask = ua.BrowseResultMask.All
    return description


__all__ = ["NamespaceExplorer"]
