"""SQL parser built on Tree-sitter.

Extracts the *schema* declared by a `.sql` file — tables and their columns,
views, indexes, and triggers — plus the references between them (foreign
keys, the table a view selects from, the table an index or trigger is
attached to), mapping them onto the same language-neutral graph model the
Java and Kotlin parsers produce. See graph/model.py's "SQL-specific
representation notes" for the ID/case/merge rules this module implements.

Scope: schema, not queries
---------------------------
Only statements that *declare* schema produce nodes. `SELECT`/`INSERT`/
`UPDATE`/`DELETE` outside a view or trigger definition are not represented:
they describe one moment's data access, not repository structure, and the
graph is a structural index. `ALTER TABLE ... ADD COLUMN` is the one
mutation that is followed, because in a versioned-migration layout it is
how a table's shape is actually defined, and ignoring it would mean the
graph describes v1 of a schema rather than the current one.

Grammar shape, worth knowing before touching this file
-------------------------------------------------------
* Statements are wrapped: `program` -> `statement` -> `create_table` (or
  `create_view` / `create_index` / `create_trigger` / `alter_table` /
  `drop_table`).
* Almost nothing is a named field, so children are matched by type and by
  position relative to keyword tokens (`keyword_on`, `keyword_references`)
  rather than via `child_by_field_name`.
* A table/view/index/trigger name is an `object_reference` holding one
  `identifier` per dotted part (`"schema"."table"` -> two identifiers).
* Foreign keys appear two ways: inline in a `column_definition`
  (`aid INT REFERENCES users(id)`) and as a table-level `constraint` under
  `constraints` (`FOREIGN KEY (aid) REFERENCES users(id)`). Both are
  handled; both produce a TABLE -> TABLE REFERENCES edge.

Known limitations (deliberate, to avoid inventing unreliable relationships)
----------------------------------------------------------------------------
* This grammar targets mainstream SQL and does not accept every dialect
  extension. SQLite's `AUTOINCREMENT` keyword and a trigger body written as
  `BEGIN ... END` both produce ERROR nodes. Those are reported as ordinary
  syntax errors and parsing continues, so surrounding declarations are
  still extracted — but a `BEGIN ... END` trigger body in particular can
  swallow following statements into its ERROR node, in which case those
  statements are simply absent rather than guessed at.
* A column's declared type is recorded as raw text (`value_type`); it is
  not normalized across dialects (`INT` and `INTEGER` stay distinct).
* Only tables named directly in a view's `FROM`/`JOIN` relations become
  REFERENCES edges. Table names appearing only inside subqueries or
  expressions are not chased, and range-variable aliases (`FROM users u`)
  are resolved to the real table name rather than recorded as their alias.
* `DROP TABLE` records a REFERENCES edge from the file's own node (the file
  touches that table) but never removes nodes: the graph describes what the
  files declare, and replaying migrations in order to compute a final
  schema would be a different tool.
"""

from __future__ import annotations

import tree_sitter
import tree_sitter_sql

from local_code_graph.graph.model import (
    Edge,
    EdgeType,
    Location,
    Node,
    NodeType,
    ParseError,
)
from local_code_graph.parser._ts_utils import (
    find_error_nodes,
    first_named_child_of_type,
    iter_tree,
    line_count,
    node_line_span,
)
from local_code_graph.parser.base import FileContext, LanguageParser, ParseResult, PendingRef

_LANGUAGE = tree_sitter.Language(tree_sitter_sql.language())

# Child types of `statement` this parser knows how to turn into graph output.
_CREATE_TABLE = "create_table"
_CREATE_VIEW = "create_view"
_CREATE_INDEX = "create_index"
_CREATE_TRIGGER = "create_trigger"
_ALTER_TABLE = "alter_table"
_DROP_TABLE = "drop_table"


def canonical_identifier(raw: str) -> str:
    """Fold one SQL identifier to the form used in node IDs.

    Unquoted identifiers are case-insensitive in SQL, so they fold to
    lowercase; a quoted identifier is case-*sensitive* and keeps its
    spelling exactly, minus the quotes. That distinction is the whole
    reason this function exists: without it `REFERENCES Users` would not
    find the node created by `CREATE TABLE users`.

    Handles the three quoting styles the common dialects use: double
    quotes (standard/Postgres/SQLite), backticks (MySQL), and square
    brackets (T-SQL).
    """
    text = raw.strip()
    if len(text) >= 2:
        first, last = text[0], text[-1]
        if (first == '"' and last == '"') or (first == "`" and last == "`"):
            return text[1:-1]
        if first == "[" and last == "]":
            return text[1:-1]
    return text.lower()


