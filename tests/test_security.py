"""Phase 7: security hardening and offline-enforcement regression tests.

These don't test parsing/query correctness (covered elsewhere) — they lock
in the project's core privacy/security invariants so a future change can't
silently violate them:

  * the whole build -> save -> load -> query pipeline works with all
    socket creation blocked (the practical, portable equivalent of running
    it with no network available — this sandbox doesn't have permission to
    create a network namespace via `unshare --net` to test that literally,
    so blocking `socket.socket` at the Python level is the next best proof
    that nothing on the hot path even attempts a connection)
  * no module under src/ imports a networking library
  * malicious-looking file content/names are never executed or shelled out to
  * a maliciously crafted graph.json is inert data, even when it contains
    path-traversal-shaped strings
"""

from __future__ import annotations

import ast
import socket
import subprocess
from pathlib import Path

import pytest

from local_code_graph import cli
from local_code_graph.graph import storage
from local_code_graph.graph.builder import build_graph
from local_code_graph.query.engine import QueryEngine

SRC_DIR = Path(__file__).parent.parent / "src" / "local_code_graph"
JAVA_FIXTURES = Path(__file__).parent / "fixtures" / "java"

_FORBIDDEN_MODULES = {
    "socket",
    "urllib",
    "urllib2",
    "http",
    "http.client",
    "requests",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "telnetlib",
    "poplib",
    "imaplib",
    "xmlrpc",
    "asyncio",  # not forbidden for correctness, but this project has no use for it — its
    # presence would suggest something (e.g. a background network task) snuck in
}
# asyncio is over-cautious for a purely synchronous local tool; if it's ever
# genuinely needed, remove it here rather than working around this test.


def _block_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("attempted to create a network socket")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)


# -- offline enforcement: the real pipeline works with sockets blocked ------------------


def test_build_save_load_query_works_with_sockets_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _block_sockets(monkeypatch)

    graph = build_graph(JAVA_FIXTURES / "full_features")
    storage.save_graph(graph, tmp_path)
    loaded = storage.load_graph(tmp_path)
    qe = QueryEngine(loaded)

    assert len(loaded.nodes) == len(graph.nodes)
    result = qe.find("SoundController")
    assert not result.is_empty
    assert qe.overview().file_count == 5


def test_cli_build_and_query_works_with_sockets_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import shutil

    repo = tmp_path / "repo"
    shutil.copytree(JAVA_FIXTURES / "full_features", repo)

    _block_sockets(monkeypatch)

    assert cli.main(["build", str(repo)]) == cli.EXIT_OK
    assert cli.main(["overview", str(repo)]) == cli.EXIT_OK
    assert cli.main(["find", str(repo), "com.example.app.SoundController"]) == cli.EXIT_OK
    assert cli.main(["show", str(repo), "com.example.app.SoundController"]) == cli.EXIT_OK
    assert (
        cli.main(
            ["path", str(repo), "com.example.app.SoundController", "com.example.app.BaseController"]
        )
        == cli.EXIT_OK
    )


# -- static import audit -----------------------------------------------------------------


def _imported_top_level_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


def test_no_networking_imports_anywhere_in_src():
    offenders: dict[str, set[str]] = {}
    for path in SRC_DIR.rglob("*.py"):
        modules = _imported_top_level_modules(path.read_text(encoding="utf-8"))
        hit = modules & _FORBIDDEN_MODULES
        if hit:
            offenders[str(path.relative_to(SRC_DIR))] = hit
    assert offenders == {}


def test_no_subprocess_or_dynamic_execution_in_src():
    """The tool must never shell out or eval/exec anything derived from
    repository content — see graph/storage.py's and cli.py's docstrings."""
    forbidden_names = {"subprocess", "eval", "exec", "compile", "__import__", "importlib"}
    offenders: dict[str, set[str]] = {}
    for path in SRC_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                used.add(node.id)
            if isinstance(node, ast.Import):
                used.update(a.name.split(".")[0] for a in node.names if a.name.split(".")[0] in forbidden_names)
        if used:
            offenders[str(path.relative_to(SRC_DIR))] = used
    assert offenders == {}


# -- malicious repository content is never executed -----------------------------------------


def test_repository_with_shell_metacharacters_in_source_is_parsed_inertly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    # Class/annotation names that would be dangerous if ever interpolated
    # into a shell command or eval()'d — they must end up as inert node
    # metadata, nothing more.
    (repo / "Evil.java").write_text(
        'package com.example;\n\n'
        '@SuppressWarnings("$(rm -rf /)")\n'
        'public class Evil {\n'
        '    public void run$IFS$whoami() {}\n'
        '}\n'
    )

    def boom_run(*args, **kwargs):
        raise AssertionError("subprocess.run was called while parsing repository content")

    monkeypatch.setattr(subprocess, "run", boom_run)

    graph = build_graph(repo)
    evil = graph.nodes.get("type:com.example.Evil")
    assert evil is not None
    assert evil.annotations == ("SuppressWarnings",)


def test_repository_with_executable_looking_files_are_skipped_not_run(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "build.sh").write_text("#!/bin/sh\nrm -rf /\n")
    (repo / "build.sh").chmod(0o755)
    (repo / "Makefile").write_text("all:\n\trm -rf /\n")
    (repo / "Real.java").write_text("public class Real {}\n")

    graph = build_graph(repo)
    # Only the real Java source was parsed; the executable-looking files
    # were never even opened for parsing (unsupported extension/no
    # extension), let alone run.
    file_names = {n.name for n in graph.nodes.values() if n.type.value == "FILE"}
    assert file_names == {"Real.java"}


# -- a crafted graph.json is inert data, even with path-traversal-shaped strings ------------


def test_path_traversal_shaped_node_fields_are_never_used_for_file_access(tmp_path: Path):
    node = {
        "id": "type:X",
        "type": "CLASS",
        "name": "X",
        "file": "../../../../../../etc/passwd",
        "start_line": 1,
        "end_line": 1,
        "qualified_name": "X",
        "parent_id": None,
        "modifiers": [],
        "annotations": [],
        "type_parameters": [],
        "param_types": None,
        "return_type": None,
        "value_type": None,
        "receiver_type": None,
        "mutable": None,
    }
    data = {"schema_version": 1, "root": "/x", "nodes": [node], "edges": []}
    path = storage.graph_json_path(tmp_path)
    path.parent.mkdir(parents=True)
    import json

    path.write_text(json.dumps(data), encoding="utf-8")

    graph = storage.load_graph(tmp_path)
    qe = QueryEngine(graph)
    x = qe.find("type:X").matches[0]
    # The traversal-shaped string is preserved as inert text — never opened.
    assert x.file == "../../../../../../etc/passwd"
    summary = qe.summarize(x)
    assert summary.node.file == "../../../../../../etc/passwd"
