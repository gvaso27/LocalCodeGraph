"""Kotlin parser built on Tree-sitter.

Extracts packages, imports (regular/aliased/wildcard), classes, interfaces,
objects (including companion objects), enum classes, annotation classes,
properties, functions (including extension functions), constructors
(primary and secondary), parameters, and their explicit `extends`/
`implements`/`import` references, mapping them onto the *same*
language-neutral graph model the Java parser produces (see
graph/model.py's "Kotlin-specific representation notes" for how Kotlin
concepts without a direct Java equivalent — objects, properties, extension
receivers — are represented). Tree-sitter-specific logic is confined to
this module (plus the shared helpers in parser/_ts_utils.py).

Grammar shape vs. Java, worth knowing before touching this file:

* Kotlin's grammar gives classes and interfaces the *same* node type
  (`class_declaration`); they're told apart by an anonymous `class` vs.
  `interface` keyword token. `object`/`companion object` are their own node
  types (`object_declaration`/`companion_object`).
* Almost nothing is a named field here (unlike Java) — `type_parameters`,
  `primary_constructor`, `class_body`, `delegation_specifiers`, `modifiers`
  are all plain positional named children, matched by type rather than
  `child_by_field_name`. Only `name` (on declarations) is a real field.

Known limitations (deliberate, to avoid inventing unreliable relationships):

* Local classes/objects/functions declared inside a function body, and
  anonymous objects (`object : Foo { ... }`), are not extracted.
* `init { ... }` blocks are not extracted as nodes.
* A class's `extends`/`implements` split is inferred syntactically: a
  delegation specifier written as a call (`: Base()`) is EXTENDS, a plain
  type reference (`: SomeInterface`) is IMPLEMENTS on a class/object/enum
  or EXTENDS on an interface. A class hierarchy where the superclass is
  only referenced without parentheses (legal when every constructor is
  secondary and delegates via `super(...)`) is misclassified as
  IMPLEMENTS — a narrow, documented gap rather than guessed at.
* `extends`/`implements`/`import` targets are matched only via exact
  fully-qualified-name lookup, enclosing-type scope, the file's own
  explicit (non-wildcard) imports, or same-package lookup. External/stdlib
  types, wildcard imports, and aliased-import targets that don't already
  match a known qualified name are always left unresolved rather than
  guessed at.
* Constructor-parameter properties (`class Foo(val x: Int)`) are recorded
  as PROPERTY nodes declared by the class, not as PARAMETER nodes of the
  constructor — that's what they actually are from outside the class. They
  still count toward the primary constructor's signature for ID purposes.
* KNOWN GRAMMAR QUIRK: a class-level annotation immediately preceding an
  `annotation class` declaration (e.g. `@Target(...)\\nannotation class
  Foo`) is misparsed by this version of tree-sitter-kotlin as an
  expression rather than a declaration — Tree-sitter reports no error
  (`has_error` stays False), so the class is just silently absent from the
  graph. Reproduced and regression-tested in
  tests/test_kotlin_parser.py; not a bug in this module, and not worked
  around here per the project's rule against grammar-quirk hacks. Writing
  `annotation class Foo` without a preceding annotation (or an annotation
  targeting anything else) is unaffected.
"""

from __future__ import annotations

import tree_sitter
import tree_sitter_kotlin

from local_code_graph.graph.model import (
    Edge,
    EdgeType,
    Location,
    Node,
    NodeType,
    ParseError,
)
from local_code_graph.parser._ts_utils import (
    child_index_by_field,
    child_index_by_type,
    find_error_nodes,
    first_named_child_of_type,
    line_count,
    node_line_span,
)
from local_code_graph.parser.base import FileContext, LanguageParser, ParseResult, PendingRef

_LANGUAGE = tree_sitter.Language(tree_sitter_kotlin.language())

_TYPE_DECL_TYPES = frozenset({"class_declaration", "object_declaration"})


