"""Deterministic, in-memory structural queries over an already-loaded Graph.

    Graph (loaded once, e.g. via graph/storage.py) -> QueryEngine(graph) -> queries

Every operation here reads only the Graph object given to the constructor.
Nothing in this module touches the filesystem, the network, or a source
repository — once a QueryEngine exists, the repository it was built from
never needs to be looked at again. This is deliberate: the point is to let
a caller (eventually Claude Code) answer "what's relevant here?" from graph
data alone, before deciding whether to read any actual source.

Determinism
-----------
All indexes are built once in ``__init__`` with explicit sorting (never
relying on dict/set iteration order or Python's hash randomization), and
every query result is sorted before being returned:

  * node lists: by ``Node.id``
  * edge/relationship lists: by ``graph.model.edge_sort_key`` (type,
    source_id, target_id, target_name, location)
  * neighborhood results: by ``(depth reached, node.id)``

Two QueryEngines built from graphs that describe the same code but were
constructed/inserted in a different order always return identical results
for identical queries.

Relationship policies
----------------------
Several operations name an *explicit* set of edge types they consider,
rather than "all edges" — see each method's docstring, and in particular:

  * ``children()``: CONTAINS + DECLARES (structural nesting/declaration).
  * ``dependencies()``/``dependents()``/``affected()``: IMPORTS + EXTENDS +
    IMPLEMENTS (``DEPENDENCY_EDGE_TYPES``) — a *structural* notion of
    "depends on", built entirely from syntactically-explicit relationships
    already in the graph. This is not compiler-level or runtime dependency
    analysis; CALLS/REFERENCES edges don't exist in this graph model (see
    graph/model.py), so no version of "depends on" here can see method
    calls, only imports/extends/implements.
  * ``neighbors()``: every edge type, both directions — the deliberately
    unopinionated "what's around this node" query.
  * ``affected()``: the same DEPENDENCY_EDGE_TYPES policy as
    dependencies(), traversed transitively *backwards* (who points at this
    node, and who points at those, ...). This is structural graph
    reachability — "what else references this, directly or indirectly" —
    not a claim about actual runtime or build impact.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from local_code_graph.graph.model import (
    Edge,
    EdgeType,
    Graph,
    Node,
    NodeType,
    edge_sort_key,
)
from local_code_graph.query.results import (
    FindResult,
    MemberSummary,
    NeighborEdge,
    NeighborhoodResult,
    OverviewResult,
    PathEdge,
    PathResult,
    RelatedNode,
    RelationshipSummary,
    SummaryResult,
)
from local_code_graph.scanner import EXTENSION_LANGUAGE_MAP

CONTAINMENT_EDGE_TYPES = (EdgeType.CONTAINS, EdgeType.DECLARES)
"""Edge types considered by children()/summarize()'s "members"."""

DEPENDENCY_EDGE_TYPES = (
    EdgeType.IMPORTS,
    EdgeType.EXTENDS,
    EdgeType.IMPLEMENTS,
    EdgeType.REFERENCES,
)
"""Edge types considered "a structural dependency" by dependencies()/
dependents()/affected() — see the module docstring's "Relationship
policies" section for exactly what that does and doesn't mean."""

_DEFAULT_PATH_MAX_DEPTH = 6

_TYPE_LABELS: dict[NodeType, str] = {
    NodeType.FILE: "Files",
    NodeType.PACKAGE: "Packages",
    NodeType.CLASS: "Classes",
    NodeType.INTERFACE: "Interfaces",
    NodeType.ENUM: "Enums",
    NodeType.ANNOTATION: "Annotations",
    NodeType.OBJECT: "Objects",
    NodeType.METHOD: "Methods",
    NodeType.CONSTRUCTOR: "Constructors",
    NodeType.FIELD: "Fields",
    NodeType.PROPERTY: "Properties",
    NodeType.PARAMETER: "Parameters",
    NodeType.TABLE: "Tables",
    NodeType.COLUMN: "Columns",
    NodeType.VIEW: "Views",
    NodeType.INDEX: "Indexes",
    NodeType.TRIGGER: "Triggers",
}


