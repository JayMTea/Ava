"""`ava-brand/1` import — the refusals.

This file is the security contract for anything that PRODUCES a pack, including
the hosted generator. If a change here makes a fixture pass that used to fail,
the generator's output has stopped being safe to load.

The pack is the least trusted input in the whole app: unlike an upload, which at
least came from someone sitting at the machine, a pack arrives over a wire from
a third party. Every test below is a thing a hostile pack would actually try.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest
import yaml

from ava_bridge import brand, brand_pack, settings


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_CFG", {}, raising=False)
    monkeypatch.setenv("AVA_BRAND_DIR", str(tmp_path / "branding"))
    # save_patch would write a real ava.yaml; capture it instead so these tests
    # assert on the PATCH, which is the thing the allowlist is about.
    saved: list[dict] = []
    monkeypatch.setattr(settings, "save_patch", lambda patch: saved.append(patch) or patch)
    return saved


def _png(w=256, h=256, color=(11, 114, 133)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _pack(*, brand_block=None, files=None, fmt=brand_pack.FORMAT,
          manifest_files=None, extra=None) -> bytes:
    """Build a pack. Every knob exists so a test can break exactly one rule."""
    files = files if files is not None else {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if brand_block is not None:
            z.writestr("brand.yaml", yaml.safe_dump({"brand": brand_block}))
        entries = []
        for arc, raw in files.items():
            z.writestr(arc, raw)
            entries.append({"name": arc, "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
        for name, raw in (extra or {}).items():
            z.writestr(name, raw)
        z.writestr("MANIFEST.json", json.dumps({
            "format": fmt, "created": 0, "generator": "test",
            "files": manifest_files if manifest_files is not None else entries,
        }))
    return buf.getvalue()


# ---- THE rule: a pack cannot reach outside `brand.*` ------------------------
def test_a_pack_cannot_set_server_settings(_home) -> None:
    """The single most important assertion in the feature.

    A pack that could set `server.trusted_hosts` would turn a logo file into
    remote code execution. The loader ALLOWLISTS keys instead of merging the
    document, so these are not refused — they are never seen.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("brand.yaml", yaml.safe_dump({
            "brand": {"name": "Northwind", "accent": "#2f7d4f"},
            "server": {"trusted_hosts": ["*"], "host": "0.0.0.0"},
            "code": {"approval": "none"},
            "paths": {"data": "/etc"},
        }))
        z.writestr("MANIFEST.json", json.dumps(
            {"format": brand_pack.FORMAT, "created": 0, "files": []}))
    brand_pack.import_pack(buf.getvalue())

    assert len(_home) == 1
    patch = _home[0]
    assert set(patch) == {"brand"}, f"a pack escaped the brand block: {patch}"
    assert patch["brand"]["name"] == "Northwind"
    for forbidden in ("server", "code", "paths", "inference", "features"):
        assert forbidden not in patch


def test_unknown_brand_keys_are_ignored_not_applied(_home) -> None:
    """Ignored rather than refused: an unknown key is far more likely to be a
    newer generator than an attack, and refusing the pack for it would make the
    format unextendable. It still must not reach the patch."""
    brand_pack.import_pack(_pack(brand_block={
        "name": "Northwind", "accent": "#2f7d4f",
        "logo": "../../etc/passwd", "font": "evil.ttf", "public": False,
    }))
    written = _home[0]["brand"]
    assert written.get("name") == "Northwind"
    assert "logo" not in written, "a pack set an ASSET FILENAME directly"
    assert "font" not in written and "public" not in written


# ---- Zip hardening ----------------------------------------------------------
def test_a_traversing_entry_name_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../etc/cron.d/x", "* * * * * root sh\n")
        z.writestr("MANIFEST.json", json.dumps({"format": brand_pack.FORMAT, "files": []}))
    with pytest.raises(brand_pack.PackError, match="unsafe entry name"):
        brand_pack.import_pack(buf.getvalue())


def test_an_absolute_entry_name_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("/etc/passwd", "root:x:0:0\n")
        z.writestr("MANIFEST.json", json.dumps({"format": brand_pack.FORMAT, "files": []}))
    with pytest.raises(brand_pack.PackError, match="unsafe entry name"):
        brand_pack.import_pack(buf.getvalue())


