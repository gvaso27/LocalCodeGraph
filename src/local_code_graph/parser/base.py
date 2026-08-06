"""Common interface all language parsers implement.

Every language parser turns one source file's content into graph-model
nodes/edges without knowing anything about the rest of the repository.
Relationships that can only be confirmed once every file has been parsed
(EXTENDS/IMPLEMENTS targets defined in another file, IMPORTS targets that
live elsewhere in the repo) are reported as `PendingRef`s instead of `Edge`s;
`graph/builder.py` resolves those in a second pass once it has a full
cross-file symbol table, then turns the resolvable ones into real Edges.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from local_code_graph.graph.model import Edge, EdgeType, Location, Node, ParseError


@dataclass(frozen=True)
class PendingRef:
    """A syntactically explicit reference to another type by name.

    ``type`` is always one of IMPORTS, EXTENDS, IMPLEMENTS, or REFERENCES
    (the SQL foreign-key/table dependency) — CONTAINS and DECLARES are
    always resolvable within a single file and are emitted directly as
    Edges instead.
    """

    type: EdgeType
    source_id: str
    raw_name: str
    location: Location | None


@dataclass(frozen=True)
class FileContext:
    """Per-file context the graph builder needs to resolve PendingRefs."""

    package: str | None
    imports: tuple[tuple[str, str], ...] = ()
    """(simple_name, fully_qualified_name) pairs for explicit, non-wildcard,
    non-static imports in this file."""


@dataclass(frozen=True)
class ParseResult:
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    pending_refs: tuple[PendingRef, ...]
    errors: tuple[ParseError, ...]
    context: FileContext


class LanguageParser(ABC):
    """Parses one file's raw bytes into a ParseResult.

    Implementations must not raise on malformed input; syntax errors found
    in the file should be captured in ``ParseResult.errors`` so a single
    broken file cannot abort parsing the rest of the repository.
    """

    @abstractmethod
    def parse(self, relative_path: str, content: bytes) -> ParseResult: ...
