from __future__ import annotations

from pathlib import Path

import pytest

from local_code_graph.graph.model import EdgeType, NodeType
from local_code_graph.parser.kotlin import KotlinParser

FIXTURES = Path(__file__).parent / "fixtures" / "kotlin"


def parse(source: str, path: str = "Test.kt"):
    return KotlinParser().parse(path, source.encode("utf-8"))


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
    result = parse("package com.example.app\n\nclass Foo\n")
    assert result.context.package == "com.example.app"
    pkg = node(result, "package:com.example.app")
    assert pkg.type == NodeType.PACKAGE
    assert pkg.file is None


def test_no_package_declaration():
    result = parse("class Foo\n")
    assert result.context.package is None
    assert node(result, "file:Test.kt").parent_id is None


def test_regular_import():
    result = parse("import java.util.List\n\nclass Foo\n")
    imports = pending(result, EdgeType.IMPORTS)
    assert imports[0].raw_name == "java.util.List"
    assert dict(result.context.imports) == {"List": "java.util.List"}


def test_aliased_import():
    result = parse("import java.util.Map as JMap\n\nclass Foo\n")
    imports = pending(result, EdgeType.IMPORTS)
    assert imports[0].raw_name == "java.util.Map"
    assert dict(result.context.imports) == {"JMap": "java.util.Map"}


def test_wildcard_import():
    result = parse("import com.example.util.*\n\nclass Foo\n")
    imports = pending(result, EdgeType.IMPORTS)
    assert imports[0].raw_name == "com.example.util.*"
    assert dict(result.context.imports) == {}


# -- classes -----------------------------------------------------------------


def test_plain_class():
    result = parse("class Foo\n")
    foo = node(result, "type:Foo")
    assert foo.type == NodeType.CLASS
    assert foo.modifiers == ()


def test_abstract_class():
    result = parse("abstract class Foo {\n    abstract fun bar()\n}\n")
    foo = node(result, "type:Foo")
    assert "abstract" in foo.modifiers
    bar = node(result, "method:Foo#bar()")
    assert "abstract" in bar.modifiers


def test_open_class():
    result = parse("open class Foo\n")
    foo = node(result, "type:Foo")
    assert "open" in foo.modifiers


def test_data_class_generates_constructor_and_properties():
    result = parse("data class Point(val x: Int, val y: Int)\n")
    foo = node(result, "type:Point")
    assert "data" in foo.modifiers
    ctor = node(result, "method:Point#<init>(Int, Int)")
    assert ctor.type == NodeType.CONSTRUCTOR
    x = node(result, "property:Point#x")
    y = node(result, "property:Point#y")
    assert x.type == NodeType.PROPERTY
    assert x.value_type == "Int"
    assert x.mutable is False
    assert y.mutable is False
    # Constructor-parameter properties are declared by the type, not the ctor.
    declares_from_ctor = [e for e in result.edges if e.source_id == ctor.id]
    assert declares_from_ctor == []
    declares_from_type = {
        e.target_id for e in result.edges if e.type == EdgeType.DECLARES and e.source_id == foo.id
    }
    assert {ctor.id, x.id, y.id} <= declares_from_type


def test_sealed_class():
    result = parse("sealed class Result\n")
    foo = node(result, "type:Result")
    assert "sealed" in foo.modifiers


def test_nested_class():
    result = parse(
        """
        package com.example
        class Outer {
            class Nested {
            }
        }
        """
    )
    nested = node(result, "type:com.example.Outer.Nested")
    assert nested.parent_id == "type:com.example.Outer"
    assert "inner" not in nested.modifiers


def test_inner_class():
    result = parse(
        """
        class Outer {
            inner class Inner {
            }
        }
        """
    )
    inner = node(result, "type:Outer.Inner")
    assert "inner" in inner.modifiers


def test_generic_class():
    result = parse("class Box<T>(val value: T)\n")
    box = node(result, "type:Box")
    assert box.type_parameters == ("T",)
    value = node(result, "property:Box#value")
    assert value.value_type == "T"


def test_class_with_constructor_parameters_mixed_property_and_plain():
    result = parse("class Foo(val a: Int, b: String)\n")
    ctor = node(result, "method:Foo#<init>(Int, String)")
    a = node(result, "property:Foo#a")
    assert a.type == NodeType.PROPERTY
    b = node(result, f"{ctor.id}/param/1:b")
    assert b.type == NodeType.PARAMETER
    assert b.value_type == "String"


