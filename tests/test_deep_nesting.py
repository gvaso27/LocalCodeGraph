"""Regression guards for deeply nested source files.

These exist because of a concrete, observed failure: `lcg build` on a real
Compose-heavy Android repository reported dozens of files as "parser
crashed" and left them out of the graph entirely. Three separate defects
produced that single symptom, and each is pinned down here:

1. `find_error_nodes` recursed once per AST level, so a file whose AST was
   deeper than Python's recursion limit raised RecursionError.
2. That RecursionError propagated out of the parse worker, killing the
   whole subprocess — which the parent could only interpret as a native
   crash, so the file was reported as a Tree-sitter bug and the batch paid
   a full process respawn.
3. tree-sitter 0.26.0 genuinely segfaults while walking a deeply nested
   tree, on any grammar. That one is not fixable from Python; the
   dependency is pinned below 0.26 (see pyproject.toml).

Because failure #3 takes the interpreter down rather than raising, these
tests do not fail politely if it returns — the test process dies. That is
intentional: a segfault is exactly the thing being guarded against, and a
loud crash here is far better than files silently missing from a graph.
"""

from __future__ import annotations

import pytest

from local_code_graph.parser._ts_utils import find_error_nodes, iter_tree
from local_code_graph.parser.java import JavaParser
from local_code_graph.parser.kotlin import KotlinParser

# Deep enough that the AST comfortably exceeds Python's default 1000-frame
# recursion limit: Kotlin nesting measured ~3 AST levels per source level,
# so 400 levels reaches ~1200. The recursive implementation this replaced
# raised RecursionError at exactly this shape.
_DEEP = 400


def compose_style_kotlin(levels: int, *, broken: bool = False) -> bytes:
    """Nested builder-lambda calls — the shape Jetpack Compose UI code takes."""
    lines = ["package com.example.ui", "", "fun Screen() {"]
    for i in range(levels):
        lines.append(f"Column(modifier = Modifier.padding({i})) {{")
    lines.append('Text(text = "hello")')
    if broken:
        lines.append("@@@ !!!")
    lines += ["}"] * levels + ["}"]
    return ("\n".join(lines) + "\n").encode()


def nested_java(levels: int, *, broken: bool = False) -> bytes:
    body = "int x = 0;" + (" @@@ " if broken else "")
    source = "class Deep {\n  void m() {\n"
    source += "".join("if (c) {\n" for _ in range(levels))
    source += body + "\n"
    source += "}\n" * levels
    source += "  }\n}\n"
    return source.encode()


# -- the parsers themselves --------------------------------------------------


@pytest.mark.parametrize("levels", [100, _DEEP])
def test_deeply_nested_kotlin_parses(levels):
    result = KotlinParser().parse("Screen.kt", compose_style_kotlin(levels))
    assert any(n.name == "Screen" for n in result.nodes)
    assert result.errors == ()


@pytest.mark.parametrize("levels", [100, _DEEP])
def test_deeply_nested_java_parses(levels):
    result = JavaParser().parse("Deep.java", nested_java(levels))
    assert any(n.name == "Deep" for n in result.nodes)
    assert result.errors == ()


def test_deeply_nested_kotlin_with_syntax_error_still_reports_it():
    """The hardest case: deep *and* broken, so the error walk actually runs
    over the whole tree instead of short-circuiting on has_error."""
    result = KotlinParser().parse("Broken.kt", compose_style_kotlin(_DEEP, broken=True))
    assert any(e.message == "syntax error" for e in result.errors)


def test_deeply_nested_java_with_syntax_error_still_reports_it():
    result = JavaParser().parse("Broken.java", nested_java(_DEEP, broken=True))
    assert any(e.message == "syntax error" for e in result.errors)


def test_parsing_many_deep_files_in_one_process_stays_stable():
    """A single parse succeeding is not enough — `lcg build` parses a whole
    repository in one process, and the corruption this guards against
    accumulated across parses rather than showing up on the first one."""
    parser = KotlinParser()
    source = compose_style_kotlin(_DEEP)
    for index in range(15):
        result = parser.parse(f"Screen{index}.kt", source)
        assert result.errors == ()


# -- the shared traversal helpers -------------------------------------------


def test_find_error_nodes_survives_ast_deeper_than_pythons_recursion_limit():
    import tree_sitter
    import tree_sitter_kotlin

    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_kotlin.language()))
    tree = parser.parse(compose_style_kotlin(_DEEP, broken=True))
    assert tree.root_node.has_error is True
    # The recursive implementation raised RecursionError here rather than
    # returning; that it returns at all is the assertion.
    assert find_error_nodes(tree.root_node)


def test_find_error_nodes_short_circuits_on_a_clean_tree():
    import tree_sitter
    import tree_sitter_kotlin

    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_kotlin.language()))
    tree = parser.parse(b"package a\n\nclass Foo {\n    fun bar() {}\n}\n")
    assert tree.root_node.has_error is False
    assert find_error_nodes(tree.root_node) == []


def test_find_error_nodes_returns_errors_in_source_order():
    import tree_sitter
    import tree_sitter_java

    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_java.language()))
    tree = parser.parse(b"class A { @@@ }\nclass B { ### }\nclass C { $$$ }\n")
    rows = [n.start_point.row for n in find_error_nodes(tree.root_node)]
    assert rows == sorted(rows)


def test_iter_tree_visits_every_node_in_document_order():
    import tree_sitter
    import tree_sitter_java

    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_java.language()))
    tree = parser.parse(b"class A { int x; int y; }\n")
    starts = [n.start_byte for n in iter_tree(tree.root_node)]
    assert starts == sorted(starts)
    # Anonymous nodes count too — this is a full traversal, not a named-only one.
    assert any(n.type == "{" for n in iter_tree(tree.root_node))


def test_iter_tree_handles_a_deep_tree():
    import tree_sitter
    import tree_sitter_kotlin

    parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_kotlin.language()))
    tree = parser.parse(compose_style_kotlin(_DEEP))
    assert sum(1 for _ in iter_tree(tree.root_node)) > _DEEP
