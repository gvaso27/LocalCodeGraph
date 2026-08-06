"""Structured result types returned by QueryEngine.

Kept separate from query/engine.py's traversal logic, and separate from any
future serialization concerns (a CLI or other consumer decides how to print
or JSON-encode these; this module only defines their shape). Every type
here is a plain, immutable dataclass over the existing graph model
(graph/model.py) — no query result invents new node/edge concepts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from local_code_graph.graph.model import EdgeType, Node, NodeType


@dataclass(frozen=True)
class RelatedNode:
    """One relationship edge, from the perspective of a node being queried.

    ``node`` is the resolved Node on the other end of the edge, or None if
    the edge's target could not be resolved when the graph was built (e.g.
    an import of an external library) — ``target_name`` always holds the
    raw name either way, so unresolved relationships are never silently
    dropped.
    """

    edge_type: EdgeType
    node: Node | None
    target_name: str


@dataclass(frozen=True)
class FindResult:
    """Result of QueryEngine.find(query).

    ``matches`` is sorted by node ID. ``is_ambiguous`` is true whenever
    more than one node matched — callers must not guess which one was
    meant; they see all of them and decide.
    """

    query: str
    matches: tuple[Node, ...]

    @property
    def is_empty(self) -> bool:
        return len(self.matches) == 0

    @property
    def is_ambiguous(self) -> bool:
        return len(self.matches) > 1


@dataclass(frozen=True)
class PathEdge:
    """One hop of a PathResult.

    ``reversed`` is True when this hop was walked against the edge's
    natural source->target direction (path() treats the graph as
    undirected for connectivity — see QueryEngine.path's docstring).
    """

    type: EdgeType
    from_id: str
    to_id: str
    reversed: bool


@dataclass(frozen=True)
class PathResult:
    source: Node
    target: Node
    found: bool
    nodes: tuple[Node, ...]
    """The full node sequence from source to target, inclusive. Empty when
    not found."""
    edges: tuple[PathEdge, ...]
    """len(edges) == len(nodes) - 1 when found; empty otherwise."""


@dataclass(frozen=True)
class NeighborEdge:
    type: EdgeType
    source_id: str
    target_id: str


@dataclass(frozen=True)
class NeighborhoodResult:
    """A bounded local subgraph around ``center``.

    Used by both QueryEngine.neighbors() (all edge types, both directions)
    and QueryEngine.affected() (a specific dependency-edge policy, one
    direction) — see their docstrings for which edges/direction populate
    ``nodes``/``edges`` in each case; the shape is the same either way.

    ``nodes`` excludes ``center`` itself and is sorted by (depth reached,
    node ID) — nearer nodes first, alphabetical by ID as a tiebreak.
    """

    center: Node
    depth: int
    nodes: tuple[Node, ...]
    edges: tuple[NeighborEdge, ...]


@dataclass(frozen=True)
class MemberSummary:
    node_type: NodeType
    name: str
    signature: str
    start_line: int | None
    end_line: int | None


@dataclass(frozen=True)
class RelationshipSummary:
    type: EdgeType
    target_name: str
    resolved: bool
    """True when the relationship's target_id was resolved to a node in
    this graph; False for external/unresolved references."""


@dataclass(frozen=True)
class SummaryResult:
    node: Node
    members: tuple[MemberSummary, ...]
    """This node's CONTAINS/DECLARES children (see QueryEngine.children),
    rendered compactly. Never includes source code."""
    relationships: tuple[RelationshipSummary, ...]
    """This node's outgoing IMPORTS/EXTENDS/IMPLEMENTS edges."""


@dataclass(frozen=True)
class OverviewResult:
    file_count: int
    node_count: int
    edge_count: int
    files_by_language: Mapping[str, int]
    counts_by_type: Mapping[NodeType, int]
    packages: tuple[str, ...]
    """Sorted, fully qualified package names."""
