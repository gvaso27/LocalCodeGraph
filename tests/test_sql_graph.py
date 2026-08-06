"""Cross-file SQL behaviour — the parts a single-file parser test can't reach.

Foreign keys, migrations that alter a table created elsewhere, and
case-insensitive name resolution all only mean something once the whole
repository has been parsed, so they are tested through the graph builder.
"""

from __future__ import annotations

from pathlib import Path

from local_code_graph.graph import storage
from local_code_graph.graph.builder import build_graph, build_graph_isolated
from local_code_graph.graph.model import EdgeType, Graph, NodeType


def write(root: Path, name: str, sql: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql)


def edges_of(graph: Graph, edge_type: EdgeType):
    return [e for e in graph.edges if e.type == edge_type]


def reference(graph: Graph, source_id: str, target_name: str):
    for edge in edges_of(graph, EdgeType.REFERENCES):
        if edge.source_id == source_id and edge.target_name == target_name:
            return edge
    raise AssertionError(
        f"no REFERENCES edge {source_id} -> {target_name}; "
        f"have {[(e.source_id, e.target_name) for e in edges_of(graph, EdgeType.REFERENCES)]}"
    )


# -- foreign keys across files ------------------------------------------------


def test_foreign_key_resolves_to_a_table_declared_in_another_file(tmp_path: Path):
    write(tmp_path, "db/001_users.sql", "CREATE TABLE users (id INTEGER PRIMARY KEY);\n")
    write(
        tmp_path,
        "db/002_posts.sql",
        "CREATE TABLE posts (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  author_id INTEGER,\n"
        "  FOREIGN KEY (author_id) REFERENCES users(id)\n"
        ");\n",
    )

    graph = build_graph(tmp_path)

    assert reference(graph, "table:posts", "users").target_id == "table:users"


def test_reference_to_an_undeclared_table_stays_unresolved_but_recorded(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE posts (aid INT REFERENCES external_users(id));\n")

    graph = build_graph(tmp_path)

    edge = reference(graph, "table:posts", "external_users")
    assert edge.target_id is None  # never invented


def test_reference_resolution_ignores_identifier_case(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE Users (id INT);\n")
    write(tmp_path, "db/002.sql", "CREATE TABLE posts (aid INT REFERENCES USERS(id));\n")

    graph = build_graph(tmp_path)

    assert reference(graph, "table:posts", "users").target_id == "table:users"
    assert graph.nodes["table:users"].name == "Users"


def test_view_and_index_reference_their_tables_across_files(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE users (id INT, active INT);\n")
    write(
        tmp_path,
        "db/002.sql",
        "CREATE VIEW active_users AS SELECT id FROM users WHERE active = 1;\n"
        "CREATE INDEX idx_active ON users (active);\n",
    )

    graph = build_graph(tmp_path)

    assert reference(graph, "view:active_users", "users").target_id == "table:users"
    assert reference(graph, "index:idx_active", "users").target_id == "table:users"


# -- migrations ----------------------------------------------------------------


def test_alter_table_in_a_later_migration_adds_a_column_to_the_original_table(tmp_path: Path):
    write(tmp_path, "db/001_init.sql", "CREATE TABLE posts (id INTEGER PRIMARY KEY);\n")
    write(tmp_path, "db/007_add_body.sql", "ALTER TABLE posts ADD COLUMN body TEXT;\n")

    graph = build_graph(tmp_path)

    column = graph.nodes["column:posts#body"]
    assert column.type == NodeType.COLUMN
    assert column.parent_id == "table:posts"
    assert column.value_type == "TEXT"
    # The column is attributed to the migration that introduced it, while
    # the table stays attributed to the file that created it.
    assert column.file == "db/007_add_body.sql"
    assert graph.nodes["table:posts"].file == "db/001_init.sql"


def test_column_added_to_a_table_no_file_declares_is_dropped(tmp_path: Path):
    """Without a CREATE anywhere, the column has no table to belong to.
    Dropping it keeps the graph loadable instead of writing a dangling
    parent that fails validation on the next load."""
    write(tmp_path, "db/007_add_body.sql", "ALTER TABLE ghost ADD COLUMN body TEXT;\n")

    graph = build_graph(tmp_path)

    assert "column:ghost#body" not in graph.nodes
    assert not any(e.source_id == "table:ghost" for e in graph.edges)
    # The dependency on that table is still recorded.
    assert reference(graph, "file:db/007_add_body.sql", "ghost").target_id is None


def test_same_table_declared_in_two_files_merges_without_a_duplicate_error(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE users (id INT);\n")
    write(tmp_path, "db/002_recreate.sql", "CREATE TABLE users (id INT, email TEXT);\n")

    graph = build_graph(tmp_path)

    assert "table:users" in graph.nodes
    assert not any("duplicate declaration id" in e.message for e in graph.errors)
    # Columns from both declarations are present on the one table node.
    assert "column:users#id" in graph.nodes
    assert "column:users#email" in graph.nodes


# -- whole-graph integration ---------------------------------------------------


def test_sql_and_jvm_sources_coexist_in_one_graph(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE users (id INT);\n")
    (tmp_path / "User.java").write_text("package com.example;\npublic class User {}\n")

    graph = build_graph(tmp_path)

    assert graph.nodes["table:users"].type == NodeType.TABLE
    assert graph.nodes["type:com.example.User"].type == NodeType.CLASS


def test_sql_graph_round_trips_through_storage(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE users (id INT);\n")
    write(tmp_path, "db/002.sql", "CREATE TABLE posts (aid INT REFERENCES users(id));\n")
    write(tmp_path, "db/003.sql", "ALTER TABLE posts ADD COLUMN body TEXT;\n")

    graph = build_graph(tmp_path)
    storage.save_graph(graph, tmp_path)
    reloaded = storage.load_graph(tmp_path)

    assert set(reloaded.nodes) == set(graph.nodes)
    assert reference(reloaded, "table:posts", "users").target_id == "table:users"


def test_isolated_build_produces_the_same_sql_graph(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE users (id INT);\n")
    write(tmp_path, "db/002.sql", "CREATE TABLE posts (aid INT REFERENCES users(id));\n")
    write(tmp_path, "db/003.sql", "ALTER TABLE posts ADD COLUMN body TEXT;\n")

    direct = build_graph(tmp_path)
    isolated = build_graph_isolated(tmp_path)

    assert set(direct.nodes) == set(isolated.nodes)


def test_metadata_reports_sql_as_a_language(tmp_path: Path):
    write(tmp_path, "db/001.sql", "CREATE TABLE users (id INT);\n")

    graph = build_graph(tmp_path)
    storage.save_graph(graph, tmp_path)

    assert "sql" in storage.load_metadata(tmp_path).languages
