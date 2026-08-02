"""Branding image ingest — the refusals, and the re-encode.

Uploading an image is the one place this feature accepts bytes from outside, so
this file is its security contract. Every case here is a real class of attack or
a real class of broken file, not a shape check.

The most important assertion is `test_the_served_bytes_are_not_the_uploaded_bytes`.
Everything else defends a boundary; the re-encode makes several boundaries stop
mattering, and it is the property most easily lost to a well-meaning "why decode
it twice?" refactor.
"""
from __future__ import annotations

import io
import os
import zipfile

import pytest

from ava_bridge import brand, settings


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """A throwaway AVA_HOME per test — these write real files."""
    monkeypatch.setattr(settings, "_CFG", {}, raising=False)
    monkeypatch.setenv("AVA_BRAND_DIR", str(tmp_path / "branding"))
    return tmp_path


def _png(w=256, h=256, color=(11, 114, 133)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_with_exif() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    img = Image.new("RGB", (256, 256), (200, 30, 60))
    exif = img.getexif()
    exif[0x010E] = "SECRET CAMERA NOTE"      # ImageDescription
    exif[0x8298] = "Copyright Somebody Else"  # Copyright
    img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


# ---- The property the whole design rests on --------------------------------
def test_the_served_bytes_are_not_the_uploaded_bytes() -> None:
    """The re-encode is the security property, so assert it directly.

    Because the stored bytes are bytes Pillow produced, an appended payload, a
    polyglot, hostile EXIF and ICC trickery all stop existing rather than
    needing to be detected. A refactor that "optimises away" the second encode
    would silently remove all of that, and every other test here would still
    pass.
    """
    uploaded = _png()
    fname, err = brand.ingest_asset("logo", uploaded)
    assert not err, err
    served = (settings.brand_dir() and
              open(os.path.join(settings.brand_dir(), fname), "rb").read())
    assert served != uploaded, "the upload was stored verbatim — the re-encode is gone"


def test_a_payload_appended_after_the_image_does_not_survive() -> None:
    """The classic polyglot: a valid PNG with arbitrary bytes glued on the end.
    Pillow decodes the image and ignores the tail; the re-encode drops it."""
    payload = b"<?php system($_GET['c']); ?>" * 40
    fname, err = brand.ingest_asset("logo", _png() + payload)
    assert not err, err
    stored = open(os.path.join(settings.brand_dir(), fname), "rb").read()
    assert payload not in stored


def test_exif_is_stripped() -> None:
    fname, err = brand.ingest_asset("logo", _jpeg_with_exif())
    assert not err, err
    stored = open(os.path.join(settings.brand_dir(), fname), "rb").read()
    assert b"SECRET CAMERA NOTE" not in stored
    assert b"Copyright Somebody Else" not in stored


# ---- Refusals ---------------------------------------------------------------
def test_svg_is_refused() -> None:
    """Not sanitised — refused. Ava serves assets from its own origin and sets no
    Content-Security-Policy anywhere, so a served SVG runs its own <script> with
    the session cookie: stored XSS from a one-click upload."""
    svg = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" '
           b'width="64" height="64"><script>fetch("/api/hub/branding")</script></svg>')
    fname, err = brand.ingest_asset("logo", svg)
    assert not fname
    assert err
    assert not os.path.isdir(settings.brand_dir()) or not os.listdir(settings.brand_dir())


