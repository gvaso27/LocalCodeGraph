"""``lcg``: a thin command-line front end over the scanner, parsers, graph
builder, storage layer, and QueryEngine that already exist in this package.

    lcg build <repo>      repository -> scanner -> parsers -> GraphBuilder -> save_graph()
    lcg <query-command>    .local-code-graph/graph.json -> load_graph() -> QueryEngine -> result

That split is deliberate and enforced structurally, not just by convention:
every query command (find/show/children/imports/.../overview/path/...)
calls ``_require_graph``, which only ever calls ``storage.load_graph`` — it
never imports or calls the scanner, a language parser, or GraphBuilder.
Only ``lcg build`` touches source files. If no graph has been saved yet, a
query command fails with a clear message rather than silently building one
(see ``_require_graph``'s GraphNotFoundError branch below).

This module contains no query/graph logic of its own — every command is a
thin translation from CLI arguments to a QueryEngine/storage call and back
to text or JSON. See query/engine.py for what each operation actually means
(relationship policies, path semantics, determinism, etc.).

Exit codes
----------
    0  success
    1  general/runtime error
    2  invalid command/arguments (argparse's own default for bad args)
    3  no graph found for this repository
    4  query target not found
    5  ambiguous query (more than one exact match)
    6  invalid/corrupted graph file

Output
------
Every query command accepts ``--json``. With ``--json``, stdout is valid
JSON and nothing else — no decorative text, no logging. Errors always go to
stderr, regardless of ``--json``, and Python tracebacks are never shown to
the user for expected failure modes (missing repo, missing/corrupt graph,
not-found, ambiguous, invalid args) — see ``CliExit``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from local_code_graph import __version__
from local_code_graph.graph import storage
from local_code_graph.graph.builder import GraphBuilder
from local_code_graph.graph.incremental import compute_and_save_hashes, update_repository
from local_code_graph.graph.model import Graph, Node
from local_code_graph.parser.isolated import SKIPPED_FILE_MARKER, is_skipped_file_error
from local_code_graph.query.engine import (
    InvalidDepthError,
    InvalidQueryError,
    QueryEngine,
)
from local_code_graph.query.results import NeighborhoodResult, PathResult, RelatedNode, SummaryResult
from local_code_graph.query.engine import format_overview, format_summary
from local_code_graph.scanner import scan_repository

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_INVALID_ARGS = 2
EXIT_GRAPH_NOT_FOUND = 3
EXIT_NOT_FOUND = 4
EXIT_AMBIGUOUS = 5
EXIT_INVALID_GRAPH = 6


class CliExit(Exception):
    """Raised by command handlers to stop with a specific exit code and an
    optional stderr message. Caught once, centrally, in ``main``."""

    def __init__(self, code: int, message: str | None = None) -> None:
        super().__init__(message or "")
        self.code = code
        self.message = message


# -- shared helpers -----------------------------------------------------------------


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _skip_reason(message: str) -> str:
    """Strip the machine-readable marker off a skipped-file ParseError,
    leaving just the human-readable reason.

    The marker exists for detection (see parser/isolated.py); repeating it
    on every line of the CLI's skipped-files list would be noise.
    """
    reason = message
    if reason.startswith(SKIPPED_FILE_MARKER):
        reason = reason[len(SKIPPED_FILE_MARKER) :].lstrip(": ")
    return reason


def _graph_not_found_exit(repository: Path) -> CliExit:
    return CliExit(
        EXIT_GRAPH_NOT_FOUND,
        "No local code graph exists for this repository.\n\nRun:\n" f"  lcg build {repository}",
    )


def _invalid_graph_exit(repository: Path, exc: Exception) -> CliExit:
    return CliExit(
        EXIT_INVALID_GRAPH,
        f"The local code graph is invalid or corrupted: {exc}\n\nRun:\n" f"  lcg build {repository}",
    )


def _require_graph(repository: Path) -> Graph:
    """Load the saved graph for ``repository`` — never scans/parses source."""
    try:
        return storage.load_graph(repository)
    except storage.GraphNotFoundError:
        raise _graph_not_found_exit(repository) from None
    except storage.GraphStorageError as exc:
        raise _invalid_graph_exit(repository, exc) from exc


def _resolve_single(qe: QueryEngine, query: str) -> Node:
    """Resolve a user-typed query string to exactly one Node, or raise a
    CliExit with the appropriate not-found/ambiguous exit code. Never
    guesses when more than one node matches (see QueryEngine.find)."""
    try:
        result = qe.find(query)
    except InvalidQueryError as exc:
        raise CliExit(EXIT_INVALID_ARGS, f"Invalid query: {exc}") from exc

    if result.is_empty:
        raise CliExit(EXIT_NOT_FOUND, f"No match found for: {query}")
    if result.is_ambiguous:
        lines = [f"Ambiguous symbol: {query}", "", "Matches:"]
        for m in result.matches:
            lines.append(f"  {m.qualified_name or m.name}  ({m.id})")
        raise CliExit(EXIT_AMBIGUOUS, "\n".join(lines))
    return result.matches[0]


def _node_label(node: Node) -> str:
    return node.qualified_name or node.name


def _related_to_dict(r: RelatedNode) -> dict:
    return {
        "edge_type": r.edge_type.value,
        "target_name": r.target_name,
        "node": storage.node_to_dict(r.node) if r.node is not None else None,
    }


def _print_related(node: Node, results: tuple[RelatedNode, ...], *, as_json: bool) -> None:
    if as_json:
        _print_json({"node": storage.node_to_dict(node), "relationships": [_related_to_dict(r) for r in results]})
        return
    print(f"{node.type.value} {_node_label(node)}")
    if not results:
        print("  (none)")
        return
    for r in results:
        target = r.node.id if r.node is not None else "(unresolved)"
        print(f"  {r.edge_type.value}  {r.target_name}  -> {target}")


def _neighborhood_to_dict(result: NeighborhoodResult) -> dict:
    return {
        "center": storage.node_to_dict(result.center),
        "depth": result.depth,
        "nodes": [storage.node_to_dict(n) for n in result.nodes],
        "edges": [
            {"type": e.type.value, "source_id": e.source_id, "target_id": e.target_id} for e in result.edges
        ],
    }


def _print_neighborhood(result: NeighborhoodResult, *, as_json: bool) -> None:
    if as_json:
        _print_json(_neighborhood_to_dict(result))
        return
    print(f"{result.center.type.value} {_node_label(result.center)}  (depth={result.depth})")
    print()
    if not result.nodes:
        print("  (no nodes within this depth)")
        return
    print("nodes:")
    for n in result.nodes:
        print(f"  {n.type.value:<10} {_node_label(n)}")
    print()
    print("edges:")
    for e in result.edges:
        print(f"  {e.type.value:<10} {e.source_id} -> {e.target_id}")


def _summary_to_dict(s: SummaryResult) -> dict:
    return {
        "node": storage.node_to_dict(s.node),
        "members": [
            {
                "node_type": m.node_type.value,
                "name": m.name,
                "signature": m.signature,
                "start_line": m.start_line,
                "end_line": m.end_line,
            }
            for m in s.members
        ],
        "relationships": [
            {"type": r.type.value, "target_name": r.target_name, "resolved": r.resolved}
            for r in s.relationships
        ],
    }


def _path_to_dict(result: PathResult) -> dict:
    return {
        "found": result.found,
        "source": storage.node_to_dict(result.source),
        "target": storage.node_to_dict(result.target),
        "nodes": [storage.node_to_dict(n) for n in result.nodes],
        "edges": [
            {"type": e.type.value, "from_id": e.from_id, "to_id": e.to_id, "reversed": e.reversed}
            for e in result.edges
        ],
    }


def _overview_to_dict(ov) -> dict:
    return {
        "file_count": ov.file_count,
        "node_count": ov.node_count,
        "edge_count": ov.edge_count,
        "files_by_language": dict(ov.files_by_language),
        "counts_by_type": {t.value: c for t, c in ov.counts_by_type.items()},
        "packages": list(ov.packages),
    }


# -- command handlers -----------------------------------------------------------------


def _cmd_build(args: argparse.Namespace) -> int:
    repository = Path(args.repository)
    if not repository.is_dir():
        raise CliExit(EXIT_ERROR, f"Not a directory: {repository}")

    scan_result = scan_repository(repository)
    graph = GraphBuilder().build_isolated(scan_result)
    storage.save_graph(graph, repository)
    # Save a hash baseline from the same scan, so the very next `lcg
    # update` sees "nothing changed" instead of treating every file as new.
    compute_and_save_hashes(scan_result, repository)

    overview = QueryEngine(graph).overview()
    graph_path = storage.graph_json_path(repository)
    # Keep the first reason per file: one file yields at most one skip
    # error, but dict insertion order also keeps the listing deterministic.
    skipped_reasons: dict[str, str] = {}
    for error in graph.errors:
        if is_skipped_file_error(error):
            skipped_reasons.setdefault(error.file, _skip_reason(error.message))
    skipped = sorted(skipped_reasons)

    if args.json:
        _print_json(
            {
                "files": overview.file_count,
                "nodes": overview.node_count,
                "edges": overview.edge_count,
                "languages": sorted(overview.files_by_language),
                "skipped_files": skipped,
                "skipped_file_reasons": {p: skipped_reasons[p] for p in skipped},
                "graph_path": str(graph_path),
            }
        )
        return EXIT_OK

    languages = ", ".join(lang.capitalize() for lang in sorted(overview.files_by_language)) or "none"
    print("Built local code graph")
    print()
    print(f"Files: {overview.file_count}")
    print(f"Nodes: {overview.node_count}")
    print(f"Edges: {overview.edge_count}")
    print(f"Languages: {languages}")

    if skipped:
        # Never let a partial graph look like a complete one: these files
        # are absent from every query result, so say so plainly rather
        # than reporting an unqualified success.
        print()
        print(f"Skipped {len(skipped)} file(s) — the parser could not process them,")
        print("so their declarations are NOT in the graph:")
        for path in skipped[:10]:
            print(f"  {path}")
            print(f"      {skipped_reasons[path]}")
        if len(skipped) > 10:
            print(f"  ... and {len(skipped) - 10} more")

    print()
    print("Graph:")
    print(f"  {graph_path}")
    return EXIT_OK


def _cmd_update(args: argparse.Namespace) -> int:
    repository = Path(args.repository)
    try:
        result = update_repository(repository)
    except storage.GraphNotFoundError:
        raise _graph_not_found_exit(repository) from None
    except storage.GraphStorageError as exc:
        raise _invalid_graph_exit(repository, exc) from exc

    overview = QueryEngine(result.graph).overview()
    graph_path = storage.graph_json_path(repository)

    if args.json:
        _print_json(
            {
                "changed_files": list(result.changed_files),
                "removed_files": list(result.removed_files),
                "unchanged_file_count": result.unchanged_file_count,
                "nodes": overview.node_count,
                "edges": overview.edge_count,
                "graph_path": str(graph_path),
            }
        )
        return EXIT_OK

    print("Updated local code graph")
    print()
    print(f"Changed: {len(result.changed_files)}")
    print(f"Removed: {len(result.removed_files)}")
    print(f"Unchanged: {result.unchanged_file_count}")
    print()
    print(f"Nodes: {overview.node_count}")
    print(f"Edges: {overview.edge_count}")
    print()
    print("Graph:")
    print(f"  {graph_path}")
    return EXIT_OK


def _cmd_overview(args: argparse.Namespace) -> int:
    graph = _require_graph(Path(args.repository))
    qe = QueryEngine(graph)
    result = qe.overview()
    if args.json:
        _print_json(_overview_to_dict(result))
    else:
        print(format_overview(result))
    return EXIT_OK


def _cmd_find(args: argparse.Namespace) -> int:
    graph = _require_graph(Path(args.repository))
    qe = QueryEngine(graph)
    try:
        result = qe.find(args.query)
    except InvalidQueryError as exc:
        raise CliExit(EXIT_INVALID_ARGS, f"Invalid query: {exc}") from exc

    if args.json:
        _print_json({"query": result.query, "matches": [storage.node_to_dict(n) for n in result.matches]})
        return EXIT_OK

    if result.is_empty:
        print(f"No match found for: {result.query}")
        return EXIT_OK
    for n in result.matches:
        print(f"{n.type.value:<12} {_node_label(n)}  ({n.id})")
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    graph = _require_graph(Path(args.repository))
    qe = QueryEngine(graph)
    node = _resolve_single(qe, args.query)
    summary = qe.summarize(node)
    if args.json:
        _print_json(_summary_to_dict(summary))
    else:
        print(format_summary(summary))
    return EXIT_OK


def _make_relationship_handler(method_name: str):
    def handler(args: argparse.Namespace) -> int:
        graph = _require_graph(Path(args.repository))
        qe = QueryEngine(graph)
        node = _resolve_single(qe, args.query)
        results = getattr(qe, method_name)(node)
        _print_related(node, results, as_json=args.json)
        return EXIT_OK

    return handler


def _cmd_neighbors(args: argparse.Namespace) -> int:
    graph = _require_graph(Path(args.repository))
    qe = QueryEngine(graph)
    node = _resolve_single(qe, args.query)
    try:
        result = qe.neighbors(node, depth=args.depth)
    except InvalidDepthError as exc:
        raise CliExit(EXIT_INVALID_ARGS, f"Invalid --depth: {exc}") from exc
    _print_neighborhood(result, as_json=args.json)
    return EXIT_OK


def _cmd_affected(args: argparse.Namespace) -> int:
    graph = _require_graph(Path(args.repository))
    qe = QueryEngine(graph)
    node = _resolve_single(qe, args.query)
    try:
        result = qe.affected(node, depth=args.depth)
    except InvalidDepthError as exc:
        raise CliExit(EXIT_INVALID_ARGS, f"Invalid --depth: {exc}") from exc
    _print_neighborhood(result, as_json=args.json)
    return EXIT_OK


def _cmd_path(args: argparse.Namespace) -> int:
    graph = _require_graph(Path(args.repository))
    qe = QueryEngine(graph)
    source = _resolve_single(qe, args.source)
    target = _resolve_single(qe, args.target)
    try:
        result = qe.path(source, target, max_depth=args.max_depth)
    except InvalidDepthError as exc:
        raise CliExit(EXIT_INVALID_ARGS, f"Invalid --max-depth: {exc}") from exc

    if args.json:
        _print_json(_path_to_dict(result))
        return EXIT_OK

    if not result.found:
        print(
            f"No path found between {_node_label(source)} and {_node_label(target)} "
            f"within {args.max_depth} hops."
        )
        return EXIT_OK

    for index, node in enumerate(result.nodes):
        if index > 0:
            edge = result.edges[index - 1]
            marker = " (reversed)" if edge.reversed else ""
            print(f"  --{edge.type.value}-->{marker}")
        print(_node_label(node))
    return EXIT_OK


# -- argument parser -------------------------------------------------------------------

_RELATIONSHIP_COMMANDS: dict[str, str] = {
    "children": "children",
    "imports": "imports",
    "imported-by": "imported_by",
    "extends": "extends",
    "extended-by": "extended_by",
    "implements": "implements",
    "implemented-by": "implemented_by",
    "dependencies": "dependencies",
    "dependents": "dependents",
}

_RELATIONSHIP_HELP: dict[str, str] = {
    "children": "This node's CONTAINS/DECLARES children (nested types, members).",
    "imports": "This node's outgoing IMPORTS edges.",
    "imported-by": "Nodes that import this node.",
    "extends": "This node's outgoing EXTENDS edge(s).",
    "extended-by": "Nodes that extend this node.",
    "implements": "This node's outgoing IMPLEMENTS edges.",
    "implemented-by": "Nodes that implement this node.",
    "dependencies": "Direct IMPORTS/EXTENDS/IMPLEMENTS targets (structural, not runtime).",
    "dependents": "Nodes with a direct IMPORTS/EXTENDS/IMPLEMENTS edge onto this node.",
}


def _add_repository_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("repository", help="Path to the repository (explicit; never assumed).")


def _add_query_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="Exact ID, fully qualified name, or simple name to look up.")


def _add_json_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable JSON on stdout instead of text."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lcg",
        description=(
            "Local, offline structural code graph for Java/Kotlin repositories. "
            "'lcg build' reads source files and writes .local-code-graph/; every "
            "other command only ever reads that saved graph, never source files."
        ),
    )
    parser.add_argument("--version", action="version", version=f"local-code-graph {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_p = subparsers.add_parser(
        "build",
        help="Scan and parse a repository, then save its graph (full rebuild). Reads source; writes the graph.",
        description=(
            "Scans <repository> for Java/Kotlin source, parses it, builds the graph "
            "from scratch, and saves it to <repository>/.local-code-graph/. Reads "
            "every source file every time — see 'update' to reparse only what changed."
        ),
    )
    _add_repository_arg(build_p)
    _add_json_arg(build_p)
    build_p.set_defaults(handler=_cmd_build)

    update_p = subparsers.add_parser(
        "update",
        help="Reparse only files changed since the last build/update. Requires an existing graph.",
        description=(
            "Reparses only files whose content changed since the last build/update "
            "(tracked via .local-code-graph/file_hashes.json), merges the result into "
            "the existing graph, and saves it. Fails with the same error as any query "
            "command if no graph exists yet — never falls back to a full build "
            "implicitly; run 'lcg build' first. Note: only newly reparsed files get "
            "their cross-file references re-resolved against the current graph — an "
            "old, still-unresolved reference is not retroactively fixed just because "
            "this update happens to add what it was missing elsewhere. Run 'lcg build' "
            "for a full, fully-resolved rebuild."
        ),
    )
    _add_repository_arg(update_p)
    _add_json_arg(update_p)
    update_p.set_defaults(handler=_cmd_update)

    overview_p = subparsers.add_parser(
        "overview",
        help="Repository-wide counts and package list. Reads only the saved graph.",
        description="Repository-wide structural summary. Reads only .local-code-graph/graph.json — never source files or the graph.",
    )
    _add_repository_arg(overview_p)
    _add_json_arg(overview_p)
    overview_p.set_defaults(handler=_cmd_overview)

    find_p = subparsers.add_parser(
        "find",
        help="Search for nodes by exact ID / qualified name / simple name. Reads only the saved graph.",
        description=(
            "Exact-match search only (ID, then qualified name, then simple name — the "
            "first tier with any match wins). Never guesses: prints every match, "
            "including when there's more than one. Reads only the saved graph."
        ),
    )
    _add_repository_arg(find_p)
    _add_query_arg(find_p)
    _add_json_arg(find_p)
    find_p.set_defaults(handler=_cmd_find)

    show_p = subparsers.add_parser(
        "show",
        help="Compact structural summary of one node (members + relationships, no source code). Reads only the saved graph.",
        description="Compact structural summary: members (CONTAINS/DECLARES) and relationships (IMPORTS/EXTENDS/IMPLEMENTS). Never includes source code. Reads only the saved graph.",
    )
    _add_repository_arg(show_p)
    _add_query_arg(show_p)
    _add_json_arg(show_p)
    show_p.set_defaults(handler=_cmd_show)

    for command_name, method_name in _RELATIONSHIP_COMMANDS.items():
        sub = subparsers.add_parser(
            command_name,
            help=f"{_RELATIONSHIP_HELP[command_name]} Reads only the saved graph.",
            description=f"{_RELATIONSHIP_HELP[command_name]} Reads only the saved graph; never modifies it.",
        )
        _add_repository_arg(sub)
        _add_query_arg(sub)
        _add_json_arg(sub)
        sub.set_defaults(handler=_make_relationship_handler(method_name))

    neighbors_p = subparsers.add_parser(
        "neighbors",
        help="Bounded local subgraph around a node, any edge type/direction. Reads only the saved graph.",
        description=(
            "All nodes reachable within --depth hops via any edge type, either direction. "
            "Reads only the saved graph."
        ),
    )
    _add_repository_arg(neighbors_p)
    _add_query_arg(neighbors_p)
    neighbors_p.add_argument("--depth", type=int, default=1, help="Traversal depth (default: 1).")
    _add_json_arg(neighbors_p)
    neighbors_p.set_defaults(handler=_cmd_neighbors)

    affected_p = subparsers.add_parser(
        "affected",
        help="Structural graph reachability via reverse IMPORTS/EXTENDS/IMPLEMENTS — NOT runtime impact analysis. Reads only the saved graph.",
        description=(
            "Nodes that transitively depend on this one (reverse IMPORTS/EXTENDS/IMPLEMENTS, "
            "up to --depth hops). This is structural graph reachability over syntactic "
            "relationships only — not compiler-level or runtime impact analysis. "
            "Reads only the saved graph."
        ),
    )
    _add_repository_arg(affected_p)
    _add_query_arg(affected_p)
    affected_p.add_argument("--depth", type=int, required=True, help="Traversal depth (required).")
    _add_json_arg(affected_p)
    affected_p.set_defaults(handler=_cmd_affected)

    path_p = subparsers.add_parser(
        "path",
        help="Shortest relationship chain between two nodes (graph path, not runtime execution flow). Reads only the saved graph.",
        description=(
            "Shortest chain of graph relationships connecting <source> and <target>, "
            "treating edges as undirected for connectivity. This is a structural graph "
            "path, not a claim about runtime execution or call flow. Reads only the saved graph."
        ),
    )
    _add_repository_arg(path_p)
    path_p.add_argument("source", help="Exact ID, qualified name, or simple name of the start node.")
    path_p.add_argument("target", help="Exact ID, qualified name, or simple name of the end node.")
    path_p.add_argument("--max-depth", type=int, default=6, dest="max_depth", help="Maximum hops to search (default: 6).")
    _add_json_arg(path_p)
    path_p.set_defaults(handler=_cmd_path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except CliExit as exit_:
        if exit_.message:
            print(exit_.message, file=sys.stderr)
        return exit_.code
    except Exception as exc:
        # Deliberately broad: never show a raw traceback to a CLI user for
        # an unexpected failure — print a concise message instead.
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
