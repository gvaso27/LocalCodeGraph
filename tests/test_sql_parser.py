from __future__ import annotations

from local_code_graph.graph.model import EdgeType, NodeType
from local_code_graph.parser.sql import SqlParser, canonical_identifier, display_identifier


def parse(source: str, path: str = "schema.sql"):
    return SqlParser().parse(path, source.encode("utf-8"))


def node(result, node_id: str):
    for n in result.nodes:
        if n.id == node_id:
            return n
    raise AssertionError(f"no node with id {node_id!r} in {[n.id for n in result.nodes]}")


def find_nodes(result, node_type: NodeType):
    return [n for n in result.nodes if n.type == node_type]


def pending(result, edge_type: EdgeType):
    return [p for p in result.pending_refs if p.type == edge_type]


def edges(result, edge_type: EdgeType):
    return [e for e in result.edges if e.type == edge_type]


# -- file node -------------------------------------------------------------


def test_file_node_is_created():
    result = parse("CREATE TABLE t (a INT);\n", path="db/001.sql")
    file_node = node(result, "file:db/001.sql")
    assert file_node.type == NodeType.FILE
    assert file_node.name == "001.sql"


def test_empty_file_produces_only_a_file_node():
    result = parse("")
    assert [n.type for n in result.nodes] == [NodeType.FILE]


def test_sql_has_no_package_context():
    result = parse("CREATE TABLE t (a INT);\n")
    assert result.context.package is None
    assert result.context.imports == ()


# -- CREATE TABLE ----------------------------------------------------------


def test_create_table_produces_table_node_contained_by_file():
    result = parse("CREATE TABLE users (id INT);\n")
    table = node(result, "table:users")
    assert table.type == NodeType.TABLE
    assert table.name == "users"
    assert table.qualified_name == "users"
    assert table.parent_id == "file:schema.sql"
    contains = edges(result, EdgeType.CONTAINS)
    assert any(e.source_id == "file:schema.sql" and e.target_id == "table:users" for e in contains)


def test_table_columns_become_column_nodes():
    result = parse(
        "CREATE TABLE users (\n"
        "  id INTEGER PRIMARY KEY,\n"
        "  email TEXT NOT NULL UNIQUE,\n"
        "  age INT\n"
        ");\n"
    )
    columns = find_nodes(result, NodeType.COLUMN)
    assert {c.name for c in columns} == {"id", "email", "age"}
    email = node(result, "column:users#email")
    assert email.parent_id == "table:users"
    assert email.qualified_name == "users.email"
    assert email.value_type == "TEXT"
    assert node(result, "column:users#id").value_type == "INTEGER"


def test_table_declares_its_columns():
    result = parse("CREATE TABLE t (a INT, b TEXT);\n")
    declares = edges(result, EdgeType.DECLARES)
    assert {(e.source_id, e.target_id) for e in declares} == {
        ("table:t", "column:t#a"),
        ("table:t", "column:t#b"),
    }


def test_if_not_exists_is_accepted():
    result = parse("CREATE TABLE IF NOT EXISTS users (id INT);\n")
    assert node(result, "table:users").type == NodeType.TABLE


def test_column_without_declared_type():
    result = parse("CREATE TABLE t (a);\n")
    assert node(result, "column:t#a").value_type is None


# -- foreign keys ----------------------------------------------------------


def test_table_level_foreign_key_becomes_references_ref():
    result = parse(
        "CREATE TABLE posts (\n"
        "  author_id INT,\n"
        "  FOREIGN KEY (author_id) REFERENCES users(id)\n"
        ");\n"
    )
    refs = pending(result, EdgeType.REFERENCES)
    assert [(r.source_id, r.raw_name) for r in refs] == [("table:posts", "users")]


def test_inline_column_foreign_key_becomes_references_ref():
    result = parse("CREATE TABLE posts (author_id INT REFERENCES users(id));\n")
    refs = pending(result, EdgeType.REFERENCES)
    assert [(r.source_id, r.raw_name) for r in refs] == [("table:posts", "users")]


