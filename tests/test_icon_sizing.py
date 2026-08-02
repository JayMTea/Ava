"""Every <Icon> must have a size, and the default that guarantees it must stay
first in the cascade.

`lib/icons.tsx` renders each glyph as an inline <svg> carrying a viewBox and no
width/height, wrapped in a `.ico` span that global.css sets to
`display: contents`. That combination has a sharp edge: the bare SVG lands in the
HOST's box with an aspect ratio and NO intrinsic size, so `width: auto` resolves
against the host rather than the glyph. An unsized host therefore renders the
icon at an arbitrary size with no error and no warning — measured, against the
real stylesheets, at 370px inside a `.hub-note`, 376px inside a
`.hub-preview-head`, and 0x0 (invisible) as a flex item in `.hub-restart` and
`.hub-step-mark`. Six hosts were wrong in three different directions before
anyone noticed, because the twenty hosts that looked right only looked right
thanks to a hand-added `<host> svg { width }` rule.

So the size lives in ONE place, and the per-host overrides that legitimately
differ (`.tile svg` at 52%, `.wx-chart svg` at 100%) keep working the way CSS
already made them work: equal specificity, later source order wins.

House style is tests/test_diagram_sync.py — a static scan that needs no build,
no node and no browser.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
STYLES = ROOT / "frontend" / "src" / "styles"
GLOBAL = STYLES / "global.css"
HUB = STYLES / "hub.css"

# The rule under test, and the only one allowed to size icons generically.
_DEFAULT = re.compile(r"\.ico\s*>\s*svg\s*\{[^}]*\bwidth:\s*[^;}]+", re.S)
# Any other rule that sizes an svg: `.thing svg { … width: … }`.
_SVG_SIZED = re.compile(r"(?P<sel>[^{}]*?\bsvg)\s*\{(?P<body>[^}]*\bwidth:[^}]*)\}", re.S)


def test_every_icon_has_a_default_size() -> None:
    """The one rule that stops a new icon host from being born broken."""
    assert _DEFAULT.search(GLOBAL.read_text(encoding="utf-8")), (
        "`.ico > svg` no longer sets a width in global.css. Without it an <Icon> "
        "in a host that has no `<host> svg { width }` rule of its own renders at "
        "the size of the host — a 370px glyph in a note, or 0x0 inside a flex "
        "row. Restore the default; per-host rules still override it.")


def test_the_default_stays_ahead_of_the_per_host_overrides() -> None:
    """`.ico > svg` and `.hub-tab svg` are BOTH specificity (0,1,1), so nothing
    but source order decides between them. Every stylesheet that overrides the
    default is imported after global.css (main.tsx), and within global.css itself
    the default must come first — move it below `.wx-chart svg { width:100% }`
    and the charts silently shrink to glyph size."""
    css = GLOBAL.read_text(encoding="utf-8")
    at = _DEFAULT.search(css)
    assert at, "the default is gone — see test_every_icon_has_a_default_size"
    later = [m.group("sel").strip() for m in _SVG_SIZED.finditer(css)
             if m.start() < at.start() and ".ico" not in m.group("sel")]
    assert not later, (
        "these global.css rules size an svg BEFORE `.ico > svg` sets the default, "
        "so the default now overrides them instead of the other way round:\n  "
        + "\n  ".join(later)
        + "\nMove `.ico > svg` above them.")


def test_the_note_that_started_it_keeps_its_size() -> None:
    """`.hub-note` is where this was found: ProvisionRun's "the sandbox container
    is not running" note rendered its ⓘ at 370px across. The generic default now
    covers it, but the explicit rule is what pins the 15px and the row layout, so
    the note reads as a sentence with an icon rather than a stack."""
    css = HUB.read_text(encoding="utf-8")
    assert re.search(r"\.hub-note svg\s*\{[^}]*width:", css), (
        "`.hub-note svg` lost its explicit size")
    assert re.search(r"\.hub-note\.with-icon\s*\{[^}]*display:\s*flex", css), (
        "`.hub-note.with-icon` is no longer a flex row, so an icon-led note "
        "stacks the glyph above its own sentence")
