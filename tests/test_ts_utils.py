from __future__ import annotations

from types import SimpleNamespace

from local_code_graph.parser._ts_utils import node_line_span


def _fake_node(start_row: int, end_row: int):
    """A minimal stand-in for tree_sitter.Node exposing only what
    node_line_span reads (start_point.row / end_point.row)."""
    return SimpleNamespace(
        start_point=SimpleNamespace(row=start_row),
        end_point=SimpleNamespace(row=end_row),
    )


def test_normal_span_converts_zero_based_to_one_based():
    node = _fake_node(4, 9)
    assert node_line_span(node, max_line=100) == (5, 10)


def test_single_line_node():
    node = _fake_node(2, 2)
    assert node_line_span(node, max_line=100) == (3, 3)


def test_clamps_negative_start_row():
    # A regression guard for a real, observed tree-sitter-kotlin bug: on
    # some large real-world files, a node's reported start position comes
    # back corrupted (e.g. row -1) despite the parse otherwise succeeding
    # cleanly (has_error is False). Unclamped, this produced a graph.json
    # with an invalid (< 1) line number that then failed to even load —
    # see graph/storage.py's line-range validation. Clamping at the
    # source means `lcg build` never produces that broken output.
    node = _fake_node(-1, 5)
    start, end = node_line_span(node, max_line=100)
    assert start == 1
    assert end == 6
    assert start <= end


def test_clamps_row_beyond_file_length():
    node = _fake_node(0, 99999)
    start, end = node_line_span(node, max_line=50)
    assert start == 1
    assert end == 50


def test_clamps_start_after_end_so_start_never_exceeds_end():
    # A corrupted node where the reported start is *after* the end.
    node = _fake_node(20, 3)
    start, end = node_line_span(node, max_line=100)
    assert start <= end


def test_result_always_within_one_to_max_line():
    for start_row, end_row in [(-5, -1), (0, 0), (10**9, 10**9), (-1, 10**9)]:
        node = _fake_node(start_row, end_row)
        start, end = node_line_span(node, max_line=200)
        assert 1 <= start <= 200
        assert 1 <= end <= 200
        assert start <= end
