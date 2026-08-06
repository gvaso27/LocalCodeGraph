"""Process-isolated parsing: run each file's parse where a native crash
can't take the whole build down with it.

Tree-sitter grammars are compiled C extensions. A small fraction of real
files can trigger a genuine bug in the grammar/binding — not malformed
input, just a rare native-code edge case — that segfaults the process
outright. A segfault is not a Python exception: nothing in Python, no
``try``/``except``, can catch it, because the operating system kills the
process before the interpreter gets a chance to run any more code. The
only way to survive that is to never let the parse call happen in the
process you care about keeping alive.

``parse_source_files_isolated`` runs a batch of files in one subprocess
(one process-spawn for a whole clean build, which is fast). If that
subprocess dies partway through, we know *exactly* which file it was on:
the worker appends one record per file in order, so the first file with
no record is the one that crashed (or hung). That file gets a ParseError
recorded against its exact path, and parsing resumes with a fresh
subprocess starting at the very next file — so the total number of
spawns is 1 + (number of files that actually crash), not something that
blows up with batch size.

Only a *native* crash costs a respawn. An ordinary Python exception
raised while parsing one file is caught inside the worker and recorded as
that file's result, so the batch continues in the same process and the
user sees the real exception instead of a misleading "native crash".
Conflating the two was a real bug: files that merely hit a RecursionError
were reported as unfixable Tree-sitter grammar bugs, and each one forced
a fresh subprocess.

Why a file, not a multiprocessing.Queue
----------------------------------------
An earlier version used a Queue and deadlocked in practice. A Queue is
backed by a pipe carrying length-prefixed pickled messages. When a child
segfaults *while writing* a message, it leaves a partial message in that
pipe; the parent's next ``get()`` reads the length header, then blocks
forever waiting for bytes that will never arrive. ``get(timeout=...)``
does not save you — that timeout bounds acquiring the queue's read lock,
not the subsequent read of an already-started message. Since children
crashing mid-write is the *expected* case here, the Queue was structurally
the wrong transport.

Instead the worker appends length-prefixed pickle records to a plain file
and flushes after each one. Bytes already written are in the OS page cache
and survive the writing process dying, so the parent can read every
complete record afterwards; a truncated trailing record (the crash) is
detected by its short read and discarded. The parent only reads the file
*after* the child has exited, so there is nothing to block on.

The pickled payloads are our own ParseResult dataclasses, produced by our
own parser, written to a file in a private temporary directory this
process just created — the same trust model multiprocessing itself uses,
not a channel for untrusted input. Repository content is only ever passed
*to* Tree-sitter as bytes; it is never executed, and nothing derived from
it crosses back as anything but plain dataclass fields.
"""

from __future__ import annotations

import multiprocessing
import pickle
import signal
import tempfile
import time
from pathlib import Path

from local_code_graph.graph.model import ParseError
from local_code_graph.parser.base import FileContext, ParseResult
from local_code_graph.scanner import Language

_DEFAULT_TIMEOUT_SECONDS = 60.0
_LENGTH_PREFIX_BYTES = 8
_POLL_INTERVAL_SECONDS = 0.05

SKIPPED_FILE_MARKER = "file skipped by parser"
"""Stable prefix on every ParseError meaning "this file contributed nothing
to the graph".

The CLI has to tell these apart from ordinary in-file syntax errors (which
still produce a perfectly good partial graph) so it can warn that results
are incomplete. Matching on a named constant rather than on prose keeps
that detection from silently breaking the next time these messages are
reworded — which has already happened once.
"""


def is_skipped_file_error(error: ParseError) -> bool:
    """True if ``error`` means its file is entirely absent from the graph."""
    return error.message.startswith(SKIPPED_FILE_MARKER)


