"""Every skill the agent ships must be surfaceable in the UI. The Agent tab
auto-discovers skills from their SKILL.md frontmatter (ava_bridge/skills.py), so
a malformed or metadata-less SKILL.md would appear as a broken card. This is the
`tests/test_diagram_sync.py`-style guard: a static scan over `git ls-files` that
runs anywhere (no bridge, no sandbox) and fails with instructions.
"""
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None


def _tracked_skill_files() -> list[pathlib.Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "agent/skills/*/SKILL.md"],
                         capture_output=True, text=True, check=True).stdout
    return [ROOT / line for line in out.splitlines() if line]


def _frontmatter(text: str) -> dict | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    try:
        fm = yaml.safe_load(text[3:end]) if yaml else None
    except Exception:  # noqa: BLE001
        return None
    return fm if isinstance(fm, dict) else None


@pytest.mark.skipif(yaml is None, reason="pyyaml not installed")
def test_every_shipped_skill_has_valid_frontmatter():
    files = _tracked_skill_files()
    assert files, "no agent/skills/*/SKILL.md found — did skills move?"
    bad = []
    for f in files:
        fm = _frontmatter(f.read_text(encoding="utf-8"))
        if not fm or not fm.get("name") or not fm.get("description"):
            bad.append(str(f.relative_to(ROOT)))
    assert not bad, (
        "these SKILL.md files lack valid frontmatter (need a `name:` and "
        f"`description:` between --- fences so the Agent tab can show them): {bad}"
    )


@pytest.mark.skipif(yaml is None, reason="pyyaml not installed")
def test_shipped_skill_icons_exist_in_the_icon_set():
    # A skill may only name a glyph the frontend actually ships: an unknown name
    # renders nothing, silently, in the Agent tab (an `icon: email` once did
    # exactly that — the set has no envelope). Guard: every declared icon must be
    # a key of ICONS in frontend/src/lib/icons.tsx.
    icons_src = (ROOT / "frontend/src/lib/icons.tsx").read_text(encoding="utf-8")
    known = set(re.findall(r"^  ([A-Za-z][A-Za-z0-9_]*):$", icons_src, re.M))
    assert known, "could not parse ICONS from frontend/src/lib/icons.tsx"
    offenders = []
    for f in _tracked_skill_files():
        icon = (_frontmatter(f.read_text(encoding="utf-8")) or {}).get("icon")
        if icon and icon not in known:
            offenders.append(f"{f.parent.name} (icon: {icon})")
    assert not offenders, (
        "these skills declare an icon the frontend does not ship, so their card "
        f"renders no glyph: {offenders}. Use one of: {sorted(known)}"
    )


def test_registry_discovers_the_shipped_skills():
    # The live registry must find at least every tracked core skill — proves the
    # glob path and frontmatter parsing stay in sync with what's on disk.
    from ava_bridge import skills
    discovered = {s["dir"] for s in skills.catalog(force=True)}
    for f in _tracked_skill_files():
        assert f.parent.name in discovered, f"{f.parent.name} not discovered by skills.catalog()"


@pytest.mark.skipif(yaml is None, reason="pyyaml not installed")
def test_shipped_skills_declare_no_category():
    # Categorization is OWNER-owned (ava.yaml `skills.categories`), never shipped
    # — so forks define their own taxonomy without editing shipped skills, and
    # the product imposes none. Guard: no tracked core SKILL.md pins a category.
    offenders = []
    for f in _tracked_skill_files():
        fm = _frontmatter(f.read_text(encoding="utf-8")) or {}
        if fm.get("category"):
            offenders.append(f"{f.parent.name} (category: {fm['category']})")
    assert not offenders, (
        "shipped skills must not declare a category — put categorization in "
        "ava.yaml `skills.categories` (per-instance, gitignored) instead: "
        f"{offenders}"
    )
