from __future__ import annotations

from pathlib import Path

from local_code_graph.graph.builder import GraphBuilder, build_graph, default_parsers
from local_code_graph.graph.model import Edge, EdgeType, Graph, Node, NodeType
from local_code_graph.scanner import scan_repository

FIXTURES = Path(__file__).parent / "fixtures" / "java"
KOTLIN_FIXTURES = Path(__file__).parent / "fixtures" / "kotlin"


def build(root: Path) -> Graph:
    return build_graph(root)


# -- node model basics ---------------------------------------------------------


def test_add_node_reports_collision():
    graph = Graph(root="/repo")
    n1 = Node(id="x", type=NodeType.CLASS, name="X", file="X.java", start_line=1, end_line=1)
    n2 = Node(id="x", type=NodeType.CLASS, name="X2", file="X2.java", start_line=1, end_line=1)
    assert graph.add_node(n1) is True
    assert graph.add_node(n2) is False
    assert graph.nodes["x"].name == "X"  # first write wins


def test_nodes_of_type_and_edge_lookups():
    graph = Graph(root="/repo")
    graph.add_node(Node(id="a", type=NodeType.CLASS, name="A", file="A.java", start_line=1, end_line=1))
    graph.add_node(Node(id="b", type=NodeType.CLASS, name="B", file="B.java", start_line=1, end_line=1))
    graph.add_edge(Edge(type=EdgeType.EXTENDS, source_id="a", target_id="b", target_name="B"))

    assert [n.id for n in graph.nodes_of_type(NodeType.CLASS)] == ["a", "b"]
    assert [e.target_id for e in graph.edges_from("a")] == ["b"]
    assert [e.source_id for e in graph.edges_to("b")] == ["a"]


# -- end to end: full_features fixture -------------------------------------------


def test_full_features_builds_without_errors():
    graph = build(FIXTURES / "full_features")
    assert graph.errors == []
    assert len(graph.nodes) > 0


def test_cross_file_extends_and_implements_resolve_within_same_package():
    graph = build(FIXTURES / "full_features")

    sound_controller_id = "type:com.example.app.SoundController"
    extends_edges = [
        e for e in graph.edges if e.type == EdgeType.EXTENDS and e.source_id == sound_controller_id
    ]
    assert len(extends_edges) == 1
    assert extends_edges[0].target_id == "type:com.example.app.BaseController"
    assert extends_edges[0].target_name == "BaseController"

    implements_edges = {
        e.target_name: e.target_id
        for e in graph.edges
        if e.type == EdgeType.IMPLEMENTS and e.source_id == sound_controller_id
    }
    assert implements_edges["Loggable"] == "type:com.example.app.Loggable"
    # Comparable<SoundController> is a JDK type, never declared in this repo.
    assert implements_edges["Comparable<SoundController>"] is None


def test_interface_multi_extends_resolves_both_targets():
    graph = build(FIXTURES / "full_features")
    multi_extend_id = "type:com.example.app.MultiExtend"
    extends_edges = {
        e.target_name: e.target_id
        for e in graph.edges
        if e.type == EdgeType.EXTENDS and e.source_id == multi_extend_id
    }
    assert extends_edges["BaseRepository<String>"] == "type:com.example.app.BaseRepository"
    assert extends_edges["Loggable"] == "type:com.example.app.Loggable"


def test_unresolvable_imports_kept_with_raw_name_and_null_target():
    graph = build(FIXTURES / "full_features")
    file_id = "file:src/main/java/com/example/app/SoundController.java"
    import_edges = [e for e in graph.edges if e.type == EdgeType.IMPORTS and e.source_id == file_id]
    raw_names = {e.target_name for e in import_edges}
    assert "java.util.List" in raw_names
    assert "com.example.util.*" in raw_names
    assert "com.example.util.Constants.MAX_SOUNDS" in raw_names
    for e in import_edges:
        assert e.target_id is None  # nothing in this repo defines any of these


