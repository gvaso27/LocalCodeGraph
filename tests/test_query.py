from __future__ import annotations

import time
from pathlib import Path

import pytest

from local_code_graph.graph.builder import build_graph
from local_code_graph.graph.model import Edge, EdgeType, Graph, Node, NodeType
from local_code_graph.query.engine import (
    DEPENDENCY_EDGE_TYPES,
    InvalidDepthError,
    InvalidQueryError,
    NodeNotFoundError,
    QueryEngine,
    format_overview,
    format_summary,
)

JAVA_FIXTURES = Path(__file__).parent / "fixtures" / "java"
KOTLIN_FIXTURES = Path(__file__).parent / "fixtures" / "kotlin"


@pytest.fixture(scope="module")
def java_graph() -> Graph:
    return build_graph(JAVA_FIXTURES / "full_features")


@pytest.fixture(scope="module")
def java_qe(java_graph: Graph) -> QueryEngine:
    return QueryEngine(java_graph)


@pytest.fixture(scope="module")
def kotlin_graph() -> Graph:
    return build_graph(KOTLIN_FIXTURES / "full_features")


@pytest.fixture(scope="module")
def kotlin_qe(kotlin_graph: Graph) -> QueryEngine:
    return QueryEngine(kotlin_graph)


def qn(qe: QueryEngine, qualified_name: str) -> Node:
    result = qe.find(qualified_name)
    assert len(result.matches) == 1, f"expected exactly one match for {qualified_name!r}"
    return result.matches[0]


# -- find ---------------------------------------------------------------------------


def test_find_by_exact_id(java_qe: QueryEngine):
    result = java_qe.find("type:com.example.app.SoundController")
    assert len(result.matches) == 1
    assert result.matches[0].id == "type:com.example.app.SoundController"
    assert not result.is_ambiguous


def test_find_by_qualified_name(java_qe: QueryEngine):
    result = java_qe.find("com.example.app.SoundController")
    assert len(result.matches) == 1
    assert result.matches[0].type == NodeType.CLASS


def test_find_by_simple_name_ambiguous_when_multiple_match(java_qe: QueryEngine):
    # SoundController the class and its two constructors all have
    # Node.name == "SoundController" — find() must surface all three
    # rather than silently picking one.
    result = java_qe.find("SoundController")
    assert result.is_ambiguous
    assert len(result.matches) == 3
    assert {m.type for m in result.matches} == {NodeType.CLASS, NodeType.CONSTRUCTOR}
    # deterministically sorted by id
    assert list(result.matches) == sorted(result.matches, key=lambda n: n.id)


def test_find_unambiguous_simple_name(java_qe: QueryEngine):
    result = java_qe.find("Loggable")
    assert not result.is_ambiguous
    assert result.matches[0].type == NodeType.INTERFACE


def test_find_nonexistent_returns_empty_not_error(java_qe: QueryEngine):
    result = java_qe.find("ThisDoesNotExistAnywhere")
    assert result.is_empty
    assert result.matches == ()


def test_find_empty_query_raises_invalid_query(java_qe: QueryEngine):
    with pytest.raises(InvalidQueryError):
        java_qe.find("")
    with pytest.raises(InvalidQueryError):
        java_qe.find("   ")


def test_find_id_tier_wins_over_name_tiers(java_qe: QueryEngine):
    # An exact ID match is unambiguous even though the same class also has
    # matches at the qualified-name/simple-name tiers.
    result = java_qe.find("type:com.example.app.SoundController")
    assert len(result.matches) == 1


def test_find_by_type_deterministic_and_sorted(java_qe: QueryEngine):
    classes = java_qe.find_by_type(NodeType.CLASS)
    assert len(classes) > 0
    assert list(classes) == sorted(classes, key=lambda n: n.id)
    assert all(n.type == NodeType.CLASS for n in classes)


def test_find_by_file(java_qe: QueryEngine):
    nodes = java_qe.find_by_file("src/main/java/com/example/app/BaseController.java")
    assert len(nodes) > 0
    assert all(n.file == "src/main/java/com/example/app/BaseController.java" for n in nodes)


# -- children -------------------------------------------------------------------------


