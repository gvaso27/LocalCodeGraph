"""Local, on-disk persistence for the Graph model.

    Graph -> serialize_graph -> graph.json (atomic write)
    graph.json -> deserialize_graph (validated) -> Graph

Everything here is pure file I/O against paths the caller supplies — there
is no networking, no remote database, and no opaque/binary format. Graph
data is stored as plain, human-readable, UTF-8 JSON.

Layout
------
By default a repository's graph lives inside that repository, never in a
global/shared location::

    <repository_root>/.local-code-graph/graph.json
    <repository_root>/.local-code-graph/metadata.json

``graph.json`` is the versioned graph itself (schema_version, root, nodes,
edges). ``metadata.json`` holds informational, non-authoritative build
details (generator version, counts, a generation timestamp, which
languages were seen) that are useful to a human or a future CLI but are
deliberately kept out of graph.json so that repeated builds of unchanged
source produce byte-for-byte identical graph.json output — see
"Determinism" below.

Node paths (``Node.file``) are already repository-relative, not absolute
(the scanner and parsers only ever record paths relative to the scanned
root — see scanner.py), so a graph.json is portable: copying it elsewhere
does not leak the original machine's absolute filesystem layout through
node data. The one place an absolute path does appear is the top-level
``root`` field (and metadata.json's ``root``) — kept because a human or
tool inspecting a graph later legitimately needs to know which repository
it came from; it is never required to *interpret* node/edge data, only to
identify the source repository.

Determinism
-----------
``serialize_graph`` sorts nodes by ID and edges by a canonical
(type, source, target, name, location) key before emitting JSON, and never
includes a timestamp — so building the same unchanged source tree twice
and saving both results produces byte-for-byte identical graph.json files.
(metadata.json intentionally does include a generation timestamp, and so
is *not* expected to be byte-identical across runs unless the caller pins
one via ``save_graph(..., timestamp=...)``.)

Validation
----------
Loading treats graph JSON as untrusted input: malformed JSON, an
unsupported schema version, duplicate node IDs, unknown node/edge type
values, edges referencing nodes that don't exist, and missing required
fields are all rejected with a specific exception (see the exception
classes below) rather than either crashing with a raw KeyError/TypeError
or silently "fixing" the data. Nothing in this module executes, imports,
or evaluates anything derived from the loaded JSON — deserialization only
ever constructs plain dataclass instances.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_code_graph import __version__ as _GENERATOR_VERSION
from local_code_graph.graph.model import Edge, EdgeType, Graph, Location, Node, NodeType, edge_sort_key
from local_code_graph.scanner import EXTENSION_LANGUAGE_MAP

SCHEMA_VERSION = 1

STORAGE_DIRNAME = ".local-code-graph"
GRAPH_FILENAME = "graph.json"
METADATA_FILENAME = "metadata.json"


# -- exceptions ------------------------------------------------------------------


class GraphStorageError(Exception):
    """Base class for all storage-layer failures."""


class GraphNotFoundError(GraphStorageError, FileNotFoundError):
    """No graph file exists at the expected location."""


class CorruptedGraphError(GraphStorageError):
    """The graph file is not valid JSON."""


class UnsupportedSchemaVersionError(GraphStorageError):
    """The graph file declares a schema_version this build doesn't support."""


class InvalidGraphError(GraphStorageError):
    """The JSON is well-formed but does not describe a valid graph."""


class MissingFieldError(InvalidGraphError):
    """A required field is absent from a node/edge/top-level object."""


class DuplicateNodeIdError(InvalidGraphError):
    """Two nodes in the same file share an ID."""


class InvalidNodeReferenceError(InvalidGraphError):
    """An edge refers to a node ID that doesn't exist in the same file."""


# -- metadata ----------------------------------------------------------------------


@dataclass(frozen=True)
class GraphMetadata:
    schema_version: int
    generator_version: str
    root: str
    generated_at: str | None
    file_count: int
    node_count: int
    edge_count: int
    languages: tuple[str, ...]


def _languages_in_graph(graph: Graph) -> tuple[str, ...]:
    found: set[str] = set()
    for n in graph.nodes.values():
        if n.type != NodeType.FILE or not n.name:
            continue
        suffix = "." + n.name.rsplit(".", 1)[-1] if "." in n.name else ""
        language = EXTENSION_LANGUAGE_MAP.get(suffix.lower())
        if language is not None:
            found.add(language.value)
    return tuple(sorted(found))


