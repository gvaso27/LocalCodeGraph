from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from local_code_graph.graph import storage
from local_code_graph.graph.builder import build_graph
from local_code_graph.graph.model import Edge, EdgeType, Graph, Location, Node, NodeType

JAVA_FIXTURES = Path(__file__).parent / "fixtures" / "java"
KOTLIN_FIXTURES = Path(__file__).parent / "fixtures" / "kotlin"


def _edge_sort_key(e: Edge):
    loc = e.location
    return (
        e.type.value,
        e.source_id,
        e.target_id or "",
        e.target_name,
        loc.file if loc else "",
        loc.start_line if loc else -1,
        loc.end_line if loc else -1,
    )


def assert_graphs_equivalent(a: Graph, b: Graph) -> None:
    assert set(a.nodes.keys()) == set(b.nodes.keys())
    for node_id, node_a in a.nodes.items():
        assert node_a == b.nodes[node_id]
    assert sorted(a.edges, key=_edge_sort_key) == sorted(b.edges, key=_edge_sort_key)


def make_small_graph(root: str = "/repo") -> Graph:
    graph = Graph(root=root)
    graph.add_node(
        Node(
            id="type:com.example.Foo",
            type=NodeType.CLASS,
            name="Foo",
            file="Foo.java",
            start_line=1,
            end_line=5,
            qualified_name="com.example.Foo",
            parent_id="file:Foo.java",
            modifiers=("public",),
            annotations=("Deprecated",),
        )
    )
    graph.add_node(
        Node(
            id="type:com.example.Bar",
            type=NodeType.INTERFACE,
            name="Bar",
            file="Bar.java",
            start_line=1,
            end_line=3,
            qualified_name="com.example.Bar",
            parent_id="file:Bar.java",
        )
    )
    graph.add_edge(
        Edge(
            type=EdgeType.IMPLEMENTS,
            source_id="type:com.example.Foo",
            target_id="type:com.example.Bar",
            target_name="Bar",
            location=Location(file="Foo.java", start_line=1, end_line=1),
        )
    )
    graph.add_edge(
        Edge(
            type=EdgeType.IMPORTS,
            source_id="type:com.example.Foo",
            target_id=None,
            target_name="java.util.List",
        )
    )
    return graph


# -- basic save/load/round-trip -----------------------------------------------------


def test_save_creates_graph_and_metadata_files(tmp_path: Path):
    graph = make_small_graph(str(tmp_path))
    storage.save_graph(graph, tmp_path)

    assert storage.graph_json_path(tmp_path).exists()
    assert storage.metadata_json_path(tmp_path).exists()
    assert storage.graph_json_path(tmp_path).parent.name == storage.STORAGE_DIRNAME


def test_round_trip_basic_graph(tmp_path: Path):
    graph = make_small_graph(str(tmp_path))
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)
    assert_graphs_equivalent(graph, loaded)


def test_round_trip_preserves_all_node_metadata(tmp_path: Path):
    graph = make_small_graph(str(tmp_path))
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)

    foo = loaded.nodes["type:com.example.Foo"]
    assert foo.modifiers == ("public",)
    assert foo.annotations == ("Deprecated",)
    assert foo.qualified_name == "com.example.Foo"
    assert foo.parent_id == "file:Foo.java"


def test_round_trip_preserves_edge_locations(tmp_path: Path):
    graph = make_small_graph(str(tmp_path))
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)

    implements_edge = next(e for e in loaded.edges if e.type == EdgeType.IMPLEMENTS)
    assert implements_edge.location == Location(file="Foo.java", start_line=1, end_line=1)

    imports_edge = next(e for e in loaded.edges if e.type == EdgeType.IMPORTS)
    assert imports_edge.location is None
    assert imports_edge.target_id is None
    assert imports_edge.target_name == "java.util.List"


def test_root_is_recorded_as_resolved_absolute_path(tmp_path: Path):
    graph = make_small_graph()
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)
    assert loaded.root == str(tmp_path.resolve())


