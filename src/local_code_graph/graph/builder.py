"""Combines scanner output and per-file parser output into one Graph.

    repository -> scanner -> language parser (per file) -> GraphBuilder -> Graph

Each file is parsed independently (parsers never see other files). Once
every file has been parsed, the builder has a complete cross-file symbol
table (fully qualified type name -> node id) and uses it to resolve the
IMPORTS/EXTENDS/IMPLEMENTS PendingRefs collected along the way into real
Edges — see `_resolve_pending_ref` for the exact (conservative) resolution
rules.

``GraphBuilder.parse_files``/``resolve_pending_refs`` are exposed as
separate steps (not just inlined into ``build``) so graph/incremental.py
can reuse the exact same per-file parsing and resolution logic while only
re-parsing a changed subset of files, with the symbol table seeded from
nodes carried over unchanged from a previous build.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import replace

from local_code_graph.graph.model import Edge, EdgeType, Graph, Node, NodeType, ParseError
from local_code_graph.parser.base import FileContext, LanguageParser, PendingRef
from local_code_graph.parser.java import JavaParser
from local_code_graph.parser.kotlin import KotlinParser
from local_code_graph.parser.sql import SqlParser
from local_code_graph.scanner import Language, ScanResult, SourceFile, scan_repository

TYPE_NODE_TYPES = frozenset(
    {NodeType.CLASS, NodeType.INTERFACE, NodeType.ENUM, NodeType.ANNOTATION, NodeType.OBJECT}
)

SQL_DECLARATION_NODE_TYPES = frozenset(
    {NodeType.TABLE, NodeType.VIEW, NodeType.INDEX, NodeType.TRIGGER}
)
"""SQL top-level declarations, which resolve by name like types do.

Kept separate from TYPE_NODE_TYPES because these are not JVM types: they
share the "registered in the symbol table under a qualified name" behavior
but not the EXTENDS/IMPLEMENTS scoping rules that apply to classes.
"""

MERGEABLE_NODE_TYPES = (
    frozenset({NodeType.PACKAGE, NodeType.COLUMN}) | SQL_DECLARATION_NODE_TYPES
)
"""Node types where the same ID legitimately appears in several files.

A PACKAGE is re-declared by every file in it, and a SQL table — along with
the columns it declares — is re-declared by every migration that recreates
it. In both cases the repeated declarations describe one entity, so the
second one is a merge, not a collision. See graph/model.py's "SQL-specific
representation notes".

A duplicate *within* a single file is still a real mistake, and the parsers
report that themselves before their output ever reaches this set.
"""

PendingItem = tuple[PendingRef, FileContext]


def default_parsers() -> dict[Language, LanguageParser]:
    """The parser registry used when a GraphBuilder isn't given one explicitly."""
    return {
        Language.JAVA: JavaParser(),
        Language.KOTLIN: KotlinParser(),
        Language.SQL: SqlParser(),
    }


def _with_valid_line_span(node: Node) -> Node:
    """Return ``node`` with a line span that graph/storage.py will accept:
    both bounds None, or both 1-based integers with start <= end.

    The parsers already clamp spans as they read them from Tree-sitter
    (see parser/_ts_utils.node_line_span), so in the normal case this is a
    no-op. It exists because parse results also arrive here from a
    *separate process* (parser/isolated.py), and the native
    Tree-sitter bug that module works around corrupts memory rather than
    failing cleanly — corruption can therefore land on already-built
    objects after the parser's own clamping, producing impossible spans
    like start=54/end=0 in the parent. Observed in practice on a real
    Kotlin repository: `lcg build` reported success while writing a
    graph.json that then failed its own load-time validation.

    Treating parser output as untrusted at the point it enters the graph
    means a corrupted span costs one node an approximate location instead
    of costing the user an unloadable graph.
    """
    start, end = node.start_line, node.end_line
    if start is None and end is None:
        return node
    # A half-populated span is meaningless; fall back to the other bound.
    if start is None:
        start = end
    if end is None:
        end = start
    if not isinstance(start, int) or not isinstance(end, int):
        return replace(node, start_line=None, end_line=None)
    fixed_start = max(1, start)
    fixed_end = max(fixed_start, end)
    if fixed_start == node.start_line and fixed_end == node.end_line:
        return node
    return replace(node, start_line=fixed_start, end_line=fixed_end)


