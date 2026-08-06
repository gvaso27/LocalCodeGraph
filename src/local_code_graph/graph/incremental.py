"""Incremental graph updates: reparse only files that changed since the
last build.

    old graph.json + file_hashes.json
            |
            v
    scan repository, hash every file, diff against file_hashes.json
            |
    +-------+--------+-------------------+
    |                |                   |
 unchanged        changed             removed
 (keep as-is)   (reparse via       (drop their nodes/
                 GraphBuilder)       edges/errors)
            |
            v
    merged Graph (+ a dangling-reference cleanup pass, since an unchanged
                    file's old edge might have pointed at a node that lived
                    in a file that just changed or was removed)

This module never talks to the network, executes anything from repository
content, or does anything ``lcg build`` doesn't already do — it's the same
per-file parsing and resolution logic (graph/builder.py), just applied to a
subset of files, seeded with what a previous build already determined.

Known, deliberate limitation (documented rather than worked around): only
newly reparsed files get their IMPORTS/EXTENDS/IMPLEMENTS references
resolved against the *current* full symbol table. An edge carried over
unchanged from an old, still-unresolved reference (e.g. importing a type
that didn't exist in the repo before) is **not** retroactively re-resolved
just because this update happens to add the missing type elsewhere —
re-checking every old reference against the current table would mean
re-parsing everything, which defeats the purpose of an incremental update.
Run ``lcg build`` for a full, fully-resolved rebuild.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from local_code_graph.graph.builder import (
    GraphBuilder,
    PendingItem,
    SQL_DECLARATION_NODE_TYPES,
    TYPE_NODE_TYPES,
    prune_orphaned_sql_columns,
)
from local_code_graph.graph.model import Graph, Node, NodeType, ParseError
from local_code_graph.graph.storage import (
    atomic_write_text,
    load_graph,
    save_graph,
    storage_dir,
)
from local_code_graph.parser.isolated import parse_source_files_isolated
from local_code_graph.scanner import Language, ScanResult, scan_repository

_RESOLVABLE_NODE_TYPES = TYPE_NODE_TYPES | SQL_DECLARATION_NODE_TYPES
"""Node types a cross-file reference can resolve to — mirrors what
graph/builder.py registers in its symbol table during a full build."""

HASH_ALGORITHM = "sha256"
FILE_HASHES_SCHEMA_VERSION = 1
FILE_HASHES_FILENAME = "file_hashes.json"


def file_hashes_path(repository_root: str | os.PathLike[str]) -> Path:
    return storage_dir(repository_root) / FILE_HASHES_FILENAME


def compute_file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_file_hashes(repository_root: str | os.PathLike[str]) -> dict[str, str]:
    """Returns {} if no hash manifest exists yet (e.g. the graph was saved
    by a version of this tool predating incremental updates) — that's not
    an error, it just means every file looks "changed" on the next update."""
    path = file_hashes_path(repository_root)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("files", {}))


def save_file_hashes(hashes: dict[str, str], repository_root: str | os.PathLike[str]) -> None:
    data: dict[str, Any] = {
        "schema_version": FILE_HASHES_SCHEMA_VERSION,
        "algorithm": HASH_ALGORITHM,
        "files": dict(sorted(hashes.items())),
    }
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(file_hashes_path(repository_root), content)


def compute_and_save_hashes(scan_result: ScanResult, repository_root: str | os.PathLike[str]) -> dict[str, str]:
    """Hash every file in ``scan_result`` and save the manifest.

    Meant to be called right after a full build (``lcg build``), using the
    same ScanResult the build itself already produced — so the very next
    ``lcg update`` has a correct baseline instead of finding no manifest
    and treating every file as changed.
    """
    hashes: dict[str, str] = {}
    for source_file in scan_result.files:
        try:
            content = source_file.path.read_bytes()
        except OSError:
            continue
        hashes[source_file.relative_path] = compute_file_hash(content)
    save_file_hashes(hashes, repository_root)
    return hashes


@dataclass(frozen=True)
class UpdateResult:
    graph: Graph
    changed_files: tuple[str, ...]
    removed_files: tuple[str, ...]
    unchanged_file_count: int
    new_hashes: dict[str, str]
    """Every currently-scanned file's content hash (unchanged files' old
    hashes carried through, changed files' freshly computed) — ready to
    pass straight to save_file_hashes without re-reading anything."""


def _edge_owning_file(edge, nodes_by_id: dict[str, Node]) -> str | None:
    """Which file's parse produced ``edge`` — see the module docstring.
    A PACKAGE-sourced edge (package CONTAINS file) belongs to its target
    file; every other edge belongs to its source node's file."""
    source = nodes_by_id.get(edge.source_id)
    if source is not None and source.type != NodeType.PACKAGE:
        return source.file
    target = nodes_by_id.get(edge.target_id) if edge.target_id else None
    return target.file if target is not None else None