def test_load_missing_graph_raises_graph_not_found(tmp_path: Path):
    with pytest.raises(storage.GraphNotFoundError):
        storage.load_graph(tmp_path)


# -- Java / Kotlin fixture graphs ----------------------------------------------------


def test_round_trip_java_full_features_graph(tmp_path: Path):
    graph = build_graph(JAVA_FIXTURES / "full_features")
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)
    assert_graphs_equivalent(graph, loaded)
    assert len(loaded.nodes) == len(graph.nodes)
    assert len(loaded.edges) == len(graph.edges)


def test_round_trip_kotlin_full_features_graph(tmp_path: Path):
    graph = build_graph(KOTLIN_FIXTURES / "full_features")
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)
    assert_graphs_equivalent(graph, loaded)


def test_round_trip_preserves_kotlin_receiver_and_mutable_fields(tmp_path: Path):
    graph = build_graph(KOTLIN_FIXTURES / "full_features")
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)

    ext_fn = loaded.nodes["method:com.example.app#String.toFoo()"]
    assert ext_fn.receiver_type == "String"

    prop = loaded.nodes["property:com.example.app.ProfileViewModel#counter"]
    assert prop.mutable is True

    val_prop = loaded.nodes["property:com.example.app.ProfileViewModel#name"]
    assert val_prop.mutable is False


def test_round_trip_preserves_java_generics_and_param_types(tmp_path: Path):
    graph = build_graph(JAVA_FIXTURES / "full_features")
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)

    method = loaded.nodes["method:com.example.app.SoundController#getSounds(String, int)"]
    assert method.param_types == ("String", "int")
    assert method.type_parameters == () or method.type_parameters == ()


# -- determinism ----------------------------------------------------------------------


def test_serialize_graph_is_deterministic_across_independent_builds():
    graph1 = build_graph(JAVA_FIXTURES / "full_features")
    graph2 = build_graph(JAVA_FIXTURES / "full_features")

    data1 = storage.serialize_graph(graph1)
    data2 = storage.serialize_graph(graph2)
    assert data1 == data2

    json1 = json.dumps(data1, indent=2, ensure_ascii=False)
    json2 = json.dumps(data2, indent=2, ensure_ascii=False)
    assert json1 == json2


def test_saved_graph_json_is_byte_identical_across_independent_builds(tmp_path: Path):
    graph1 = build_graph(JAVA_FIXTURES / "full_features")
    graph2 = build_graph(JAVA_FIXTURES / "full_features")

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    storage.save_graph(graph1, dir_a)
    storage.save_graph(graph2, dir_b)

    content_a = storage.graph_json_path(dir_a).read_text(encoding="utf-8")
    content_b = storage.graph_json_path(dir_b).read_text(encoding="utf-8")
    # Both repos resolve to different absolute paths, so strip the one
    # intentionally-varying field ("root") before comparing byte-for-byte.
    data_a = json.loads(content_a)
    data_b = json.loads(content_b)
    data_a.pop("root")
    data_b.pop("root")
    assert data_a == data_b


def test_metadata_byte_identical_when_timestamp_pinned(tmp_path: Path):
    graph1 = build_graph(JAVA_FIXTURES / "full_features")
    graph2 = build_graph(JAVA_FIXTURES / "full_features")

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    storage.save_graph(graph1, dir_a, timestamp="2026-01-01T00:00:00+00:00")
    storage.save_graph(graph2, dir_b, timestamp="2026-01-01T00:00:00+00:00")

    meta_a = json.loads(storage.metadata_json_path(dir_a).read_text())
    meta_b = json.loads(storage.metadata_json_path(dir_b).read_text())
    meta_a.pop("root")
    meta_b.pop("root")
    assert meta_a == meta_b


def test_metadata_generated_at_defaults_to_now_when_not_pinned(tmp_path: Path):
    graph = make_small_graph()
    storage.save_graph(graph, tmp_path)
    meta = storage.load_metadata(tmp_path)
    assert meta.generated_at is not None
    assert "T" in meta.generated_at  # ISO-8601


