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


# ---- Derived icons ----------------------------------------------------------
def test_derived_icons_render_at_the_right_sizes(monkeypatch) -> None:
    from PIL import Image
    fname, err = brand.ingest_asset("icon", _png(512, 512))
    assert not err, err
    monkeypatch.setattr(settings, "_CFG", {"brand": {"icon": fname}}, raising=False)
    for name, size in brand.DERIVED.items():
        path = brand.derived_icon(name)
        assert path, f"{name} did not render"
        with Image.open(path) as im:
            assert im.size == (size, size), name


def test_the_maskable_icon_pads_to_its_safe_zone(monkeypatch) -> None:
    """A launcher crops a maskable icon to a circle, so artwork that runs to the
    edge loses its edges. The corner pixel must therefore be the padding, not
    the artwork."""
    from PIL import Image
    fname, err = brand.ingest_asset("icon", _png(512, 512, color=(255, 0, 0)))
    assert not err
    monkeypatch.setattr(settings, "_CFG",
                        {"brand": {"icon": fname, "chrome": "#123456"}}, raising=False)
    with Image.open(brand.derived_icon("pwa-maskable-512")) as im:
        assert im.convert("RGB").getpixel((2, 2)) == (0x12, 0x34, 0x56)
        assert im.convert("RGB").getpixel((256, 256)) == (255, 0, 0)


def test_the_icon_set_falls_back_to_the_logo(monkeypatch) -> None:
    """One upload should be enough to fix the home-screen tile — the thing people
    forget and then notice on their phone a week later."""
    fname, err = brand.ingest_asset("logo", _png(512, 512))
    assert not err
    monkeypatch.setattr(settings, "_CFG", {"brand": {"logo": fname}}, raising=False)
    assert brand.icon_source() == fname
    assert brand.derived_icon("pwa-192")


def test_no_source_means_no_derived_icons(monkeypatch) -> None:
    monkeypatch.setattr(settings, "_CFG", {}, raising=False)
    assert brand.icon_source() == ""
    assert brand.derived_icon("pwa-192") == ""


def test_clearing_a_slot_removes_the_file_and_the_derived_cache(monkeypatch) -> None:
    fname, err = brand.ingest_asset("icon", _png(512, 512))
    assert not err
    monkeypatch.setattr(settings, "_CFG", {"brand": {"icon": fname}}, raising=False)
    assert brand.derived_icon("pwa-192")
    assert os.path.isdir(os.path.join(settings.brand_dir(), "derived"))
    brand.clear_asset("icon")
    assert not os.path.isfile(os.path.join(settings.brand_dir(), fname))
    assert not os.path.isdir(os.path.join(settings.brand_dir(), "derived"))