def display_identifier(raw: str) -> str:
    """The identifier as a human wrote it, with any quoting removed."""
    text = raw.strip()
    if len(text) >= 2:
        first, last = text[0], text[-1]
        if (
            (first == '"' and last == '"')
            or (first == "`" and last == "`")
            or (first == "[" and last == "]")
        ):
            return text[1:-1]
    return text


class SqlParser(LanguageParser):
    def __init__(self) -> None:
        self._parser = tree_sitter.Parser(_LANGUAGE)

    def parse(self, relative_path: str, content: bytes) -> ParseResult:
        tree = self._parser.parse(content)
        builder = _SqlFileBuilder(relative_path, content)
        builder.walk_program(tree.root_node)

        errors = list(builder.errors)
        for error_node in find_error_nodes(tree.root_node):
            errors.append(
                ParseError(
                    file=relative_path,
                    message="syntax error",
                    start_line=error_node.start_point.row + 1,
                    end_line=error_node.end_point.row + 1,
                )
            )

        return ParseResult(
            nodes=tuple(builder.nodes),
            edges=tuple(builder.edges),
            pending_refs=tuple(builder.pending_refs),
            errors=tuple(errors),
            # SQL has no package or import concept; an empty context keeps
            # the builder's cross-file resolution rules from trying to
            # apply Java/Kotlin scoping to SQL references.
            context=FileContext(package=None),
        )


