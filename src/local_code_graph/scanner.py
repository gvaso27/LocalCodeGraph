"""Local filesystem scanner.

Walks a user-provided repository root and identifies supported source files
(Java, Kotlin, and SQL). The scanner never reads or follows paths outside the
supplied root, ignores common build/cache/VCS directories, and reports any
file it could not process instead of silently dropping it.

No network access is performed anywhere in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Language(str, Enum):
    JAVA = "java"
    KOTLIN = "kotlin"
    SQL = "sql"


# Maps file extensions to the language that should parse them.
EXTENSION_LANGUAGE_MAP: dict[str, Language] = {
    ".java": Language.JAVA,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".sql": Language.SQL,
}

# Directory names that are never descended into, regardless of depth.
DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".local-code-graph",
        "build",
        "out",
        "target",
        "dist",
        "node_modules",
        ".gradle",
        ".idea",
        ".vscode",
        "bin",
        "obj",
        "__pycache__",
        ".venv",
        "venv",
        ".mvn",
        ".settings",
    }
)


@dataclass(frozen=True)
class SourceFile:
    """A single source file selected for parsing."""

    path: Path
    """Absolute path to the file on disk."""

    relative_path: str
    """Path relative to the scanned repository root, using forward slashes."""

    language: Language


@dataclass(frozen=True)
class SkippedFile:
    """A file (or path) that was seen but not included, with the reason why."""

    path: str
    reason: str


@dataclass
class ScanResult:
    root: Path
    files: list[SourceFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def scan_repository(
    root: str | os.PathLike[str],
    *,
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
) -> ScanResult:
    """Recursively scan ``root`` for supported source files.

    Only files that resolve (after following any symlinks) to a location
    inside ``root`` are included. Symlinks that point outside the repository
    root are skipped and reported rather than followed, so the scanner can
    never be tricked into reading arbitrary files elsewhere on disk.
    """
    root_path = Path(root)
    if not root_path.exists():
        raise FileNotFoundError(f"Repository root does not exist: {root_path}")
    if not root_path.is_dir():
        raise NotADirectoryError(f"Repository root is not a directory: {root_path}")

    resolved_root = root_path.resolve()
    result = ScanResult(root=root_path)

    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        current_dir = Path(dirpath)

        # Prune ignored directories in-place so os.walk does not descend into them.
        # Note: os.walk() is called with followlinks=False below, so it already
        # refuses to recurse into any symlinked directory. We still detect them
        # here so we can report *why* they were skipped, and to guarantee no
        # accounting relies on os.walk's default behavior alone.
        pruned = []
        for dirname in list(dirnames):
            if dirname in ignored_dirs:
                pruned.append(dirname)
                continue
            candidate = current_dir / dirname
            if candidate.is_symlink():
                target = _safe_resolve(candidate)
                if target is None or not _is_within_root(target, resolved_root):
                    reason = "symlink escapes repository root"
                else:
                    reason = "symlinked directories are not followed"
                result.skipped.append(
                    SkippedFile(
                        path=_relative_posix(candidate, root_path),
                        reason=reason,
                    )
                )
                pruned.append(dirname)
        for dirname in pruned:
            dirnames.remove(dirname)

        for filename in filenames:
            file_path = current_dir / filename
            rel = _relative_posix(file_path, root_path)

            if file_path.is_symlink():
                target = _safe_resolve(file_path)
                if target is None or not _is_within_root(target, resolved_root):
                    result.skipped.append(
                        SkippedFile(path=rel, reason="symlink escapes repository root")
                    )
                    continue

            suffix = file_path.suffix.lower()
            language = EXTENSION_LANGUAGE_MAP.get(suffix)
            if language is None:
                result.skipped.append(
                    SkippedFile(path=rel, reason="unsupported file type")
                )
                continue

            if not os.access(file_path, os.R_OK):
                result.skipped.append(SkippedFile(path=rel, reason="unreadable file"))
                continue

            result.files.append(
                SourceFile(path=file_path, relative_path=rel, language=language)
            )

    result.files.sort(key=lambda f: f.relative_path)
    result.skipped.sort(key=lambda f: f.path)
    return result


def _safe_resolve(path: Path) -> Path | None:
    """Resolve a symlink's target, returning None if it cannot be resolved."""
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