class QueryError(Exception):
    """Base class for all query-engine errors."""


class NodeNotFoundError(QueryError):
    """A node ID passed directly to a query (not a find() search) doesn't
    exist in this graph."""


class InvalidQueryError(QueryError):
    """The query itself is malformed (e.g. an empty search string)."""


class InvalidDepthError(QueryError):
    """A depth/max_depth argument was negative."""


class QueryEngine:
    """Structural queries over one in-memory Graph.

    All indexes are built once here; every query method afterwards is a
    lookup/traversal over those indexes, not a fresh scan of the graph.
    """

    def __init__(self, graph: Graph) -> None:
        self.graph = graph

        self._by_name: dict[str, list[Node]] = {}
        self._by_qualified_name: dict[str, list[Node]] = {}
        self._by_type: dict[NodeType, list[Node]] = {}
        self._by_file: dict[str, list[Node]] = {}

        for node in sorted(graph.nodes.values(), key=lambda n: n.id):
            self._by_name.setdefault(node.name, []).append(node)
            if node.qualified_name:
                self._by_qualified_name.setdefault(node.qualified_name, []).append(node)
            self._by_type.setdefault(node.type, []).append(node)
            if node.file:
                self._by_file.setdefault(node.file, []).append(node)

        self._outgoing: dict[str, list[Edge]] = {}
        self._incoming: dict[str, list[Edge]] = {}
        for edge in sorted(graph.edges, key=edge_sort_key):
            self._outgoing.setdefault(edge.source_id, []).append(edge)
            if edge.target_id is not None:
                self._incoming.setdefault(edge.target_id, []).append(edge)

    # -- node resolution -----------------------------------------------------------

    def _resolve(self, node_or_id) -> Node:
        node_id = node_or_id.id if isinstance(node_or_id, Node) else node_or_id
        node = self.graph.nodes.get(node_id)
        if node is None:
            raise NodeNotFoundError(f"no node with id {node_id!r} in this graph")
        return node

    # -- find ------------------------------------------------------------------------

    def find(self, query: str) -> FindResult:
        """Find nodes by exact ID, then exact qualified name, then exact
        simple name — the first of these three tiers with any match wins;
        later tiers are not consulted. No fuzzy matching is ever performed.

        Returns every match at the winning tier (possibly more than one,
        e.g. two same-named classes in different packages) rather than
        guessing which one the caller meant — check ``.is_ambiguous``.
        """
        if not isinstance(query, str) or not query.strip():
            raise InvalidQueryError("find() query must be a non-empty string")

        direct = self.graph.nodes.get(query)
        if direct is not None:
            return FindResult(query=query, matches=(direct,))

        by_qn = self._by_qualified_name.get(query)
        if by_qn:
            return FindResult(query=query, matches=tuple(by_qn))

        by_name = self._by_name.get(query)
        if by_name:
            return FindResult(query=query, matches=tuple(by_name))

        return FindResult(query=query, matches=())

    def find_by_type(self, node_type: NodeType) -> tuple[Node, ...]:
        return tuple(self._by_type.get(node_type, ()))

    def find_by_file(self, file: str) -> tuple[Node, ...]:
        """Every node whose ``Node.file`` equals ``file`` exactly (a
        repository-relative path, matching how paths are stored — see
        graph/model.py), sorted by (start_line, id)."""
        nodes = self._by_file.get(file, ())
        return tuple(sorted(nodes, key=lambda n: (n.start_line or 0, n.id)))

    # -- relationship helpers ---------------------------------------------------------

    def _outgoing_of_types(self, node_id: str, types: Iterable[EdgeType]) -> list[Edge]:
        wanted = set(types)
        return [e for e in self._outgoing.get(node_id, ()) if e.type in wanted]

    def _incoming_of_types(self, node_id: str, types: Iterable[EdgeType]) -> list[Edge]:
        wanted = set(types)
        return [e for e in self._incoming.get(node_id, ()) if e.type in wanted]

    def _wrap_outgoing(self, edges: list[Edge]) -> tuple[RelatedNode, ...]:
        return tuple(
            RelatedNode(
                edge_type=e.type,
                node=self.graph.nodes.get(e.target_id) if e.target_id else None,
                target_name=e.target_name,
            )
            for e in edges
        )

    def _wrap_incoming(self, edges: list[Edge]) -> tuple[RelatedNode, ...]:
        return tuple(
            RelatedNode(
                edge_type=e.type,
                node=self.graph.nodes.get(e.source_id),
                target_name=e.target_name,
            )
            for e in edges
        )

    # -- children / members -----------------------------------------------------------

    def children(self, node_or_id) -> tuple[RelatedNode, ...]:
        """This node's CONTAINS and DECLARES children, combined and sorted.

        Both relationship types are included since they're both "structural
        children" of a node; each result's ``edge_type`` tells you which one
        it was (CONTAINS = physical/namespace nesting, DECLARES = "this
        type/method declares this member" — see graph/model.py).
        """
        node = self._resolve(node_or_id)
        return self._wrap_outgoing(self._outgoing_of_types(node.id, CONTAINMENT_EDGE_TYPES))

    # -- imports ------------------------------------------------------------------------

    def imports(self, node_or_id) -> tuple[RelatedNode, ...]:
        node = self._resolve(node_or_id)
        return self._wrap_outgoing(self._outgoing_of_types(node.id, (EdgeType.IMPORTS,)))

    def imported_by(self, node_or_id) -> tuple[RelatedNode, ...]:
        node = self._resolve(node_or_id)
        return self._wrap_incoming(self._incoming_of_types(node.id, (EdgeType.IMPORTS,)))

    # -- inheritance ----------------------------------------------------------------------

    def extends(self, node_or_id) -> tuple[RelatedNode, ...]:
        node = self._resolve(node_or_id)
        return self._wrap_outgoing(self._outgoing_of_types(node.id, (EdgeType.EXTENDS,)))

    def extended_by(self, node_or_id) -> tuple[RelatedNode, ...]:
        node = self._resolve(node_or_id)
        return self._wrap_incoming(self._incoming_of_types(node.id, (EdgeType.EXTENDS,)))

    def implements(self, node_or_id) -> tuple[RelatedNode, ...]:
        node = self._resolve(node_or_id)
        return self._wrap_outgoing(self._outgoing_of_types(node.id, (EdgeType.IMPLEMENTS,)))

    def implemented_by(self, node_or_id) -> tuple[RelatedNode, ...]:
        node = self._resolve(node_or_id)
        return self._wrap_incoming(self._incoming_of_types(node.id, (EdgeType.IMPLEMENTS,)))

    # -- dependency-oriented summaries ------------------------------------------------------

    def dependencies(self, node_or_id) -> tuple[RelatedNode, ...]:
        """Direct (depth-1) structural dependencies of this node: its
        outgoing IMPORTS/EXTENDS/IMPLEMENTS edges (DEPENDENCY_EDGE_TYPES).
        Not runtime or build dependency analysis — see the module
        docstring."""
        node = self._resolve(node_or_id)
        return self._wrap_outgoing(self._outgoing_of_types(node.id, DEPENDENCY_EDGE_TYPES))

    def dependents(self, node_or_id) -> tuple[RelatedNode, ...]:
        """Direct (depth-1) structural dependents of this node: nodes whose
        outgoing IMPORTS/EXTENDS/IMPLEMENTS edge points at this node."""
        node = self._resolve(node_or_id)
        return self._wrap_incoming(self._incoming_of_types(node.id, DEPENDENCY_EDGE_TYPES))

    # -- affected (structural reachability) --------------------------------------------------

    def affected(self, node_or_id, depth: int) -> NeighborhoodResult:
        """Nodes structurally reachable from this node by transitively
        following DEPENDENCY_EDGE_TYPES edges *backwards* (dependents of
        dependents, up to ``depth`` hops) — i.e. "what else, directly or
        indirectly, imports/extends/implements this".

        This is graph reachability over the relationships this tool
        happens to have extracted (imports/extends/implements). It is
        **not** compiler-level or runtime impact analysis: it cannot see
        method calls (this graph model has no CALLS/REFERENCES edges —
        see graph/model.py), reflection, dependency injection, or anything
        not syntactically visible as an import/extends/implements clause.
        """
        node = self._resolve(node_or_id)
        return self._bfs_neighborhood(
            node,
            depth=depth,
            edge_types=DEPENDENCY_EDGE_TYPES,
            direction="incoming",
        )

    # -- neighbors ------------------------------------------------------------------------

    def neighbors(self, node_or_id, depth: int = 1) -> NeighborhoodResult:
        """Every node reachable within ``depth`` hops via *any* edge type,
        in *either* direction — the general-purpose "what's around this
        node" query. Use dependencies()/dependents()/affected() instead
        when you specifically want the narrower IMPORTS/EXTENDS/IMPLEMENTS
        policy.
        """
        node = self._resolve(node_or_id)
        return self._bfs_neighborhood(node, depth=depth, edge_types=None, direction="both")

    def _bfs_neighborhood(
        self,
        start: Node,
        *,
        depth: int,
        edge_types: tuple[EdgeType, ...] | None,
        direction: str,
    ) -> NeighborhoodResult:
        if depth < 0:
            raise InvalidDepthError(f"depth must be >= 0, got {depth}")

        visited_depth: dict[str, int] = {start.id: 0}
        collected_edges: dict[tuple[str, str, str], NeighborEdge] = {}
        queue: deque[tuple[str, int]] = deque([(start.id, 0)])

        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            candidate_edges: list[Edge] = []
            if direction in ("outgoing", "both"):
                edges = self._outgoing.get(current_id, ())
                candidate_edges.extend(e for e in edges if edge_types is None or e.type in edge_types)
            if direction in ("incoming", "both"):
                edges = self._incoming.get(current_id, ())
                candidate_edges.extend(e for e in edges if edge_types is None or e.type in edge_types)

            for edge in sorted(candidate_edges, key=edge_sort_key):
                other_id = edge.target_id if edge.source_id == current_id else edge.source_id
                if other_id is None or other_id not in self.graph.nodes:
                    continue
                # edge.target_id is guaranteed non-None here: outgoing edges
                # with an unresolved (None) target were already filtered out
                # above, and self._incoming only ever holds edges that do
                # have a target_id (see __init__).
                key = (edge.type.value, edge.source_id, edge.target_id)
                collected_edges[key] = NeighborEdge(edge.type, edge.source_id, edge.target_id)

                if other_id not in visited_depth:
                    visited_depth[other_id] = current_depth + 1
                    queue.append((other_id, current_depth + 1))

        neighbor_ids = [nid for nid in visited_depth if nid != start.id]
        neighbor_ids.sort(key=lambda nid: (visited_depth[nid], nid))
        nodes = tuple(self.graph.nodes[nid] for nid in neighbor_ids)

        neighbor_id_set = set(neighbor_ids) | {start.id}
        edges = tuple(
            sorted(
                (
                    e
                    for e in collected_edges.values()
                    if e.source_id in neighbor_id_set and e.target_id in neighbor_id_set
                ),
                key=lambda e: (e.type.value, e.source_id, e.target_id),
            )
        )

        return NeighborhoodResult(center=start, depth=depth, nodes=nodes, edges=edges)

    # -- path finding -------------------------------------------------------------------

    def path(self, source_or_id, target_or_id, *, max_depth: int = _DEFAULT_PATH_MAX_DEPTH) -> PathResult:
        """Shortest chain of relationships connecting ``source`` and
        ``target``, considering the graph as **undirected** for
        connectivity — any edge (regardless of which end is source/target)
        can be walked in either direction. Each hop's ``PathEdge.reversed``
        records whether that hop went against the edge's natural
        source->target direction, so a caller can still render the true
        direction of each relationship.

        This is a graph-relationship path, not a runtime execution or call
        path — see the module docstring.

        Returns ``PathResult(found=False, nodes=(), edges=())`` if no path
        exists within ``max_depth`` hops. Cycle-safe (visited-set BFS).
        """
        if max_depth < 0:
            raise InvalidDepthError(f"max_depth must be >= 0, got {max_depth}")
        source = self._resolve(source_or_id)
        target = self._resolve(target_or_id)

        if source.id == target.id:
            return PathResult(source=source, target=target, found=True, nodes=(source,), edges=())

        came_from: dict[str, tuple[str, Edge, bool]] = {}
        visited = {source.id}
        queue: deque[tuple[str, int]] = deque([(source.id, 0)])

        found_target = False
        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= max_depth:
                continue

            neighbors_edges: list[tuple[Edge, bool]] = []
            neighbors_edges.extend((e, False) for e in self._outgoing.get(current_id, ()))
            neighbors_edges.extend((e, True) for e in self._incoming.get(current_id, ()))
            neighbors_edges.sort(key=lambda pair: edge_sort_key(pair[0]))

            for edge, is_reversed in neighbors_edges:
                other_id = edge.source_id if is_reversed else edge.target_id
                if other_id is None or other_id in visited or other_id not in self.graph.nodes:
                    continue
                visited.add(other_id)
                came_from[other_id] = (current_id, edge, is_reversed)
                if other_id == target.id:
                    found_target = True
                    break
                queue.append((other_id, current_depth + 1))
            if found_target:
                break

        if not found_target:
            return PathResult(source=source, target=target, found=False, nodes=(), edges=())

        chain_ids: list[str] = [target.id]
        chain_edges: list[PathEdge] = []
        current = target.id
        while current != source.id:
            prev_id, edge, is_reversed = came_from[current]
            chain_edges.append(
                PathEdge(type=edge.type, from_id=prev_id, to_id=current, reversed=is_reversed)
            )
            chain_ids.append(prev_id)
            current = prev_id
        chain_ids.reverse()
        chain_edges.reverse()

        nodes = tuple(self.graph.nodes[nid] for nid in chain_ids)
        return PathResult(source=source, target=target, found=True, nodes=nodes, edges=tuple(chain_edges))

    # -- summaries -------------------------------------------------------------------------

    def summarize(self, node_or_id) -> SummaryResult:
        """A compact structural summary of one node: its CONTAINS/DECLARES
        children (as ``members``) and its outgoing IMPORTS/EXTENDS/
        IMPLEMENTS edges (as ``relationships``). Never includes source
        code — see ``format_summary`` for a human-readable rendering.
        """
        node = self._resolve(node_or_id)
        member_edges = self._wrap_outgoing(
            self._outgoing_of_types(node.id, CONTAINMENT_EDGE_TYPES)
        )
        members = tuple(
            MemberSummary(
                node_type=m.node.type,
                name=m.node.name,
                signature=_format_member_signature(m.node),
                start_line=m.node.start_line,
                end_line=m.node.end_line,
            )
            for m in member_edges
            if m.node is not None
        )

        rel_edges = self._outgoing_of_types(
            node.id, (EdgeType.IMPORTS, EdgeType.EXTENDS, EdgeType.IMPLEMENTS)
        )
        relationships = tuple(
            RelationshipSummary(type=e.type, target_name=e.target_name, resolved=e.target_id is not None)
            for e in rel_edges
        )

        return SummaryResult(node=node, members=members, relationships=relationships)

    def summarize_many(self, nodes_or_ids: Iterable) -> tuple[SummaryResult, ...]:
        return tuple(self.summarize(n) for n in nodes_or_ids)

    # -- repository overview ----------------------------------------------------------------

    def overview(self) -> OverviewResult:
        file_nodes = self._by_type.get(NodeType.FILE, [])
        files_by_language: dict[str, int] = {}
        for f in file_nodes:
            if "." not in f.name:
                continue
            suffix = "." + f.name.rsplit(".", 1)[-1]
            language = EXTENSION_LANGUAGE_MAP.get(suffix.lower())
            if language is not None:
                files_by_language[language.value] = files_by_language.get(language.value, 0) + 1

        counts_by_type = {t: len(self._by_type.get(t, ())) for t in NodeType}
        packages = tuple(
            sorted(
                n.qualified_name
                for n in self._by_type.get(NodeType.PACKAGE, ())
                if n.qualified_name
            )
        )

        return OverviewResult(
            file_count=len(file_nodes),
            node_count=len(self.graph.nodes),
            edge_count=len(self.graph.edges),
            files_by_language=dict(sorted(files_by_language.items())),
            counts_by_type=counts_by_type,
            packages=packages,
        )