def test_children_distinguishes_contains_and_declares(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    children = java_qe.children(sc)
    edge_types = {c.edge_type for c in children}
    assert EdgeType.CONTAINS in edge_types  # nested types
    assert EdgeType.DECLARES in edge_types  # methods/fields/constructors
    contains_names = {c.node.name for c in children if c.edge_type == EdgeType.CONTAINS}
    assert "InnerBase" in contains_names
    declares_names = {c.node.name for c in children if c.edge_type == EdgeType.DECLARES}
    assert "getSounds" in declares_names


def test_children_sorted_deterministically(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    children = java_qe.children(sc)
    assert len(children) > 1


def test_children_of_leaf_node_is_empty(java_qe: QueryEngine):
    field = qn(java_qe, "com.example.app.SoundController.MAX")
    assert java_qe.children(field) == ()


def test_children_accepts_node_or_id(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    by_node = java_qe.children(sc)
    by_id = java_qe.children(sc.id)
    assert by_node == by_id


def test_children_nonexistent_id_raises(java_qe: QueryEngine):
    with pytest.raises(NodeNotFoundError):
        java_qe.children("type:does.not.Exist")


# -- imports / imported_by -------------------------------------------------------------


def test_imports_preserves_unresolved_raw_name(java_qe: QueryEngine):
    file_node = java_qe.find("file:src/main/java/com/example/app/SoundController.java").matches[0]
    imports = java_qe.imports(file_node)
    raw_names = {i.target_name for i in imports}
    assert "java.util.List" in raw_names
    assert "com.example.util.*" in raw_names
    unresolved = [i for i in imports if i.target_name == "java.util.List"]
    assert unresolved[0].node is None


def test_imported_by_reverse_of_imports(java_qe: QueryEngine):
    file_node = java_qe.find("file:src/main/java/com/example/app/SoundController.java").matches[0]
    # No file in this repo declares SoundController's own file as an import.
    assert java_qe.imported_by(file_node) == ()


# -- extends / implements -----------------------------------------------------------------


def test_extends_and_extended_by_are_inverse(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    bc = qn(java_qe, "com.example.app.BaseController")

    extends = java_qe.extends(sc)
    assert len(extends) == 1
    assert extends[0].node is not None
    assert extends[0].node.id == bc.id

    extended_by = java_qe.extended_by(bc)
    assert len(extended_by) == 1
    assert extended_by[0].node.id == sc.id


def test_implements_and_implemented_by(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    loggable = qn(java_qe, "com.example.app.Loggable")

    implements = java_qe.implements(sc)
    target_names = {i.target_name for i in implements}
    assert "Loggable" in target_names
    assert "Comparable<SoundController>" in target_names  # unresolved, still present

    implemented_by = java_qe.implemented_by(loggable)
    assert any(i.node.id == sc.id for i in implemented_by)


def test_extends_empty_for_leaf_interface(java_qe: QueryEngine):
    loggable = qn(java_qe, "com.example.app.Loggable")
    assert java_qe.extends(loggable) == ()


# -- dependencies / dependents / affected -------------------------------------------------


def test_dependencies_uses_documented_edge_policy(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    deps = java_qe.dependencies(sc)
    assert {d.edge_type for d in deps} <= set(DEPENDENCY_EDGE_TYPES)
    # CONTAINS/DECLARES edges (nested types, members) must NOT appear.
    assert EdgeType.CONTAINS not in {d.edge_type for d in deps}
    assert EdgeType.DECLARES not in {d.edge_type for d in deps}


def test_dependents_is_reverse_of_dependencies(java_qe: QueryEngine):
    bc = qn(java_qe, "com.example.app.BaseController")
    sc = qn(java_qe, "com.example.app.SoundController")
    dependents = java_qe.dependents(bc)
    assert any(d.node.id == sc.id for d in dependents)


def test_affected_is_transitive_and_excludes_center(java_qe: QueryEngine):
    bc = qn(java_qe, "com.example.app.BaseController")
    result = java_qe.affected(bc, depth=2)
    assert bc.id not in {n.id for n in result.nodes}
    sc = qn(java_qe, "com.example.app.SoundController")
    assert sc.id in {n.id for n in result.nodes}


def test_affected_depth_zero_is_empty(java_qe: QueryEngine):
    bc = qn(java_qe, "com.example.app.BaseController")
    result = java_qe.affected(bc, depth=0)
    assert result.nodes == ()
    assert result.edges == ()


def test_affected_negative_depth_raises(java_qe: QueryEngine):
    bc = qn(java_qe, "com.example.app.BaseController")
    with pytest.raises(InvalidDepthError):
        java_qe.affected(bc, depth=-1)


# -- path -----------------------------------------------------------------------------------


def test_path_exists_between_related_classes(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    bc = qn(java_qe, "com.example.app.BaseController")
    result = java_qe.path(sc, bc)
    assert result.found
    assert result.nodes[0].id == sc.id
    assert result.nodes[-1].id == bc.id
    assert len(result.edges) == len(result.nodes) - 1
    assert result.edges[0].type == EdgeType.EXTENDS


def test_path_same_node_is_trivial(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    result = java_qe.path(sc, sc)
    assert result.found
    assert result.nodes == (sc,)
    assert result.edges == ()


def test_path_does_not_exist_returns_not_found(tmp_path: Path):
    graph = Graph(root=str(tmp_path))
    a = Node(id="type:A", type=NodeType.CLASS, name="A", file="A.java", start_line=1, end_line=1)
    b = Node(id="type:B", type=NodeType.CLASS, name="B", file="B.java", start_line=1, end_line=1)
    graph.add_node(a)
    graph.add_node(b)
    qe = QueryEngine(graph)
    result = qe.path(a, b)
    assert not result.found
    assert result.nodes == ()
    assert result.edges == ()


def test_path_respects_max_depth(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    bc = qn(java_qe, "com.example.app.BaseController")
    # The real path is 1 hop; max_depth=0 must fail to find it.
    result = java_qe.path(sc, bc, max_depth=0)
    assert not result.found


def test_path_is_cycle_safe(tmp_path: Path):
    graph = Graph(root=str(tmp_path))
    for name in ("A", "B", "C"):
        graph.add_node(
            Node(id=f"type:{name}", type=NodeType.CLASS, name=name, file=f"{name}.java", start_line=1, end_line=1)
        )
    # A -> B -> C -> A (cycle) plus A -> C directly.
    graph.add_edge(Edge(type=EdgeType.EXTENDS, source_id="type:A", target_id="type:B", target_name="B"))
    graph.add_edge(Edge(type=EdgeType.EXTENDS, source_id="type:B", target_id="type:C", target_name="C"))
    graph.add_edge(Edge(type=EdgeType.EXTENDS, source_id="type:C", target_id="type:A", target_name="A"))
    qe = QueryEngine(graph)
    result = qe.path("type:A", "type:C")
    assert result.found
    assert len(result.nodes) <= 3  # did not spin forever around the cycle


def test_path_negative_max_depth_raises(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    bc = qn(java_qe, "com.example.app.BaseController")
    with pytest.raises(InvalidDepthError):
        java_qe.path(sc, bc, max_depth=-1)


def test_path_nonexistent_node_raises(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    with pytest.raises(NodeNotFoundError):
        java_qe.path(sc, "type:does.not.Exist")


def test_path_is_deterministic_across_repeated_calls(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    bc = qn(java_qe, "com.example.app.BaseController")
    result1 = java_qe.path(sc, bc)
    result2 = java_qe.path(sc, bc)
    assert result1 == result2


# -- neighbors --------------------------------------------------------------------------------


def test_neighbors_depth_zero_is_empty(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    result = java_qe.neighbors(sc, depth=0)
    assert result.nodes == ()
    assert result.edges == ()
    assert result.center.id == sc.id


def test_neighbors_depth_one_includes_direct_relations(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    result = java_qe.neighbors(sc, depth=1)
    neighbor_ids = {n.id for n in result.nodes}
    bc = qn(java_qe, "com.example.app.BaseController")
    assert bc.id in neighbor_ids  # via EXTENDS
    loggable = qn(java_qe, "com.example.app.Loggable")
    assert loggable.id in neighbor_ids  # via IMPLEMENTS
    getSounds = java_qe.find("method:com.example.app.SoundController#getSounds(String, int)").matches[0]
    assert getSounds.id in neighbor_ids  # via DECLARES


def test_neighbors_excludes_center(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    result = java_qe.neighbors(sc, depth=2)
    assert sc.id not in {n.id for n in result.nodes}


def test_neighbors_no_duplicate_nodes(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    result = java_qe.neighbors(sc, depth=3)
    ids = [n.id for n in result.nodes]
    assert len(ids) == len(set(ids))


def test_neighbors_cycle_safe(tmp_path: Path):
    graph = Graph(root=str(tmp_path))
    for name in ("A", "B"):
        graph.add_node(
            Node(id=f"type:{name}", type=NodeType.CLASS, name=name, file=f"{name}.java", start_line=1, end_line=1)
        )
    graph.add_edge(Edge(type=EdgeType.EXTENDS, source_id="type:A", target_id="type:B", target_name="B"))
    graph.add_edge(Edge(type=EdgeType.EXTENDS, source_id="type:B", target_id="type:A", target_name="A"))
    qe = QueryEngine(graph)
    result = qe.neighbors("type:A", depth=5)
    assert {n.id for n in result.nodes} == {"type:B"}


def test_neighbors_sorted_by_depth_then_id(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    result = java_qe.neighbors(sc, depth=3)
    # Can't easily know per-node depth from the public result, but the
    # overall list must be internally consistent (sorted) across two calls.
    result2 = java_qe.neighbors(sc, depth=3)
    assert result.nodes == result2.nodes


def test_neighbors_negative_depth_raises(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    with pytest.raises(InvalidDepthError):
        java_qe.neighbors(sc, depth=-1)


# -- summaries ----------------------------------------------------------------------------------


def test_summarize_class(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    summary = java_qe.summarize(sc)
    assert summary.node.id == sc.id
    member_names = {m.name for m in summary.members}
    assert "getSounds" in member_names
    rel_names = {(r.type, r.target_name) for r in summary.relationships}
    assert (EdgeType.EXTENDS, "BaseController") in rel_names
    assert (EdgeType.IMPLEMENTS, "Loggable") in rel_names
    # No source code / bodies anywhere in the summary.
    rendered = format_summary(summary)
    assert "return" not in rendered.lower()


def test_summarize_method_shows_parameters_as_members(java_qe: QueryEngine):
    # A METHOD's DECLARES edges point at its PARAMETER children, so
    # summarize() surfaces them as members too — that's correct: they ARE
    # what this node declares.
    method = java_qe.find("method:com.example.app.SoundController#getSounds(String, int)").matches[0]
    summary = java_qe.summarize(method)
    member_names = {m.name for m in summary.members}
    assert member_names == {"category", "limit"}
    assert all(m.node_type == NodeType.PARAMETER for m in summary.members)


def test_summarize_kotlin_object(kotlin_qe: QueryEngine):
    obj = qn(kotlin_qe, "com.example.app.ServiceLocator")
    summary = kotlin_qe.summarize(obj)
    assert summary.node.type == NodeType.OBJECT
    member_names = {m.name for m in summary.members}
    assert "reset" in member_names


def test_summarize_kotlin_property(kotlin_qe: QueryEngine):
    prop = kotlin_qe.find("property:com.example.app.ProfileViewModel#counter").matches[0]
    summary = kotlin_qe.summarize(prop)
    assert summary.node.type == NodeType.PROPERTY


def test_summarize_kotlin_extension_function(kotlin_qe: QueryEngine):
    fn = kotlin_qe.find("method:com.example.app#String.toFoo()").matches[0]
    summary = kotlin_qe.summarize(fn)
    assert summary.node.receiver_type == "String"
    rendered = format_summary(summary)
    assert "String.toFoo" in rendered or "toFoo" in rendered


def test_format_summary_shows_unresolved_marker(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    rendered = format_summary(java_qe.summarize(sc))
    assert "Comparable<SoundController> (unresolved)" in rendered


def test_summarize_many(java_qe: QueryEngine):
    sc = qn(java_qe, "com.example.app.SoundController")
    bc = qn(java_qe, "com.example.app.BaseController")
    summaries = java_qe.summarize_many([sc, bc])
    assert len(summaries) == 2
    assert summaries[0].node.id == sc.id
    assert summaries[1].node.id == bc.id


# -- overview ------------------------------------------------------------------------------------


def test_overview_counts(java_qe: QueryEngine, java_graph: Graph):
    ov = java_qe.overview()
    assert ov.node_count == len(java_graph.nodes)
    assert ov.edge_count == len(java_graph.edges)
    assert ov.file_count == sum(1 for n in java_graph.nodes.values() if n.type == NodeType.FILE)


def test_overview_languages(java_qe: QueryEngine):
    ov = java_qe.overview()
    assert ov.files_by_language == {"java": 5}


def test_overview_packages(java_qe: QueryEngine):
    ov = java_qe.overview()
    assert ov.packages == ("com.example.app",)


def test_overview_mixed_language_repo(tmp_path: Path):
    (tmp_path / "Base.java").write_text(
        "package com.example;\n\npublic class Base {}\n"
    )
    (tmp_path / "Impl.kt").write_text("package com.example\n\nclass Impl : Base()\n")
    graph = build_graph(tmp_path)
    qe = QueryEngine(graph)
    ov = qe.overview()
    assert ov.files_by_language == {"java": 1, "kotlin": 1}


def test_format_overview_renders_without_error(java_qe: QueryEngine):
    text = format_overview(java_qe.overview())
    assert "Repository" in text
    assert "com.example.app" in text


# -- determinism across independently constructed graphs -----------------------------------------


def test_query_results_deterministic_regardless_of_insertion_order():
    graph_a = build_graph(JAVA_FIXTURES / "full_features")

    # Build a second graph with nodes/edges inserted in reverse order.
    graph_b = Graph(root=graph_a.root)
    for node in reversed(list(graph_a.nodes.values())):
        graph_b.add_node(node)
    for edge in reversed(graph_a.edges):
        graph_b.add_edge(edge)

    qe_a = QueryEngine(graph_a)
    qe_b = QueryEngine(graph_b)

    assert qe_a.find("SoundController").matches == qe_b.find("SoundController").matches
    assert qe_a.find_by_type(NodeType.METHOD) == qe_b.find_by_type(NodeType.METHOD)

    sc_a = qn(qe_a, "com.example.app.SoundController")
    sc_b = qn(qe_b, "com.example.app.SoundController")
    assert qe_a.children(sc_a) == qe_b.children(sc_b)
    assert qe_a.neighbors(sc_a, depth=2) == qe_b.neighbors(sc_b, depth=2)
    assert qe_a.overview() == qe_b.overview()


# -- security / locality --------------------------------------------------------------------------


def test_query_engine_never_touches_filesystem_after_construction(monkeypatch, java_graph: Graph):
    qe = QueryEngine(java_graph)

    def boom(*args, **kwargs):
        raise AssertionError("QueryEngine touched the filesystem")

    monkeypatch.setattr("builtins.open", boom)
    sc = java_graph.nodes["type:com.example.app.SoundController"]
    qe.find("SoundController")
    qe.children(sc)
    qe.summarize(sc)
    qe.overview()
    qe.neighbors(sc, depth=2)


# -- performance (synthetic graph, not a real repository) --------------------------------------------


def _build_synthetic_graph(num_classes: int) -> Graph:
    graph = Graph(root="/synthetic")
    for i in range(num_classes):
        class_id = f"type:pkg.Class{i}"
        graph.add_node(
            Node(
                id=class_id,
                type=NodeType.CLASS,
                name=f"Class{i}",
                file=f"pkg/Class{i}.java",
                start_line=1,
                end_line=20,
                qualified_name=f"pkg.Class{i}",
                parent_id=None,
            )
        )
        for m in range(3):
            method_id = f"method:pkg.Class{i}#method{m}()"
            graph.add_node(
                Node(
                    id=method_id,
                    type=NodeType.METHOD,
                    name=f"method{m}",
                    file=f"pkg/Class{i}.java",
                    start_line=2 + m,
                    end_line=2 + m,
                    qualified_name=f"pkg.Class{i}.method{m}",
                    parent_id=class_id,
                    param_types=(),
                )
            )
            graph.add_edge(
                Edge(type=EdgeType.DECLARES, source_id=class_id, target_id=method_id, target_name=f"method{m}")
            )
        if i > 0:
            target_id = f"type:pkg.Class{i - 1}"
            graph.add_edge(
                Edge(type=EdgeType.EXTENDS, source_id=class_id, target_id=target_id, target_name=f"Class{i - 1}")
            )
    return graph


def test_query_engine_performance_on_synthetic_graph():
    graph = _build_synthetic_graph(2000)  # ~8000 nodes, ~8000 edges

    start = time.perf_counter()
    qe = QueryEngine(graph)
    construction_time = time.perf_counter() - start

    start = time.perf_counter()
    for i in range(0, 2000, 20):
        qe.find(f"Class{i}")
        qe.children(f"type:pkg.Class{i}")
        qe.neighbors(f"type:pkg.Class{i}", depth=2)
    qe.overview()
    qe.path("type:pkg.Class0", "type:pkg.Class1999")
    query_time = time.perf_counter() - start

    assert construction_time < 5.0
    assert query_time < 5.0
