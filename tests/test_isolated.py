from __future__ import annotations

import multiprocessing
import signal
import time

from local_code_graph.parser.isolated import (
    _read_records,
    _shut_down,
    _worker,
    is_skipped_file_error,
    parse_source_files_isolated,
)
from local_code_graph.scanner import Language

JAVA_SOURCE = b"package com.example;\n\npublic class Foo {\n    public void bar() {}\n}\n"


def test_parses_normal_files():
    files = [(Language.JAVA, "Foo.java", JAVA_SOURCE)]
    results = parse_source_files_isolated(files)
    assert "Foo.java" in results
    result = results["Foo.java"]
    assert result.errors == ()
    assert any(n.name == "Foo" for n in result.nodes)


def test_parses_multiple_files_in_one_subprocess():
    files = [
        (Language.JAVA, "A.java", b"public class A {}\n"),
        (Language.JAVA, "B.java", b"public class B {}\n"),
        (Language.JAVA, "C.java", b"public class C {}\n"),
    ]
    results = parse_source_files_isolated(files)
    assert set(results.keys()) == {"A.java", "B.java", "C.java"}
    for path, result in results.items():
        assert result.errors == ()


def test_empty_file_list_returns_empty_dict():
    assert parse_source_files_isolated([]) == {}


# -- the actual regression: shutdown must never hang, even against a process
# that ignores SIGTERM (this is what a genuinely stuck native call looked
# like in practice — see the module docstring) --------------------------------


def _ignore_sigterm_and_sleep_forever() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(3600)


def _do_nothing() -> None:
    pass


def test_shut_down_kills_a_process_that_ignores_sigterm_within_bounded_time():
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_ignore_sigterm_and_sleep_forever)
    process.start()
    # Give it a moment to actually install the SIGTERM handler before we
    # try to shut it down, so this test can't pass by accident (racing a
    # default-SIGTERM-kills-it path before the handler is installed).
    time.sleep(0.5)

    start = time.monotonic()
    _shut_down(process)
    elapsed = time.monotonic() - start

    assert not process.is_alive()
    # terminate() (ignored) + kill() escalation, each bounded at 5s: this
    # must finish well under the old failure mode (an unbounded hang).
    assert elapsed < 20


def test_shut_down_returns_immediately_for_already_finished_process():
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_do_nothing)
    process.start()
    process.join()

    start = time.monotonic()
    _shut_down(process)
    elapsed = time.monotonic() - start

    assert elapsed < 6


# -- a Python exception is not a native crash ---------------------------------
#
# Before this was handled, any exception raised while parsing one file
# escaped the worker and killed it. Three things went wrong at once: the
# rest of the batch had to be re-run in a fresh process, the file was
# reported to the user as a native Tree-sitter crash, and the real
# exception — the only actionable part — was discarded. Files hitting a
# plain RecursionError were being reported as unfixable grammar bugs.
#
# These drive `_worker` in-process rather than through
# parse_source_files_isolated: the worker is deliberately spawned, so a
# parser substituted in this process would never reach it. Calling the
# worker directly is what lets the crash barrier itself be tested.


class _ExplodingParser:
    """Parser that raises on one specific file and works normally otherwise."""

    def __init__(self, exploding_path: str, exc: BaseException) -> None:
        self.exploding_path = exploding_path
        self.exc = exc

    def parse(self, relative_path: str, content: bytes):
        if relative_path == self.exploding_path:
            raise self.exc
        from local_code_graph.parser.java import JavaParser

        return JavaParser().parse(relative_path, content)


def _run_worker_inline(tmp_path, items, parsers):
    import local_code_graph.graph.builder as builder_module

    original = builder_module.default_parsers
    builder_module.default_parsers = lambda: parsers
    try:
        result_path = tmp_path / "results.bin"
        _worker(items, str(result_path))
        return dict(_read_records(result_path))
    finally:
        builder_module.default_parsers = original


def test_python_exception_is_recorded_per_file_and_batch_continues(tmp_path):
    parsers = {Language.JAVA: _ExplodingParser("Bad.java", ValueError("boom"))}
    items = [
        (Language.JAVA, "A.java", b"public class A {}\n"),
        (Language.JAVA, "Bad.java", b"public class Bad {}\n"),
        (Language.JAVA, "B.java", b"public class B {}\n"),
    ]
    results = _run_worker_inline(tmp_path, items, parsers)

    # Every file got a record: the worker survived the exception, so no
    # respawn was needed and nothing after the bad file was lost.
    assert set(results) == {"A.java", "Bad.java", "B.java"}
    assert results["A.java"].errors == ()
    assert results["B.java"].errors == ()

    bad = results["Bad.java"]
    assert bad.nodes == ()
    assert len(bad.errors) == 1
    message = bad.errors[0].message
    assert is_skipped_file_error(bad.errors[0])
    assert "ValueError" in message
    assert "boom" in message
    assert "native" not in message


def test_recursion_error_is_reported_as_itself_not_as_a_native_crash(tmp_path):
    parsers = {Language.JAVA: _ExplodingParser("Deep.java", RecursionError("too deep"))}
    items = [(Language.JAVA, "Deep.java", b"class Deep {}\n")]
    results = _run_worker_inline(tmp_path, items, parsers)

    error = results["Deep.java"].errors[0]
    assert "RecursionError" in error.message
    assert is_skipped_file_error(error)
    assert "native" not in error.message


def test_worker_keeps_one_record_per_item_including_unparseable_languages(tmp_path):
    """Record-position alignment is how the parent identifies a crashing
    file, so every item must produce exactly one record — including ones
    with no registered parser and ones that raised."""
    parsers = {Language.JAVA: _ExplodingParser("Bad.java", ValueError("boom"))}
    items = [
        (Language.JAVA, "A.java", b"class A {}\n"),
        (Language.KOTLIN, "Unregistered.kt", b"class K\n"),
        (Language.JAVA, "Bad.java", b"class Bad {}\n"),
    ]
    import local_code_graph.graph.builder as builder_module

    original = builder_module.default_parsers
    builder_module.default_parsers = lambda: parsers
    try:
        result_path = tmp_path / "results.bin"
        _worker(items, str(result_path))
        records = _read_records(result_path)
    finally:
        builder_module.default_parsers = original

    assert [path for path, _ in records] == ["A.java", "Unregistered.kt", "Bad.java"]
    assert records[1][1] is None  # no parser registered -> recorded as None


def test_skipped_file_marker_distinguishes_skips_from_ordinary_syntax_errors():
    from local_code_graph.graph.model import ParseError

    assert not is_skipped_file_error(ParseError(file="A.java", message="syntax error"))


def test_a_hang_is_reported_differently_from_a_crash():
    """SIGKILL is this module's own escalation after a timeout, so it means
    the file hung — a different problem, with a different fix (a longer
    timeout), than the parser faulting on its own."""
    import signal as signal_module

    from local_code_graph.parser.isolated import _crash_result

    hung = _crash_result("Hung.kt", -signal_module.SIGKILL).errors[0]
    crashed = _crash_result("Crashed.kt", -signal_module.SIGSEGV).errors[0]

    assert is_skipped_file_error(hung) and is_skipped_file_error(crashed)
    assert "hung" in hung.message
    assert "crashed" not in hung.message
    assert "crashed" in crashed.message
    assert "11" in crashed.message  # the signal, so a bug report can name it
