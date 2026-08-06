"""The graph model: node/edge types and the value objects that make up a
LocalCodeGraph.

ID strategy
-----------
Node IDs are deterministic strings derived purely from an entity's location
in the source structure (package/type/member names, signatures), never from
random UUIDs or from insertion order. Parsing the same unchanged source tree
twice always produces the same IDs. This matters because Phase 8
(incremental updates) needs IDs that survive a rebuild, and because a stable
ID lets Claude Code refer to a symbol across separate `local-code-graph`
invocations.

    FILE          "file:" + <repo-relative posix path>
    PACKAGE       "package:" + <fully qualified package name>
    CLASS/
    INTERFACE/
    ENUM/
    ANNOTATION/
    OBJECT        "type:" + <fully qualified type name>
    METHOD        "method:" + <enclosing scope fqn> + "#" + <name> + "(" + <comma-joined raw parameter type text> + ")"
    CONSTRUCTOR   "method:" + <enclosing type fqn> + "#<init>(" + <params> + ")"
    FIELD         "field:" + <enclosing type fqn> + "#" + <name>
    PROPERTY      "property:" + <enclosing scope fqn> + "#" + <name>
    PARAMETER     <owning method/constructor id> + "/param/" + <index> + ":" + <name>
    TABLE         "table:" + <canonical table name>
    VIEW          "view:" + <canonical view name>
    INDEX         "index:" + <canonical index name>
    TRIGGER       "trigger:" + <canonical trigger name>
    COLUMN        "column:" + <canonical owning table name> + "#" + <canonical column name>

"Enclosing scope fqn" is normally an enclosing type's fully qualified name,
but METHOD and PROPERTY also support Kotlin top-level functions/properties,
whose enclosing scope is the file's package (or "" for the default package)
rather than a type — see parser/kotlin.py.

Nested types use a dot-joined qualified name based purely on *enclosing type*
names (package.Outer.Inner), ignoring any enclosing method — so a class
declared inside a method body (a "local class") gets the same qualified name
it would have if it were a direct member of the innermost enclosing type.
This is a known, documented simplification: local and anonymous classes are
out of scope for Phase 2/3 (see parser/java.py and parser/kotlin.py module
docstrings).

A PACKAGE node's ID depends only on the package name, so every file that
declares `package com.example;` contributes to the *same* PACKAGE node. The
graph builder is responsible for de-duplicating repeated PACKAGE nodes by ID
(see graph/builder.py); this module does not perform that merge itself.

Method/constructor signatures are built from the *raw declared parameter
type text* (e.g. "List<String>", "int", "String..."), not a resolved/erased
JVM signature — this is enough to disambiguate overloads without requiring a
type checker. Kotlin extension functions fold their receiver type into the
signature too (see parser/kotlin.py) since two extension functions with the
same name/params but different receivers are different declarations.

Line numbers
------------
All `start_line`/`end_line` values on Node are **1-based and inclusive**,
matching what a human reads in an editor or in `local-code-graph show`
output — even though Tree-sitter itself reports 0-based rows internally.
Parsers are responsible for the +1 conversion before constructing a Node.

PACKAGE nodes have no single physical location (they may be declared by many
files), so `file`, `start_line`, and `end_line` are always None for them.

CONTAINS vs. DECLARES
----------------------
Both are structural, but they answer different questions:

  * CONTAINS is physical/namespace nesting: PACKAGE -> FILE, FILE -> a
    top-level type, and TYPE -> a nested type declared inside it. A Kotlin
    FILE also CONTAINS any top-level METHOD/PROPERTY declared directly in
    it (Kotlin, unlike Java, allows functions and properties outside any
    type).
  * DECLARES is "this type/method declares this member": TYPE -> METHOD,
    TYPE -> CONSTRUCTOR, TYPE -> FIELD, TYPE -> PROPERTY, and
    METHOD/CONSTRUCTOR -> PARAMETER.

Kotlin-specific representation notes
-------------------------------------
  * Kotlin has no separate interface AST node — `class` and `interface` are
    the same grammar production distinguished only by a keyword. The parser
    still emits NodeType.INTERFACE for the latter, so the graph stays
    language-neutral: a query for "all interfaces" doesn't need to know
    which language produced them.
  * `object` declarations (including companion objects) become
    NodeType.OBJECT. An unnamed companion object is recorded with the name
    Kotlin itself gives it implicitly: "Companion".
  * Kotlin properties (`val`/`var`) become NodeType.PROPERTY, not FIELD —
    they are a distinct language concept from a Java field (getter/setter
    semantics, can exist at top level outside any type). FIELD is still
    used for Java fields and for enum constants in both languages (a
    constant is a fixed instance, closer to what FIELD already means than
    to a Kotlin property).
  * A Kotlin extension function (`fun String.toFoo(): Foo`) is declared
    inside whatever type/file its `fun` keyword textually appears in — that
    is what its `parent_id`/CONTAINS-or-DECLARES edge reflects. It is
    *never* linked as a member of its receiver type (`String` here), which
    it structurally is not. The receiver type text is preserved separately
    in `Node.receiver_type` instead, and folded into the node's ID so two
    extension functions with the same name/parameters but different
    receivers don't collide.

SQL-specific representation notes
----------------------------------
  * SQL has no package/namespace hierarchy to hang declarations off, so a
    FILE directly CONTAINS the TABLE/VIEW/INDEX/TRIGGER nodes declared in
    it, and a TABLE DECLARES its COLUMNs.
  * REFERENCES is the SQL structural dependency edge, and it always points
    *table-ward*: TABLE -> TABLE for a foreign key, and VIEW/INDEX/TRIGGER
    -> TABLE for the table each is built on or reads from. It is the SQL
    analogue of EXTENDS/IMPLEMENTS between types, and like them it records
    `target_name` even when the target table isn't declared anywhere in the
    repository (a table created outside the scanned files stays unresolved
    rather than invented).
  * Identifier case: SQL identifiers are case-insensitive unless quoted, so
    `USERS`, `Users`, and `users` are one table and must land on one node.
    Node IDs and `qualified_name` therefore use a *canonical* form —
    unquoted identifiers folded to lowercase, quoted identifiers left
    exactly as written (minus the quotes), matching the case-sensitivity
    rule SQL itself applies. `Node.name` keeps the original spelling for
    display. This is why a SQL node's `name` and `qualified_name` can
    differ in case where a Java node's never would.
  * The same table is routinely re-declared across versioned migration
    files, and a migration that adds a column is describing the same
    logical table as the original CREATE. Repeated TABLE/VIEW/INDEX/TRIGGER
    declarations across *different* files are therefore merged onto one
    node (first declaration wins for location) exactly as PACKAGE nodes
    are, rather than reported as duplicate-ID collisions. A duplicate
    within a *single* file is still a genuine mistake and is still
    reported.

Accuracy over completeness
---------------------------
EXTENDS, IMPLEMENTS, and IMPORTS reference another type by name as written
in the source. That name is not always resolvable to a node in this graph
(external libraries, JDK types, ambiguous/unresolvable symbols). In that
case the Edge is still recorded — with `target_name` holding the raw
reference text — but `target_id` is left as None rather than guessing. A
missing relationship is preferred over a false one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class NodeType(str, Enum):
    FILE = "FILE"
    PACKAGE = "PACKAGE"
    CLASS = "CLASS"
    INTERFACE = "INTERFACE"
    ENUM = "ENUM"
    ANNOTATION = "ANNOTATION"
    OBJECT = "OBJECT"
    METHOD = "METHOD"
    CONSTRUCTOR = "CONSTRUCTOR"
    FIELD = "FIELD"
    PROPERTY = "PROPERTY"
    PARAMETER = "PARAMETER"
    TABLE = "TABLE"
    COLUMN = "COLUMN"
    VIEW = "VIEW"
    INDEX = "INDEX"
    TRIGGER = "TRIGGER"


class EdgeType(str, Enum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    DECLARES = "DECLARES"
    REFERENCES = "REFERENCES"


@dataclass(frozen=True)
class Node:
    """A single graph entity (a file, type, member, ...).

    Only a subset of the optional fields is populated for any given
    ``type`` — see the module docstring and parser/java.py / parser/kotlin.py
    for which fields are meaningful for which NodeType.
    """

    id: str
    type: NodeType
    name: str
    file: str | None
    start_line: int | None
    end_line: int | None
    qualified_name: str | None = None
    parent_id: str | None = None
    modifiers: tuple[str, ...] = ()
    annotations: tuple[str, ...] = ()
    type_parameters: tuple[str, ...] = ()
    param_types: tuple[str, ...] | None = None
    """Raw declared parameter type text, in order. Populated for METHOD and
    CONSTRUCTOR nodes only."""
    return_type: str | None = None
    """Raw declared return type text (e.g. "void", "List<String>").
    Populated for METHOD nodes only."""
    value_type: str | None = None
    """Raw declared type text. Populated for FIELD, PROPERTY, and PARAMETER
    nodes only."""
    receiver_type: str | None = None
    """Raw receiver type text (e.g. "String"). Populated for Kotlin
    extension functions/properties only — see the module docstring's
    "Kotlin-specific representation notes"."""
    mutable: bool | None = None
    """True for `var`, False for `val`. Populated for PROPERTY nodes only."""


@dataclass(frozen=True)
class Location:
    """A source span, e.g. where a reference (not a declaration) occurs."""

    file: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class Edge:
    """A relationship between two nodes.

    ``target_id`` is None when the target could not be resolved to a known
    node (see the module docstring's "Accuracy over completeness" section);
    ``target_name`` always holds the raw name/reference text so no
    information is lost even when resolution fails.
    """

    type: EdgeType
    source_id: str
    target_id: str | None
    target_name: str
    location: Location | None = None


def edge_sort_key(edge: Edge) -> tuple[str, str, str, str, str, int, int]:
    """Canonical ordering for edges, independent of insertion/build order.

    Used anywhere edges need a deterministic order — graph.json
    serialization (graph/storage.py) and query-engine adjacency indexes
    (query/engine.py) both sort with this key so that two builds of the
    same unchanged source tree, or two differently-ordered scans, produce
    identical output.
    """
    loc = edge.location
    return (
        edge.type.value,
        edge.source_id,
        edge.target_id or "",
        edge.target_name,
        loc.file if loc is not None else "",
        loc.start_line if loc is not None else -1,
        loc.end_line if loc is not None else -1,
    )


@dataclass(frozen=True)
class ParseError:
    """A diagnostic recorded while parsing or building the graph.

    Used both for genuine Tree-sitter syntax errors inside a single file and
    for graph-builder-level issues (e.g. two declarations that produced the
    same node ID). Never raised as an exception — always collected so one
    bad file cannot abort the rest of the repository parse.
    """

    file: str
    message: str
    start_line: int | None = None
    end_line: int | None = None


@dataclass
class Graph:
    """The complete graph for one repository build.

    ``nodes`` is keyed by Node.id. Mutation happens through ``add_node`` /
    ``add_edge`` rather than touching the containers directly, so future
    callers get consistent collision handling.
    """

    root: str
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    def add_node(self, node: Node) -> bool:
        """Insert ``node`` if its ID is not already present.

        Returns True if inserted, False if an entry with that ID already
        existed (left untouched) — callers decide whether a collision is
        expected (e.g. a PACKAGE node repeated across files) or should be
        reported as a diagnostic.
        """
        if node.id in self.nodes:
            return False
        self.nodes[node.id] = node
        return True

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)

    def nodes_of_type(self, node_type: NodeType) -> list[Node]:
        return [n for n in self.nodes.values() if n.type == node_type]

    def edges_from(self, source_id: str) -> list[Edge]:
        return [e for e in self.edges if e.source_id == source_id]

    def edges_to(self, target_id: str) -> list[Edge]:
        return [e for e in self.edges if e.target_id == target_id]
