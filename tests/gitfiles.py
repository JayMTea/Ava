"""One `git ls-files` helper for the convention guards.

Eleven guard modules each carried their own copy of the same six lines, all with
`check=True`. Outside a usable git checkout that raises CalledProcessError, so
the guards ERROR rather than skip — and `.dockerignore` excludes `.git`, so
running the suite inside the built image (or from a source tarball, or a shallow
export) turned every convention guard into a hard failure that looked like a real
violation.

Skipping is the correct degradation: these guards assert facts about what is
*tracked*, which is a question that has no answer without git. A guard that
cannot run has not found a problem.
"""
import functools
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@functools.lru_cache(maxsize=1)
def _git_available() -> bool:
    """Is this a working git checkout with the `git` binary on PATH?"""
    if not shutil.which("git"):
        return False
    try:
        subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-dir"],
                       capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def require_git() -> None:
    """Skip the calling test when the tracked-file question is unanswerable."""
    if not _git_available():
        pytest.skip("not a git checkout (no .git or no git binary) — "
                    "convention guards assert facts about tracked files")


def tracked(pattern: str = "") -> list[str]:
    """Repo-relative paths of tracked files, optionally filtered by a pathspec.

    Note the pathspec is a git glob and matches RECURSIVELY: `a/*.svg` also
    returns `a/icons/x.svg`. Compare full paths, not basenames.
    """
    require_git()
    argv = ["git", "-C", str(ROOT), "ls-files"]
    if pattern:
        argv.append(pattern)
    out = subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def tracked_paths(pattern: str = "") -> list[pathlib.Path]:
    """`tracked()` as absolute Paths, for guards that open the files."""
    return [ROOT / rel for rel in tracked(pattern)]