class GraphBuilder:
    def __init__(self, parsers: Mapping[Language, LanguageParser] | None = None) -> None:
        self._parsers: Mapping[Language, LanguageParser] = (
            parsers if parsers is not None else default_parsers()
        )

    def build(self, scan_result: ScanResult) -> Graph:
        graph = Graph(root=str(scan_result.root))
        symbol_table: dict[str, str] = {}
        pending: list[PendingItem] = []

        self.parse_files(scan_result.files, graph, symbol_table, pending)
        self.resolve_pending_refs(graph, symbol_table, pending)
        prune_orphaned_sql_columns(graph)
        return graph

    def build_isolated(self, scan_result: ScanResult, *, timeout: float = 60.0) -> Graph:
        """Like ``build``, but parses each file in an isolated subprocess
        (see parser/isolated.py) so a native crash in Tree-sitter itself on
        one file — a real, observed failure mode on some Compose-heavy
        Kotlin files — can't take down the whole build. The crashing file
        gets a ParseError instead of being silently missing or aborting
        everything else. This is what `lcg build`/`lcg update` actually
        use; plain ``build`` stays fast and simple for library use and
        tests where that extra safety isn't needed.
        """
        from local_code_graph.parser.isolated import parse_source_files_isolated

        graph = Graph(root=str(scan_result.root))
        symbol_table: dict[str, str] = {}
        pending: list[PendingItem] = []

        items: list[tuple[Language, str, bytes]] = []
        for source_file in scan_result.files:
            if source_file.language not in self._parsers:
                continue
            try:
                content = source_file.path.read_bytes()
            except OSError as exc:
                graph.errors.append(
                    ParseError(file=source_file.relative_path, message=f"could not read file: {exc}")
                )
                continue
            items.append((source_file.language, source_file.relative_path, content))

        results = parse_source_files_isolated(items, timeout=timeout)
        for relative_path, result in results.items():
            self.merge_result(relative_path, result, graph, symbol_table, pending)

        self.resolve_pending_refs(graph, symbol_table, pending)
        prune_orphaned_sql_columns(graph)
        return graph

    def parse_files(
        self,
        files: Iterable[SourceFile],
        graph: Graph,
        symbol_table: dict[str, str],
        pending: list[PendingItem],
    ) -> None:
        """Parse ``files`` and merge their nodes/edges/errors into ``graph``
        in place, registering type nodes into ``symbol_table`` and
        collecting IMPORTS/EXTENDS/IMPLEMENTS references into ``pending``
        for a later ``resolve_pending_refs`` call. Does not itself resolve
        anything, so it's safe to call multiple times (e.g. once per
        changed file) before resolving once against the complete table.
        """
        for source_file in files:
            parser = self._parsers.get(source_file.language)
            if parser is None:
                continue

            try:
                content = source_file.path.read_bytes()
            except OSError as exc:
                graph.errors.append(
                    ParseError(
                        file=source_file.relative_path,
                        message=f"could not read file: {exc}",
                    )
                )
                continue

            result = parser.parse(source_file.relative_path, content)
            self.merge_result(source_file.relative_path, result, graph, symbol_table, pending)

    def merge_result(
        self,
        relative_path: str,
        result,
        graph: Graph,
        symbol_table: dict[str, str],
        pending: list[PendingItem],
    ) -> None:
        """Merge one file's already-computed ParseResult into ``graph`` in
        place. Split out from ``parse_files`` so callers that obtained a
        ParseResult some other way (e.g. parser/isolated.py, which runs the
        actual parse in a subprocess so a native crash on one file can't
        take down the whole build) can reuse the exact same merge logic.

        Node line spans are re-validated here (``_with_valid_line_span``)
        even though the parsers already clamp them, because this is the
        boundary where parser output — possibly produced in another
        process whose memory a native Tree-sitter bug may have corrupted —
        enters the graph. See that helper for why belt-and-braces is
        warranted.
        """
        for node in (_with_valid_line_span(n) for n in result.nodes):
            if node.type in MERGEABLE_NODE_TYPES:
                # Re-declaring these across files is expected, not a
                # collision: every file in a package re-declares its
                # PACKAGE, and every migration touching a table re-declares
                # that TABLE. First declaration wins; the rest merge.
                graph.add_node(node)
                # PACKAGE deliberately stays out of the symbol table: it
                # resolves EXTENDS/IMPLEMENTS targets, and a package is
                # never one. SQL declarations are exactly what REFERENCES
                # resolves against, so they do belong there.
                if node.type in SQL_DECLARATION_NODE_TYPES and node.qualified_name:
                    symbol_table.setdefault(node.qualified_name, node.id)
                continue
            if not graph.add_node(node):
                graph.errors.append(
                    ParseError(
                        file=relative_path,
                        message=f"duplicate declaration id (skipped): {node.id}",
                        start_line=node.start_line,
                        end_line=node.end_line,
                    )
                )
                continue
            if node.type in TYPE_NODE_TYPES and node.qualified_name:
                symbol_table.setdefault(node.qualified_name, node.id)

        for edge in result.edges:
            graph.add_edge(edge)

        for pending_ref in result.pending_refs:
            pending.append((pending_ref, result.context))

        graph.errors.extend(result.errors)

    def resolve_pending_refs(
        self,
        graph: Graph,
        symbol_table: dict[str, str],
        pending: list[PendingItem],
    ) -> None:
        for pending_ref, context in pending:
            target_id = _resolve_pending_ref(pending_ref, context, symbol_table)
            graph.add_edge(
                Edge(
                    type=pending_ref.type,
                    source_id=pending_ref.source_id,
                    target_id=target_id,
                    target_name=pending_ref.raw_name,
                    location=pending_ref.location,
                )
            )