def _worker(items: list[tuple[Language, str, bytes]], result_path: str) -> None:
    # Parsers are built fresh inside this subprocess, never passed in from
    # the parent: a tree_sitter.Parser wraps native C state and isn't
    # picklable, so it can't cross the process boundary as an argument.
    from local_code_graph.graph.builder import default_parsers

    parsers = default_parsers()
    with open(result_path, "wb") as f:
        for language, relative_path, content in items:
            parser = parsers.get(language)
            # One record per item, in order, *including* items with no
            # registered parser (recorded as None) — the parent locates a
            # crashing file by record position, so that alignment has to
            # hold for every item, not just the parsed ones.
            if parser is None:
                payload = pickle.dumps((relative_path, None))
            else:
                try:
                    # If this call segfaults, the process dies right here.
                    # The records already written and flushed above remain
                    # readable by the parent; this item's record simply
                    # never appears, which is exactly how the parent
                    # identifies it.
                    result = parser.parse(relative_path, content)
                except Exception as exc:
                    # A *Python* exception is not a native crash, and must
                    # not be treated like one. Letting it propagate would
                    # kill this worker, costing a subprocess respawn per
                    # affected file and — worse — reporting the file to the
                    # user as a native Tree-sitter crash, which is both
                    # wrong and unactionable. Catching it here keeps the
                    # batch running and preserves the real diagnosis.
                    #
                    # Deliberately broad: this is a crash barrier around
                    # third-party native bindings walking untrusted input,
                    # where the useful contract is "no single file can
                    # abort the build", not "handle these five exception
                    # types". RecursionError and MemoryError are both
                    # Exception subclasses and both realistically reachable
                    # on pathological files, so both are caught here.
                    result = _error_result(relative_path, exc)
                payload = pickle.dumps((relative_path, result))
            f.write(len(payload).to_bytes(_LENGTH_PREFIX_BYTES, "big"))
            f.write(payload)
            f.flush()


def _read_records(result_path: Path) -> list[tuple[str, ParseResult | None]]:
    """Read every *complete* length-prefixed record, stopping at the first
    truncated one (which is the write that a crash interrupted)."""
    records: list[tuple[str, ParseResult | None]] = []
    try:
        data = result_path.read_bytes()
    except OSError:
        return records

    offset = 0
    total = len(data)
    while offset + _LENGTH_PREFIX_BYTES <= total:
        length = int.from_bytes(data[offset : offset + _LENGTH_PREFIX_BYTES], "big")
        start = offset + _LENGTH_PREFIX_BYTES
        end = start + length
        if end > total:
            break  # truncated trailing record: the interrupted write
        try:
            records.append(pickle.loads(data[start:end]))
        except Exception:
            break  # corrupt record; treat like a truncated one
        offset = end
    return records


def _shut_down(process: multiprocessing.Process) -> None:
    """Make sure ``process`` is actually gone before returning — never
    blocks forever.

    SIGTERM (``terminate()``) can be silently ineffective against a
    process stuck inside a native call (observed in practice: a hung
    Tree-sitter parse left the worker unresponsive to SIGTERM, and an
    earlier version of this function followed ``terminate()`` with an
    untimed ``join()``, which then blocked forever waiting for a process
    that was never going to exit). SIGKILL (``kill()``) cannot be caught
    or ignored, so it's the actual guarantee here; every join is bounded,
    so this function always returns.
    """
    process.join(timeout=5)
    if not process.is_alive():
        return
    process.terminate()
    process.join(timeout=5)
    if not process.is_alive():
        return
    process.kill()
    process.join(timeout=5)


def _run_batch(
    items: list[tuple[Language, str, bytes]],
    *,
    timeout: float,
) -> tuple[list[tuple[str, ParseResult | None]], int | None]:
    """Run ``items`` in one subprocess and return ``(records, exitcode)``:
    the records it managed to write, in order, before finishing / crashing
    / hanging, plus the worker's exit status for diagnosing *why* it
    stopped.

    ``timeout`` bounds time *without progress*, not total runtime: as long
    as the worker keeps appending records, a large batch keeps going. A
    worker that stops producing for ``timeout`` seconds while still alive
    is treated as hung and killed.
    """
    if not items:
        return [], None

    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="lcg-parse-") as tmpdir:
        result_path = Path(tmpdir) / "results.bin"
        result_path.touch()

        process = ctx.Process(target=_worker, args=(items, str(result_path)))
        process.start()

        last_size = 0
        last_progress = time.monotonic()
        while process.is_alive():
            time.sleep(_POLL_INTERVAL_SECONDS)
            try:
                size = result_path.stat().st_size
            except OSError:
                size = last_size
            if size != last_size:
                last_size = size
                last_progress = time.monotonic()
            elif time.monotonic() - last_progress > timeout:
                break  # alive but produced nothing for the whole timeout: hung

        _shut_down(process)
        # Read only after the child is gone, so there's no partially
        # written record still in flight.
        return _read_records(result_path), process.exitcode


