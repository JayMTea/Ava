"""No file may hardcode the shipped accent outside the token SSOT.

A re-brandable app has exactly one place the accent is written down. Eight
literals had drifted out of tokens.css — five in claude.css, one in
dashboard.css, one inline in HubView.tsx and one in setup_wizard.html — and each
was a surface no re-brand could reach: hover fills, the recording ring, the
pending-app chip. They were invisible until someone set a red accent and found
blue hover states underneath it.

House style is tests/test_diagram_sync.py: a static scan over `git ls-files`
that runs anywhere and fails with the fix in the message. The allowlist is short
and every entry states why, per the standard tests/test_config_template_sync.py
sets — a guard needing thirty exceptions is one nobody maintains.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The shipped accent, in the two spellings that actually occurred.
_BLUE = re.compile(r"#007acc|rgba\(\s*0\s*,\s*122\s*,\s*204", re.I)

_SCAN_SUFFIXES = {".css", ".ts", ".tsx", ".html"}

# path -> why it may name the shipped blue.
_ALLOW = {
    # THE source of truth. The whole point of the guard is that this is the only
    # file that declares the value.
    "frontend/src/styles/tokens.css":
        "the design-token SSOT — the one place the accent is declared",
    # getComputedStyle fallbacks, used only if a token is missing entirely (e.g.
    # a chart rendered before the stylesheet loads). Documented as fallbacks in
    # the file itself; they are not a second palette.
    "frontend/src/components/dashboard/chartTheme.ts":
        "documented getComputedStyle() fallbacks, not declarations",
    # This guard names the value it is looking for.
    "tests/test_brand_tokens.py": "the guard itself",
    # These assert the shipped default is preserved, which requires naming it.
    "tests/test_brand_contrast.py": "asserts the shipped calibration",
    "tests/test_brand_pages.py": "asserts the unbranded pages are unchanged",
    "qa/e2e/brand-tokens.spec.ts": "asserts the unbranded palette in a browser",
    "qa/e2e/brand-flash.spec.ts": "asserts the default is painted on a cold profile",
    "qa/e2e/brand-panel.spec.ts": "drives the panel's default-accent field by placeholder",
}


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p]


def test_the_shipped_accent_is_declared_in_exactly_one_place() -> None:
    offenders: list[str] = []
    for rel in _tracked():
        if pathlib.Path(rel).suffix not in _SCAN_SUFFIXES:
            continue
        if rel in _ALLOW or rel.startswith("frontend/dist/"):
            continue
        try:
            body = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _BLUE.finditer(body):
            line = body[:m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}  {m.group(0)}")

    assert not offenders, (
        "the shipped accent is hardcoded outside frontend/src/styles/tokens.css:\n    "
        + "\n    ".join(offenders)
        + "\n\nUse the token instead — `var(--accent)`, or for a translucent tint\n"
          "`color-mix(in srgb, var(--accent) N%, transparent)`, which is exactly\n"
          "equal to rgba(accent, N%) and follows a re-brand. A literal here is a\n"
          "surface that stays blue when someone sets their own colour."
    )


def test_the_allowlist_has_not_gone_stale() -> None:
    """An allowlist entry for a file that no longer needs it is how a guard
    quietly stops guarding."""
    stale = []
    for rel in _ALLOW:
        p = ROOT / rel
        if not p.is_file():
            stale.append(f"{rel} (no such file)")
            continue
        if not _BLUE.search(p.read_text(encoding="utf-8", errors="replace")):
            stale.append(f"{rel} (no longer names the accent — drop the entry)")
    assert not stale, "stale allowlist entries:\n    " + "\n    ".join(stale)


def test_the_brand_override_block_exists_and_touches_no_shipped_value() -> None:
    """The `[data-brand]` block must be ADDITIVE.

    --accent-tint (#8ec9f0) and --accent-line (#1d4e75) are hand-picked, not
    formulaic — no simple mix reproduces them. Rewriting the shipped blocks as
    derivations would have shifted every existing install's pixels, so the
    override is a separate selector that an unbranded install never matches.
    """
    css = (ROOT / "frontend/src/styles/tokens.css").read_text(encoding="utf-8")
    assert ':root[data-brand="on"]' in css
    assert ':root[data-theme="light"][data-brand="on"]' in css
    # The shipped hand-picked values must still be present, verbatim.
    for shipped in ("--accent:#007acc", "--accent-hover:#0068ad",
                    "--accent-tint:#8ec9f0", "--accent-line:#1d4e75",
                    "--accent:#006bb3", "--accent-tint:#005a94"):
        assert shipped in css, f"a shipped token value was edited away: {shipped}"


def test_learning_view_uses_the_one_app_palette() -> None:
    """CLAUDE.md mandates appAccent() for anything representing a connected app.

    LearningView carried a SECOND palette with its own `% 7` hash, so a connector
    got one colour there and a different one in the rail — and its first entry
    was a hardcoded #007acc no re-brand could reach.
    """
    src = (ROOT / "frontend/src/components/learning/LearningView.tsx").read_text(
        encoding="utf-8")
    assert "APP_COLORS" not in src, "the rogue palette is back"
    assert "appAccent" in src