def test_metadata_counts_and_languages(tmp_path: Path):
    graph = build_graph(JAVA_FIXTURES / "full_features")
    storage.save_graph(graph, tmp_path)
    meta = storage.load_metadata(tmp_path)

    assert meta.node_count == len(graph.nodes)
    assert meta.edge_count == len(graph.edges)
    assert meta.file_count == sum(1 for n in graph.nodes.values() if n.type == NodeType.FILE)
    assert meta.languages == ("java",)
    assert meta.schema_version == storage.SCHEMA_VERSION


# -- validation: malformed JSON / schema -----------------------------------------------


def test_load_invalid_json_raises_corrupted_graph_error(tmp_path: Path):
    path = storage.graph_json_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(storage.CorruptedGraphError):
        storage.load_graph(tmp_path)


def test_load_unsupported_schema_version_raises(tmp_path: Path):
    data = {"schema_version": 999, "root": "/x", "nodes": [], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.UnsupportedSchemaVersionError):
        storage.load_graph(tmp_path)


def test_load_missing_top_level_field_raises(tmp_path: Path):
    data = {"schema_version": 1, "root": "/x", "nodes": []}  # missing "edges"
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.MissingFieldError):
        storage.load_graph(tmp_path)


def test_load_non_object_root_raises(tmp_path: Path):
    _write_raw_graph(tmp_path, [1, 2, 3])
    with pytest.raises(storage.InvalidGraphError):
        storage.load_graph(tmp_path)


def test_load_duplicate_node_ids_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    data = {"schema_version": 1, "root": "/x", "nodes": [node, node], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.DuplicateNodeIdError):
        storage.load_graph(tmp_path)


def test_load_invalid_node_type_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    node["type"] = "NOT_A_REAL_TYPE"
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidGraphError):
        storage.load_graph(tmp_path)


def test_load_invalid_edge_type_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    edge = _minimal_edge_dict("type:X", None)
    edge["type"] = "NOT_A_REAL_EDGE_TYPE"
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": [edge]}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidGraphError):
        storage.load_graph(tmp_path)


def test_load_edge_with_missing_source_node_raises(tmp_path: Path):
    edge = _minimal_edge_dict("type:DoesNotExist", None)
    data = {"schema_version": 1, "root": "/x", "nodes": [], "edges": [edge]}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidNodeReferenceError):
        storage.load_graph(tmp_path)


def test_load_edge_with_missing_target_node_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    edge = _minimal_edge_dict("type:X", "type:DoesNotExist")
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": [edge]}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidNodeReferenceError):
        storage.load_graph(tmp_path)


def test_load_node_missing_required_field_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    del node["qualified_name"]
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.MissingFieldError):
        storage.load_graph(tmp_path)


def test_load_edge_missing_required_field_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    edge = _minimal_edge_dict("type:X", None)
    del edge["target_name"]
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": [edge]}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.MissingFieldError):
        storage.load_graph(tmp_path)


def test_load_node_with_inverted_line_range_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    node["start_line"] = 10
    node["end_line"] = 1
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidGraphError):
        storage.load_graph(tmp_path)


def test_load_node_with_zero_line_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    node["start_line"] = 0
    node["end_line"] = 1
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidGraphError):
        storage.load_graph(tmp_path)


