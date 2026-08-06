"""Tree-sitter tree-walking helpers shared by every language parser.

Nothing here is language-specific — these only rely on generic Tree-sitter
Node API (fields, named/anonymous children, error/missing flags).
"""

from __future__ import annotations

from collections.abc import Iterator

import tree_sitter


def line_count(content: bytes) -> int:
    """Number of lines a human/editor would report for ``content``.

    A trailing newline does not count as starting a new (phantom) line —
    unlike Tree-sitter's end_point, which advances to row N+1, column 0
    after a final "\\n".
    """
    if not content:
        return 1
    newlines = content.count(b"\n")
    return newlines if content.endswith(b"\n") else newlines + 1


def node_line_span(node: tree_sitter.Node, *, max_line: int) -> tuple[int, int]:
    """Convert a Tree-sitter node's 0-based start/end rows to a safe,
    1-based inclusive (start_line, end_line), clamped to [1, max_line]
    with start_line <= end_line guaranteed.

    Guards against a rare but real native bug observed in
    tree-sitter-kotlin on some large real-world files, where a node's
    reported position comes back corrupted (negative, or wildly out of
    range for the file) despite the parse otherwise succeeding cleanly
    (no ERROR/MISSING node, has_error is False). Left unclamped, that bad
    data would still satisfy every check *at parse time* but produce a
    graph.json that fails graph/storage.py's line-range validation on the
    very next load — i.e. `lcg build` would report success while quietly
    writing an unusable graph. Clamping here means the worst case is one
    node's recorded span being approximate instead of exact, not a broken
    graph.
    """
    start = max(1, min(node.start_point.row + 1, max_line))
    end = max(start, min(node.end_point.row + 1, max_line))
    return start, end


def iter_tree(node: tree_sitter.Node) -> Iterator[tree_sitter.Node]:
    """Yield every node in ``node``'s subtree, in document order.

    Uses a TreeCursor, and that choice is load-bearing rather than
    stylistic. Two different failure modes made the obvious
    implementations unusable on real files:

    * **Recursion** overflows. AST depth tracks source nesting times a
      constant (measured at ~3 AST levels per nested call on Kotlin), so a
      Compose-style file blows Python's 1000-frame limit while looking
      perfectly ordinary. The resulting RecursionError killed the parser
      subprocess and got reported to the user as a native crash.

    * **An explicit stack of Nodes** segfaults. Keeping a frontier of
      `tree_sitter.Node` objects alive at once reproducibly corrupted
      memory on deeply nested trees — 6/6 runs crashed on a 400-level
      Kotlin file, against 0/6 for the cursor doing the identical
      traversal. The trigger is how many Node objects are simultaneously
      live, not how many are created, so a cursor (which keeps exactly one
      alive at a time) avoids it while a stack cannot.

    Callers must therefore *consume* this lazily and retain only the few
    nodes they actually care about; collecting everything it yields into a
    list reintroduces exactly the condition that crashes.
    """
    cursor = node.walk()
    visited_children = False
    while True:
        if not visited_children:
            yield cursor.node
            if not cursor.goto_first_child():
                visited_children = True
        elif cursor.goto_next_sibling():
            visited_children = False
        elif not cursor.goto_parent():
            return


def find_error_nodes(node: tree_sitter.Node) -> list[tree_sitter.Node]:
    """Collect every ERROR / MISSING node in the tree, in document order.

    Only the error nodes are retained, which keeps the number of live Node
    objects proportional to the number of *problems* in a file rather than
    to its size — see ``iter_tree`` for why that distinction is what keeps
    this from segfaulting.

    The ``has_error`` fast path is a genuine short-circuit, not just a
    speed-up: Tree-sitter sets that flag on any subtree containing an ERROR
    or MISSING node, so when it is clear this provably returns [] without
    touching the tree at all. Since almost every file parses cleanly, the
    full traversal below is the rare path.
    """
    if not node.has_error:
        return []

    return [current for current in iter_tree(node) if current.type == "ERROR" or current.is_missing]


def first_child_of_type(node: tree_sitter.Node, type_name: str) -> tree_sitter.Node | None:
    """First direct child (named or anonymous) with the given type."""
    for child in node.children:
        if child.type == type_name:
            return child
    return None


def first_named_child_of_type(
    node: tree_sitter.Node, type_names: tuple[str, ...]
) -> tree_sitter.Node | None:
    """First direct *named* child whose type is one of ``type_names``."""
    for child in node.named_children:
        if child.type in type_names:
            return child
    return None


def child_index_by_field(node: tree_sitter.Node, field_name: str) -> int | None:
    """Index of the child assigned to ``field_name``, or None.

    Useful when a caller needs positional siblings around a field-designated
    child (e.g. Kotlin extension-function receiver-type detection), where
    ``child_by_field_name`` alone isn't enough because it discards position.
    """
    for i in range(node.child_count):
        if node.field_name_for_child(i) == field_name:
            return i
    return None


def child_index_by_type(node: tree_sitter.Node, type_name: str) -> int | None:
    """Index of the first child (named or anonymous) with the given type."""
    for i in range(node.child_count):
        if node.child(i).type == type_name:
            return i
    return None
