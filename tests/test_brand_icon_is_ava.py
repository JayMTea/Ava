"""The app icon is Ava's on every install, and is not configurable.

Whatever an install calls itself, the browser tab, the home-screen tile and the
maskable icon say Ava. Only surfaces *inside* the signed-in app follow the
owner's brand. This is a product rule rather than a technical constraint, so
nothing enforces it structurally — which is exactly why it needs a guard.

It used to work the other way round, and by two routes rather than one:

  * an `icon` slot the owner uploaded to, and
  * `icon_source()`, which FELL BACK to the `logo` slot — so an owner who only
    ever uploaded a logo silently re-branded their favicon too.

Both are gone, along with the whole render-on-demand pipeline (`DERIVED`,
`derived_icon`, `_maskable_bg`) that existed only to serve them. This file fails
if any of it returns, in the source as well as at runtime, because a
re-introduced fallback is invisible from the UI: the tab just quietly stops
being Ava's on installs that never asked for that.

House style is tests/test_diagram_sync.py — a static scan plus a few runtime
assertions, no bridge and no browser.
"""
from __future__ import annotations

import pathlib
import re

from ava_bridge import brand

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRAND = ROOT / "ava_bridge" / "brand.py"
PAGES = ROOT / "ava_bridge" / "pages.py"
PANEL = (ROOT / "frontend" / "src" / "components" / "hub" / "panels"
         / "BrandingPanel.tsx")


def test_icon_is_not_a_brandable_slot() -> None:
    assert "icon" not in brand.SLOTS, (
        "`icon` is a branding slot again. The favicon, the PWA tile and the "
        "maskable icon are Ava's on every install.")
    assert "icon" not in brand.DEFAULT_ASSETS


def test_the_upload_path_refuses_the_icon_slot() -> None:
    """Belt and braces on the enum: the API must refuse it too, so a client that
    still knows the old slot name cannot re-brand the tab directly."""
    fname, err = brand.ingest_asset("icon", b"")
    assert not fname and "unknown slot" in err


def test_the_render_pipeline_is_gone() -> None:
    """These existed ONLY to turn an owner's upload into a favicon and PWA set."""
    for gone in ("icon_source", "derived_icon", "DERIVED", "_maskable_bg",
                 "invalidate_derived"):
        assert not hasattr(brand, gone), (
            f"brand.{gone} is back — the icon set is being rendered from an "
            "owner asset again.")


def test_nothing_serves_a_branded_icon() -> None:
    """A static read of the routes, so this fails on the diff rather than on a
    request that only happens on a branded install."""
    src = PAGES.read_text(encoding="utf-8")
    for banned in ("derived_icon", "icon_source"):
        assert banned not in src, (
            f"pages.py calls {banned} again. /favicon.ico, /favicon.svg and the "
            "PWA manifest must serve Ava's shipped files unconditionally.")
    # The manifest's `icons` array must stay whatever the build produced.
    assert 'man["icons"]' not in src, (
        "the PWA manifest is overriding its icons again — that is the "
        "home-screen tile being re-branded.")


def test_the_logo_no_longer_claims_to_drive_the_favicon() -> None:
    """The panel's own copy is what taught owners the old behaviour, and it is
    the thing that has to stop saying it."""
    hint = PANEL.read_text(encoding="utf-8")
    assert "slot: 'icon'" not in hint, "the App icon row is back in the panel"
    logo_hint = re.search(r"slot: 'logo'.*?hint: '([^']*)'", hint, re.S)
    assert logo_hint, "could not find the logo row's hint text"
    body = logo_hint.group(1).lower()
    for claim in ("favicon", "home-screen icon unless", "app icon below"):
        assert claim not in body, (
            f"the Logo hint still promises {claim!r} — it does not drive the "
            "browser tab any more.")


def test_the_sign_in_card_shows_avas_mark() -> None:
    """Pre-auth is the product identifying itself to a visitor, not the app
    dressing itself for its owner — same category as the tab."""
    src = BRAND.read_text(encoding="utf-8")
    fn = src[src.index("def pre_auth_mark"):]
    fn = fn[:fn.index("\ndef ", 1)]
    assert "/brand/asset/logo" not in fn, (
        "the sign-in card renders the owner's logo again")
    assert "_SHIPPED_MARK" in fn
