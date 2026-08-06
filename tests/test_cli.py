from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from local_code_graph.cli import (
    EXIT_AMBIGUOUS,
    EXIT_GRAPH_NOT_FOUND,
    EXIT_INVALID_ARGS,
    EXIT_INVALID_GRAPH,
    EXIT_NOT_FOUND,
    EXIT_OK,
)

JAVA_FIXTURES = Path(__file__).parent / "fixtures" / "java"
KOTLIN_FIXTURES = Path(__file__).parent / "fixtures" / "kotlin"


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "local_code_graph", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )


@pytest.fixture(scope="module")
def built_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of the Java full_features fixture, already built once via the CLI."""
    repo = tmp_path_factory.mktemp("java_repo")
    shutil.copytree(JAVA_FIXTURES / "full_features", repo, dirs_exist_ok=True)
    result = run_cli("build", str(repo))
    assert result.returncode == EXIT_OK, result.stderr
    return repo


@pytest.fixture(scope="module")
def built_kotlin_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("kotlin_repo")
    shutil.copytree(KOTLIN_FIXTURES / "full_features", repo, dirs_exist_ok=True)
    result = run_cli("build", str(repo))
    assert result.returncode == EXIT_OK, result.stderr
    return repo


# -- entry points ------------------------------------------------------------------


def test_help_exits_zero():
    result = run_cli("--help")
    assert result.returncode == EXIT_OK
    assert "build" in result.stdout
    assert "overview" in result.stdout


def test_version_flag():
    result = run_cli("--version")
    assert result.returncode == EXIT_OK
    assert "local-code-graph" in result.stdout


def test_lcg_script_entry_point_matches_module_invocation(built_repo: Path):
    """The installed `lcg` console script must behave the same as
    `python -m local_code_graph` (Phase 6 section 3's "consistent" requirement)."""
    lcg_path = Path(sys.executable).parent / "lcg"
    if not lcg_path.exists():
        pytest.skip("lcg console script not installed in this environment")
    result = subprocess.run([str(lcg_path), "overview", str(built_repo)], capture_output=True, text=True)
    module_result = run_cli("overview", str(built_repo))
    assert result.returncode == module_result.returncode == EXIT_OK
    assert result.stdout == module_result.stdout


def test_subcommand_help_mentions_source_vs_graph_access():
    build_help = run_cli("build", "--help")
    assert "source" in build_help.stdout.lower()

    find_help = run_cli("find", "--help")
    assert "graph" in find_help.stdout.lower()


# -- build --------------------------------------------------------------------------


def test_build_creates_graph_and_metadata(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    result = run_cli("build", str(repo))
    assert result.returncode == EXIT_OK, result.stderr
    assert (repo / ".local-code-graph" / "graph.json").exists()
    assert (repo / ".local-code-graph" / "metadata.json").exists()


def test_build_prints_human_summary(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    result = run_cli("build", str(repo))
    assert "Built local code graph" in result.stdout
    assert "Files:" in result.stdout
    assert "Nodes:" in result.stdout
    assert "Edges:" in result.stdout
    assert "Languages:" in result.stdout
    assert "Java" in result.stdout


def test_build_json_output(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    result = run_cli("build", str(repo), "--json")
    data = json.loads(result.stdout)
    assert data["files"] == 5
    assert data["languages"] == ["java"]
    assert "graph_path" in data


def test_build_does_not_print_source_code(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    result = run_cli("build", str(repo))
    assert "public class" not in result.stdout
    assert "return" not in result.stdout


def test_build_output_deterministic_across_independent_builds(tmp_path: Path):
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo_a)
    shutil.copytree(JAVA_FIXTURES / "full_features", repo_b)
    run_cli("build", str(repo_a))
    run_cli("build", str(repo_b))

    graph_a = json.loads((repo_a / ".local-code-graph" / "graph.json").read_text())
    graph_b = json.loads((repo_b / ".local-code-graph" / "graph.json").read_text())
    graph_a.pop("root")
    graph_b.pop("root")
    assert graph_a == graph_b


def test_build_nonexistent_repository_fails_clearly(tmp_path: Path):
    result = run_cli("build", str(tmp_path / "does" / "not" / "exist"))
    assert result.returncode != EXIT_OK
    assert result.stderr.strip() != ""


# -- update -----------------------------------------------------------------------------


def test_update_without_prior_build_fails_like_a_query_command(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    result = run_cli("update", str(repo))
    assert result.returncode == EXIT_GRAPH_NOT_FOUND
    assert "lcg build" in result.stderr


def test_update_after_build_reports_zero_changed(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    run_cli("build", str(repo))
    result = run_cli("update", str(repo))
    assert result.returncode == EXIT_OK
    assert "Changed: 0" in result.stdout
    assert "Unchanged: 5" in result.stdout


def test_update_detects_edited_file_json(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    run_cli("build", str(repo))

    target = repo / "src/main/java/com/example/app/Loggable.java"
    target.write_text(target.read_text() + "\n// edited\n")

    result = run_cli("update", str(repo), "--json")
    assert result.returncode == EXIT_OK
    data = json.loads(result.stdout)
    assert data["changed_files"] == ["src/main/java/com/example/app/Loggable.java"]
    assert data["unchanged_file_count"] == 4


def test_update_then_query_reflects_new_declaration(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    run_cli("build", str(repo))

    (repo / "src/main/java/com/example/app/Extra.java").write_text(
        "package com.example.app;\n\npublic class Extra extends SoundController {}\n"
    )
    update_result = run_cli("update", str(repo))
    assert update_result.returncode == EXIT_OK

    find_result = run_cli("find", str(repo), "com.example.app.Extra")
    assert find_result.returncode == EXIT_OK
    assert "Extra" in find_result.stdout

    path_result = run_cli(
        "path", str(repo), "com.example.app.Extra", "com.example.app.SoundController"
    )
    assert path_result.returncode == EXIT_OK
    assert "EXTENDS" in path_result.stdout


def test_update_after_removing_file_drops_it_from_queries(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    run_cli("build", str(repo))

    (repo / "src/main/java/com/example/app/MultiExtend.java").unlink()
    update_result = run_cli("update", str(repo))
    assert "Removed: 1" in update_result.stdout

    find_result = run_cli("find", str(repo), "com.example.app.MultiExtend")
    assert find_result.returncode == EXIT_OK
    assert "No match found" in find_result.stdout


def test_update_corrupted_graph_error(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".local-code-graph").mkdir(parents=True)
    (repo / ".local-code-graph" / "graph.json").write_text("{not valid json")
    result = run_cli("update", str(repo))
    assert result.returncode == EXIT_INVALID_GRAPH


def test_update_help_documents_reparse_scope():
    result = run_cli("update", "--help")
    assert result.returncode == EXIT_OK
    assert "changed" in result.stdout.lower()


# -- overview / find / show ----------------------------------------------------------


def test_overview_human(built_repo: Path):
    result = run_cli("overview", str(built_repo))
    assert result.returncode == EXIT_OK
    assert "Repository" in result.stdout
    assert "com.example.app" in result.stdout


def test_overview_json(built_repo: Path):
    result = run_cli("overview", str(built_repo), "--json")
    data = json.loads(result.stdout)
    assert data["file_count"] == 5
    assert data["packages"] == ["com.example.app"]


def test_find_human(built_repo: Path):
    result = run_cli("find", str(built_repo), "com.example.app.BaseController")
    assert result.returncode == EXIT_OK
    assert "CLASS" in result.stdout
    assert "com.example.app.BaseController" in result.stdout


def test_find_json(built_repo: Path):
    result = run_cli("find", str(built_repo), "com.example.app.BaseController", "--json")
    data = json.loads(result.stdout)
    assert len(data["matches"]) == 1
    assert data["matches"][0]["id"] == "type:com.example.app.BaseController"


def test_find_no_match_exits_ok(built_repo: Path):
    result = run_cli("find", str(built_repo), "TotallyNotThere")
    assert result.returncode == EXIT_OK
    assert "No match found" in result.stdout


def test_show_human(built_repo: Path):
    result = run_cli("show", str(built_repo), "com.example.app.SoundController")
    assert result.returncode == EXIT_OK
    assert "CLASS com.example.app.SoundController" in result.stdout
    assert "members:" in result.stdout
    assert "relationships:" in result.stdout
    assert "getSounds" in result.stdout


def test_show_json(built_repo: Path):
    result = run_cli("show", str(built_repo), "com.example.app.SoundController", "--json")
    data = json.loads(result.stdout)
    assert data["node"]["id"] == "type:com.example.app.SoundController"
    assert any(m["name"] == "getSounds" for m in data["members"])
    assert any(r["target_name"] == "BaseController" for r in data["relationships"])


def test_show_never_prints_source_code(built_repo: Path):
    result = run_cli("show", str(built_repo), "com.example.app.SoundController")
    assert "return" not in result.stdout


# -- children / imports / inheritance / dependencies ------------------------------------


def test_children_human(built_repo: Path):
    result = run_cli("children", str(built_repo), "com.example.app.SoundController")
    assert result.returncode == EXIT_OK
    assert "DECLARES" in result.stdout
    assert "getSounds" in result.stdout


def test_children_json(built_repo: Path):
    result = run_cli("children", str(built_repo), "com.example.app.SoundController", "--json")
    data = json.loads(result.stdout)
    assert any(r["target_name"] == "getSounds" for r in data["relationships"])


def test_imports_shows_unresolved(built_repo: Path):
    file_id = "file:src/main/java/com/example/app/SoundController.java"
    result = run_cli("imports", str(built_repo), file_id, "--json")
    data = json.loads(result.stdout)
    names = {r["target_name"] for r in data["relationships"]}
    assert "java.util.List" in names
    unresolved = [r for r in data["relationships"] if r["target_name"] == "java.util.List"]
    assert unresolved[0]["node"] is None


def test_extends_and_extended_by(built_repo: Path):
    result = run_cli("extends", str(built_repo), "com.example.app.SoundController")
    assert "BaseController" in result.stdout

    result = run_cli("extended-by", str(built_repo), "com.example.app.BaseController")
    assert "SoundController" in result.stdout


def test_implements_and_implemented_by(built_repo: Path):
    result = run_cli("implements", str(built_repo), "com.example.app.SoundController")
    assert "Loggable" in result.stdout

    result = run_cli("implemented-by", str(built_repo), "com.example.app.Loggable")
    assert "SoundController" in result.stdout


def test_dependencies_and_dependents(built_repo: Path):
    result = run_cli("dependencies", str(built_repo), "com.example.app.SoundController", "--json")
    data = json.loads(result.stdout)
    edge_types = {r["edge_type"] for r in data["relationships"]}
    assert edge_types <= {"IMPORTS", "EXTENDS", "IMPLEMENTS"}

    result = run_cli("dependents", str(built_repo), "com.example.app.BaseController", "--json")
    data = json.loads(result.stdout)
    assert any(r["node"] and "SoundController" in r["node"]["qualified_name"] for r in data["relationships"])


# -- neighbors / affected / path --------------------------------------------------------


def test_neighbors_default_depth(built_repo: Path):
    result = run_cli("neighbors", str(built_repo), "com.example.app.SoundController", "--json")
    data = json.loads(result.stdout)
    assert data["depth"] == 1
    assert len(data["nodes"]) > 0


def test_neighbors_custom_depth(built_repo: Path):
    result = run_cli("neighbors", str(built_repo), "com.example.app.SoundController", "--depth", "2", "--json")
    data = json.loads(result.stdout)
    assert data["depth"] == 2


def test_affected_requires_depth_flag(built_repo: Path):
    result = run_cli("affected", str(built_repo), "com.example.app.BaseController")
    assert result.returncode == EXIT_INVALID_ARGS


def test_affected_with_depth(built_repo: Path):
    result = run_cli("affected", str(built_repo), "com.example.app.BaseController", "--depth", "2", "--json")
    assert result.returncode == EXIT_OK
    data = json.loads(result.stdout)
    assert any("SoundController" in n["qualified_name"] for n in data["nodes"])


def test_path_found(built_repo: Path):
    result = run_cli("path", str(built_repo), "com.example.app.SoundController", "com.example.app.BaseController")
    assert result.returncode == EXIT_OK
    assert "SoundController" in result.stdout
    assert "BaseController" in result.stdout
    assert "EXTENDS" in result.stdout


def test_path_json(built_repo: Path):
    result = run_cli(
        "path", str(built_repo), "com.example.app.SoundController", "com.example.app.BaseController", "--json"
    )
    data = json.loads(result.stdout)
    assert data["found"] is True
    assert len(data["edges"]) == 1


def test_path_not_found_exits_ok_with_message(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.java").write_text("public class A {}\n")
    (repo / "B.java").write_text("public class B {}\n")
    run_cli("build", str(repo))
    result = run_cli("path", str(repo), "A", "B")
    assert result.returncode == EXIT_OK
    assert "No path found" in result.stdout


def test_path_max_depth(built_repo: Path):
    result = run_cli(
        "path",
        str(built_repo),
        "com.example.app.SoundController",
        "com.example.app.BaseController",
        "--max-depth",
        "0",
    )
    assert "No path found" in result.stdout


# -- Kotlin -------------------------------------------------------------------------------


def test_kotlin_repo_build_and_query(built_kotlin_repo: Path):
    result = run_cli("overview", str(built_kotlin_repo), "--json")
    data = json.loads(result.stdout)
    assert data["files_by_language"] == {"kotlin": data["file_count"]}

    result = run_cli("show", str(built_kotlin_repo), "com.example.app.ServiceLocator", "--json")
    data = json.loads(result.stdout)
    assert data["node"]["type"] == "OBJECT"


# -- JSON hygiene -------------------------------------------------------------------------


def test_json_output_is_only_json_nothing_else(built_repo: Path):
    result = run_cli("overview", str(built_repo), "--json")
    # The entire stdout must parse as one JSON document — no banners, no logs mixed in.
    json.loads(result.stdout)
    assert result.stdout.strip().startswith("{")


def test_json_output_deterministic(built_repo: Path):
    result1 = run_cli("show", str(built_repo), "com.example.app.SoundController", "--json")
    result2 = run_cli("show", str(built_repo), "com.example.app.SoundController", "--json")
    assert result1.stdout == result2.stdout


# -- errors ------------------------------------------------------------------------------


def test_missing_repository_argument():
    result = run_cli("overview")
    assert result.returncode == EXIT_INVALID_ARGS
    assert result.stdout == ""


def test_missing_graph_error(tmp_path: Path):
    repo = tmp_path / "no_graph"
    repo.mkdir()
    result = run_cli("overview", str(repo))
    assert result.returncode == EXIT_GRAPH_NOT_FOUND
    assert "No local code graph exists" in result.stderr
    assert "lcg build" in result.stderr
    assert result.stdout == ""


def test_corrupted_graph_error(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".local-code-graph").mkdir(parents=True)
    (repo / ".local-code-graph" / "graph.json").write_text("{not valid json")
    result = run_cli("overview", str(repo))
    assert result.returncode == EXIT_INVALID_GRAPH
    assert "invalid or corrupted" in result.stderr


def test_invalid_command_exits_two():
    result = run_cli("not-a-real-command", ".")
    assert result.returncode == EXIT_INVALID_ARGS
    assert result.stdout == ""


def test_nonexistent_symbol_exits_four(built_repo: Path):
    result = run_cli("show", str(built_repo), "ThisSymbolDoesNotExist")
    assert result.returncode == EXIT_NOT_FOUND
    assert result.stdout == ""


def test_ambiguous_symbol_exits_five(built_repo: Path):
    result = run_cli("show", str(built_repo), "SoundController")
    assert result.returncode == EXIT_AMBIGUOUS
    assert "Ambiguous symbol" in result.stderr
    assert "Matches:" in result.stderr
    assert result.stdout == ""


def test_no_traceback_leaks_to_user(built_repo: Path):
    result = run_cli("show", str(built_repo), "ThisSymbolDoesNotExist")
    assert "Traceback" not in result.stderr
    assert "Traceback" not in result.stdout


# -- critical: queries never require source access ------------------------------------------


def test_queries_work_after_source_files_are_removed(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    build_result = run_cli("build", str(repo))
    assert build_result.returncode == EXIT_OK

    # Remove every source file — keep only the saved graph directory.
    graph_dir = repo / ".local-code-graph"
    for child in repo.iterdir():
        if child != graph_dir:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    remaining = {p.name for p in repo.iterdir()}
    assert remaining == {".local-code-graph"}

    overview = run_cli("overview", str(repo))
    assert overview.returncode == EXIT_OK
    assert "com.example.app" in overview.stdout

    find = run_cli("find", str(repo), "com.example.app.SoundController")
    assert find.returncode == EXIT_OK
    assert "SoundController" in find.stdout

    show = run_cli("show", str(repo), "com.example.app.SoundController")
    assert show.returncode == EXIT_OK
    assert "members:" in show.stdout

    neighbors = run_cli("neighbors", str(repo), "com.example.app.SoundController", "--depth", "2")
    assert neighbors.returncode == EXIT_OK

    path = run_cli("path", str(repo), "com.example.app.SoundController", "com.example.app.BaseController")
    assert path.returncode == EXIT_OK
    assert "EXTENDS" in path.stdout


def test_queries_work_when_source_directory_is_unreadable(tmp_path: Path):
    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)
    run_cli("build", str(repo))

    src_dir = repo / "src"
    original_mode = src_dir.stat().st_mode
    src_dir.chmod(0o000)
    try:
        result = run_cli("overview", str(repo))
        assert result.returncode == EXIT_OK, result.stderr
        result = run_cli("show", str(repo), "com.example.app.SoundController")
        assert result.returncode == EXIT_OK, result.stderr
    finally:
        src_dir.chmod(original_mode)


# -- network safety (static check on the CLI module itself) --------------------------------


def test_cli_module_has_no_network_imports():
    cli_source = (Path(__file__).parent.parent / "src" / "local_code_graph" / "cli.py").read_text()
    forbidden = ["socket", "urllib", "http.client", "requests", "httpx", "aiohttp"]
    for token in forbidden:
        assert token not in cli_source, f"unexpected networking reference: {token}"


# -- SQL through the CLI -------------------------------------------------------


def _sql_repo(root: Path) -> Path:
    (root / "db").mkdir(parents=True)
    (root / "db" / "001_init.sql").write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT);\n"
        "CREATE TABLE posts (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  author_id INTEGER,\n"
        "  FOREIGN KEY (author_id) REFERENCES users(id)\n"
        ");\n"
        "CREATE INDEX idx_author ON posts (author_id);\n"
    )
    (root / "db" / "002_add_body.sql").write_text("ALTER TABLE posts ADD COLUMN body TEXT;\n")
    return root


def test_build_reports_sql_as_a_language(tmp_path: Path):
    repo = _sql_repo(tmp_path / "repo")
    result = run_cli("build", str(repo))
    assert result.returncode == EXIT_OK, result.stderr
    assert "Sql" in result.stdout


def test_build_json_lists_sql(tmp_path: Path):
    repo = _sql_repo(tmp_path / "repo")
    result = run_cli("build", str(repo), "--json")
    data = json.loads(result.stdout)
    assert data["languages"] == ["sql"]
    assert data["skipped_files"] == []


def test_overview_counts_sql_declarations(tmp_path: Path):
    repo = _sql_repo(tmp_path / "repo")
    run_cli("build", str(repo))
    result = run_cli("overview", str(repo))
    assert "Tables: 2" in result.stdout
    assert "Indexes: 1" in result.stdout


def test_show_a_table_lists_its_columns(tmp_path: Path):
    repo = _sql_repo(tmp_path / "repo")
    run_cli("build", str(repo))
    result = run_cli("show", str(repo), "posts")
    assert "TABLE posts" in result.stdout
    assert "author_id" in result.stdout
    # ...including one added by a later migration file.
    assert "body" in result.stdout


def test_dependents_of_a_table_include_its_foreign_keys(tmp_path: Path):
    repo = _sql_repo(tmp_path / "repo")
    run_cli("build", str(repo))
    result = run_cli("dependents", str(repo), "users")
    assert "posts" in result.stdout


def test_build_does_not_print_sql_contents(tmp_path: Path):
    repo = _sql_repo(tmp_path / "repo")
    result = run_cli("build", str(repo))
    assert "CREATE TABLE" not in result.stdout
    assert "PRIMARY KEY" not in result.stdout
