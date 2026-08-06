from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config) -> None:
    """Recreate the nested-.git fixture that Git itself refuses to track.

    tests/fixtures/java/simple_project/.git/HEAD exists locally to give
    test_scanner.py a real ".git"-named directory to verify the scanner
    ignores — but Git structurally refuses to add any file inside a
    directory literally named ".git" (it's not a gitignore issue; `git add`
    just declines). So after a fresh clone that file is missing and the
    "scanner ignores .git/" assertions would pass vacuously (nothing to
    find) rather than actually exercising the ignore behavior. Recreate it
    here, before tests run, so the fixture is always present regardless of
    whether this is a fresh clone or a local working copy.
    """
    git_dir = FIXTURES / "java" / "simple_project" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    head_file = git_dir / "HEAD"
    if not head_file.exists():
        head_file.write_text("fake git internals\n")