class KotlinParser(LanguageParser):
    def __init__(self) -> None:
        self._parser = tree_sitter.Parser(_LANGUAGE)

    def parse(self, relative_path: str, content: bytes) -> ParseResult:
        tree = self._parser.parse(content)
        builder = _KotlinFileBuilder(relative_path, content)
        builder.walk_source_file(tree.root_node)

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
            context=FileContext(
                package=builder.package,
                imports=tuple(sorted(builder.import_map.items())),
            ),
        )


class _ParamInfo:
    __slots__ = ("name", "type_text", "modifiers", "annotations")

    def __init__(
        self,
        name: str,
        type_text: str,
        modifiers: tuple[str, ...],
        annotations: tuple[str, ...],
    ) -> None:
        self.name = name
        self.type_text = type_text
        self.modifiers = modifiers
        self.annotations = annotations


def _receiver_type_node(node: tree_sitter.Node, anchor_index: int | None) -> tree_sitter.Node | None:
    """If the child at ``anchor_index`` is immediately preceded by a `.` and
    a named type node, return that type node (the extension receiver type).

    Positional, not identity-based, so it's safe to call with an index
    derived from ``child_index_by_field``/``child_index_by_type`` on the
    same node.
    """
    if anchor_index is None or anchor_index < 2:
        return None
    dot = node.child(anchor_index - 1)
    if dot is None or dot.type != ".":
        return None
    candidate = node.child(anchor_index - 2)
    if candidate is not None and candidate.is_named:
        return candidate
    return None


def _delegation_type_and_kind(
    spec_node: tree_sitter.Node,
) -> tuple[tree_sitter.Node | None, bool]:
    """For one `delegation_specifier` child, return (type_node, is_constructor_call)."""
    if not spec_node.named_children:
        return None, False
    inner = spec_node.named_children[0]
    if inner.type == "constructor_invocation":
        type_node = inner.named_children[0] if inner.named_children else None
        return type_node, True
    if inner.type == "explicit_delegation":
        type_node = inner.named_children[0] if inner.named_children else None
        return type_node, False
    return inner, False


