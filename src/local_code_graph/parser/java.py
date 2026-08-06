"""Java parser built on Tree-sitter.

Extracts packages, imports, classes, interfaces, enums, annotation types,
fields, constructors, methods, parameters, and their explicit `extends` /
`implements` / `import` references, and turns them into the common graph
model (see graph/model.py). Tree-sitter-specific logic is confined to this
module — the rest of the application only ever sees Node/Edge/ParseResult.

Known limitations (deliberate, to avoid inventing unreliable relationships):

* Local classes/interfaces/enums declared inside a method body, and
  anonymous classes (`new Foo() { ... }`), are not extracted. Only types
  declared directly inside a file or inside another type's body are.
* Static and instance initializer blocks (`static { ... }` / `{ ... }`) are
  not extracted as nodes.
* C-style array declarators (`int legacyArr[];`, where `[]` follows the
  variable name instead of the type) are not reflected in the recorded
  field/parameter type text — only the type before the declarator is used.
* `extends`/`implements`/`import` targets are matched only via exact
  fully-qualified-name lookup, the file's own explicit (non-wildcard,
  non-static) imports, or same-package lookup. External/JDK/library types,
  wildcard imports, and static imports are always left unresolved
  (`target_id is None`) rather than guessed at.
* Method/constructor overload identity is based on raw declared parameter
  type text, not a resolved/erased JVM signature — this is sufficient to
  tell overloads apart without a type checker.
"""

from __future__ import annotations

import tree_sitter
import tree_sitter_java

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
    first_child_of_type,
    first_named_child_of_type,
    line_count,
    node_line_span,
)
from local_code_graph.parser.base import FileContext, LanguageParser, ParseResult, PendingRef

_LANGUAGE = tree_sitter.Language(tree_sitter_java.language())

_TYPE_DECL_TYPES = frozenset(
    {
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "annotation_type_declaration",
    }
)

_TYPE_DECL_NODE_TYPE = {
    "class_declaration": NodeType.CLASS,
    "interface_declaration": NodeType.INTERFACE,
    "enum_declaration": NodeType.ENUM,
    "annotation_type_declaration": NodeType.ANNOTATION,
}


