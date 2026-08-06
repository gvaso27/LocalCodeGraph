from __future__ import annotations

from pathlib import Path

import pytest

from local_code_graph.graph.model import EdgeType, NodeType
from local_code_graph.parser.java import JavaParser

FIXTURES = Path(__file__).parent / "fixtures" / "java"


def parse(source: str, path: str = "Test.java"):
    return JavaParser().parse(path, source.encode("utf-8"))


def node(result, node_id: str):
    for n in result.nodes:
        if n.id == node_id:
            return n
    raise AssertionError(f"no node with id {node_id!r} in {[n.id for n in result.nodes]}")


def find_nodes(result, node_type: NodeType):
    return [n for n in result.nodes if n.type == node_type]


def pending(result, edge_type: EdgeType):
    return [p for p in result.pending_refs if p.type == edge_type]


# -- package / imports -----------------------------------------------------


def test_package_declaration():
    result = parse("package com.example.sound;\n\nclass Foo {}\n")
    assert result.context.package == "com.example.sound"
    pkg = node(result, "package:com.example.sound")
    assert pkg.type == NodeType.PACKAGE
    assert pkg.qualified_name == "com.example.sound"
    assert pkg.file is None
    assert pkg.start_line is None


def test_no_package_declaration_leaves_context_package_none():
    result = parse("class Foo {}\n")
    assert result.context.package is None
    file_node = node(result, "file:Test.java")
    assert file_node.parent_id is None


def test_regular_import_recorded_and_added_to_import_map():
    result = parse("import java.util.List;\n\nclass Foo {}\n")
    imports = pending(result, EdgeType.IMPORTS)
    assert len(imports) == 1
    assert imports[0].raw_name == "java.util.List"
    assert dict(result.context.imports) == {"List": "java.util.List"}


def test_wildcard_import_recorded_but_not_in_import_map():
    result = parse("import com.example.common.*;\n\nclass Foo {}\n")
    imports = pending(result, EdgeType.IMPORTS)
    assert imports[0].raw_name == "com.example.common.*"
    assert dict(result.context.imports) == {}


def test_static_import_recorded_but_not_in_import_map():
    result = parse("import static com.example.util.Helpers.util;\n\nclass Foo {}\n")
    imports = pending(result, EdgeType.IMPORTS)
    assert imports[0].raw_name == "com.example.util.Helpers.util"
    assert dict(result.context.imports) == {}


# -- classes / visibility ----------------------------------------------------


@pytest.mark.parametrize(
    "modifier,expected",
    [
        ("public", ("public",)),
        ("", ()),  # package-private
    ],
)
def test_class_visibility_modifiers(modifier, expected):
    source = f"{modifier} class Foo {{}}\n"
    result = parse(source)
    foo = node(result, "type:Foo")
    assert foo.modifiers == expected


def test_abstract_class():
    result = parse("public abstract class Foo { public abstract void bar(); }\n")
    foo = node(result, "type:Foo")
    assert "abstract" in foo.modifiers
    method = node(result, "method:Foo#bar()")
    assert "abstract" in method.modifiers


def test_nested_class_qualified_name_and_parent():
    result = parse(
        """
        package com.example;
        public class Outer {
            public class Inner {
            }
        }
        """
    )
    inner = node(result, "type:com.example.Outer.Inner")
    assert inner.qualified_name == "com.example.Outer.Inner"
    assert inner.parent_id == "type:com.example.Outer"
    contains_edges = [
        e for e in result.edges if e.type == EdgeType.CONTAINS and e.source_id == "type:com.example.Outer"
    ]
    assert any(e.target_id == "type:com.example.Outer.Inner" for e in contains_edges)


def test_multiple_top_level_types_in_one_file():
    result = parse("public class Foo {}\nclass Helper {}\n")
    foo = node(result, "type:Foo")
    helper = node(result, "type:Helper")
    assert foo.parent_id == "file:Test.java"
    assert helper.parent_id == "file:Test.java"
    assert helper.modifiers == ()


# -- interfaces / enums / annotations ----------------------------------------


def test_interface_declaration():
    result = parse("public interface Foo { void bar(); }\n")
    foo = node(result, "type:Foo")
    assert foo.type == NodeType.INTERFACE
    bar = node(result, "method:Foo#bar()")
    assert "abstract" in bar.modifiers


def test_interface_extends_multiple_interfaces():
    result = parse("public interface Foo extends A, B {}\n")
    extends = pending(result, EdgeType.EXTENDS)
    names = {p.raw_name for p in extends}
    assert names == {"A", "B"}
    assert all(p.source_id == "type:Foo" for p in extends)