def _build_metadata(graph: Graph, *, root: str, timestamp: str | None) -> GraphMetadata:
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    file_count = sum(1 for n in graph.nodes.values() if n.type == NodeType.FILE)
    return GraphMetadata(
        schema_version=SCHEMA_VERSION,
        generator_version=_GENERATOR_VERSION,
        root=root,
        generated_at=timestamp,
        file_count=file_count,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
        languages=_languages_in_graph(graph),
    )


def serialize_metadata(metadata: GraphMetadata) -> dict[str, Any]:
    data = asdict(metadata)
    data["languages"] = list(metadata.languages)
    return data


def deserialize_metadata(data: Mapping[str, Any]) -> GraphMetadata:
    required = {
        "schema_version",
        "generator_version",
        "root",
        "generated_at",
        "file_count",
        "node_count",
        "edge_count",
        "languages",
    }
    missing = required - data.keys()
    if missing:
        raise MissingFieldError(f"metadata missing fields: {sorted(missing)}")
    return GraphMetadata(
        schema_version=data["schema_version"],
        generator_version=data["generator_version"],
        root=data["root"],
        generated_at=data["generated_at"],
        file_count=data["file_count"],
        node_count=data["node_count"],
        edge_count=data["edge_count"],
        languages=tuple(data["languages"]),
    )


# -- node / edge (de)serialization --------------------------------------------------

_NODE_FIELDS = (
    "id",
    "type",
    "name",
    "file",
    "start_line",
    "end_line",
    "qualified_name",
    "parent_id",
    "modifiers",
    "annotations",
    "type_parameters",
    "param_types",
    "return_type",
    "value_type",
    "receiver_type",
    "mutable",
)
_EDGE_FIELDS = ("type", "source_id", "target_id", "target_name", "location")

_VALID_NODE_TYPES = {t.value for t in NodeType}
_VALID_EDGE_TYPES = {t.value for t in EdgeType}


def node_to_dict(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type.value,
        "name": node.name,
        "file": node.file,
        "start_line": node.start_line,
        "end_line": node.end_line,
        "qualified_name": node.qualified_name,
        "parent_id": node.parent_id,
        "modifiers": list(node.modifiers),
        "annotations": list(node.annotations),
        "type_parameters": list(node.type_parameters),
        "param_types": list(node.param_types) if node.param_types is not None else None,
        "return_type": node.return_type,
        "value_type": node.value_type,
        "receiver_type": node.receiver_type,
        "mutable": node.mutable,
    }


def edge_to_dict(edge: Edge) -> dict[str, Any]:
    location = None
    if edge.location is not None:
        location = {
            "file": edge.location.file,
            "start_line": edge.location.start_line,
            "end_line": edge.location.end_line,
        }
    return {
        "type": edge.type.value,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "target_name": edge.target_name,
        "location": location,
    }


