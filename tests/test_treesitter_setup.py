"""Smoke tests confirming the Tree-sitter grammars are installed and usable.

These do not test LocalCodeGraph's own parsing logic (that lives in
test_java_parser.py / test_kotlin_parser.py, added in later phases) — they
just verify the local, offline Tree-sitter setup itself works.
"""

from __future__ import annotations

import tree_sitter
import tree_sitter_java
import tree_sitter_kotlin


def test_java_grammar_loads_and_parses():
    language = tree_sitter.Language(tree_sitter_java.language())
    parser = tree_sitter.Parser(language)

    tree = parser.parse(b"class Foo { void bar() {} }")

    assert tree.root_node.type == "program"
    assert tree.root_node.child(0).type == "class_declaration"
    assert not tree.root_node.has_error


def test_kotlin_grammar_loads_and_parses():
    language = tree_sitter.Language(tree_sitter_kotlin.language())
    parser = tree_sitter.Parser(language)

    # The Kotlin grammar is newline-sensitive (like the language itself), so
    # exercise it with normally-formatted, multi-line source rather than a
    # single compressed line.
    source = b"""
        package com.example

        class Foo {
            fun bar() {
            }
        }
        """
    tree = parser.parse(source)

    assert not tree.root_node.has_error