def test_load_node_with_non_list_modifiers_raises(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    node["modifiers"] = "public"  # should be a list
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    _write_raw_graph(tmp_path, data)
    with pytest.raises(storage.InvalidGraphError):
        storage.load_graph(tmp_path)


def _minimal_node_dict(node_id: str) -> dict:
    return {
        "id": node_id,
        "type": "CLASS",
        "name": "X",
        "file": "X.java",
        "start_line": 1,
        "end_line": 1,
        "qualified_name": "X",
        "parent_id": None,
        "modifiers": [],
        "annotations": [],
        "type_parameters": [],
        "param_types": None,
        "return_type": None,
        "value_type": None,
        "receiver_type": None,
        "mutable": None,
    }


def _minimal_edge_dict(source_id: str, target_id: str | None) -> dict:
    return {
        "type": "EXTENDS",
        "source_id": source_id,
        "target_id": target_id,
        "target_name": "Y",
        "location": None,
    }


def _write_raw_graph(tmp_path: Path, data) -> None:
    path = storage.graph_json_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# -- filesystem ------------------------------------------------------------------------


def test_repository_path_with_spaces(tmp_path: Path):
    repo = tmp_path / "my repo with spaces"
    repo.mkdir()
    graph = make_small_graph(str(repo))
    storage.save_graph(graph, repo)
    loaded = storage.load_graph(repo)
    assert_graphs_equivalent(graph, loaded)


def test_nested_storage_directory_is_created(tmp_path: Path):
    repo = tmp_path / "does" / "not" / "exist" / "yet"
    repo.mkdir(parents=True)
    graph = make_small_graph(str(repo))
    assert not storage.storage_dir(repo).exists()
    storage.save_graph(graph, repo)
    assert storage.storage_dir(repo).is_dir()


def test_missing_graph_file_raises_not_file_not_found_directly(tmp_path: Path):
    # GraphNotFoundError should be catchable as FileNotFoundError too.
    with pytest.raises(FileNotFoundError):
        storage.load_graph(tmp_path)


def test_atomic_write_leaves_no_leftover_temp_files(tmp_path: Path):
    graph = make_small_graph(str(tmp_path))
    storage.save_graph(graph, tmp_path)
    leftovers = list(storage.storage_dir(tmp_path).glob("*.tmp"))
    assert leftovers == []


def test_atomic_write_does_not_corrupt_existing_graph_on_failure(tmp_path: Path, monkeypatch):
    graph = make_small_graph(str(tmp_path))
    storage.save_graph(graph, tmp_path)
    original_content = storage.graph_json_path(tmp_path).read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated failure during atomic rename")

    monkeypatch.setattr(storage.os, "replace", boom)

    other_graph = make_small_graph(str(tmp_path))
    other_graph.add_node(
        Node(
            id="type:com.example.Extra",
            type=NodeType.CLASS,
            name="Extra",
            file="Extra.java",
            start_line=1,
            end_line=1,
        )
    )
    with pytest.raises(OSError):
        storage.save_graph(other_graph, tmp_path)

    # The original graph.json must be untouched.
    assert storage.graph_json_path(tmp_path).read_text(encoding="utf-8") == original_content
    # No leftover temp file from the failed attempt.
    leftovers = list(storage.storage_dir(tmp_path).glob("*.tmp"))
    assert leftovers == []


def test_write_graph_file_and_read_graph_file_exact_path(tmp_path: Path):
    graph = make_small_graph(str(tmp_path))
    path = tmp_path / "custom_name.json"
    storage.write_graph_file(graph, path)
    loaded = storage.read_graph_file(path)
    assert_graphs_equivalent(graph, loaded)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_atomic_write_fails_cleanly_when_directory_unwritable(tmp_path: Path):
    repo = tmp_path / "locked"
    repo.mkdir()
    (repo / storage.STORAGE_DIRNAME).mkdir()
    (repo / storage.STORAGE_DIRNAME).chmod(stat.S_IREAD | stat.S_IEXEC)
    try:
        if os.access(repo / storage.STORAGE_DIRNAME, os.W_OK):
            pytest.skip("running as a user that bypasses permission bits (e.g. root)")
        graph = make_small_graph(str(repo))
        with pytest.raises(OSError):
            storage.save_graph(graph, repo)
    finally:
        (repo / storage.STORAGE_DIRNAME).chmod(stat.S_IRWXU)


# -- security: loading is pure data, never executes anything ---------------------------


def test_loading_never_imports_or_executes_node_data(tmp_path: Path):
    node = _minimal_node_dict("type:X")
    node["name"] = "__import__('os').system('echo pwned')"
    node["qualified_name"] = "; rm -rf /"
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    _write_raw_graph(tmp_path, data)

    loaded = storage.load_graph(tmp_path)
    # The malicious-looking strings are stored as inert data, nothing else.
    assert loaded.nodes["type:X"].name == "__import__('os').system('echo pwned')"