def test_enum_declaration_with_constants_and_implements():
    result = parse(
        """
        public enum Status implements Runnable {
            ACTIVE, INACTIVE;

            public void run() {}
        }
        """
    )
    status = node(result, "type:Status")
    assert status.type == NodeType.ENUM
    active = node(result, "field:Status#ACTIVE")
    assert active.type == NodeType.FIELD
    assert active.modifiers == ("public", "static", "final")
    assert active.value_type == "Status"
    implements = pending(result, EdgeType.IMPLEMENTS)
    assert implements[0].raw_name == "Runnable"
    run_method = node(result, "method:Status#run()")
    assert run_method.type == NodeType.METHOD


def test_annotation_type_declaration():
    result = parse(
        """
        public @interface MyAnno {
            String value() default "x";
            int priority();
        }
        """
    )
    anno = node(result, "type:MyAnno")
    assert anno.type == NodeType.ANNOTATION
    value = node(result, "method:MyAnno#value()")
    assert value.return_type == "String"
    priority = node(result, "method:MyAnno#priority()")
    assert priority.return_type == "int"


def test_annotations_on_class_method_and_field():
    result = parse(
        """
        @Deprecated
        public class Foo {
            @Deprecated
            private int x;

            @Override
            public void bar() {}
        }
        """
    )
    foo = node(result, "type:Foo")
    assert foo.annotations == ("Deprecated",)
    x = node(result, "field:Foo#x")
    assert x.annotations == ("Deprecated",)
    bar = node(result, "method:Foo#bar()")
    assert bar.annotations == ("Override",)


# -- generics -----------------------------------------------------------------


def test_generic_class_type_parameters():
    result = parse("public class Box<T> { T value; }\n")
    box = node(result, "type:Box")
    assert box.type_parameters == ("T",)
    value = node(result, "field:Box#value")
    assert value.value_type == "T"


def test_generic_method_type_parameters_and_bound():
    result = parse(
        "public class Foo { public <T extends Comparable<T>> T max(T a, T b) { return a; } }\n"
    )
    method_id = "method:Foo#max(T, T)"
    method = node(result, method_id)
    assert method.type_parameters == ("T",)
    assert method.param_types == ("T", "T")


def test_generic_field_and_extends_generic_type_stripped_for_resolution():
    result = parse(
        """
        package com.example;
        public class Repo<T> {}
        public class Impl extends Repo<String> {}
        """
    )
    extends_refs = pending(result, EdgeType.EXTENDS)
    assert extends_refs[0].raw_name == "Repo<String>"


# -- constructors / overloads --------------------------------------------------


def test_constructor_node_and_id():
    result = parse("public class Foo { public Foo(int x) {} }\n")
    ctor = node(result, "method:Foo#<init>(int)")
    assert ctor.type == NodeType.CONSTRUCTOR
    assert ctor.name == "Foo"
    assert ctor.qualified_name == "Foo.<init>"


def test_overloaded_constructors_get_distinct_ids():
    result = parse(
        """
        public class Foo {
            public Foo() {}
            public Foo(int x) {}
            public Foo(String s, int x) {}
        }
        """
    )
    constructors = find_nodes(result, NodeType.CONSTRUCTOR)
    ids = {c.id for c in constructors}
    assert ids == {
        "method:Foo#<init>()",
        "method:Foo#<init>(int)",
        "method:Foo#<init>(String, int)",
    }


def test_overloaded_methods_get_distinct_ids():
    result = parse(
        """
        public class Foo {
            public void bar() {}
            public void bar(int x) {}
            public void bar(String s) {}
        }
        """
    )
    methods = find_nodes(result, NodeType.METHOD)
    ids = {m.id for m in methods}
    assert ids == {
        "method:Foo#bar()",
        "method:Foo#bar(int)",
        "method:Foo#bar(String)",
    }


def test_method_without_body_is_marked_abstract():
    result = parse("public interface Foo { void bar(); }\n")
    bar = node(result, "method:Foo#bar()")
    assert "abstract" in bar.modifiers


# -- fields ---------------------------------------------------------------------


def test_static_field():
    result = parse("public class Foo { public static final int MAX = 5; }\n")
    field = node(result, "field:Foo#MAX")
    assert field.type == NodeType.FIELD
    assert set(field.modifiers) == {"public", "static", "final"}
    assert field.value_type == "int"


def test_multiple_declarators_in_one_field_declaration():
    result = parse("public class Foo { int a, b, c; }\n")
    a = node(result, "field:Foo#a")
    b = node(result, "field:Foo#b")
    c = node(result, "field:Foo#c")
    assert {a.value_type, b.value_type, c.value_type} == {"int"}


def test_generic_field_type_text_preserved():
    result = parse("import java.util.List;\npublic class Foo { private List<String> names; }\n")
    names = node(result, "field:Foo#names")
    assert names.value_type == "List<String>"


# -- parameters -----------------------------------------------------------------