def _require_dict(raw: Any, *, what: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise InvalidGraphError(f"{what} must be a JSON object")
    return raw


def _require_fields(raw: dict[str, Any], fields: tuple[str, ...], *, what: str) -> None:
    missing = set(fields) - raw.keys()
    if missing:
        raise MissingFieldError(f"{what} missing fields: {sorted(missing)}")


def _require_str(raw: Any, *, what: str, allow_empty: bool = True) -> str:
    if not isinstance(raw, str) or (not allow_empty and not raw):
        raise InvalidGraphError(f"{what} must be a non-empty string")
    return raw


def _require_list(raw: Any, *, what: str) -> list[Any]:
    if not isinstance(raw, list):
        raise InvalidGraphError(f"{what} must be an array")
    return raw


def _is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_location_pair(start: Any, end: Any, *, what: str) -> None:
    if start is None and end is None:
        return
    if not _is_plain_int(start) or not _is_plain_int(end):
        raise InvalidGraphError(f"{what}: start_line/end_line must both be integers or both null")
    if start < 1 or end < 1:
        raise InvalidGraphError(f"{what}: line numbers must be 1-based (>= 1)")
    if start > end:
        raise InvalidGraphError(f"{what}: start_line ({start}) must be <= end_line ({end})")


def _node_from_dict(raw: Any, *, index: int) -> Node:
    what = f"node #{index}"
    raw = _require_dict(raw, what=what)
    _require_fields(raw, _NODE_FIELDS, what=what)

    node_id = _require_str(raw["id"], what=f"{what} 'id'", allow_empty=False)
    what = f"node {node_id!r}"

    type_value = raw["type"]
    if type_value not in _VALID_NODE_TYPES:
        raise InvalidGraphError(f"{what} has invalid type: {type_value!r}")

    name = _require_str(raw["name"], what=f"{what} 'name'")
    _validate_location_pair(raw["start_line"], raw["end_line"], what=what)

    modifiers = _require_list(raw["modifiers"], what=f"{what} 'modifiers'")
    annotations = _require_list(raw["annotations"], what=f"{what} 'annotations'")
    type_parameters = _require_list(raw["type_parameters"], what=f"{what} 'type_parameters'")
    param_types_raw = raw["param_types"]
    if param_types_raw is not None:
        param_types_raw = _require_list(param_types_raw, what=f"{what} 'param_types'")

    return Node(
        id=node_id,
        type=NodeType(type_value),
        name=name,
        file=raw["file"],
        start_line=raw["start_line"],
        end_line=raw["end_line"],
        qualified_name=raw["qualified_name"],
        parent_id=raw["parent_id"],
        modifiers=tuple(modifiers),
        annotations=tuple(annotations),
        type_parameters=tuple(type_parameters),
        param_types=tuple(param_types_raw) if param_types_raw is not None else None,
        return_type=raw["return_type"],
        value_type=raw["value_type"],
        receiver_type=raw["receiver_type"],
        mutable=raw["mutable"],
    )


def _edge_from_dict(raw: Any, *, index: int) -> Edge:
    what = f"edge #{index}"
    raw = _require_dict(raw, what=what)
    _require_fields(raw, _EDGE_FIELDS, what=what)

    type_value = raw["type"]
    if type_value not in _VALID_EDGE_TYPES:
        raise InvalidGraphError(f"{what} has invalid type: {type_value!r}")

    source_id = _require_str(raw["source_id"], what=f"{what} 'source_id'", allow_empty=False)
    target_id = raw["target_id"]
    if target_id is not None:
        target_id = _require_str(target_id, what=f"{what} 'target_id'", allow_empty=False)
    target_name = _require_str(raw["target_name"], what=f"{what} 'target_name'")

    location_raw = raw["location"]
    location: Location | None = None
    if location_raw is not None:
        location_raw = _require_dict(location_raw, what=f"{what} 'location'")
        _require_fields(location_raw, ("file", "start_line", "end_line"), what=f"{what} 'location'")
        _validate_location_pair(
            location_raw["start_line"], location_raw["end_line"], what=f"{what} 'location'"
        )
        location = Location(
            file=_require_str(location_raw["file"], what=f"{what} 'location.file'"),
            start_line=location_raw["start_line"],
            end_line=location_raw["end_line"],
        )

    return Edge(
        type=EdgeType(type_value),
        source_id=source_id,
        target_id=target_id,
        target_name=target_name,
        location=location,
    )


# -- graph (de)serialization ---------------------------------------------------------


def serialize_graph(graph: Graph) -> dict[str, Any]:
    """Pure transform: Graph -> a deterministic, JSON-able dict.

    Nodes are sorted by ID and edges by a canonical key so that two builds
    of the same unchanged source tree serialize identically regardless of
    file-scan order or dict-iteration order.
    """
    sorted_nodes = sorted(graph.nodes.values(), key=lambda n: n.id)
    sorted_edges = sorted(graph.edges, key=edge_sort_key)
    return {
        "schema_version": SCHEMA_VERSION,
        "root": graph.root,
        "nodes": [node_to_dict(n) for n in sorted_nodes],
        "edges": [edge_to_dict(e) for e in sorted_edges],
    }


def deserialize_graph(data: Any) -> Graph:
    """Pure transform: a parsed JSON dict -> a validated Graph.

    Raises a GraphStorageError subclass (never a bare KeyError/TypeError)
    on anything structurally wrong — see the module docstring's
    "Validation" section for exactly what is checked.
    """
    data = _require_dict(data, what="graph")
    _require_fields(data, ("schema_version", "root", "nodes", "edges"), what="graph")

    schema_version = data["schema_version"]
    if schema_version != SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"unsupported schema_version {schema_version!r}; "
            f"this build of local-code-graph supports schema_version {SCHEMA_VERSION}"
        )

    root = _require_str(data["root"], what="graph 'root'")
    raw_nodes = _require_list(data["nodes"], what="graph 'nodes'")
    raw_edges = _require_list(data["edges"], what="graph 'edges'")

    graph = Graph(root=root)
    for index, raw_node in enumerate(raw_nodes):
        node = _node_from_dict(raw_node, index=index)
        if node.id in graph.nodes:
            raise DuplicateNodeIdError(f"duplicate node id: {node.id!r}")
        graph.nodes[node.id] = node

    for index, raw_edge in enumerate(raw_edges):
        edge = _edge_from_dict(raw_edge, index=index)
        if edge.source_id not in graph.nodes:
            raise InvalidNodeReferenceError(
                f"edge #{index} ({edge.type.value}) references unknown source_id {edge.source_id!r}"
            )
        if edge.target_id is not None and edge.target_id not in graph.nodes:
            raise InvalidNodeReferenceError(
                f"edge #{index} ({edge.type.value}) references unknown target_id {edge.target_id!r}"
            )
        graph.edges.append(edge)

    return graph