class _KotlinFileBuilder:
    """Walks one file's Tree-sitter AST, accumulating graph-model output."""

    def __init__(self, relative_path: str, content: bytes) -> None:
        self.relative_path = relative_path
        self.content = content
        self.file_line_count = line_count(content)
        self.nodes: list[Node] = []
        self.edges: list[Edge] = []
        self.pending_refs: list[PendingRef] = []
        self.errors: list[ParseError] = []
        self.package: str | None = None
        self.import_map: dict[str, str] = {}
        self._node_ids: set[str] = set()

    def text(self, node: tree_sitter.Node) -> str:
        return self.content[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def loc(self, node: tree_sitter.Node) -> tuple[int, int]:
        return node_line_span(node, max_line=self.file_line_count)

    @property
    def owner_scope(self) -> str:
        """Package-or-empty scope used for top-level function/property IDs."""
        return self.package or ""

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

    # -- top level ---------------------------------------------------

    def walk_source_file(self, root: tree_sitter.Node) -> None:
        file_id = f"file:{self.relative_path}"
        file_name = self.relative_path.rsplit("/", 1)[-1]

        pkg_node = first_named_child_of_type(root, ("package_header",))
        parent_for_file: str | None = None
        if pkg_node is not None:
            name_node = first_named_child_of_type(
                pkg_node, ("qualified_identifier", "identifier")
            )
            if name_node is not None:
                self.package = self.text(name_node)
                package_id = f"package:{self.package}"
                parent_for_file = package_id
                self.nodes.append(
                    Node(
                        id=package_id,
                        type=NodeType.PACKAGE,
                        name=self.package,
                        file=None,
                        start_line=None,
                        end_line=None,
                        qualified_name=self.package,
                        parent_id=None,
                    )
                )
                self._node_ids.add(package_id)
                self.edges.append(
                    Edge(
                        type=EdgeType.CONTAINS,
                        source_id=package_id,
                        target_id=file_id,
                        target_name=file_name,
                    )
                )

        file_end_line = self.file_line_count
        self.nodes.append(
            Node(
                id=file_id,
                type=NodeType.FILE,
                name=file_name,
                file=self.relative_path,
                start_line=1,
                end_line=file_end_line,
                qualified_name=self.relative_path,
                parent_id=parent_for_file,
            )
        )
        self._node_ids.add(file_id)

        for child in root.named_children:
            if child.type == "import":
                self._handle_import(child, file_id)
            elif child.type in _TYPE_DECL_TYPES:
                self._handle_type_decl(child, enclosing_fqn=None, parent_id=file_id)
            elif child.type == "function_declaration":
                self._handle_function(
                    child, owner_id=file_id, owner_scope=self.owner_scope, edge_type=EdgeType.CONTAINS
                )
            elif child.type == "property_declaration":
                self._handle_property(
                    child, owner_id=file_id, owner_scope=self.owner_scope, edge_type=EdgeType.CONTAINS
                )
            # package_header handled above; typealias and other top-level
            # constructs are intentionally out of scope for Phase 3.

    def _handle_import(self, node: tree_sitter.Node, file_id: str) -> None:
        children = list(node.children)
        if len(children) < 2:
            return
        path_node = children[1]
        if not path_node.is_named:
            return
        raw = self.text(path_node)
        alias: str | None = None
        wildcard = False
        i = 2
        while i < len(children):
            c = children[i]
            if c.type == "as" and i + 1 < len(children):
                alias = self.text(children[i + 1])
                i += 1
            elif c.type == "*":
                wildcard = True
            i += 1

        raw_name = raw + ".*" if wildcard else raw
        start, end = self.loc(node)
        self.pending_refs.append(
            PendingRef(
                type=EdgeType.IMPORTS,
                source_id=file_id,
                raw_name=raw_name,
                location=Location(self.relative_path, start, end),
            )
        )
        if not wildcard:
            simple = alias if alias is not None else raw.rsplit(".", 1)[-1]
            self.import_map[simple] = raw

    # -- type declarations --------------------------------------------

    def _handle_type_decl(
        self, node: tree_sitter.Node, enclosing_fqn: str | None, parent_id: str
    ) -> None:
        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)

        if node.type == "object_declaration":
            node_type = NodeType.OBJECT
        else:
            is_interface = any(c.type == "interface" for c in node.children)
            body = first_named_child_of_type(node, ("class_body", "enum_class_body"))
            is_enum = "enum" in modifiers or (body is not None and body.type == "enum_class_body")
            if is_interface:
                node_type = NodeType.INTERFACE
            elif is_enum:
                node_type = NodeType.ENUM
            elif "annotation" in modifiers:
                node_type = NodeType.ANNOTATION
            else:
                node_type = NodeType.CLASS

        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        simple_name = self.text(name_node)
        if enclosing_fqn:
            fqn = f"{enclosing_fqn}.{simple_name}"
        elif self.package:
            fqn = f"{self.package}.{simple_name}"
        else:
            fqn = simple_name
        type_id = f"type:{fqn}"
        start, end = self.loc(node)
        type_params = self._extract_type_params(node)

        if not self.add_node(
            Node(
                id=type_id,
                type=node_type,
                name=simple_name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=fqn,
                parent_id=parent_id,
                modifiers=modifiers,
                annotations=annotations,
                type_parameters=type_params,
            )
        ):
            return

        self.edges.append(
            Edge(
                type=EdgeType.CONTAINS,
                source_id=parent_id,
                target_id=type_id,
                target_name=simple_name,
            )
        )

        self._handle_delegation_specifiers(node, type_id, node_type)

        primary_ctor = first_named_child_of_type(node, ("primary_constructor",))
        if primary_ctor is not None:
            self._handle_primary_constructor(primary_ctor, type_id, fqn)

        body = first_named_child_of_type(node, ("class_body", "enum_class_body"))
        if body is not None:
            if body.type == "enum_class_body":
                self._handle_enum_class_body(body, type_id, fqn)
            else:
                self._handle_body(body, type_id, fqn)

    def _handle_delegation_specifiers(
        self, node: tree_sitter.Node, type_id: str, node_type: NodeType
    ) -> None:
        wrapper = first_named_child_of_type(node, ("delegation_specifiers",))
        if wrapper is None:
            return
        for spec in wrapper.named_children:
            if spec.type != "delegation_specifier":
                continue
            type_node, is_ctor_call = _delegation_type_and_kind(spec)
            if type_node is None:
                continue
            if is_ctor_call:
                edge_type = EdgeType.EXTENDS
            elif node_type == NodeType.INTERFACE:
                edge_type = EdgeType.EXTENDS
            else:
                edge_type = EdgeType.IMPLEMENTS
            self._add_type_ref(type_id, edge_type, type_node)

    def _add_type_ref(
        self, source_id: str, edge_type: EdgeType, type_node: tree_sitter.Node
    ) -> None:
        raw = self.text(type_node)
        start, end = self.loc(type_node)
        self.pending_refs.append(
            PendingRef(
                type=edge_type,
                source_id=source_id,
                raw_name=raw,
                location=Location(self.relative_path, start, end),
            )
        )

    # -- primary constructor / class parameters --------------------------

    def _handle_primary_constructor(
        self, node: tree_sitter.Node, owner_id: str, owner_fqn: str
    ) -> None:
        params_wrapper = first_named_child_of_type(node, ("class_parameters",))
        entries = (
            [c for c in params_wrapper.named_children if c.type == "class_parameter"]
            if params_wrapper is not None
            else []
        )
        parsed = [self._extract_class_parameter(cp) for cp in entries]
        param_types = tuple(p[1] for p in parsed)
        signature = ", ".join(param_types)
        ctor_id = f"method:{owner_fqn}#<init>({signature})"

        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)
        start, end = self.loc(node)

        if not self.add_node(
            Node(
                id=ctor_id,
                type=NodeType.CONSTRUCTOR,
                name=owner_fqn.rsplit(".", 1)[-1],
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=f"{owner_fqn}.<init>",
                parent_id=owner_id,
                modifiers=modifiers,
                annotations=annotations,
                param_types=param_types,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.DECLARES,
                source_id=owner_id,
                target_id=ctor_id,
                target_name="<init>",
            )
        )

        param_index = 0
        for name, type_text, mods, annots, is_property, is_var in parsed:
            if is_property:
                self._add_constructor_property(
                    owner_id, owner_fqn, name, type_text, mods, annots, is_var, start, end
                )
            else:
                param_id = f"{ctor_id}/param/{param_index}:{name}"
                if self.add_node(
                    Node(
                        id=param_id,
                        type=NodeType.PARAMETER,
                        name=name,
                        file=self.relative_path,
                        start_line=start,
                        end_line=end,
                        parent_id=ctor_id,
                        modifiers=mods,
                        annotations=annots,
                        value_type=type_text,
                    )
                ):
                    self.edges.append(
                        Edge(
                            type=EdgeType.DECLARES,
                            source_id=ctor_id,
                            target_id=param_id,
                            target_name=name,
                        )
                    )
            param_index += 1

    def _add_constructor_property(
        self,
        owner_id: str,
        owner_fqn: str,
        name: str,
        type_text: str,
        modifiers: tuple[str, ...],
        annotations: tuple[str, ...],
        is_var: bool,
        start: int,
        end: int,
    ) -> None:
        property_id = f"property:{owner_fqn}#{name}"
        if not self.add_node(
            Node(
                id=property_id,
                type=NodeType.PROPERTY,
                name=name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=f"{owner_fqn}.{name}",
                parent_id=owner_id,
                modifiers=modifiers,
                annotations=annotations,
                value_type=type_text,
                mutable=is_var,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.DECLARES,
                source_id=owner_id,
                target_id=property_id,
                target_name=name,
            )
        )

    def _extract_class_parameter(
        self, node: tree_sitter.Node
    ) -> tuple[str, str, tuple[str, ...], tuple[str, ...], bool, bool]:
        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)
        is_val = any(c.type == "val" for c in node.children)
        is_var = any(c.type == "var" for c in node.children)
        named = [c for c in node.named_children if c.type != "modifiers"]
        name_node = named[0] if named else None
        type_node = named[1] if len(named) > 1 else None
        name = self.text(name_node) if name_node is not None else "?"
        type_text = self.text(type_node) if type_node is not None else ""
        return name, type_text, modifiers, annotations, (is_val or is_var), is_var

    # -- members --------------------------------------------------------

    def _handle_body(self, body: tree_sitter.Node, owner_id: str, owner_fqn: str) -> None:
        for child in body.named_children:
            if child.type in _TYPE_DECL_TYPES:
                self._handle_type_decl(child, enclosing_fqn=owner_fqn, parent_id=owner_id)
            elif child.type == "companion_object":
                self._handle_companion_object(child, owner_id, owner_fqn)
            elif child.type == "function_declaration":
                self._handle_function(
                    child, owner_id=owner_id, owner_scope=owner_fqn, edge_type=EdgeType.DECLARES
                )
            elif child.type == "property_declaration":
                self._handle_property(
                    child, owner_id=owner_id, owner_scope=owner_fqn, edge_type=EdgeType.DECLARES
                )
            elif child.type == "secondary_constructor":
                self._handle_secondary_constructor(child, owner_id, owner_fqn)
            # anonymous_initializer (`init { ... }`) and anything else:
            # intentionally out of scope for Phase 3.

    def _handle_enum_class_body(
        self, body: tree_sitter.Node, owner_id: str, owner_fqn: str
    ) -> None:
        for child in body.named_children:
            if child.type == "enum_entry":
                self._handle_enum_entry(child, owner_id, owner_fqn)
        self._handle_body(body, owner_id, owner_fqn)

    def _handle_enum_entry(self, node: tree_sitter.Node, owner_id: str, owner_fqn: str) -> None:
        name_node = first_named_child_of_type(node, ("identifier",))
        if name_node is None:
            return
        name = self.text(name_node)
        field_id = f"field:{owner_fqn}#{name}"
        start, end = self.loc(node)
        if not self.add_node(
            Node(
                id=field_id,
                type=NodeType.FIELD,
                name=name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=f"{owner_fqn}.{name}",
                parent_id=owner_id,
                value_type=owner_fqn,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.DECLARES,
                source_id=owner_id,
                target_id=field_id,
                target_name=name,
            )
        )

    def _handle_companion_object(
        self, node: tree_sitter.Node, owner_id: str, owner_fqn: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        simple_name = self.text(name_node) if name_node is not None else "Companion"
        fqn = f"{owner_fqn}.{simple_name}"
        type_id = f"type:{fqn}"
        start, end = self.loc(node)
        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)

        if not self.add_node(
            Node(
                id=type_id,
                type=NodeType.OBJECT,
                name=simple_name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=fqn,
                parent_id=owner_id,
                modifiers=modifiers,
                annotations=annotations,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.CONTAINS,
                source_id=owner_id,
                target_id=type_id,
                target_name=simple_name,
            )
        )

        self._handle_delegation_specifiers(node, type_id, NodeType.OBJECT)

        body = first_named_child_of_type(node, ("class_body",))
        if body is not None:
            self._handle_body(body, type_id, fqn)

    def _handle_secondary_constructor(
        self, node: tree_sitter.Node, owner_id: str, owner_fqn: str
    ) -> None:
        params_node = first_named_child_of_type(node, ("function_value_parameters",))
        param_infos = self._extract_params(params_node) if params_node is not None else []
        param_types = tuple(p.type_text for p in param_infos)
        signature = ", ".join(param_types)
        ctor_id = f"method:{owner_fqn}#<init>({signature})"

        start, end = self.loc(node)
        if not self.add_node(
            Node(
                id=ctor_id,
                type=NodeType.CONSTRUCTOR,
                name=owner_fqn.rsplit(".", 1)[-1],
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=f"{owner_fqn}.<init>",
                parent_id=owner_id,
                param_types=param_types,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.DECLARES,
                source_id=owner_id,
                target_id=ctor_id,
                target_name="<init>",
            )
        )
        self._add_param_nodes(ctor_id, param_infos, start, end)

    def _handle_function(
        self,
        node: tree_sitter.Node,
        *,
        owner_id: str,
        owner_scope: str,
        edge_type: EdgeType,
    ) -> None:
        name_idx = child_index_by_field(node, "name")
        name_node = node.child(name_idx) if name_idx is not None else None
        if name_node is None:
            return
        simple_name = self.text(name_node)
        receiver_node = _receiver_type_node(node, name_idx)
        receiver_type = self.text(receiver_node) if receiver_node is not None else None

        params_node = first_named_child_of_type(node, ("function_value_parameters",))
        param_infos = self._extract_params(params_node) if params_node is not None else []
        param_types = tuple(p.type_text for p in param_infos)
        signature = ", ".join(param_types)

        member_key = f"{receiver_type}.{simple_name}" if receiver_type else simple_name
        method_id = f"method:{owner_scope}#{member_key}({signature})"

        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)
        type_params = self._extract_type_params(node)

        # Find the return type by scanning for a ":" positioned after the
        # parameter list (there may be an unrelated ":" earlier, e.g. inside
        # a receiver-type-less signature there is none, but scanning after
        # params_idx keeps this unambiguous either way) and taking the type
        # node immediately following it — purely positional, so it can't be
        # confused with the receiver type, which only ever appears before
        # the parameter list.
        return_type: str | None = None
        params_idx = child_index_by_type(node, "function_value_parameters")
        if params_idx is not None:
            for i in range(params_idx + 1, node.child_count):
                c = node.child(i)
                if c is None:
                    continue
                if c.type == "function_body":
                    break
                if c.type == ":":
                    following = node.child(i + 1) if i + 1 < node.child_count else None
                    if following is not None and following.is_named:
                        return_type = self.text(following)
                    break

        has_body = first_named_child_of_type(node, ("function_body",)) is not None
        if not has_body and "abstract" not in modifiers:
            modifiers = modifiers + ("abstract",)

        start, end = self.loc(node)
        fqn = f"{owner_scope}.{member_key}" if owner_scope else member_key

        if not self.add_node(
            Node(
                id=method_id,
                type=NodeType.METHOD,
                name=simple_name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=fqn,
                parent_id=owner_id,
                modifiers=modifiers,
                annotations=annotations,
                type_parameters=type_params,
                param_types=param_types,
                return_type=return_type,
                receiver_type=receiver_type,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=edge_type,
                source_id=owner_id,
                target_id=method_id,
                target_name=simple_name,
            )
        )
        self._add_param_nodes(method_id, param_infos, start, end)

    def _add_param_nodes(
        self, method_id: str, param_infos: list[_ParamInfo], start: int, end: int
    ) -> None:
        for index, param in enumerate(param_infos):
            param_id = f"{method_id}/param/{index}:{param.name}"
            if not self.add_node(
                Node(
                    id=param_id,
                    type=NodeType.PARAMETER,
                    name=param.name,
                    file=self.relative_path,
                    start_line=start,
                    end_line=end,
                    parent_id=method_id,
                    modifiers=param.modifiers,
                    annotations=param.annotations,
                    value_type=param.type_text,
                )
            ):
                continue
            self.edges.append(
                Edge(
                    type=EdgeType.DECLARES,
                    source_id=method_id,
                    target_id=param_id,
                    target_name=param.name,
                )
            )

    def _handle_property(
        self,
        node: tree_sitter.Node,
        *,
        owner_id: str,
        owner_scope: str,
        edge_type: EdgeType,
    ) -> None:
        var_decl_idx = child_index_by_type(node, "variable_declaration")
        var_decl = node.child(var_decl_idx) if var_decl_idx is not None else None
        if var_decl is None:
            return
        name_node = first_named_child_of_type(var_decl, ("identifier",))
        if name_node is None:
            return
        simple_name = self.text(name_node)

        receiver_node = _receiver_type_node(node, var_decl_idx)
        receiver_type = self.text(receiver_node) if receiver_node is not None else None

        type_nodes = [c for c in var_decl.named_children if c.type != "identifier"]
        type_text = self.text(type_nodes[0]) if type_nodes else None

        is_var = any(c.type == "var" for c in node.children)
        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)

        member_key = f"{receiver_type}.{simple_name}" if receiver_type else simple_name
        property_id = f"property:{owner_scope}#{member_key}"
        fqn = f"{owner_scope}.{member_key}" if owner_scope else member_key
        start, end = self.loc(node)

        if not self.add_node(
            Node(
                id=property_id,
                type=NodeType.PROPERTY,
                name=simple_name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=fqn,
                parent_id=owner_id,
                modifiers=modifiers,
                annotations=annotations,
                value_type=type_text,
                receiver_type=receiver_type,
                mutable=is_var,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=edge_type,
                source_id=owner_id,
                target_id=property_id,
                target_name=simple_name,
            )
        )

    # -- shared helpers ---------------------------------------------------

    def _extract_modifiers(
        self, node: tree_sitter.Node | None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if node is None:
            return (), ()
        modifiers: list[str] = []
        annotations: list[str] = []
        for child in node.named_children:
            if child.type == "annotation":
                name = self._annotation_name(child)
                if name is not None:
                    annotations.append(name)
            else:
                # inheritance_modifier / class_modifier / member_modifier /
                # visibility_modifier / function_modifier / property_modifier
                # / platform_modifier: each wraps exactly one keyword token,
                # so its raw text *is* the modifier name.
                modifiers.append(self.text(child))
        return tuple(modifiers), tuple(annotations)

    def _annotation_name(self, node: tree_sitter.Node) -> str | None:
        for child in node.named_children:
            if child.type == "use_site_target":
                continue
            if child.type == "constructor_invocation":
                inner = first_named_child_of_type(child, ("user_type",))
                return self.text(inner) if inner is not None else None
            if child.type == "user_type":
                return self.text(child)
        return None

    def _extract_type_params(self, node: tree_sitter.Node) -> tuple[str, ...]:
        tp_node = first_named_child_of_type(node, ("type_parameters",))
        if tp_node is None:
            return ()
        names: list[str] = []
        for child in tp_node.named_children:
            if child.type == "type_parameter" and child.child_count:
                first = child.child(0)
                if first is not None and first.type == "identifier":
                    names.append(self.text(first))
        return tuple(names)

    def _extract_params(self, params_node: tree_sitter.Node) -> list[_ParamInfo]:
        results: list[_ParamInfo] = []
        pending_modifiers: tuple[str, ...] = ()
        pending_annotations: tuple[str, ...] = ()
        for child in params_node.named_children:
            if child.type == "parameter_modifiers":
                mods: list[str] = []
                annots: list[str] = []
                for m in child.named_children:
                    if m.type == "annotation":
                        name = self._annotation_name(m)
                        if name is not None:
                            annots.append(name)
                    elif m.type == "parameter_modifier":
                        mods.append(self.text(m))
                pending_modifiers = tuple(mods)
                pending_annotations = tuple(annots)
            elif child.type == "parameter":
                name_node = first_named_child_of_type(child, ("identifier",))
                if name_node is None:
                    pending_modifiers, pending_annotations = (), ()
                    continue
                type_nodes = [c for c in child.named_children if c.type != "identifier"]
                type_text = self.text(type_nodes[0]) if type_nodes else ""
                results.append(
                    _ParamInfo(
                        self.text(name_node), type_text, pending_modifiers, pending_annotations
                    )
                )
                pending_modifiers, pending_annotations = (), ()
        return results