def test_parameter_nodes_and_annotations():
    result = parse(
        "public class Foo { public void bar(@Deprecated String s, int limit) {} }\n"
    )
    method_id = "method:Foo#bar(String, int)"
    p0 = node(result, f"{method_id}/param/0:s")
    p1 = node(result, f"{method_id}/param/1:limit")
    assert p0.type == NodeType.PARAMETER
    assert p0.value_type == "String"
    assert p0.annotations == ("Deprecated",)
    assert p1.value_type == "int"
    declares = [e for e in result.edges if e.type == EdgeType.DECLARES and e.source_id == method_id]
    assert {e.target_id for e in declares} == {p0.id, p1.id}


def test_varargs_parameter():
    result = parse("public class Foo { public void bar(String... args) {} }\n")
    method_id = "method:Foo#bar(String...)"
    method = node(result, method_id)
    assert method.param_types == ("String...",)
    param = node(result, f"{method_id}/param/0:args")
    assert param.value_type == "String..."


# -- extends / implements -------------------------------------------------------


def test_class_extends_single_class():
    result = parse("public class Foo extends Bar {}\n")
    extends = pending(result, EdgeType.EXTENDS)
    assert len(extends) == 1
    assert extends[0].raw_name == "Bar"
    assert extends[0].source_id == "type:Foo"


def test_class_implements_multiple_interfaces():
    result = parse("public class Foo implements A, B, C {}\n")
    implements = pending(result, EdgeType.IMPLEMENTS)
    names = {p.raw_name for p in implements}
    assert names == {"A", "B", "C"}


def test_class_extends_and_implements_together_multiline():
    result = parse(
        """
        public class Foo
                extends Bar
                implements A, B {
        }
        """
    )
    extends = pending(result, EdgeType.EXTENDS)
    implements = pending(result, EdgeType.IMPLEMENTS)
    assert extends[0].raw_name == "Bar"
    assert {p.raw_name for p in implements} == {"A", "B"}


def test_qualified_extends_target_preserved_raw():
    result = parse("public class Foo extends com.example.base.Base {}\n")
    extends = pending(result, EdgeType.EXTENDS)
    assert extends[0].raw_name == "com.example.base.Base"


# -- locations --------------------------------------------------------------------


def test_source_locations_are_one_based_and_exact():
    source = "package com.example;\n\npublic class Foo {\n    void bar() {\n    }\n}\n"
    result = parse(source)
    foo = node(result, "type:com.example.Foo")
    # line 3 (1-based): "public class Foo {"      line 6: closing brace "}"
    assert foo.start_line == 3
    assert foo.end_line == 6
    bar = node(result, "method:com.example.Foo#bar()")
    assert bar.start_line == 4
    assert bar.end_line == 5


def test_file_node_spans_whole_file():
    source = "class Foo {\n}\n"
    result = parse(source)
    file_node = node(result, "file:Test.java")
    assert file_node.start_line == 1
    assert file_node.end_line == 2


# -- malformed input -----------------------------------------------------------


def test_malformed_file_records_errors_but_does_not_raise():
    content = (FIXTURES / "malformed" / "Broken.java").read_bytes()
    result = JavaParser().parse("Broken.java", content)
    assert len(result.errors) > 0
    # The still-parseable class declaration should have been extracted.
    assert any(n.id == "type:com.example.broken.Broken" for n in result.nodes)


def test_completely_garbage_content_does_not_raise():
    result = JavaParser().parse("Garbage.java", b"{{{ not java at all ]][ ")
    assert isinstance(result.errors, tuple)
    file_node = node(result, "file:Garbage.java")
    assert file_node.type == NodeType.FILE


def test_empty_file_does_not_raise():
    result = JavaParser().parse("Empty.java", b"")
    file_node = node(result, "file:Empty.java")
    assert file_node.start_line == 1
    assert file_node.end_line == 1
    assert result.errors == ()


# -- fixture-based end-to-end sanity check -----------------------------------


def test_full_features_fixture_parses_without_errors():
    path = FIXTURES / "full_features" / "src/main/java/com/example/app/SoundController.java"
    result = JavaParser().parse(str(path), path.read_bytes())
    assert result.errors == ()
    type_names = {n.qualified_name for n in find_nodes(result, NodeType.CLASS)}
    assert "com.example.app.SoundController" in type_names
    assert "com.example.app.SoundController.InnerBase" in type_names
    assert "com.example.app.SoundController.InnerImpl" in type_names
    enum_names = {n.qualified_name for n in find_nodes(result, NodeType.ENUM)}
    assert "com.example.app.SoundController.Status" in enum_names
    annotation_names = {n.qualified_name for n in find_nodes(result, NodeType.ANNOTATION)}
    assert "com.example.app.SoundController.SoundAnnotation" in annotation_names
    method_ids = {n.id for n in find_nodes(result, NodeType.METHOD)}
    assert "method:com.example.app.SoundController#getSounds()" in method_ids
    assert "method:com.example.app.SoundController#getSounds(String, int)" in method_ids
