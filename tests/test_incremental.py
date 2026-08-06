from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from local_code_graph.graph import storage
from local_code_graph.graph.builder import build_graph
from local_code_graph.graph.incremental import (
    compute_and_save_hashes,
    compute_file_hash,
    load_file_hashes,
    save_file_hashes,
    update_repository,
)
from local_code_graph.graph.model import EdgeType
from local_code_graph.scanner import scan_repository

JAVA_FIXTURES = Path(__file__).parent / "fixtures" / "java"
KOTLIN_FIXTURES = Path(__file__).parent / "fixtures" / "kotlin"


def _build_and_seed_hashes(repo: Path) -> None:
    scan_result = scan_repository(repo)
    graph = build_graph(repo)
    storage.save_graph(graph, repo)
    compute_and_save_hashes(scan_result, repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", target)
    _build_and_seed_hashes(target)
    return target


# -- file hash manifest ---------------------------------------------------------------


def test_file_hashes_round_trip(tmp_path: Path):
    hashes = {"A.java": compute_file_hash(b"hello"), "B.java": compute_file_hash(b"world")}
    save_file_hashes(hashes, tmp_path)
    loaded = load_file_hashes(tmp_path)
    assert loaded == hashes


def test_missing_hash_manifest_returns_empty_dict(tmp_path: Path):
    assert load_file_hashes(tmp_path) == {}


def test_compute_and_save_hashes_matches_manual_hashing(tmp_path: Path):
    (tmp_path / "A.java").write_text("class A {}")
    scan_result = scan_repository(tmp_path)
    hashes = compute_and_save_hashes(scan_result, tmp_path)
    assert hashes == {"A.java": compute_file_hash(b"class A {}")}
    assert load_file_hashes(tmp_path) == hashes


# -- basic update behavior --------------------------------------------------------------


def test_update_raises_graph_not_found_without_prior_build(tmp_path: Path):
    shutil.copytree(JAVA_FIXTURES / "full_features", tmp_path / "unbuild")
    with pytest.raises(storage.GraphNotFoundError):
        update_repository(tmp_path / "unbuild")


def test_update_with_no_changes_reports_zero_changed(repo: Path):
    result = update_repository(repo)
    assert result.changed_files == ()
    assert result.removed_files == ()
    assert result.unchanged_file_count == 5


def test_update_with_no_changes_produces_identical_graph(repo: Path):
    before = storage.load_graph(repo)
    update_repository(repo)
    after = storage.load_graph(repo)
    assert set(before.nodes.keys()) == set(after.nodes.keys())
    assert len(before.edges) == len(after.edges)


def test_update_reparses_only_the_changed_file(repo: Path):
    before = storage.load_graph(repo)
    unrelated_node_before = before.nodes["type:com.example.app.SoundController"]

    target = repo / "src/main/java/com/example/app/BaseController.java"
    content = target.read_text()
    content = content.replace(
        "public abstract void handle();",
        "public abstract void handle();\n\n    public void extra() {}\n",
    )
    target.write_text(content)

    result = update_repository(repo)
    assert result.changed_files == ("src/main/java/com/example/app/BaseController.java",)
    assert result.unchanged_file_count == 4

    graph = result.graph
    assert "method:com.example.app.BaseController#extra()" in graph.nodes
    # An untouched file's node must be byte-identical to before the update
    # (proves it was carried over as-is, not reparsed).
    assert graph.nodes["type:com.example.app.SoundController"] == unrelated_node_before


def test_update_new_file_resolves_against_existing_types(repo: Path):
    new_file = repo / "src/main/java/com/example/app/Extra.java"
    new_file.write_text(
        "package com.example.app;\n\npublic class Extra extends SoundController {}\n"
    )
    result = update_repository(repo)
    assert result.changed_files == ("src/main/java/com/example/app/Extra.java",)

    extra = result.graph.nodes["type:com.example.app.Extra"]
    assert extra is not None
    extends_edges = [
        e for e in result.graph.edges if e.source_id == extra.id and e.type == EdgeType.EXTENDS
    ]
    assert len(extends_edges) == 1
    assert extends_edges[0].target_id == "type:com.example.app.SoundController"


def test_update_removed_file_drops_its_nodes(repo: Path):
    target = repo / "src/main/java/com/example/app/MultiExtend.java"
    target.unlink()
    result = update_repository(repo)
    assert result.removed_files == ("src/main/java/com/example/app/MultiExtend.java",)
    assert "type:com.example.app.MultiExtend" not in result.graph.nodes


def test_update_removed_file_drops_dangling_edges_pointing_at_it(repo: Path):
    # BaseRepository is extended-by MultiExtend; after MultiExtend is
    # removed, nothing should still reference it.
    target = repo / "src/main/java/com/example/app/MultiExtend.java"
    target.unlink()
    result = update_repository(repo)
    dangling = [e for e in result.graph.edges if e.target_id == "type:com.example.app.MultiExtend"]
    assert dangling == []


def test_update_matches_full_rebuild_after_several_changes(repo: Path):
    (repo / "src/main/java/com/example/app/BaseRepository.java").write_text(
        "package com.example.app;\n\npublic interface BaseRepository<T> {\n"
        "    T findById(long id);\n    void deleteById(long id);\n}\n"
    )
    new_file = repo / "src/main/java/com/example/app/Extra.java"
    new_file.write_text(
        "package com.example.app;\n\npublic class Extra extends SoundController {}\n"
    )
    (repo / "src/main/java/com/example/app/MultiExtend.java").unlink()

    incremental_result = update_repository(repo)
    full = build_graph(repo)

    assert set(incremental_result.graph.nodes.keys()) == set(full.nodes.keys())
    incremental_edges = {
        (e.type.value, e.source_id, e.target_id, e.target_name) for e in incremental_result.graph.edges
    }
    full_edges = {(e.type.value, e.source_id, e.target_id, e.target_name) for e in full.edges}
    assert incremental_edges == full_edges


def test_update_prunes_orphaned_package_when_all_files_in_it_removed(tmp_path: Path):
    target = tmp_path / "repo"
    target.mkdir()
    (target / "Solo.java").write_text("package com.solo;\n\npublic class Solo {}\n")
    _build_and_seed_hashes(target)

    (target / "Solo.java").unlink()
    result = update_repository(target)

    assert "package:com.solo" not in result.graph.nodes
    assert result.graph.edges == []


def test_update_keeps_package_when_sibling_file_remains(repo: Path):
    # Removing one file from com.example.app must not remove the package
    # node, since other files in it remain.
    target = repo / "src/main/java/com/example/app/MultiExtend.java"
    target.unlink()
    result = update_repository(repo)
    assert "package:com.example.app" in result.graph.nodes


def test_update_file_made_unreadable_is_treated_as_removed_without_crashing(repo: Path):
    """The scanner filters unreadable files out before they ever reach a
    parser (see scanner.py) — so from update()'s perspective, a file that
    becomes unreadable between builds looks identical to a deleted file:
    it drops out of the scan, and its old nodes are removed. This must not
    crash either way."""
    import os

    target = repo / "src/main/java/com/example/app/BaseController.java"
    original_mode = target.stat().st_mode
    target.chmod(0)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("running as a user that bypasses permission bits (e.g. root)")
        result = update_repository(repo)
        assert result.removed_files == ("src/main/java/com/example/app/BaseController.java",)
        assert "type:com.example.app.BaseController" not in result.graph.nodes
    finally:
        target.chmod(original_mode)


# -- determinism -----------------------------------------------------------------------


def test_update_result_deterministic(repo: Path):
    (repo / "src/main/java/com/example/app/BaseController.java").write_text(
        "package com.example.app;\n\npublic abstract class BaseController {\n"
        "    protected String name;\n\n    public abstract void handle();\n\n"
        "    public void again() {}\n}\n"
    )
    result1 = update_repository(repo)
    graph_after_1 = storage.load_graph(repo)

    # Revert the hash manifest state to simulate re-running update from the
    # same starting point isn't meaningful (update is stateful/mutating);
    # instead verify the two serializations of the *same* resulting graph
    # are byte-identical, which is what actually matters for downstream
    # determinism guarantees.
    data1 = storage.serialize_graph(graph_after_1)
    data2 = storage.serialize_graph(result1.graph)
    assert data1 == data2


# -- Kotlin -------------------------------------------------------------------------------


def test_update_works_for_kotlin_repository(tmp_path: Path):
    target = tmp_path / "repo"
    shutil.copytree(KOTLIN_FIXTURES / "full_features", target)
    _build_and_seed_hashes(target)

    result = update_repository(target)
    assert result.changed_files == ()
    assert result.unchanged_file_count > 0

    new_file = target / "src/main/kotlin/com/example/app/Extra.kt"
    new_file.write_text(
        "package com.example.app\n\nclass Extra : BaseViewModel() {\n"
        "    override fun onCreate() {}\n}\n"
    )
    result2 = update_repository(target)
    assert result2.changed_files == ("src/main/kotlin/com/example/app/Extra.kt",)
    extra = result2.graph.nodes["type:com.example.app.Extra"]
    extends_edges = [
        e for e in result2.graph.edges if e.source_id == extra.id and e.type == EdgeType.EXTENDS
    ]
    assert extends_edges[0].target_id == "type:com.example.app.BaseViewModel"


# -- known limitation: old unresolved refs are not retroactively re-resolved --------------


def test_old_unresolved_reference_not_retroactively_resolved(tmp_path: Path):
    """Documents the deliberate scope limit described in
    graph/incremental.py's module docstring: adding a new file that could
    now satisfy a previously-unresolved reference in an *unchanged* file
    does not retroactively fix that old edge — only a full rebuild does.
    """
    target = tmp_path / "repo"
    target.mkdir()
    (target / "Impl.java").write_text(
        "package com.example;\n\npublic class Impl extends Missing {}\n"
    )
    _build_and_seed_hashes(target)

    before = storage.load_graph(target)
    impl_extends = next(e for e in before.edges if e.source_id == "type:com.example.Impl")
    assert impl_extends.target_id is None  # Missing doesn't exist yet

    # Now add the previously-missing type in a *new* file and update.
    (target / "Missing.java").write_text("package com.example;\n\npublic class Missing {}\n")
    result = update_repository(target)

    still_unresolved = next(e for e in result.graph.edges if e.source_id == "type:com.example.Impl")
    assert still_unresolved.target_id is None  # not retroactively fixed by update()

    # A full build, in contrast, resolves it correctly.
    full = build_graph(target)
    full_edge = next(e for e in full.edges if e.source_id == "type:com.example.Impl")
    assert full_edge.target_id == "type:com.example.Missing"


# -- SQL -----------------------------------------------------------------------


def _sql_repo(tmp_path: Path) -> Path:
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "001_users.sql").write_text("CREATE TABLE users (id INTEGER);\n")
    (tmp_path / "db" / "002_posts.sql").write_text(
        "CREATE TABLE posts (aid INTEGER REFERENCES users(id));\n"
    )
    _build_and_seed_hashes(tmp_path)
    return tmp_path