def test_package_node_deduplicated_across_files():
    graph = build(FIXTURES / "full_features")
    package_nodes = [n for n in graph.nodes.values() if n.qualified_name == "com.example.app"]
    assert len(package_nodes) == 1
    package_id = package_nodes[0].id
    file_targets = {
        e.target_id for e in graph.edges if e.type == EdgeType.CONTAINS and e.source_id == package_id
    }
    # Every file in the package contributes a CONTAINS edge from the *same* package node.
    assert len(file_targets) >= 5


def test_helper_package_private_class_present():
    graph = build(FIXTURES / "full_features")
    helper = graph.nodes.get("type:com.example.app.Helper")
    assert helper is not None
    assert helper.modifiers == ()


def test_nested_members_present_with_correct_parents():
    graph = build(FIXTURES / "full_features")
    inner_base_id = "type:com.example.app.SoundController.InnerBase"
    inner_impl_id = "type:com.example.app.SoundController.InnerImpl"
    assert graph.nodes[inner_base_id].parent_id == "type:com.example.app.SoundController"
    assert graph.nodes[inner_impl_id].parent_id == "type:com.example.app.SoundController"

    inner_extends = [
        e for e in graph.edges if e.type == EdgeType.EXTENDS and e.source_id == inner_impl_id
    ]
    assert inner_extends[0].target_id == inner_base_id


# -- determinism -----------------------------------------------------------------


def test_repeated_build_is_deterministic():
    graph1 = build(FIXTURES / "full_features")
    graph2 = build(FIXTURES / "full_features")

    assert set(graph1.nodes.keys()) == set(graph2.nodes.keys())
    for node_id, n1 in graph1.nodes.items():
        n2 = graph2.nodes[node_id]
        assert n1 == n2

    edges1 = sorted(graph1.edges, key=lambda e: (e.type.value, e.source_id, e.target_name))
    edges2 = sorted(graph2.edges, key=lambda e: (e.type.value, e.source_id, e.target_name))
    assert edges1 == edges2


def test_node_ids_stable_regardless_of_scan_order():
    scan_a = scan_repository(FIXTURES / "full_features")
    scan_b = scan_repository(FIXTURES / "full_features")
    scan_b.files.reverse()

    graph_a = GraphBuilder(default_parsers()).build(scan_a)
    graph_b = GraphBuilder(default_parsers()).build(scan_b)

    assert set(graph_a.nodes.keys()) == set(graph_b.nodes.keys())


# -- malformed files at the repo level --------------------------------------------


def test_malformed_file_does_not_prevent_sibling_file_from_parsing():
    graph = build(FIXTURES / "malformed")

    assert any(msg.message == "syntax error" for msg in graph.errors) or any(
        "syntax error" in e.message for e in graph.errors
    )
    good = graph.nodes.get("type:com.example.broken.Good")
    assert good is not None
    bar = graph.nodes.get("method:com.example.broken.Good#bar()")
    assert bar is not None


def test_malformed_file_still_contributes_partial_nodes():
    graph = build(FIXTURES / "malformed")
    broken = graph.nodes.get("type:com.example.broken.Broken")
    assert broken is not None


# -- empty / no-source repositories -----------------------------------------------


def test_empty_repository_produces_empty_graph(tmp_path: Path):
    graph = build(tmp_path)
    assert graph.nodes == {}
    assert graph.edges == []
    assert graph.errors == []