def test_primary_key_and_unique_constraints_create_no_references():
    result = parse("CREATE TABLE t (a INT, PRIMARY KEY (a), UNIQUE (a));\n")
    assert pending(result, EdgeType.REFERENCES) == []


def test_foreign_key_reference_records_a_location():
    result = parse("CREATE TABLE posts (\n  aid INT REFERENCES users(id)\n);\n")
    ref = pending(result, EdgeType.REFERENCES)[0]
    assert ref.location is not None
    assert ref.location.file == "schema.sql"
    assert ref.location.start_line == 2


# -- views / indexes / triggers ---------------------------------------------


def test_create_view_references_the_tables_it_selects_from():
    result = parse("CREATE VIEW v AS SELECT id FROM users WHERE id > 0;\n")
    view = node(result, "view:v")
    assert view.type == NodeType.VIEW
    assert [(r.source_id, r.raw_name) for r in pending(result, EdgeType.REFERENCES)] == [
        ("view:v", "users")
    ]


def test_create_view_references_every_joined_table():
    result = parse(
        "CREATE VIEW v AS SELECT u.id FROM users u JOIN posts p ON p.author_id = u.id;\n"
    )
    names = {r.raw_name for r in pending(result, EdgeType.REFERENCES)}
    assert names == {"users", "posts"}


def test_view_aliases_resolve_to_the_real_table_not_the_alias():
    result = parse("CREATE VIEW v AS SELECT u.id FROM users u;\n")
    names = {r.raw_name for r in pending(result, EdgeType.REFERENCES)}
    assert names == {"users"}


def test_create_index_references_its_table():
    result = parse("CREATE INDEX idx_author ON posts (author_id);\n")
    index = node(result, "index:idx_author")
    assert index.type == NodeType.INDEX
    assert index.name == "idx_author"
    assert [(r.source_id, r.raw_name) for r in pending(result, EdgeType.REFERENCES)] == [
        ("index:idx_author", "posts")
    ]


def test_unique_index_is_still_an_index():
    result = parse("CREATE UNIQUE INDEX ix ON t (a, b);\n")
    assert node(result, "index:ix").type == NodeType.INDEX


def test_create_trigger_references_its_table():
    result = parse(
        "CREATE TRIGGER trg AFTER DELETE ON users FOR EACH ROW EXECUTE FUNCTION f();\n"
    )
    trigger = node(result, "trigger:trg")
    assert trigger.type == NodeType.TRIGGER
    refs = [(r.source_id, r.raw_name) for r in pending(result, EdgeType.REFERENCES)]
    assert ("trigger:trg", "users") in refs


def test_index_name_is_not_mistaken_for_its_table():
    """`CREATE INDEX ix ON t` names two objects; only the one after ON is
    the table, and getting that backwards would invert the dependency."""
    result = parse("CREATE INDEX ix ON t (a);\n")
    assert node(result, "index:ix").name == "ix"
    assert [r.raw_name for r in pending(result, EdgeType.REFERENCES)] == ["t"]


# -- ALTER / DROP -----------------------------------------------------------


def test_alter_table_add_column_attaches_to_a_table_in_the_same_file():
    result = parse("CREATE TABLE t (a INT);\nALTER TABLE t ADD COLUMN b TEXT;\n")
    column = node(result, "column:t#b")
    assert column.parent_id == "table:t"
    assert column.value_type == "TEXT"


def test_alter_table_for_a_table_created_elsewhere_still_attaches_by_name():
    """The migration case: 001 creates the table, 007 alters it. The column
    must still land on table:t, which only the ID (not this file) knows."""
    result = parse("ALTER TABLE t ADD COLUMN b TEXT;\n")
    column = node(result, "column:t#b")
    assert column.parent_id == "table:t"
    # ...but this file must not claim to declare the table itself.
    assert find_nodes(result, NodeType.TABLE) == []