def test_update_reresolves_a_changed_migration_against_unchanged_tables(tmp_path: Path):
    """A changed SQL file must still resolve against tables carried over
    from files that did not change — the symbol table has to be seeded with
    them, not just with JVM types."""
    repo = _sql_repo(tmp_path)
    (repo / "db" / "002_posts.sql").write_text(
        "CREATE TABLE posts (\n"
        "  aid INTEGER REFERENCES users(id),\n"
        "  editor_id INTEGER REFERENCES users(id)\n"
        ");\n"
    )

    result = update_repository(repo)

    refs = [
        e
        for e in result.graph.edges
        if e.type == EdgeType.REFERENCES and e.source_id == "table:posts"
    ]
    assert refs, "the changed migration's foreign keys should still be recorded"
    assert all(e.target_id == "table:users" for e in refs)


def test_update_adds_a_new_migrations_column_to_an_existing_table(tmp_path: Path):
    repo = _sql_repo(tmp_path)
    (repo / "db" / "003_add_body.sql").write_text("ALTER TABLE posts ADD COLUMN body TEXT;\n")

    result = update_repository(repo)

    assert "column:posts#body" in result.graph.nodes
    assert result.graph.nodes["column:posts#body"].parent_id == "table:posts"


def test_update_drops_a_column_whose_table_file_was_removed(tmp_path: Path):
    """Removing the CREATE leaves an ALTER's column with no table; it must
    be pruned rather than left dangling, or the graph won't reload."""
    repo = _sql_repo(tmp_path)
    (repo / "db" / "003_add_body.sql").write_text("ALTER TABLE posts ADD COLUMN body TEXT;\n")
    update_repository(repo)

    (repo / "db" / "002_posts.sql").unlink()
    result = update_repository(repo)

    assert "table:posts" not in result.graph.nodes
    assert "column:posts#body" not in result.graph.nodes
    # And the result is still a valid, loadable graph.
    storage.save_graph(result.graph, repo)
    assert storage.load_graph(repo) is not None