def prune_orphaned_sql_columns(graph: Graph) -> None:
    """Drop COLUMN nodes (and their DECLARES edges) whose owning table is
    not declared anywhere in the graph.

    `ALTER TABLE x ADD COLUMN y` names the table it modifies but does not
    declare it, and in a versioned-migration layout the `CREATE TABLE x`
    usually lives in a different file — so parser/sql.py attaches the
    column to `table:x` on the strength of the name alone, which is correct
    whenever some file does create that table. When none does (the table
    belongs to a database this repository only talks to, or the CREATE was
    never checked in), that column would be parented to a node that doesn't
    exist. Rather than emit a graph that fails its own load-time validation,
    the unattachable column is dropped: a missing column is a gap, an
    unloadable graph is a broken tool.

    Runs after every file has been merged, since "is this table declared
    anywhere?" is only answerable then.
    """
    orphaned = {
        node.id
        for node in graph.nodes.values()
        if node.type == NodeType.COLUMN
        and node.parent_id is not None
        and node.parent_id not in graph.nodes
    }
    if not orphaned:
        return
    for column_id in orphaned:
        del graph.nodes[column_id]
    graph.edges[:] = [
        edge
        for edge in graph.edges
        if edge.source_id not in orphaned and edge.target_id not in orphaned
    ]


def _resolve_pending_ref(
    ref: PendingRef,
    context: FileContext,
    symbol_table: dict[str, str],
) -> str | None:
    if ref.type == EdgeType.REFERENCES:
        # SQL: the parser has already canonicalized both the declaration
        # names and this reference (see parser/sql.py), so an exact lookup
        # is the *complete* rule — no scoping, imports, or package fallback
        # applies, and deliberately so: SQL object names are global within
        # a schema, and trying Java's fallbacks here would let a reference
        # to a missing table silently bind to something unrelated.
        return symbol_table.get(ref.raw_name)

    if ref.type == EdgeType.IMPORTS:
        # Wildcard ("pkg.*") and static ("pkg.Type.member") imports never
        # exactly match a type's fully qualified name, so they naturally
        # fall through to unresolved here without special-casing.
        return symbol_table.get(ref.raw_name)

    # EXTENDS / IMPLEMENTS: strip generic type arguments (e.g.
    # "Comparable<Foo>" -> "Comparable") to get the base type name used for
    # lookup, while `target_name` on the eventual Edge still keeps the raw
    # text as written.
    base = ref.raw_name.split("<", 1)[0].strip()

    if "." in base:
        return symbol_table.get(base)

    # A sibling type nested in the same enclosing type (or one of its
    # enclosing types, searched innermost-first) takes priority — this
    # mirrors how Java itself resolves an unqualified name before falling
    # back to imports/same-package lookup.
    for scope_fqn in _enclosing_type_scopes(ref.source_id, context.package):
        resolved = symbol_table.get(f"{scope_fqn}.{base}")
        if resolved is not None:
            return resolved

    import_map = dict(context.imports)
    fqcn = import_map.get(base)
    if fqcn is not None:
        resolved = symbol_table.get(fqcn)
        if resolved is not None:
            return resolved

    if context.package:
        resolved = symbol_table.get(f"{context.package}.{base}")
        if resolved is not None:
            return resolved

    return symbol_table.get(base)


def _enclosing_type_scopes(source_id: str, package: str | None) -> list[str]:
    """Fully qualified names of the enclosing types of ``source_id``, from
    innermost to outermost, stopping at the package boundary (exclusive).

    Returns an empty list when ``source_id`` isn't a type node (e.g. it's a
    FILE node, as for IMPORTS refs) or the type has no enclosing type.
    """
    prefix = "type:"
    if not source_id.startswith(prefix):
        return []
    fqn = source_id[len(prefix) :]
    if "." not in fqn:
        return []

    package_prefix = package or ""
    scopes = []
    current = fqn.rsplit(".", 1)[0]
    while current and current != package_prefix:
        scopes.append(current)
        if "." not in current:
            break
        current = current.rsplit(".", 1)[0]
    return scopes


def build_graph(
    root: str | os.PathLike[str],
    *,
    parsers: Mapping[Language, LanguageParser] | None = None,
) -> Graph:
    """Convenience entry point: scan ``root`` and build its Graph in one call."""
    scan_result = scan_repository(root)
    return GraphBuilder(parsers).build(scan_result)


def build_graph_isolated(
    root: str | os.PathLike[str],
    *,
    parsers: Mapping[Language, LanguageParser] | None = None,
    timeout: float = 60.0,
) -> Graph:
    """Convenience entry point: scan ``root`` and build its Graph in one
    call, using GraphBuilder.build_isolated (see its docstring)."""
    scan_result = scan_repository(root)
    return GraphBuilder(parsers).build_isolated(scan_result, timeout=timeout)
