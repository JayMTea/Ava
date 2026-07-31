"""The PWA shell must not cache a stale brand.

Both assertions read the BUILT artifact rather than the config that produced it,
which is the whole point: the config looked correct for the entire time the bug
was live.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SW = ROOT / "frontend" / "dist" / "sw.js"
INDEX = ROOT / "frontend" / "dist" / "index.html"

pytestmark = pytest.mark.skipif(
    not SW.is_file(), reason="frontend/dist not built in this checkout")


def test_the_webmanifest_is_not_precached() -> None:
    """ava_bridge/pages.py rewrites the manifest from config at REQUEST time so a
    re-brand needs no rebuild — and that was silently defeated for every
    installed client, because a controlling service worker served the BUILT
    manifest (the one that says "Ava") and the server's version never arrived.
    The home-screen icon label is the most visible place a wrong brand shows up.

    Two plausible fixes do NOT work and were both measured failing: `globIgnores`
    cannot reach the entry (globPatterns does not list .webmanifest, so it is not
    a glob hit) and `workbox.manifestTransforms` never sees it either (it only
    transforms the manifest built from globs). vite.config.ts uses the plugin's
    own extendManifestEntries API. Asserting on dist/sw.js rather than on the
    config is deliberate — the config looked right the whole time it was wrong.
    """
    sw = SW.read_text(encoding="utf-8", errors="replace")
    assert "manifest.webmanifest" not in sw, (
        "the webmanifest is back in the service-worker precache, so an installed "
        "client will keep seeing the BUILT brand and never the server's.\n"
        "Check the ava:manifest-not-precached plugin in frontend/vite.config.ts, "
        "and re-check by grepping dist/sw.js — not by reading the config.")


def test_the_shell_stamps_the_brand_before_first_paint() -> None:
    """The mechanism has to survive into dist, because the SW serves index.html
    from the precache — that is exactly why the brand is applied by a script in
    the shell rather than by a stylesheet the server renders."""
    html = INDEX.read_text(encoding="utf-8", errors="replace")
    assert "ava.brand" in html, "the pre-paint brand stamp is missing from the built shell"
    assert "data-brand" in html
    # Two independent try blocks: a corrupt brand cache must not cost the theme
    # stamp, which would leave a light-mode user staring at a dark app.
    assert html.count("try") >= 2, (
        "the boot script must keep theme and brand in SEPARATE try blocks")