def _format_member_signature(node: Node) -> str:
    if node.type in (NodeType.METHOD, NodeType.CONSTRUCTOR):
        params = ", ".join(node.param_types) if node.param_types else ""
        signature = f"{node.name}({params})"
        if node.return_type:
            signature += f": {node.return_type}"
        return signature
    if node.type in (NodeType.FIELD, NodeType.PROPERTY, NodeType.PARAMETER, NodeType.COLUMN):
        return f"{node.name}: {node.value_type}" if node.value_type else node.name
    return node.name


def format_summary(result: SummaryResult) -> str:
    """Render a SummaryResult as the compact text format documented in the
    Phase 5 spec, e.g.::

        CLASS com.example.sound.SoundController
        file: src/main/java/com/example/sound/SoundController.java
        lines: 12-87

        members:
          METHOD getSounds(String, int): List<String> [35-48]

        relationships:
          IMPLEMENTS SoundApi
          IMPORTS SoundService (unresolved)
    """
    node = result.node
    lines = [f"{node.type.value} {node.qualified_name or node.name}"]
    if node.file:
        lines.append(f"file: {node.file}")
    if node.start_line is not None and node.end_line is not None:
        lines.append(f"lines: {node.start_line}-{node.end_line}")

    if result.members:
        lines.append("")
        lines.append("members:")
        for m in result.members:
            loc = f" [{m.start_line}-{m.end_line}]" if m.start_line is not None else ""
            lines.append(f"  {m.node_type.value} {m.signature}{loc}")

    if result.relationships:
        lines.append("")
        lines.append("relationships:")
        for r in result.relationships:
            marker = "" if r.resolved else " (unresolved)"
            lines.append(f"  {r.type.value} {r.target_name}{marker}")

    return "\n".join(lines)


def format_overview(result: OverviewResult) -> str:
    """Render an OverviewResult as compact text, e.g.::

        Repository
          Java files: 368
          Kotlin files: 400

          Classes: 420
          Interfaces: 87
          Methods: 2341
          Properties: 812

        Packages:
          com.example.controller
          com.example.service
    """
    lines = ["Repository"]
    for language, count in result.files_by_language.items():
        lines.append(f"  {language.capitalize()} files: {count}")
    lines.append("")
    for node_type in (
        NodeType.CLASS,
        NodeType.INTERFACE,
        NodeType.OBJECT,
        NodeType.ENUM,
        NodeType.METHOD,
        NodeType.PROPERTY,
        NodeType.FIELD,
        NodeType.TABLE,
        NodeType.VIEW,
        NodeType.INDEX,
        NodeType.TRIGGER,
    ):
        count = result.counts_by_type.get(node_type, 0)
        if count:
            lines.append(f"  {_TYPE_LABELS[node_type]}: {count}")

    if result.packages:
        lines.append("")
        lines.append("Packages:")
        for pkg in result.packages:
            lines.append(f"  {pkg}")

    return "\n".join(lines)