def _skipped_result(relative_path: str, message: str) -> ParseResult:
    """A stand-in ParseResult for a file that produced no usable output.

    Always empty nodes/edges — never partial or possibly-corrupted data —
    with the reason recorded as a ParseError so the CLI can tell the user
    exactly which files are missing from the graph and why.
    """
    return ParseResult(
        nodes=(),
        edges=(),
        pending_refs=(),
        errors=(ParseError(file=relative_path, message=message),),
        context=FileContext(package=None),
    )


def _error_result(relative_path: str, exc: BaseException) -> ParseResult:
    """Result for a file whose parse raised a normal Python exception.

    The exception type and message are preserved: unlike a native crash,
    this is a bug we can actually locate and fix, so throwing away the
    diagnosis would be throwing away the only useful part.
    """
    detail = f"{type(exc).__name__}: {exc}".strip()
    return _skipped_result(
        relative_path,
        f"{SKIPPED_FILE_MARKER}: parser raised {detail} — file skipped, "
        "rest of the repository parsed normally",
    )


def _crash_result(relative_path: str, exitcode: int | None) -> ParseResult:
    """Result for a file that killed the worker process outright.

    ``exitcode`` distinguishes two genuinely different failures that both
    land here, and which call for different responses:

    * SIGKILL (-9) is *this module's own* escalation after the worker made
      no progress for the timeout — so the file hung the parser rather
      than crashing it, and a longer timeout may be all it needs.
    * Any other signal is the parser faulting on its own, SIGSEGV (-11)
      being the native memory fault seen in practice. No timeout will help.

    Reporting both as "crashed" would send a user chasing a grammar bug
    when the real answer was `timeout=`.
    """
    if exitcode == -signal.SIGKILL:
        return _skipped_result(
            relative_path,
            f"{SKIPPED_FILE_MARKER}: parser made no progress on this file and was "
            "stopped (it hung, rather than crashing) — file skipped, rest of the "
            "repository parsed normally",
        )

    if exitcode is not None and exitcode < 0:
        cause = f"killed by signal {-exitcode}"
    elif exitcode is not None:
        cause = f"exited with status {exitcode}"
    else:
        cause = "died for an unknown reason"
    return _skipped_result(
        relative_path,
        f"{SKIPPED_FILE_MARKER}: parser crashed on this file ({cause}) — "
        "likely a native Tree-sitter grammar bug, not a problem with your "
        "code. File skipped, rest of the repository parsed normally",
    )


def parse_source_files_isolated(
    files: list[tuple[Language, str, bytes]],
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, ParseResult]:
    """Parse every (language, relative_path, content) tuple in ``files``,
    isolating any that crash or hang the parser process.

    Returns {relative_path: ParseResult} for every file that produced one.
    A file that crashed the parser gets a synthetic ParseResult whose
    ``errors`` explains why, with empty nodes/edges (never partial or
    corrupted data). Files with no registered parser are omitted.

    Note that only a *native* crash costs a respawn. A file whose parse
    raises an ordinary Python exception is handled inside the worker (see
    ``_worker``) and comes back as a normal record with its real error
    message, so the batch keeps running.
    """
    completed: dict[str, ParseResult] = {}
    remaining = files
    while remaining:
        records, exitcode = _run_batch(remaining, timeout=timeout)
        for relative_path, result in records:
            if result is not None:
                completed[relative_path] = result

        if len(records) == len(remaining):
            break  # the batch ran to completion

        # remaining[len(records)] is exactly the file that crashed or hung:
        # the worker writes one record per item in order, so the first item
        # with no record is the one it died on.
        crashed_path = remaining[len(records)][1]
        completed[crashed_path] = _crash_result(crashed_path, exitcode)
        remaining = remaining[len(records) + 1 :]

    return completed