def compute_update(
    old_graph: Graph,
    old_hashes: dict[str, str],
    scan_result: ScanResult,
    *,
    parsers=None,
) -> UpdateResult:
    """Pure function (no I/O beyond reading the changed source files
    themselves, via GraphBuilder): the actual incremental-merge logic,
    kept separate from update_repository's file-hash-manifest I/O so it's
    directly testable without touching disk for the graph/hashes."""
    new_paths = {sf.relative_path for sf in scan_result.files}
    removed_paths = set(old_hashes.keys()) - new_paths

    changed_source_files = []
    changed_items: list[tuple[Language, str, bytes]] = []
    unchanged_paths: set[str] = set()
    new_hashes: dict[str, str] = {}
    read_errors: list[ParseError] = []

    for source_file in scan_result.files:
        try:
            content = source_file.path.read_bytes()
        except OSError as exc:
            read_errors.append(
                ParseError(file=source_file.relative_path, message=f"could not read file: {exc}")
            )
            continue
        digest = compute_file_hash(content)
        new_hashes[source_file.relative_path] = digest
        if old_hashes.get(source_file.relative_path) == digest:
            unchanged_paths.add(source_file.relative_path)
        else:
            changed_source_files.append(source_file)
            changed_items.append((source_file.language, source_file.relative_path, content))

    stale_paths = removed_paths | {sf.relative_path for sf in changed_source_files}

    old_nodes_by_id = old_graph.nodes
    kept_nodes = [n for n in old_nodes_by_id.values() if n.file is None or n.file not in stale_paths]
    kept_edges = [e for e in old_graph.edges if _edge_owning_file(e, old_nodes_by_id) not in stale_paths]
    kept_errors = [e for e in old_graph.errors if e.file not in stale_paths]

    new_graph = Graph(root=old_graph.root)
    symbol_table: dict[str, str] = {}
    for node in kept_nodes:
        new_graph.add_node(node)
        # SQL declarations are resolution targets just like JVM types are
        # (a changed migration referencing an unchanged table must still
        # resolve), so they have to be seeded here too.
        if node.type in _RESOLVABLE_NODE_TYPES and node.qualified_name:
            symbol_table.setdefault(node.qualified_name, node.id)
    for edge in kept_edges:
        new_graph.add_edge(edge)
    new_graph.errors.extend(kept_errors)
    new_graph.errors.extend(read_errors)

    builder = GraphBuilder(parsers)
    pending: list[PendingItem] = []
    results = parse_source_files_isolated(changed_items)
    for relative_path, result in results.items():
        builder.merge_result(relative_path, result, new_graph, symbol_table, pending)
    builder.resolve_pending_refs(new_graph, symbol_table, pending)

    _drop_dangling_edge_targets(new_graph)
    _prune_orphaned_packages(new_graph)
    prune_orphaned_sql_columns(new_graph)

    return UpdateResult(
        graph=new_graph,
        changed_files=tuple(sorted(sf.relative_path for sf in changed_source_files)),
        removed_files=tuple(sorted(removed_paths)),
        unchanged_file_count=len(unchanged_paths),
        new_hashes=new_hashes,
    )


def _drop_dangling_edge_targets(graph: Graph) -> None:
    """An edge carried over unchanged might target a node whose file just
    changed or was removed. Rather than leave a target_id pointing at
    nothing, fall back to unresolved (matching how an edge that was never
    resolvable in the first place is represented) — the raw target_name is
    always preserved either way."""
    for index, edge in enumerate(graph.edges):
        if edge.target_id is not None and edge.target_id not in graph.nodes:
            graph.edges[index] = replace(edge, target_id=None)


def _prune_orphaned_packages(graph: Graph) -> None:
    """Remove a PACKAGE node once none of its files remain (e.g. the last
    file in that package was deleted this update)."""
    file_ids = {n.id for n in graph.nodes.values() if n.type == NodeType.FILE}
    contains_a_remaining_file: set[str] = set()
    for edge in graph.edges:
        source = graph.nodes.get(edge.source_id)
        if source is not None and source.type == NodeType.PACKAGE and edge.target_id in file_ids:
            contains_a_remaining_file.add(source.id)

    orphaned = {
        n.id
        for n in graph.nodes.values()
        if n.type == NodeType.PACKAGE and n.id not in contains_a_remaining_file
    }
    if not orphaned:
        return
    for package_id in orphaned:
        del graph.nodes[package_id]
    graph.edges[:] = [e for e in graph.edges if e.source_id not in orphaned and e.target_id not in orphaned]


def update_repository(
    repository_root: str | os.PathLike[str],
    *,
    parsers=None,
) -> UpdateResult:
    """Load the existing graph + file-hash manifest for ``repository_root``,
    reparse only what changed, and save the result (both graph.json and
    the hash manifest) back to the same location.

    Raises GraphNotFoundError if no graph has been built yet — this never
    falls back to performing a full build implicitly (the CLI's `lcg
    update` surfaces that as the same "run lcg build first" error as any
    query command hitting a missing graph).
    """
    old_graph = load_graph(repository_root)  # raises GraphNotFoundError / GraphStorageError
    old_hashes = load_file_hashes(repository_root)
    scan_result = scan_repository(repository_root)

    result = compute_update(old_graph, old_hashes, scan_result, parsers=parsers)

    save_graph(result.graph, repository_root)
    save_file_hashes(result.new_hashes, repository_root)
    return result