def test_class_extends_another_class():
    result = parse("open class Base\nclass Foo : Base()\n")
    extends = pending(result, EdgeType.EXTENDS)
    assert len(extends) == 1
    assert extends[0].raw_name == "Base"
    assert extends[0].source_id == "type:Foo"


def test_class_implements_interface():
    result = parse("interface Bar\nclass Foo : Bar\n")
    implements = pending(result, EdgeType.IMPLEMENTS)
    assert implements[0].raw_name == "Bar"
    assert implements[0].source_id == "type:Foo"


def test_class_extends_and_implements_together():
    result = parse("open class Base\ninterface Bar\nclass Foo : Base(), Bar\n")
    extends = pending(result, EdgeType.EXTENDS)
    implements = pending(result, EdgeType.IMPLEMENTS)
    foo_extends = [p for p in extends if p.source_id == "type:Foo"]
    foo_implements = [p for p in implements if p.source_id == "type:Foo"]
    assert foo_extends[0].raw_name == "Base"
    assert foo_implements[0].raw_name == "Bar"


# -- interfaces ----------------------------------------------------------------


def test_interface_declaration():
    result = parse("interface Foo {\n    fun bar()\n}\n")
    foo = node(result, "type:Foo")
    assert foo.type == NodeType.INTERFACE
    bar = node(result, "method:Foo#bar()")
    assert "abstract" in bar.modifiers


def test_interface_inheritance():
    result = parse("interface Parent\ninterface Child : Parent\n")
    extends = pending(result, EdgeType.EXTENDS)
    child_extends = [p for p in extends if p.source_id == "type:Child"]
    assert child_extends[0].raw_name == "Parent"


def test_generic_interface():
    result = parse("interface Repository<T> {\n    fun find(): T\n}\n")
    repo = node(result, "type:Repository")
    assert repo.type == NodeType.INTERFACE
    assert repo.type_parameters == ("T",)


# -- objects ---------------------------------------------------------------------


def test_object_declaration():
    result = parse("object ServiceLocator {\n    val name: String = \"x\"\n}\n")
    obj = node(result, "type:ServiceLocator")
    assert obj.type == NodeType.OBJECT
    name = node(result, "property:ServiceLocator#name")
    assert name.type == NodeType.PROPERTY


def test_unnamed_companion_object_defaults_to_companion():
    result = parse(
        """
        class Foo {
            companion object {
                fun create(): Foo = Foo()
            }
        }
        """
    )
    companion = node(result, "type:Foo.Companion")
    assert companion.type == NodeType.OBJECT
    assert companion.parent_id == "type:Foo"
    create = node(result, "method:Foo.Companion#create()")
    assert create.parent_id == companion.id


def test_named_companion_object():
    result = parse(
        """
        class Foo {
            companion object Factory {
                fun create(): Foo = Foo()
            }
        }
        """
    )
    companion = node(result, "type:Foo.Factory")
    assert companion.name == "Factory"


# -- enums ------------------------------------------------------------------------


def test_enum_class_and_entries():
    result = parse(
        """
        enum class Status {
            ACTIVE, INACTIVE
        }
        """
    )
    status = node(result, "type:Status")
    assert status.type == NodeType.ENUM
    active = node(result, "field:Status#ACTIVE")
    assert active.type == NodeType.FIELD
    assert active.value_type == "Status"
    inactive = node(result, "field:Status#INACTIVE")
    assert inactive.type == NodeType.FIELD


def test_enum_class_with_constructor_and_members():
    result = parse(
        """
        enum class Status(val code: Int) {
            OK(200), ERROR(500);

            fun isOk(): Boolean = this == OK
        }
        """
    )
    status = node(result, "type:Status")
    assert status.type == NodeType.ENUM
    ctor = node(result, "method:Status#<init>(Int)")
    assert ctor.type == NodeType.CONSTRUCTOR
    code = node(result, "property:Status#code")
    assert code.value_type == "Int"
    ok_entry = node(result, "field:Status#OK")
    assert ok_entry.type == NodeType.FIELD
    is_ok = node(result, "method:Status#isOk()")
    assert is_ok.return_type == "Boolean"


# -- functions ------------------------------------------------------------------


def test_top_level_function():
    result = parse("fun greet(): String = \"hi\"\n")
    fn = node(result, "method:#greet()")
    assert fn.type == NodeType.METHOD
    assert fn.return_type == "String"
    assert fn.parent_id == "file:Test.kt"
    contains = [e for e in result.edges if e.type == EdgeType.CONTAINS and e.target_id == fn.id]
    assert len(contains) == 1