def test_repository_with_only_unsupported_files_produces_empty_graph(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("hello")
    graph = build(tmp_path)
    assert graph.nodes == {}


# ==== Kotlin ======================================================================


def test_kotlin_full_features_builds_without_errors():
    graph = build(KOTLIN_FIXTURES / "full_features")
    assert graph.errors == []
    assert len(graph.nodes) > 0


def test_kotlin_cross_file_extends_and_implements_resolve():
    graph = build(KOTLIN_FIXTURES / "full_features")

    vm_id = "type:com.example.app.ProfileViewModel"
    extends_edges = {
        e.target_name: e.target_id
        for e in graph.edges
        if e.type == EdgeType.EXTENDS and e.source_id == vm_id
    }
    assert extends_edges["BaseViewModel"] == "type:com.example.app.BaseViewModel"

    implements_edges = {
        e.target_name: e.target_id
        for e in graph.edges
        if e.type == EdgeType.IMPLEMENTS and e.source_id == vm_id
    }
    assert implements_edges["Loggable"] == "type:com.example.app.Loggable"
    # Comparable<ProfileViewModel> is a stdlib type never declared in this repo.
    assert implements_edges["Comparable<ProfileViewModel>"] is None


def test_kotlin_interface_multi_extends_resolves_both_targets():
    graph = build(KOTLIN_FIXTURES / "full_features")
    multi_extend_id = "type:com.example.app.MultiExtend"
    extends_edges = {
        e.target_name: e.target_id
        for e in graph.edges
        if e.type == EdgeType.EXTENDS and e.source_id == multi_extend_id
    }
    assert extends_edges["ProfileRepository"] == "type:com.example.app.ProfileRepository"
    assert extends_edges["Loggable"] == "type:com.example.app.Loggable"


def test_kotlin_enum_implements_interface_resolves():
    graph = build(KOTLIN_FIXTURES / "full_features")
    status_id = "type:com.example.app.Status"
    implements_edges = {
        e.target_name: e.target_id
        for e in graph.edges
        if e.type == EdgeType.IMPLEMENTS and e.source_id == status_id
    }
    assert implements_edges["Loggable"] == "type:com.example.app.Loggable"


def test_kotlin_sealed_subclass_extends_resolves_within_same_file():
    graph = build(KOTLIN_FIXTURES / "full_features")
    success_id = "type:com.example.app.Result.Success"
    extends_edges = [
        e for e in graph.edges if e.type == EdgeType.EXTENDS and e.source_id == success_id
    ]
    assert extends_edges[0].target_id == "type:com.example.app.Result"


def test_kotlin_package_node_deduplicated_across_files():
    graph = build(KOTLIN_FIXTURES / "full_features")
    package_nodes = [n for n in graph.nodes.values() if n.qualified_name == "com.example.app"]
    assert len(package_nodes) == 1


def test_kotlin_companion_object_nested_correctly():
    graph = build(KOTLIN_FIXTURES / "full_features")
    companion_id = "type:com.example.app.ProfileViewModel.Companion"
    companion = graph.nodes.get(companion_id)
    assert companion is not None
    assert companion.type == NodeType.OBJECT
    assert companion.parent_id == "type:com.example.app.ProfileViewModel"
    create_id = "method:com.example.app.ProfileViewModel.Companion#create(ProfileRepository)"
    create = graph.nodes.get(create_id)
    assert create is not None
    assert create.parent_id == companion_id


def test_kotlin_repeated_build_is_deterministic():
    graph1 = build(KOTLIN_FIXTURES / "full_features")
    graph2 = build(KOTLIN_FIXTURES / "full_features")

    assert set(graph1.nodes.keys()) == set(graph2.nodes.keys())
    for node_id, n1 in graph1.nodes.items():
        assert n1 == graph2.nodes[node_id]

    edges1 = sorted(graph1.edges, key=lambda e: (e.type.value, e.source_id, e.target_name))
    edges2 = sorted(graph2.edges, key=lambda e: (e.type.value, e.source_id, e.target_name))
    assert edges1 == edges2


def test_kotlin_node_ids_stable_regardless_of_scan_order():
    scan_a = scan_repository(KOTLIN_FIXTURES / "full_features")
    scan_b = scan_repository(KOTLIN_FIXTURES / "full_features")
    scan_b.files.reverse()

    graph_a = GraphBuilder(default_parsers()).build(scan_a)
    graph_b = GraphBuilder(default_parsers()).build(scan_b)

    assert set(graph_a.nodes.keys()) == set(graph_b.nodes.keys())


def test_kotlin_malformed_file_does_not_prevent_sibling_file_from_parsing():
    graph = build(KOTLIN_FIXTURES / "malformed")

    assert any("syntax error" in e.message for e in graph.errors)
    good = graph.nodes.get("type:com.example.broken.Good")
    assert good is not None
    bar = graph.nodes.get("method:com.example.broken.Good#bar()")
    assert bar is not None


def test_mixed_java_and_kotlin_repository_builds_both(tmp_path: Path):
    (tmp_path / "Base.java").write_text(
        "package com.example;\n\npublic class Base {\n    public void foo() {}\n}\n"
    )
    (tmp_path / "Impl.kt").write_text(
        "package com.example\n\nclass Impl : Base() {\n    fun bar() {}\n}\n"
    )

    graph = build(tmp_path)

    assert graph.errors == []
    java_class = graph.nodes.get("type:com.example.Base")
    kotlin_class = graph.nodes.get("type:com.example.Impl")
    assert java_class is not None
    assert kotlin_class is not None

    # Kotlin class extends a Java class declared in the same repository: the
    # two parsers share one symbol table, so this resolves like any other
    # same-package reference.
    extends_edges = [
        e for e in graph.edges if e.type == EdgeType.EXTENDS and e.source_id == kotlin_class.id
    ]
    assert extends_edges[0].target_id == java_class.id


# ==== corrupted parser output is repaired at the graph boundary ==================
#
# Regression tests for a real failure seen on a large Kotlin repository: a
# native tree-sitter-kotlin memory-corruption bug produced nodes with
# impossible line spans (e.g. start=54, end=0) *after* the parser's own
# clamping — because the corruption can land on already-built objects, and
# results cross a process boundary (see parser/isolated.py). `lcg build`
# then reported success while writing a graph.json that failed its own
# load-time validation, leaving the user with an unusable graph.


def _merge_single_node(node: Node) -> Graph:
    from local_code_graph.graph.builder import GraphBuilder
    from local_code_graph.parser.base import FileContext, ParseResult

    result = ParseResult(
        nodes=(node,),
        edges=(),
        pending_refs=(),
        errors=(),
        context=FileContext(package=None),
    )
    graph = Graph(root="/repo")
    GraphBuilder().merge_result("X.kt", result, graph, {}, [])
    return graph


def _node_with_span(start, end) -> Node:
    return Node(
        id="type:X",
        type=NodeType.CLASS,
        name="X",
        file="X.kt",
        start_line=start,
        end_line=end,
    )


def test_merge_repairs_end_line_below_start_line():
    graph = _merge_single_node(_node_with_span(54, 0))
    node = graph.nodes["type:X"]
    assert node.start_line == 54
    assert node.end_line >= node.start_line


def test_merge_repairs_zero_and_negative_line_numbers():
    graph = _merge_single_node(_node_with_span(0, 0))
    node = graph.nodes["type:X"]
    assert node.start_line >= 1
    assert node.end_line >= node.start_line

    graph = _merge_single_node(_node_with_span(-7, -3))
    node = graph.nodes["type:X"]
    assert node.start_line >= 1
    assert node.end_line >= node.start_line


def test_merge_leaves_valid_spans_untouched():
    original = _node_with_span(10, 20)
    graph = _merge_single_node(original)
    assert graph.nodes["type:X"] == original


def test_merge_leaves_both_none_spans_untouched():
    original = _node_with_span(None, None)
    graph = _merge_single_node(original)
    assert graph.nodes["type:X"] == original


def test_graph_with_repaired_spans_survives_save_and_load(tmp_path: Path):
    from local_code_graph.graph import storage

    graph = _merge_single_node(_node_with_span(54, 0))
    storage.save_graph(graph, tmp_path)
    # The whole point: this must not raise.
    loaded = storage.load_graph(tmp_path)
    assert loaded.nodes["type:X"].end_line >= loaded.nodes["type:X"].start_line