# -- atomic file I/O -------------------------------------------------------------------


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` such that a concurrent reader, or a
    process interrupted mid-write, never observes a partial file.

    A temp file is created in the *same directory* as ``path`` (so the
    final rename is on one filesystem, hence atomic on POSIX), flushed and
    fsynced, then swapped into place with ``os.replace``. If anything goes
    wrong before the rename, the temp file is removed and the exception
    propagates — the original ``path`` (if any) is never touched until the
    swap succeeds, so an interrupted write cannot corrupt an existing graph.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _read_json_file(path: Path) -> Any:
    if not path.exists():
        raise GraphNotFoundError(f"no file at {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise CorruptedGraphError(f"{path} is not valid JSON: {exc}") from exc


# -- exact-path primitives -----------------------------------------------------------


def _graph_json_content(graph: Graph, *, root_override: str | None = None) -> str:
    data = serialize_graph(graph)
    if root_override is not None:
        data["root"] = root_override
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_graph_file(graph: Graph, path: str | os.PathLike[str]) -> None:
    """Atomically write ``graph`` as JSON to the exact path given."""
    atomic_write_text(Path(path), _graph_json_content(graph))


def read_graph_file(path: str | os.PathLike[str]) -> Graph:
    """Read and validate a Graph from the exact JSON file path given."""
    data = _read_json_file(Path(path))
    return deserialize_graph(data)


def write_metadata_file(metadata: GraphMetadata, path: str | os.PathLike[str]) -> None:
    content = json.dumps(serialize_metadata(metadata), indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(Path(path), content)


def read_metadata_file(path: str | os.PathLike[str]) -> GraphMetadata:
    data = _read_json_file(Path(path))
    data = _require_dict(data, what="metadata")
    return deserialize_metadata(data)


# -- repository-root convenience API --------------------------------------------------


def storage_dir(repository_root: str | os.PathLike[str]) -> Path:
    return Path(repository_root) / STORAGE_DIRNAME


def graph_json_path(repository_root: str | os.PathLike[str]) -> Path:
    return storage_dir(repository_root) / GRAPH_FILENAME


def metadata_json_path(repository_root: str | os.PathLike[str]) -> Path:
    return storage_dir(repository_root) / METADATA_FILENAME


def save_graph(
    graph: Graph,
    repository_root: str | os.PathLike[str],
    *,
    timestamp: str | None = None,
) -> None:
    """Save ``graph`` to ``<repository_root>/.local-code-graph/``.

    Writes graph.json first, then metadata.json — if metadata.json's write
    somehow fails, the (more important) graph.json has already landed
    successfully. Each file is written atomically on its own (see
    ``atomic_write_text``), but the pair is not a single transaction: a
    crash between the two writes can leave metadata.json describing an
    older graph.json. That's acceptable because metadata.json is purely
    informational — ``load_graph`` never reads it.

    ``timestamp`` overrides the auto-generated ``metadata.json``
    "generated_at" value (ISO-8601 UTC); pass a fixed string in tests that
    need byte-identical metadata.json across runs.
    """
    resolved_root = str(Path(repository_root).resolve())

    content = _graph_json_content(graph, root_override=resolved_root)
    atomic_write_text(graph_json_path(repository_root), content)

    metadata = _build_metadata(graph, root=resolved_root, timestamp=timestamp)
    write_metadata_file(metadata, metadata_json_path(repository_root))


def load_graph(repository_root: str | os.PathLike[str]) -> Graph:
    """Load and validate the graph stored at ``<repository_root>/.local-code-graph/graph.json``.

    Raises GraphNotFoundError if no graph has been saved there yet, or one
    of the other GraphStorageError subclasses if the file exists but is
    corrupted or invalid.
    """
    path = graph_json_path(repository_root)
    if not path.exists():
        raise GraphNotFoundError(f"no graph found at {path}")
    return read_graph_file(path)


def load_metadata(repository_root: str | os.PathLike[str]) -> GraphMetadata:
    path = metadata_json_path(repository_root)
    if not path.exists():
        raise GraphNotFoundError(f"no metadata found at {path}")
    return read_metadata_file(path)