class JavaParser(LanguageParser):
    def __init__(self) -> None:
        self._parser = tree_sitter.Parser(_LANGUAGE)

    def parse(self, relative_path: str, content: bytes) -> ParseResult:
        tree = self._parser.parse(content)
        builder = _JavaFileBuilder(relative_path, content)
        builder.walk_program(tree.root_node)

        errors = list(builder.errors)
        for error_node in find_error_nodes(tree.root_node):
            start_line = error_node.start_point.row + 1
            end_line = error_node.end_point.row + 1
            errors.append(
                ParseError(
                    file=relative_path,
                    message="syntax error",
                    start_line=start_line,
                    end_line=end_line,
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


class _JavaFileBuilder:
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

    def walk_program(self, root: tree_sitter.Node) -> None:
        file_id = f"file:{self.relative_path}"
        file_name = self.relative_path.rsplit("/", 1)[-1]

        pkg_node = first_child_of_type(root, "package_declaration")
        parent_for_file: str | None = None
        if pkg_node is not None:
            name_node = first_named_child_of_type(pkg_node, ("scoped_identifier", "identifier"))
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
            if child.type == "import_declaration":
                self._handle_import(child, file_id)
            elif child.type in _TYPE_DECL_TYPES:
                self._handle_type_decl(child, enclosing_fqn=None, parent_id=file_id)

    def _handle_import(self, node: tree_sitter.Node, file_id: str) -> None:
        is_static = any(c.type == "static" for c in node.children)
        is_wildcard = any(c.type == "asterisk" for c in node.children)
        name_node = first_named_child_of_type(node, ("scoped_identifier", "identifier"))
        if name_node is None:
            return
        raw = self.text(name_node)
        if is_wildcard:
            raw = raw + ".*"
        start, end = self.loc(node)
        self.pending_refs.append(
            PendingRef(
                type=EdgeType.IMPORTS,
                source_id=file_id,
                raw_name=raw,
                location=Location(self.relative_path, start, end),
            )
        )
        if not is_wildcard and not is_static:
            simple = raw.rsplit(".", 1)[-1]
            self.import_map[simple] = raw

    # -- type declarations --------------------------------------------

    def _handle_type_decl(
        self, node: tree_sitter.Node, enclosing_fqn: str | None, parent_id: str
    ) -> None:
        node_type = _TYPE_DECL_NODE_TYPE[node.type]
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
        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)
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

        if node.type == "class_declaration":
            superclass = node.child_by_field_name("superclass")
            if superclass is not None and superclass.named_children:
                self._add_type_ref(type_id, EdgeType.EXTENDS, superclass.named_children[0])
            interfaces = node.child_by_field_name("interfaces")
            if interfaces is not None:
                self._add_type_list_refs(type_id, EdgeType.IMPLEMENTS, interfaces)
        elif node.type == "interface_declaration":
            extends_ifaces = first_named_child_of_type(node, ("extends_interfaces",))
            if extends_ifaces is not None:
                self._add_type_list_refs(type_id, EdgeType.EXTENDS, extends_ifaces)
        elif node.type == "enum_declaration":
            interfaces = node.child_by_field_name("interfaces")
            if interfaces is not None:
                self._add_type_list_refs(type_id, EdgeType.IMPLEMENTS, interfaces)

        body = node.child_by_field_name("body")
        if body is not None:
            self._handle_body(body, type_id, fqn)

    def _add_type_list_refs(
        self, source_id: str, edge_type: EdgeType, wrapper_node: tree_sitter.Node
    ) -> None:
        type_list = first_named_child_of_type(wrapper_node, ("type_list",))
        if type_list is None:
            return
        for type_node in type_list.named_children:
            self._add_type_ref(source_id, edge_type, type_node)

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

    # -- members --------------------------------------------------------

    def _handle_body(self, body: tree_sitter.Node, owner_id: str, owner_fqn: str) -> None:
        for child in body.named_children:
            if child.type in _TYPE_DECL_TYPES:
                self._handle_type_decl(child, enclosing_fqn=owner_fqn, parent_id=owner_id)
            elif child.type == "field_declaration":
                self._handle_field(child, owner_id, owner_fqn)
            elif child.type == "constructor_declaration":
                self._handle_method_like(child, owner_id, owner_fqn, is_constructor=True)
            elif child.type == "method_declaration":
                self._handle_method_like(child, owner_id, owner_fqn, is_constructor=False)
            elif child.type == "annotation_type_element_declaration":
                self._handle_annotation_element(child, owner_id, owner_fqn)
            elif child.type == "enum_constant":
                self._handle_enum_constant(child, owner_id, owner_fqn)
            elif child.type == "enum_body_declarations":
                self._handle_body(child, owner_id, owner_fqn)
            # static/instance initializer blocks, comments, etc.: out of
            # scope for Phase 2, intentionally skipped.

    def _handle_field(self, node: tree_sitter.Node, owner_id: str, owner_fqn: str) -> None:
        type_node = node.child_by_field_name("type")
        type_text = self.text(type_node) if type_node is not None else None
        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)
        start, end = self.loc(node)
        for declarator in node.children_by_field_name("declarator"):
            name_node = declarator.child_by_field_name("name")
            if name_node is None:
                continue
            name = self.text(name_node)
            field_id = f"field:{owner_fqn}#{name}"
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
                    modifiers=modifiers,
                    annotations=annotations,
                    value_type=type_text,
                )
            ):
                continue
            self.edges.append(
                Edge(
                    type=EdgeType.DECLARES,
                    source_id=owner_id,
                    target_id=field_id,
                    target_name=name,
                )
            )

    def _handle_method_like(
        self,
        node: tree_sitter.Node,
        owner_id: str,
        owner_fqn: str,
        *,
        is_constructor: bool,
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        simple_name = self.text(name_node)
        params_node = node.child_by_field_name("parameters")
        param_infos = self._extract_params(params_node) if params_node is not None else []
        param_types = tuple(p.type_text for p in param_infos)
        member_name = "<init>" if is_constructor else simple_name
        signature = ", ".join(param_types)
        method_id = f"method:{owner_fqn}#{member_name}({signature})"

        modifiers_node = first_named_child_of_type(node, ("modifiers",))
        modifiers, annotations = self._extract_modifiers(modifiers_node)
        type_params = self._extract_type_params(node)

        return_type: str | None = None
        if not is_constructor:
            type_node = node.child_by_field_name("type")
            return_type = self.text(type_node) if type_node is not None else None

        has_body = node.child_by_field_name("body") is not None
        if not has_body and "abstract" not in modifiers:
            modifiers = modifiers + ("abstract",)

        start, end = self.loc(node)
        fqn = f"{owner_fqn}.<init>" if is_constructor else f"{owner_fqn}.{simple_name}"
        node_type = NodeType.CONSTRUCTOR if is_constructor else NodeType.METHOD

        if not self.add_node(
            Node(
                id=method_id,
                type=node_type,
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
            )
        ):
            return

        self.edges.append(
            Edge(
                type=EdgeType.DECLARES,
                source_id=owner_id,
                target_id=method_id,
                target_name=simple_name,
            )
        )

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
                    qualified_name=None,
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

    def _handle_annotation_element(
        self, node: tree_sitter.Node, owner_id: str, owner_fqn: str
    ) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        simple_name = self.text(name_node)
        type_node = node.child_by_field_name("type")
        return_type = self.text(type_node) if type_node is not None else None
        method_id = f"method:{owner_fqn}#{simple_name}()"
        start, end = self.loc(node)
        if not self.add_node(
            Node(
                id=method_id,
                type=NodeType.METHOD,
                name=simple_name,
                file=self.relative_path,
                start_line=start,
                end_line=end,
                qualified_name=f"{owner_fqn}.{simple_name}",
                parent_id=owner_id,
                modifiers=("abstract",),
                param_types=(),
                return_type=return_type,
            )
        ):
            return
        self.edges.append(
            Edge(
                type=EdgeType.DECLARES,
                source_id=owner_id,
                target_id=method_id,
                target_name=simple_name,
            )
        )

    def _handle_enum_constant(
        self, node: tree_sitter.Node, owner_id: str, owner_fqn: str
    ) -> None:
        name_node = node.child_by_field_name("name")
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
                modifiers=("public", "static", "final"),
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

    # -- shared helpers ---------------------------------------------------

    def _extract_modifiers(
        self, node: tree_sitter.Node | None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if node is None:
            return (), ()
        modifiers: list[str] = []
        annotations: list[str] = []
        for child in node.children:
            if not child.is_named:
                modifiers.append(self.text(child))
            elif child.type in ("marker_annotation", "annotation"):
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    annotations.append(self.text(name_node))
        return tuple(modifiers), tuple(annotations)

    def _extract_type_params(self, node: tree_sitter.Node) -> tuple[str, ...]:
        tp_node = node.child_by_field_name("type_parameters")
        if tp_node is None:
            return ()
        names: list[str] = []
        for child in tp_node.named_children:
            if child.type == "type_parameter" and child.child_count:
                first = child.child(0)
                if first is not None and first.type == "type_identifier":
                    names.append(self.text(first))
        return tuple(names)

    def _extract_params(self, params_node: tree_sitter.Node) -> list[_ParamInfo]:
        results: list[_ParamInfo] = []
        for child in params_node.named_children:
            if child.type == "formal_parameter":
                type_node = child.child_by_field_name("type")
                name_node = child.child_by_field_name("name")
                if name_node is None:
                    continue
                mods_node = first_named_child_of_type(child, ("modifiers",))
                mods, annots = self._extract_modifiers(mods_node)
                type_text = self.text(type_node) if type_node is not None else ""
                results.append(_ParamInfo(self.text(name_node), type_text, mods, annots))
            elif child.type == "spread_parameter":
                type_node = child.named_children[0] if child.named_children else None
                var_decl = first_named_child_of_type(child, ("variable_declarator",))
                name = "args"
                if var_decl is not None:
                    name_node = var_decl.child_by_field_name("name")
                    if name_node is not None:
                        name = self.text(name_node)
                type_text = (self.text(type_node) + "...") if type_node is not None else "..."
                results.append(_ParamInfo(name, type_text, (), ()))
            # receiver_parameter (explicit `Outer.this` receiver syntax) is
            # intentionally skipped: it is not a real parameter.
        return results