class _SqlFileBuilder:
    """Walks one file's Tree-sitter AST, accumulating graph-model output."""

    def __init__(self, relative_path: str, content: bytes) -> None:
        self.relative_path = relative_path
        self.content = content
        self.file_line_count = line_count(content)
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.pending_refs: list[PendingRef] = []
        self.errors: list[ParseError] = []
        self.file_id = f"file:{relative_path}"
        self._node_ids: set[str] = set()
        self._table_ids: dict[str, str] = {}
        """canonical table name -> node id, for tables declared in *this*
        file — lets `ALTER TABLE x ADD COLUMN` attach the column to x when
        both statements live in the same file."""

    # -- small helpers ---------------------------------------------------

    def text(self, node: tree_sitter.Node) -> str:
        return self.content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def loc(self, node: tree_sitter.Node) -> tuple[int, int]:
        return node_line_span(node, max_line=self.file_line_count)

    def location(self, node: tree_sitter.Node) -> Location:
        start, end = self.loc(node)
        return Location(self.relative_path, start, end)

    def add_node(self, node: Node) -> bool:
        if node.id in self._node_ids:
            self.errors.append(
                ParseError(
                    file=self.relative_path,
                    message=f"duplicate declaration id within file (skipped): {node.id}",
                    start_line=node.start_line,
                    end_line=node.end_line,
                )
            )
            return False
        self._node_ids.add(node.id)
        self.nodes.append(node)
        return True

    def _object_name(self, node: tree_sitter.Node) -> tuple[str, str] | None:
        """Turn an `object_reference` into (canonical_name, display_name).

        Dotted names keep every part, each canonicalized independently, so
        `"Sch".TBL` becomes `Sch.tbl` — quoted part case-preserved, unquoted
        part folded, exactly as SQL resolves it.
        """
        parts = [c for c in node.named_children if c.type == "identifier"]
        if not parts:
            return None
        raw_parts = [self.text(p) for p in parts]
        canonical = ".".join(canonical_identifier(p) for p in raw_parts)
        display = ".".join(display_identifier(p) for p in raw_parts)
        return canonical, display

    def _first_object_reference(self, node: tree_sitter.Node) -> tree_sitter.Node | None:
        return first_named_child_of_type(node, ("object_reference",))

    def _object_reference_after_on(self, node: tree_sitter.Node) -> tree_sitter.Node | None:
        """The `object_reference` that follows the `ON` keyword.

        Both `CREATE INDEX ix ON tbl (...)` and `CREATE TRIGGER t ... ON
        tbl ...` name their own object first and the table they attach to
        after `ON`, so position relative to that keyword — not child order
        alone — is what distinguishes the two.
        """
        seen_on = False
        for child in node.children:
            if child.type == "keyword_on":
                seen_on = True
            elif seen_on and child.type == "object_reference":
                return child
        return None

    def _add_reference(
        self, source_id: str, target_node: tree_sitter.Node, *, anchor: tree_sitter.Node | None = None
    ) -> None:
        """Record a REFERENCES pending ref from ``source_id`` to the table
        named by ``target_node``, to be resolved once every file is parsed
        (the referenced table is very often declared in another file)."""
        name = self._object_name(target_node)
        if name is None:
            return
        canonical, _display = name
        self.pending_refs.append(
            PendingRef(
                type=EdgeType.REFERENCES,
                source_id=source_id,
                raw_name=canonical,
                location=self.location(anchor if anchor is not None else target_node),
            )
        )

    # -- top level -------------------------------------------------------

    def walk_program(self, root: tree_sitter.Node) -> None:
        self.nodes.append(
            Node(
                id=self.file_id,
                type=NodeType.FILE,
                name=self.relative_path.rsplit("/", 1)[-1],
                file=self.relative_path,
                start_line=1,
                end_line=self.file_line_count,
                qualified_name=self.relative_path,
                parent_id=None,
            )
        )
        self._node_ids.add(self.file_id)

        for statement in root.named_children:
            if statement.type != "statement":
                continue
            for child in statement.named_children:
                if child.type == _CREATE_TABLE:
                    self._handle_create_table(child)
                elif child.type == _CREATE_VIEW:
                    self._handle_create_view(child)
                elif child.type == _CREATE_INDEX:
                    self._handle_create_index(child)
                elif child.type == _CREATE_TRIGGER:
                    self._handle_create_trigger(child)
                elif child.type == _ALTER_TABLE:
                    self._handle_alter_table(child)
                elif child.type == _DROP_TABLE:
                    self._handle_drop_table(child)

    def _declare(
        self,
        node: tree_sitter.Node,
        name_node: tree_sitter.Node,
        node_type: NodeType,
        id_prefix: str,
    ) -> tuple[str, str] | None:
        """Emit one top-level SQL declaration node plus its FILE CONTAINS
        edge. Returns (node_id, canonical_name), or None if it was a
        within-file duplicate."""
        name = self._object_name(name_node)
        if name is None:
            return None
        canonical, display = name
        node_id = f"{id_prefix}:{canonical}"
        start, end = self.loc(node)

        if not self.add_node(
            Node(
                id=node_id,
                type=node_type,
                name=display,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=canonical,
                parent_id=self.file_id,
            )
        ):
            return None
        self.edges.append(
            Edge(
                type=EdgeType.CONTAINS,
                source_id=self.file_id,
                target_id=node_id,
                target_name=display,
            )
        )
        return node_id, canonical

    # -- CREATE TABLE ----------------------------------------------------

    def _handle_create_table(self, node: tree_sitter.Node) -> None:
        name_node = self._first_object_reference(node)
        if name_node is None:
            return
        declared = self._declare(node, name_node, NodeType.TABLE, "table")
        if declared is None:
            return
        table_id, canonical = declared
        self._table_ids[canonical] = table_id

        definitions = first_named_child_of_type(node, ("column_definitions",))
        if definitions is None:
            return
        for child in definitions.named_children:
            if child.type == "column_definition":
                self._handle_column_definition(child, table_id, canonical)
            elif child.type == "constraints":
                for constraint in child.named_children:
                    if constraint.type == "constraint":
                        self._handle_table_constraint(constraint, table_id)

    def _handle_column_definition(
        self, node: tree_sitter.Node, table_id: str, table_canonical: str
    ) -> None:
        name_node = first_named_child_of_type(node, ("identifier",))
        if name_node is None:
            return
        raw_name = self.text(name_node)
        canonical = canonical_identifier(raw_name)
        column_id = f"column:{table_canonical}#{canonical}"
        start, end = self.loc(node)

        # The declared type is whatever named child follows the column name
        # and isn't itself part of a constraint clause; taking the first
        # such child keeps `a INT`, `b VARCHAR(20)`, and `c TEXT` all
        # working without enumerating every type node the grammar defines.
        type_text: str | None = None
        for child in node.named_children:
            if child is name_node or child.type == "identifier":
                continue
            if child.type.startswith("keyword_") and child.type not in _TYPE_KEYWORDS:
                continue
            # A column declared with no type at all (`CREATE TABLE t (a)`,
            # legal in SQLite) still gets a zero-width placeholder node
            # from the grammar. Recording that as the type would put an
            # empty string where "no declared type" is meant.
            candidate = self.text(child).strip()
            if not candidate:
                continue
            type_text = candidate
            break

        if self.add_node(
            Node(
                id=column_id,
                type=NodeType.COLUMN,
                name=display_identifier(raw_name),
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=f"{table_canonical}.{canonical}",
                parent_id=table_id,
                value_type=type_text,
            )
        ):
            self.edges.append(
                Edge(
                    type=EdgeType.DECLARES,
                    source_id=table_id,
                    target_id=column_id,
                    target_name=display_identifier(raw_name),
                )
            )

        # Inline foreign key: `aid INT REFERENCES users(id)`.
        self._handle_inline_reference(node, table_id)

    def _handle_inline_reference(self, node: tree_sitter.Node, table_id: str) -> None:
        seen_references = False
        for child in node.children:
            if child.type == "keyword_references":
                seen_references = True
            elif seen_references and child.type == "object_reference":
                self._add_reference(table_id, child)
                return

    def _handle_table_constraint(self, node: tree_sitter.Node, table_id: str) -> None:
        """A table-level constraint; only FOREIGN KEY ones create an edge.

        PRIMARY KEY / UNIQUE / CHECK constraints describe a single table's
        own shape and reference nothing outside it, so they contribute no
        relationship — recording them as self-edges would add noise without
        adding structure.
        """
        seen_references = False
        for child in node.children:
            if child.type == "keyword_references":
                seen_references = True
            elif seen_references and child.type == "object_reference":
                self._add_reference(table_id, child)
                return

    # -- CREATE VIEW / INDEX / TRIGGER ------------------------------------

    def _handle_create_view(self, node: tree_sitter.Node) -> None:
        name_node = self._first_object_reference(node)
        if name_node is None:
            return
        declared = self._declare(node, name_node, NodeType.VIEW, "view")
        if declared is None:
            return
        view_id, _canonical = declared

        query = first_named_child_of_type(node, ("create_query",))
        if query is None:
            return
        for relation in _find_relations(query):
            target = first_named_child_of_type(relation, ("object_reference",))
            if target is not None:
                self._add_reference(view_id, target)

    def _handle_create_index(self, node: tree_sitter.Node) -> None:
        # An index's own name is a bare `identifier` here, not an
        # `object_reference` like every other declaration — so it needs its
        # own name handling rather than `_declare`'s.
        name_node = first_named_child_of_type(node, ("identifier",))
        table_node = self._object_reference_after_on(node)
        if name_node is None or table_node is None:
            return
        raw_name = self.text(name_node)
        canonical = canonical_identifier(raw_name)
        index_id = f"index:{canonical}"
        start, end = self.loc(node)

        if not self.add_node(
            Node(
                id=index_id,
                type=NodeType.INDEX,
                name=display_identifier(raw_name),
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=canonical,
                parent_id=self.file_id,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.CONTAINS,
                source_id=self.file_id,
                target_id=index_id,
                target_name=display_identifier(raw_name),
            )
        )
        self._add_reference(index_id, table_node)

    def _handle_create_trigger(self, node: tree_sitter.Node) -> None:
        name_node = self._first_object_reference(node)
        table_node = self._object_reference_after_on(node)
        if name_node is None:
            return
        declared = self._declare(node, name_node, NodeType.TRIGGER, "trigger")
        if declared is None:
            return
        trigger_id, _canonical = declared
        if table_node is not None:
            self._add_reference(trigger_id, table_node)

    # -- ALTER / DROP ------------------------------------------------------

    def _handle_alter_table(self, node: tree_sitter.Node) -> None:
        table_node = self._first_object_reference(node)
        if table_node is None:
            return
        name = self._object_name(table_node)
        if name is None:
            return
        canonical, _display = name

        added = [c for c in node.named_children if c.type == "add_column"]
        table_id = self._table_ids.get(canonical)
        if table_id is None:
            # The table was created in some *other* file — the normal case
            # for versioned migrations, where 001 creates and 007 alters.
            # A table ID is derived purely from its canonical name, so the
            # column can still be attached to the right table without this
            # file having seen the CREATE. No TABLE node is emitted here:
            # this file modifies that table, it doesn't declare it, and the
            # difference matters for `file` and line attribution.
            #
            # If no file in the repository declares the table, the column
            # would be left parented to a node that doesn't exist, so
            # graph/builder.py prunes it (see prune_orphaned_sql_columns).
            table_id = f"table:{canonical}"
            self._add_reference(self.file_id, table_node)

        for add_column in added:
            definition = first_named_child_of_type(add_column, ("column_definition",))
            if definition is not None:
                self._handle_column_definition(definition, table_id, canonical)

    def _handle_drop_table(self, node: tree_sitter.Node) -> None:
        table_node = self._first_object_reference(node)
        if table_node is not None:
            self._add_reference(self.file_id, table_node)


# Type keywords the grammar emits directly as a column's declared type
# (rather than wrapping in a dedicated type node), so they must not be
# filtered out with the constraint keywords in `_handle_column_definition`.
_TYPE_KEYWORDS = frozenset(
    {
        "keyword_text",
        "keyword_blob",
        "keyword_boolean",
        "keyword_date",
        "keyword_datetime",
        "keyword_time",
        "keyword_timestamp",
        "keyword_json",
        "keyword_jsonb",
        "keyword_uuid",
        "keyword_money",
        "keyword_serial",
        "keyword_bigserial",
    }
)


def _find_relations(node: tree_sitter.Node) -> list[tree_sitter.Node]:
    """Every `relation` node under ``node`` (a view's query), in order.

    Cursor-based via ``iter_tree``, and retaining only the relations, for
    the reasons documented there: neither recursion depth nor the number of
    simultaneously live Node objects is something a parser should be
    betting on.
    """
    return [current for current in iter_tree(node) if current.type == "relation"]
