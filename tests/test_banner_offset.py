"""A fixed banner must never paint over the app.

`.inf-banner` is `position: fixed`, so it reserves NO space in normal flow. On
its own that means it covers the header, the top of whatever view is open, and —
on phones, where it sits at z-index 60 above a z-index-40 drawer — the top of
the drawer too. Observed: the banner sitting across the Hub's title row.

The fix is a measured offset, not a constant. The banner's text is supplied by
the server and `claude.css` wraps it to two lines under 760px, so there is no
single height to hardcode. `InferenceBanner` observes its own box and publishes
`--inf-banner-h`; the stylesheet consumes it with a `0px` fallback so a hidden
banner costs nothing and the layout returns by itself.

That is two halves in two files, and either one alone is silently useless: a
publisher nothing reads, or a reader nothing sets. This pins both ends.

House style is tests/test_icon_sizing.py — a static scan, no build, no node,
no browser.
"""
from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FE = os.path.join(ROOT, "frontend", "src")
VAR = "--inf-banner-h"


def _read(*parts: str) -> str:
    with open(os.path.join(FE, *parts), encoding="utf-8") as f:
        return f.read()


class BannerOffsetTests(unittest.TestCase):

    def test_the_banner_is_still_the_fixed_element_this_guard_is_about(self):
        """If it ever stops being position:fixed it reserves its own space and
        the whole mechanism is dead weight — which is fine, but then this file
        should go, not quietly pass."""
        css = _read("styles", "claude.css")
        block = re.search(r"\.inf-banner\s*\{(.*?)\}", css, re.S)
        self.assertIsNotNone(block, ".inf-banner rule not found")
        self.assertIn("position: fixed", " ".join(block.group(1).split()),
                      "the offset below exists only because it is fixed")

    def test_the_component_publishes_its_measured_height(self):
        tsx = _read("components", "InferenceBanner.tsx")
        self.assertIn(VAR, tsx, f"{VAR} is never set — the CSS reads nothing")
        self.assertIn("ResizeObserver", tsx,
                      "the height must be OBSERVED: the banner wraps to two "
                      "lines under 760px, so a one-off read goes stale")
        self.assertIn("removeProperty", tsx,
                      "the property must be cleared when the banner hides, or "
                      "the app keeps a gap where the banner used to be")

    def test_the_stylesheet_consumes_it_with_a_zero_fallback(self):
        css = _read("styles", "claude.css")
        uses = re.findall(rf"var\(\s*{re.escape(VAR)}\s*,\s*([^)]*)\)", css)
        self.assertTrue(uses, f"{VAR} is set but never read")
        for got in uses:
            self.assertEqual(got.strip(), "0px",
                             "every read needs a 0px fallback so a hidden "
                             "banner leaves the layout untouched")

    def test_the_main_column_is_shifted(self):
        """body is `height:100dvh; display:flex`, and `* { box-sizing:
        border-box }` is what makes padding shrink that column instead of
        overflowing the viewport."""
        css = _read("styles", "claude.css")
        self.assertRegex(
            css, rf"body\s*\{{[^}}]*padding-top:\s*var\(\s*{re.escape(VAR)}",
            "body must reserve the banner's height")
        self.assertIn("box-sizing: border-box", _read("styles", "global.css"),
                      "without border-box the padding overflows 100dvh")

    def test_every_fixed_full_height_overlay_is_offset_too(self):
        """Body padding cannot move a position:fixed box — it resolves against
        the viewport — so each one anchored at top:0 must be offset by hand or
        the banner covers it."""
        css = _read("styles", "claude.css") + _read("styles", "global.css")
        # Flatten @media wrappers. A naive rule regex treats `@media (...) {` as
        # the selector and the rule inside as its body, so every mobile-only
        # overlay — which is all of them — reads as having no selector at all.
        css = re.sub(r"@media[^{]*\{", "", css)
        # Collect EVERY selector in a rule that carries the offset, not just the
        # first: `#drawer, #artifactPanel { ... }` is one rule with two subjects,
        # and a regex anchored on the first silently exempts the rest.
        offset: set[str] = set()
        for rule in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
            if re.search(rf"top:\s*var\(\s*{re.escape(VAR)}", rule.group(2)):
                offset.update(re.findall(r"#[\w-]+", rule.group(1)))
        anchored = set(re.findall(
            r"(#[\w-]+)\s*\{[^}]*position:\s*fixed[^}]*top:\s*0[^}]*height:\s*100dvh",
            css))
        # #scrim is deliberately absent: a dimming layer covering the full
        # viewport is correct, and the banner is meant to sit above it.
        missing = {s for s in anchored if s not in offset} - {"#scrim"}
        self.assertFalse(
            missing,
            f"fixed full-height overlays with no banner offset: {sorted(missing)}")


if __name__ == "__main__":
    unittest.main()