def test_alter_table_records_a_reference_from_the_file():
    result = parse("ALTER TABLE t ADD COLUMN b TEXT;\n")
    refs = pending(result, EdgeType.REFERENCES)
    assert [(r.source_id, r.raw_name) for r in refs] == [("file:schema.sql", "t")]


def test_drop_table_references_but_does_not_remove():
    result = parse("CREATE TABLE t (a INT);\nDROP TABLE IF EXISTS other;\n")
    assert node(result, "table:t").type == NodeType.TABLE
    refs = pending(result, EdgeType.REFERENCES)
    assert [(r.source_id, r.raw_name) for r in refs] == [("file:schema.sql", "other")]


# -- identifier canonicalization -------------------------------------------


def test_unquoted_identifiers_are_case_insensitive():
    result = parse("CREATE TABLE Users (Id INT);\n")
    table = node(result, "table:users")
    assert table.name == "Users"  # display keeps the original spelling
    assert table.qualified_name == "users"
    assert node(result, "column:users#id").name == "Id"


def test_reference_to_a_differently_cased_table_uses_the_same_canonical_name():
    result = parse("CREATE TABLE posts (aid INT REFERENCES USERS(id));\n")
    assert [r.raw_name for r in pending(result, EdgeType.REFERENCES)] == ["users"]


def test_quoted_identifiers_keep_their_case():
    result = parse('CREATE TABLE "MyTable" (a INT);\n')
    table = node(result, "table:MyTable")
    assert table.name == "MyTable"
    assert table.qualified_name == "MyTable"


def test_schema_qualified_name_keeps_every_part():
    result = parse('CREATE TABLE "Sch".tbl (a INT);\n')
    table = node(result, "table:Sch.tbl")
    assert table.qualified_name == "Sch.tbl"


def test_canonical_identifier_handles_each_quoting_style():
    assert canonical_identifier("Users") == "users"
    assert canonical_identifier('"Users"') == "Users"
    assert canonical_identifier("`Users`") == "Users"
    assert canonical_identifier("[Users]") == "Users"


def test_display_identifier_strips_quotes_without_folding_case():
    assert display_identifier("Users") == "Users"
    assert display_identifier('"Users"') == "Users"
    assert display_identifier("[Users]") == "Users"


# -- error handling ---------------------------------------------------------


def test_duplicate_table_within_one_file_is_reported_not_raised():
    result = parse("CREATE TABLE t (a INT);\nCREATE TABLE t (b INT);\n")
    assert len(find_nodes(result, NodeType.TABLE)) == 1
    assert any("duplicate declaration id" in e.message for e in result.errors)


def test_syntax_error_is_reported_and_other_statements_still_parse():
    result = parse("CREATE TABLE good (a INT);\nTHIS IS NOT SQL @@@;\n")
    assert node(result, "table:good").type == NodeType.TABLE
    assert any(e.message == "syntax error" for e in result.errors)


def test_unsupported_dialect_keyword_does_not_lose_the_table():
    """SQLite's AUTOINCREMENT is not in this grammar; the surrounding
    CREATE TABLE must still be extracted rather than discarded."""
    result = parse("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, b TEXT);\n")
    assert node(result, "table:t").type == NodeType.TABLE
    assert node(result, "column:t#b").value_type == "TEXT"


def test_parser_never_raises_on_arbitrary_bytes():
    for source in (b"", b"\x00\x01\x02", b"-- just a comment\n", b"SELECT 1;", b"((("):
        result = SqlParser().parse("x.sql", source)
        assert result.nodes  # at minimum the FILE node


def test_line_numbers_are_one_based():
    result = parse("\n\nCREATE TABLE t (a INT);\n")
    assert node(result, "table:t").start_line == 3


def test_bare_select_produces_no_declarations():
    result = parse("SELECT id FROM users;\n")
    assert find_nodes(result, NodeType.TABLE) == []
    assert pending(result, EdgeType.REFERENCES) == []