def test_member_function():
    result = parse("class Foo {\n    fun bar(): Int = 1\n}\n")
    bar = node(result, "method:Foo#bar()")
    declares = [e for e in result.edges if e.type == EdgeType.DECLARES and e.target_id == bar.id]
    assert len(declares) == 1


def test_extension_function_receiver_not_falsely_a_member():
    result = parse("fun String.toFoo(): Int = this.length\n")
    fn = node(result, "method:#String.toFoo()")
    assert fn.receiver_type == "String"
    assert fn.return_type == "Int"
    # Declared in the file, never linked as a member of String.
    assert fn.parent_id == "file:Test.kt"
    assert not any(e.target_id == fn.id and e.source_id.startswith("type:String") for e in result.edges)


def test_two_extension_functions_same_name_different_receiver_do_not_collide():
    result = parse(
        "fun String.describe(): String = this\nfun Int.describe(): String = this.toString()\n"
    )
    string_fn = node(result, "method:#String.describe()")
    int_fn = node(result, "method:#Int.describe()")
    assert string_fn.id != int_fn.id
    assert string_fn.receiver_type == "String"
    assert int_fn.receiver_type == "Int"


def test_generic_function():
    result = parse("fun <T> identity(value: T): T = value\n")
    fn = node(result, "method:#identity(T)")
    assert fn.type_parameters == ("T",)


def test_suspend_function():
    result = parse("interface Repo {\n    suspend fun fetch(): String\n}\n")
    fn = node(result, "method:Repo#fetch()")
    assert "suspend" in fn.modifiers


def test_function_with_default_parameter():
    result = parse("fun greet(name: String = \"world\") {}\n")
    fn = node(result, "method:#greet(String)")
    param = node(result, f"{fn.id}/param/0:name")
    assert param.value_type == "String"


def test_function_with_vararg_parameter():
    result = parse("fun tag(vararg names: String) {}\n")
    fn = node(result, "method:#tag(String)")
    param = node(result, f"{fn.id}/param/0:names")
    assert "vararg" in param.modifiers


def test_infix_function():
    result = parse("infix fun Int.times2(x: Int): Int = this * x\n")
    fn = node(result, "method:#Int.times2(Int)")
    assert "infix" in fn.modifiers
    assert fn.receiver_type == "Int"


def test_function_with_named_parameters_preserves_order():
    result = parse("fun make(a: Int, b: String, c: Boolean) {}\n")
    fn = node(result, "method:#make(Int, String, Boolean)")
    assert fn.param_types == ("Int", "String", "Boolean")


# -- properties ------------------------------------------------------------------


def test_top_level_val_and_var():
    result = parse("val a: Int = 1\nvar b: String = \"x\"\n")
    a = node(result, "property:#a")
    b = node(result, "property:#b")
    assert a.mutable is False
    assert b.mutable is True
    assert a.value_type == "Int"


def test_member_property_explicit_type():
    result = parse("class Foo {\n    val x: Int = 1\n}\n")
    x = node(result, "property:Foo#x")
    assert x.value_type == "Int"
    assert x.mutable is False


def test_member_property_inferred_type_is_none():
    result = parse("class Foo {\n    val x = 1\n}\n")
    x = node(result, "property:Foo#x")
    assert x.value_type is None


def test_extension_property_receiver():
    result = parse("val String.lastChar: Char\n    get() = this[length - 1]\n")
    prop = node(result, "property:#String.lastChar")
    assert prop.receiver_type == "String"
    assert prop.value_type == "Char"


def test_property_with_custom_getter_still_extracted():
    result = parse("class Foo {\n    val computed: Int\n        get() = 42\n}\n")
    computed = node(result, "property:Foo#computed")
    assert computed.value_type == "Int"


# -- constructors -----------------------------------------------------------------


def test_primary_constructor_no_val_var_is_plain_parameter():
    result = parse("class Foo(a: Int, b: String)\n")
    ctor = node(result, "method:Foo#<init>(Int, String)")
    a = node(result, f"{ctor.id}/param/0:a")
    b = node(result, f"{ctor.id}/param/1:b")
    assert a.type == NodeType.PARAMETER
    assert b.type == NodeType.PARAMETER
    assert "property:Foo#a" not in {n.id for n in result.nodes}


def test_secondary_constructor():
    result = parse(
        """
        class Foo(val x: Int) {
            constructor(s: String) : this(s.length)
        }
        """
    )
    primary = node(result, "method:Foo#<init>(Int)")
    secondary = node(result, "method:Foo#<init>(String)")
    assert primary.type == NodeType.CONSTRUCTOR
    assert secondary.type == NodeType.CONSTRUCTOR
    param = node(result, f"{secondary.id}/param/0:s")
    assert param.value_type == "String"


