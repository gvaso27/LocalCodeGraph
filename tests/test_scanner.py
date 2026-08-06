from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from local_code_graph.scanner import Language, scan_repository

FIXTURES = Path(__file__).parent / "fixtures"


def test_scans_java_project_and_ignores_build_and_git():
    result = scan_repository(FIXTURES / "java" / "simple_project")

    relative_paths = [f.relative_path for f in result.files]
    assert "src/main/java/com/example/app/App.java" in relative_paths
    assert all(f.language == Language.JAVA for f in result.files)

    # Ignored directories must never contribute files.
    assert not any(p.startswith(".git/") for p in relative_paths)
    assert not any(p.startswith("build/") for p in relative_paths)


def test_scans_kotlin_project_and_ignores_build():
    result = scan_repository(FIXTURES / "kotlin" / "simple_project")

    relative_paths = [f.relative_path for f in result.files]
    assert "src/main/kotlin/com/example/app/App.kt" in relative_paths
    assert all(f.language == Language.KOTLIN for f in result.files)
    assert not any(p.startswith("build/") for p in relative_paths)


def test_reports_unsupported_file_as_skipped():
    result = scan_repository(FIXTURES / "java" / "simple_project")

    skipped_paths = {s.path: s.reason for s in result.skipped}
    assert "README.md" in skipped_paths
    assert skipped_paths["README.md"] == "unsupported file type"


def test_empty_repository_returns_no_files(tmp_path: Path):
    result = scan_repository(tmp_path)
    assert result.files == []
    assert result.skipped == []


def test_repository_with_only_unsupported_files(tmp_path: Path):
    (tmp_path / "notes.txt").write_text("just notes")
    (tmp_path / "data.json").write_text("{}")

    result = scan_repository(tmp_path)

    assert result.files == []
    reasons = {s.path: s.reason for s in result.skipped}
    assert reasons["notes.txt"] == "unsupported file type"
    assert reasons["data.json"] == "unsupported file type"


def test_nonexistent_root_raises():
    with pytest.raises(FileNotFoundError):
        scan_repository("/this/path/does/not/exist/at/all")


def test_root_that_is_a_file_raises(tmp_path: Path):
    file_path = tmp_path / "not_a_dir.java"
    file_path.write_text("class X {}")
    with pytest.raises(NotADirectoryError):
        scan_repository(file_path)


def test_path_with_spaces(tmp_path: Path):
    repo = tmp_path / "my project with spaces"
    src_dir = repo / "src main"
    src_dir.mkdir(parents=True)
    (src_dir / "Weird File.java").write_text("class WeirdFile {}")

    result = scan_repository(repo)

    relative_paths = [f.relative_path for f in result.files]
    assert "src main/Weird File.java" in relative_paths


def test_symlinked_file_escaping_root_is_skipped_and_reported(tmp_path: Path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    secret = outside_dir / "Secret.java"
    secret.write_text("class Secret { /* should never be read */ }")

    repo = tmp_path / "repo"
    repo.mkdir()
    link = repo / "Linked.java"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = scan_repository(repo)

    assert result.files == []
    reasons = {s.path: s.reason for s in result.skipped}
    assert reasons["Linked.java"] == "symlink escapes repository root"


def test_symlinked_directory_escaping_root_is_not_followed(tmp_path: Path):
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "Secret.java").write_text("class Secret {}")

    repo = tmp_path / "repo"
    repo.mkdir()
    link_dir = repo / "linked_dir"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = scan_repository(repo)

    assert result.files == []
    relative_paths = [s.path for s in result.skipped]
    assert "linked_dir" in relative_paths


def test_internal_symlinked_directory_is_not_followed(tmp_path: Path):
    repo = tmp_path / "repo"
    real_dir = repo / "real"
    real_dir.mkdir(parents=True)
    (real_dir / "Real.java").write_text("class Real {}")

    link_dir = repo / "linked"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")

    result = scan_repository(repo)

    relative_paths = [f.relative_path for f in result.files]
    # The real file is found via its real path...
    assert "real/Real.java" in relative_paths
    # ...but the scanner never descends into the internal symlink, even
    # though its target is inside the repository root.
    assert "linked/Real.java" not in relative_paths


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
def test_unreadable_file_is_skipped_and_reported(tmp_path: Path):
    f = tmp_path / "Locked.java"
    f.write_text("class Locked {}")
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):
            pytest.skip("running as a user that bypasses permission bits (e.g. root)")
        result = scan_repository(tmp_path)
        assert result.files == []
        reasons = {s.path: s.reason for s in result.skipped}
        assert reasons["Locked.java"] == "unreadable file"
    finally:
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_kts_files_are_treated_as_kotlin(tmp_path: Path):
    (tmp_path / "build.gradle.kts").write_text("plugins { }")

    result = scan_repository(tmp_path)

    assert len(result.files) == 1
    assert result.files[0].language == Language.KOTLIN
    assert result.files[0].relative_path == "build.gradle.kts"


def test_results_are_sorted_deterministically(tmp_path: Path):
    (tmp_path / "b.java").write_text("class B {}")
    (tmp_path / "a.java").write_text("class A {}")
    (tmp_path / "c.kt").write_text("class C")

    result = scan_repository(tmp_path)

    assert [f.relative_path for f in result.files] == ["a.java", "b.java", "c.kt"]


# -- SQL ---------------------------------------------------------------------


def test_scans_sql_files(tmp_path: Path):
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "001_init.sql").write_text("CREATE TABLE t (a INT);\n")
    (tmp_path / "db" / "002_add.sql").write_text("ALTER TABLE t ADD COLUMN b TEXT;\n")

    result = scan_repository(tmp_path)

    assert [f.relative_path for f in result.files] == ["db/001_init.sql", "db/002_add.sql"]
    assert {f.language for f in result.files} == {Language.SQL}


def test_sql_extension_is_case_insensitive(tmp_path: Path):
    (tmp_path / "Schema.SQL").write_text("CREATE TABLE t (a INT);\n")
    result = scan_repository(tmp_path)
    assert [f.language for f in result.files] == [Language.SQL]


def test_sql_and_jvm_sources_scan_together(tmp_path: Path):
    (tmp_path / "A.java").write_text("class A {}\n")
    (tmp_path / "B.kt").write_text("class B\n")
    (tmp_path / "c.sql").write_text("CREATE TABLE c (a INT);\n")

    result = scan_repository(tmp_path)

    assert {f.relative_path: f.language for f in result.files} == {
        "A.java": Language.JAVA,
        "B.kt": Language.KOTLIN,
        "c.sql": Language.SQL,
    }


def test_sql_under_an_ignored_build_directory_is_not_scanned(tmp_path: Path):
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "generated.sql").write_text("CREATE TABLE g (a INT);\n")
    result = scan_repository(tmp_path)
    assert result.files == []