def test_a_zip_wearing_a_png_name_is_refused() -> None:
    """The filename is never trusted — only what the decoder says."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("payload.txt", "x" * 100)
    fname, err = brand.ingest_asset("logo", buf.getvalue())
    assert not fname and err


def test_an_oversize_file_is_refused_before_decoding() -> None:
    fname, err = brand.ingest_asset("logo", b"\x89PNG\r\n\x1a\n" + b"0" * brand.MAX_ASSET_BYTES)
    assert not fname
    assert "MB" in err


def test_a_decompression_bomb_is_refused() -> None:
    """A tiny file that decodes to an enormous bitmap. Pillow raises
    DecompressionBombError on load(), which is why ingest does verify() AND
    load() rather than only verify() — verify() alone would let this through."""
    from PIL import Image
    buf = io.BytesIO()
    # Well past Pillow's default bomb threshold, but a few KB on disk.
    Image.new("L", (24000, 24000)).save(buf, format="PNG")
    fname, err = brand.ingest_asset("logo", buf.getvalue())
    assert not fname
    assert err


def test_a_tiny_image_is_refused() -> None:
    fname, err = brand.ingest_asset("logo", _png(16, 16))
    assert not fname
    assert str(brand.MIN_PX) in err


def test_garbage_is_refused() -> None:
    fname, err = brand.ingest_asset("logo", b"this is not an image at all")
    assert not fname and err


@pytest.mark.parametrize("slot", ["../../etc/passwd", "nope", "", "derived"])
def test_an_unknown_slot_is_refused(slot: str) -> None:
    """The slot enum is the whole allowlist; nothing here reaches the filesystem."""
    fname, err = brand.ingest_asset(slot, _png())
    assert not fname and err


def test_the_stored_filename_is_content_addressed_and_has_no_path(tmp_path) -> None:
    fname, err = brand.ingest_asset("logo", _png())
    assert not err
    assert "/" not in fname and "\\" not in fname and ".." not in fname
    assert fname.startswith("logo-") and fname.endswith(".png")


# ---- The app icon is not brandable -----------------------------------------
# These replace five tests that pinned the opposite: that the icon set rendered
# from an `icon` upload, that it FELL BACK to the logo so one upload re-branded
# the home-screen tile, and that the maskable size padded to its safe zone. All
# of that machinery is gone. The tab and the home screen are Ava's on every
# install, so there is nothing left to render.
def test_the_icon_slot_cannot_be_uploaded_to(monkeypatch) -> None:
    """The upload path refuses the slot outright, so a client that still knows
    the old name cannot re-brand the tab by calling the API directly."""
    monkeypatch.setattr(settings, "_CFG", {}, raising=False)
    fname, err = brand.ingest_asset("icon", _png(512, 512))
    assert not fname and "unknown slot" in err, (
        "the 'icon' slot still accepts an upload — the browser tab is "
        "re-brandable again")


def test_uploading_a_logo_does_not_touch_the_icon_set(monkeypatch) -> None:
    """The regression that motivated this: `icon_source()` fell back to the logo,
    so an owner who only ever uploaded a logo silently changed the favicon and
    the home-screen tile too."""
    fname, err = brand.ingest_asset("logo", _png(512, 512))
    assert not err
    monkeypatch.setattr(settings, "_CFG", {"brand": {"logo": fname}}, raising=False)
    for gone in ("icon_source", "derived_icon", "DERIVED"):
        assert not hasattr(brand, gone), (
            f"brand.{gone} is back. The icon set must not be rendered from an "
            "owner upload — see SLOTS.")


def test_clearing_a_slot_removes_the_file_and_the_legacy_derived_cache(monkeypatch) -> None:
    """The `derived/` directory is legacy now, but an install upgrading INTO this
    version may still have one holding an owner-branded favicon. Touching
    branding must clear it, or a stale tab icon outlives the setting for it."""
    fname, err = brand.ingest_asset("logo", _png(512, 512))
    assert not err
    monkeypatch.setattr(settings, "_CFG", {"brand": {"logo": fname}}, raising=False)
    stale = os.path.join(settings.brand_dir(), "derived")
    os.makedirs(stale, exist_ok=True)
    with open(os.path.join(stale, "favicon.png"), "wb") as fh:
        fh.write(b"stale")
    brand.clear_asset("logo")
    assert not os.path.isfile(os.path.join(settings.brand_dir(), fname))
    assert not os.path.isdir(stale), "a legacy derived favicon survived"