def test_primary_constructor_with_visibility_modifier():
    result = parse("class Foo private constructor(val x: Int)\n")
    ctor = node(result, "method:Foo#<init>(Int)")
    assert "private" in ctor.modifiers


# -- annotations --------------------------------------------------------------------


def test_annotation_on_class():
    result = parse("@Suppress(\"unused\")\nclass Foo\n")
    foo = node(result, "type:Foo")
    assert foo.annotations == ("Suppress",)


def test_annotation_on_function():
    result = parse("@Composable\nfun Screen() {}\n")
    fn = node(result, "method:#Screen()")
    assert fn.annotations == ("Composable",)


def test_annotation_on_property():
    result = parse("class Foo {\n    @JvmField\n    val x: Int = 1\n}\n")
    x = node(result, "property:Foo#x")
    assert x.annotations == ("JvmField",)


def test_annotation_on_parameter():
    result = parse("fun greet(@Suppress(\"unused\") name: String) {}\n")
    fn = node(result, "method:#greet(String)")
    param = node(result, f"{fn.id}/param/0:name")
    assert param.annotations == ("Suppress",)


def test_annotation_class_declaration():
    result = parse("annotation class MyAnno(val value: String)\n")
    anno = node(result, "type:MyAnno")
    assert anno.type == NodeType.ANNOTATION


# -- locations ----------------------------------------------------------------------


def test_source_locations_are_one_based_and_exact():
    source = "package com.example\n\nclass Foo {\n    fun bar() {\n    }\n}\n"
    result = parse(source)
    foo = node(result, "type:com.example.Foo")
    assert foo.start_line == 3
    assert foo.end_line == 6
    bar = node(result, "method:com.example.Foo#bar()")
    assert bar.start_line == 4
    assert bar.end_line == 5


def test_file_node_spans_whole_file():
    result = parse("class Foo\n")
    file_node = node(result, "file:Test.kt")
    assert file_node.start_line == 1
    assert file_node.end_line == 1


# -- malformed input ------------------------------------------------------------------


def test_malformed_file_records_errors_but_does_not_raise():
    content = (FIXTURES / "malformed" / "Broken.kt").read_bytes()
    result = KotlinParser().parse("Broken.kt", content)
    assert len(result.errors) > 0


def test_completely_garbage_content_does_not_raise():
    result = KotlinParser().parse("Garbage.kt", b"{{{ not kotlin at all ]][ ")
    file_node = node(result, "file:Garbage.kt")
    assert file_node.type == NodeType.FILE


def test_empty_file_does_not_raise():
    result = KotlinParser().parse("Empty.kt", b"")
    file_node = node(result, "file:Empty.kt")
    assert file_node.start_line == 1
    assert file_node.end_line == 1
    assert result.errors == ()


# -- grammar quirk regression -----------------------------------------------------


def test_grammar_quirk_annotation_before_annotation_class_is_not_extracted():
    """Documented, reproducible tree-sitter-kotlin limitation (see
    parser/kotlin.py's module docstring): a class-level annotation
    immediately preceding `annotation class` is misparsed as an expression
    rather than a declaration, with no error flagged by Tree-sitter itself.
    This test pins that (currently accepted) behavior so a grammar upgrade
    that changes it is noticed rather than silently masked.
    """
    result = parse('@Target(AnnotationTarget.FUNCTION)\nannotation class Marker\n')
    assert "type:Marker" not in {n.id for n in result.nodes}
    assert result.errors == ()  # Tree-sitter reports no syntax error either.


def test_annotation_class_without_preceding_annotation_is_unaffected():
    result = parse("annotation class Marker\n")
    marker = node(result, "type:Marker")
    assert marker.type == NodeType.ANNOTATION


# -- fixture-based end-to-end sanity check -----------------------------------------


def test_full_features_fixture_parses_without_errors():
    path = FIXTURES / "full_features" / "src/main/kotlin/com/example/app/ProfileViewModel.kt"
    result = KotlinParser().parse(str(path), path.read_bytes())
    assert result.errors == ()
    class_names = {n.qualified_name for n in find_nodes(result, NodeType.CLASS)}
    assert "com.example.app.ProfileViewModel" in class_names
    assert "com.example.app.ProfileViewModel.Session" in class_names
    assert "com.example.app.ProfileViewModel.Snapshot" in class_names
    object_names = {n.qualified_name for n in find_nodes(result, NodeType.OBJECT)}
    assert "com.example.app.ProfileViewModel.Companion" in object_names