def test_a_zip_bomb_is_refused() -> None:
    """A few KB that expands to megabytes of zeroes. zipfile will happily do it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("logo.png", b"\0" * (5 * 1024 * 1024))
        z.writestr("MANIFEST.json", json.dumps({"format": brand_pack.FORMAT, "files": []}))
    with pytest.raises(brand_pack.PackError, match="bomb|larger than"):
        brand_pack.import_pack(buf.getvalue())


def test_too_many_entries_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(brand_pack.MAX_ENTRIES + 5):
            z.writestr(f"f{i}.txt", b"x")
        z.writestr("MANIFEST.json", json.dumps({"format": brand_pack.FORMAT, "files": []}))
    with pytest.raises(brand_pack.PackError, match="entries"):
        brand_pack.import_pack(buf.getvalue())


def test_a_non_zip_is_refused() -> None:
    with pytest.raises(brand_pack.PackError, match="not a zip"):
        brand_pack.import_pack(b"I am definitely a brand pack, promise")


# ---- Provenance -------------------------------------------------------------
def test_a_missing_manifest_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("brand.yaml", yaml.safe_dump({"brand": {"name": "X"}}))
    with pytest.raises(brand_pack.PackError, match="MANIFEST"):
        brand_pack.import_pack(buf.getvalue())


def test_an_unknown_format_version_is_refused_by_name() -> None:
    """Naming the version is the point. Silently ignoring an unknown one is how
    a format stops being versionable."""
    with pytest.raises(brand_pack.PackError, match="ava-brand/99"):
        brand_pack.import_pack(_pack(brand_block={"name": "X"}, fmt="ava-brand/99"))


def test_a_checksum_mismatch_is_a_hard_refusal() -> None:
    """Not a warning. The pack came over a wire."""
    raw = _png()
    bad = [{"name": "logo.png", "bytes": len(raw), "sha256": "00" * 32}]
    with pytest.raises(brand_pack.PackError, match="checksum"):
        brand_pack.import_pack(_pack(brand_block={"name": "X"},
                                     files={"logo.png": raw}, manifest_files=bad))


def test_an_image_absent_from_the_manifest_is_refused() -> None:
    with pytest.raises(brand_pack.PackError, match="not in its MANIFEST"):
        brand_pack.import_pack(_pack(brand_block={"name": "X"},
                                     files={"logo.png": _png()}, manifest_files=[]))


# ---- Images go through the SAME validator ----------------------------------
def test_an_svg_renamed_to_png_is_refused() -> None:
    """There is deliberately no "trusted because it came from a pack" path."""
    svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">'
           b'<script>fetch("/api/hub/branding")</script></svg>')
    with pytest.raises(brand_pack.PackError, match="not a readable PNG"):
        brand_pack.import_pack(_pack(brand_block={"name": "X"}, files={"logo.png": svg}))


def test_a_pixel_bomb_in_a_pack_is_refused() -> None:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("L", (24000, 24000)).save(buf, format="PNG")
    with pytest.raises(brand_pack.PackError):
        brand_pack.import_pack(_pack(brand_block={"name": "X"},
                                     files={"logo.png": buf.getvalue()}))


def test_an_unreadable_accent_in_a_pack_is_refused() -> None:
    with pytest.raises(brand_pack.PackError, match="not readable"):
        brand_pack.import_pack(_pack(brand_block={"name": "X", "accent": "#ffd93d"}))


def test_a_bad_colour_in_a_pack_is_refused() -> None:
    with pytest.raises(brand_pack.PackError, match="#rrggbb"):
        brand_pack.import_pack(_pack(brand_block={"accent": "chartreuse"}))


def test_a_pack_that_sets_nothing_is_refused() -> None:
    with pytest.raises(brand_pack.PackError, match="sets nothing"):
        brand_pack.import_pack(_pack(brand_block={}))


# ---- Atomicity --------------------------------------------------------------
def test_a_pack_that_fails_midway_writes_no_config(_home) -> None:
    """Validate everything, THEN commit. A pack failing on a later image must not
    leave part of a brand applied."""
    good, svg = _png(), b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    with pytest.raises(brand_pack.PackError):
        brand_pack.import_pack(_pack(brand_block={"name": "X", "accent": "#2f7d4f"},
                                     files={"logo.png": good, "wordmark.png": svg}))
    assert _home == [], "config was written despite a failed import"


def test_a_pack_exported_before_the_icon_slot_was_removed_still_imports(_home) -> None:
    """Upgrade path. Packs in the wild carry an `icon.png`; the slot no longer
    exists, so it is ignored rather than refused — the rest of the brand applies
    and the tab icon stays Ava's, which is the whole point."""
    good = _png()
    brand_pack.import_pack(_pack(brand_block={"name": "X", "accent": "#2f7d4f"},
                                 files={"logo.png": good, "icon.png": good}))
    assert _home, "a pack carrying a legacy icon.png was rejected outright"
    written = _home[-1]["brand"]
    assert written.get("name") == "X"
    assert "icon" not in written, "the legacy icon slot was written to config"


# ---- Round trip -------------------------------------------------------------
def test_export_import_round_trip(tmp_path, monkeypatch) -> None:
    fname, err = brand.ingest_asset("logo", _png(300, 300, color=(47, 125, 79)))
    assert not err, err
    monkeypatch.setattr(settings, "_CFG", {"brand": {
        "name": "Northwind", "tagline": "the shop assistant",
        "accent": "#2f7d4f", "logo": fname}}, raising=False)

    data = brand_pack.export_pack()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        man = json.loads(z.read("MANIFEST.json"))
        assert man["format"] == brand_pack.FORMAT
        assert {f["name"] for f in man["files"]} == {"logo.png"}
        doc = yaml.safe_load(z.read("brand.yaml"))
        assert doc["brand"]["name"] == "Northwind"
        assert doc["brand"]["accent"] == "#2f7d4f"
        # A pack must never carry anything but the brand.
        assert set(doc) == {"brand"}

    saved: list[dict] = []
    monkeypatch.setattr(settings, "save_patch", lambda p: saved.append(p) or p)
    monkeypatch.setattr(settings, "_CFG", {}, raising=False)
    brand_pack.import_pack(data)
    applied = saved[0]["brand"]
    assert applied["name"] == "Northwind"
    assert applied["accent"] == "#2f7d4f"
    assert applied["logo"].startswith("logo-")


def test_an_exported_pack_carries_no_secrets(tmp_path, monkeypatch) -> None:
    """The export builds its block field by field rather than copying config, so
    there is no path by which a password or a key could ride along."""
    monkeypatch.setattr(settings, "_CFG", {
        "brand": {"name": "Northwind", "accent": "#2f7d4f"},
        "server": {"password": "hunter2"},
        "inference": {"api_key": "sk-live-secret"},
    }, raising=False)
    data = brand_pack.export_pack()
    assert b"hunter2" not in data
    assert b"sk-live-secret" not in data
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert set(yaml.safe_load(z.read("brand.yaml"))) == {"brand"}
