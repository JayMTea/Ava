"""The accessibility guard's calibration, pinned.

These three ratios are the whole reason the thresholds in `brand.py` are
asymmetric instead of tidy, so they are asserted as numbers rather than trusted
as a comment. If a refactor moves them, this fails loudly and somebody re-reads
the argument rather than quietly shipping a guard that rejects Ava's own blue.

The trap, restated because it is genuinely counter-intuitive: the accent is BOTH
a background under hardcoded white text (~15 rules, including the user's own
chat bubble) and a foreground mark on the canvas. Those pull in opposite
directions. Ava's `#007acc` passes AA text by 0.01 and FAILS 4.5:1 against the
dark canvas — where the applicable rule is WCAG 1.4.11's 3:1 non-text bar. A
guard applying 4.5:1 to both would reject the default on a fresh install.
"""
from __future__ import annotations

import pytest

from ava_bridge import brand


# ---- The measured calibration ---------------------------------------------
def test_shipped_accent_passes_text_on_accent_by_a_hair() -> None:
    """4.51:1 — over the 4.5 AA bar by 0.01. The margin IS the finding."""
    assert round(brand.contrast(brand.DEFAULT_ACCENT, brand.ON_ACCENT), 2) == 4.51


def test_shipped_accent_fails_45_against_the_dark_canvas() -> None:
    """3.36:1 — which is why the canvas rule is 3.0 (non-text), not 4.5."""
    ratio = brand.contrast(brand.DEFAULT_ACCENT, brand.DARK_BG)
    assert round(ratio, 2) == 3.36
    assert ratio < 4.5, "if this ever passes 4.5, re-read the threshold argument"
    assert ratio >= brand.ACCENT_ON_CANVAS_MIN


def test_shipped_light_accent_on_the_light_canvas() -> None:
    assert round(brand.contrast(brand.DEFAULT_ACCENT_LIGHT, brand.LIGHT_BG), 2) == 5.31


def test_the_guard_accepts_the_colour_ava_ships() -> None:
    """The one test that would have caught a 4.5-everywhere guard."""
    r = brand.check_accent(brand.DEFAULT_ACCENT, brand.DEFAULT_ACCENT_LIGHT)
    assert r["ok"], r["blocking"]
    assert r["blocking"] == []


# ---- Blank means default, everywhere --------------------------------------
@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_accent_is_always_accepted(blank: str) -> None:
    """Blank is not a value to validate — it is the statement that this install
    has not been re-branded. Rejecting it would make "reset to defaults"
    unreachable, since reset posts every field as ""."""
    assert brand.check_accent(blank)["ok"]


# ---- Refusals --------------------------------------------------------------
def test_a_LIGHT_accent_is_refused_for_unreadable_white_text() -> None:
    """Bright yellow: white on it is 1.38:1.

    Note the direction, because it is easy to get backwards and an earlier draft
    of this test did: a DARK accent is fine for white text (17:1) and fails the
    canvas rule instead. It is LIGHT accents that break text-on-accent. Yellow
    and lime are exactly the brand colours that need the `--on-accent` token
    (flip the label to black) which brand.py names as the deliberate follow-up.
    """
    r = brand.check_accent("#ffd93d")
    assert not r["ok"]
    assert any("white text" in b for b in r["blocking"])


def test_a_dark_accent_is_refused_for_vanishing_on_the_dark_canvas() -> None:
    """#1a1a1a reads fine under white text (17.40:1) and is still refused,
    because at 1.15:1 against --bg #262624 it is invisible as a mark."""
    r = brand.check_accent("#1a1a1a")
    assert not r["ok"]
    assert any("dark canvas" in b for b in r["blocking"])
    assert brand.contrast("#1a1a1a", brand.ON_ACCENT) > 4.5, \
        "this colour must PASS the text rule, or the test proves nothing"


@pytest.mark.parametrize("bad", ["blue", "#12", "#gggggg", "rgb(1,2,3)",
                                 "var(--app-accent-0)", "#1234567"])
def test_non_hex_is_refused(bad: str) -> None:
    """`var(--app-accent-N)` is accepted by hub/connectors.py's _COLOR_RE but
    must NOT be here: those slots resolve through the accent, so accepting one
    as the accent is circular."""
    r = brand.check_accent(bad)
    assert not r["ok"]
    assert any("#rrggbb" in b for b in r["blocking"])


# ---- The suggestion ---------------------------------------------------------
def test_a_refusal_offers_a_working_colour_of_the_same_hue() -> None:
    """A guard that only says no gets switched off by the first real brand."""
    r = brand.check_accent("#ff3b30")          # a bright red that fails on white
    assert not r["ok"]
    assert brand.ACCENT_RE.match(r["suggest"]), r
    assert brand.check_accent(r["suggest"])["ok"], "the suggestion must itself pass"


def test_the_suggestion_keeps_the_hue_rather_than_greying_out() -> None:
    """Walked in OKLab, not sRGB: scaling RGB channels pulls a saturated blue
    purple, and a guard that answers a brand colour with the wrong hue is a
    guard people turn off."""
    import math
    fixed = brand.suggest("#1a1a8a")           # deep blue, fails white-text
    assert fixed
    _, a1, b1 = brand._srgb_to_oklab("#1a1a8a")
    _, a2, b2 = brand._srgb_to_oklab(fixed)
    hue1, hue2 = math.atan2(b1, a1), math.atan2(b2, a2)
    drift = abs(math.degrees(hue1 - hue2))
    assert min(drift, 360 - drift) < 12, f"hue drifted {drift:.1f}°"


# ---- normalize_hex ---------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("#ABC", "#aabbcc"), ("#007ACC", "#007acc"), ("  #007acc  ", "#007acc"),
    ("nope", ""), ("", ""),
])
def test_normalize_hex(raw: str, want: str) -> None:
    assert brand.normalize_hex(raw) == want
