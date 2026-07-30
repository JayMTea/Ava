"""VERSION is the version. Anything restating it must agree with it.

`pyproject.toml` does this correctly already — `version = { file = "VERSION" }`, so
there is nothing to drift. Three other tracked files hold a literal copy, and a copy
with no guard is the defect this repo keeps rediscovering: two ruff pins in CI, two
platform tables, a Node major in three places, and a fleet test count quoted in three
files that had all three gone stale.

A stale version is worse here than in most projects, because `CITATION.cff` is what
GitHub renders behind its "Cite this repository" button. People paste that verbatim
into papers and posts, so it is the one number that gets copied somewhere the project
cannot later correct.

The assertion message is the fix instruction, per tests/test_diagram_sync.py's style.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"


def _canonical() -> str:
    v = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert v, "VERSION is empty — it is the source of truth for every other copy"
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), (
        f"VERSION is {v!r}, which is not MAJOR.MINOR.PATCH. setuptools reads this "
        "file directly, and CITATION.cff's `version` field is compared against it.")
    return v


def _cff_version() -> str | None:
    """The `version:` line, read without a YAML dependency.

    PyYAML is not in requirements.txt — only requirements-dev.txt — and this guard
    must run in the same bare environment the rest of tests/ does. The field is a
    plain scalar at column 0, so a line match is exact enough and cannot be fooled by
    the word appearing inside the abstract, which is indented.
    """
    cff = ROOT / "CITATION.cff"
    if not cff.exists():
        return None
    for line in cff.read_text(encoding="utf-8").splitlines():
        m = re.fullmatch(r"version:\s*[\"']?([^\"'#]+?)[\"']?\s*", line)
        if m:
            return m.group(1).strip()
    return None


def test_citation_version_matches_the_version_file() -> None:
    """The number GitHub shows in its citation widget must be the real one."""
    want = _canonical()
    got = _cff_version()
    assert got is not None, (
        "CITATION.cff is missing a top-level `version:` line. Either add it (matching "
        f"VERSION, currently {want}) or delete the field entirely — an absent version "
        "is honest, a wrong one gets pasted into other people's citations.")
    assert got == want, (
        f"CITATION.cff says version {got!r} but VERSION says {want!r}. Update "
        "CITATION.cff. This is the number behind GitHub's 'Cite this repository' "
        "button, so a stale value propagates into places you cannot edit.")


@pytest.mark.parametrize("rel", ["frontend/package.json", "demo/package.json"])
def test_package_json_versions_match_the_version_file(rel: str) -> None:
    """Both package.json files restate the version; neither is generated from it.

    demo/ is excluded via .git/info/exclude on the maintainer's box, so it is absent
    from a fresh checkout — skip rather than fail, the same way the fixture-contract
    test skips when its directory is not there.
    """
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} is not present in this checkout")
    got = json.loads(p.read_text(encoding="utf-8")).get("version")
    want = _canonical()
    assert got == want, (
        f"{rel} says version {got!r} but VERSION says {want!r}. Bump them together, "
        "or make this file read VERSION instead of holding its own copy.")
