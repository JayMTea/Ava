"""`ava-brand/1` — a portable brand, in one file.

A pack is a zip holding a `brand.yaml`, the images it names, and a MANIFEST that
checksums both. It exists so a brand can be produced somewhere other than this
box — by hand, by a generator, by a designer — and dropped into an install
without anyone hand-editing config or uploading five files one at a time.

**This module is the only place untrusted third-party content reaches ava.yaml,
and that shapes every decision in it.** A pack arrives over a wire from someone
who is not the owner. The rules, in the order they matter:

1. **The brand.yaml is key-ALLOWLISTED, never merged.** `settings.save_patch()`
   on a parsed pack document would let a logo file set
   `server.trusted_hosts: ["*"]`, or point
   `paths.data` at someone else's directory. That is remote code execution
   wearing a brand, and the allowlist is the single most important line here.
2. **Every image re-runs `brand.ingest_asset()`** — the same validator, the same
   Pillow re-encode, as a manual upload. There is deliberately no
   "trusted because it came from a pack" path, because a pack is the LEAST
   trusted source in the system.
3. **The zip is hardened by hand**, because `zipfile` does none of it: entry
   names, entry count, per-entry and total uncompressed size, and compression
   ratio are all checked before anything is read.
4. **Checksums are verified before ingest**, and a mismatch is a hard refusal
   rather than a warning. The pack came over a wire.
5. **Validate everything, then commit.** A pack that fails on its fourth image
   must not leave three swapped.

**Signature verification is deliberately absent, and must stay absent.** Packs
may be signed for PROVENANCE — so a future catalogue can say where one came from
— but the loader accepts any well-formed pack, unsigned included. The moment the
app verifies a signature to decide whether to load, there is a gate in it, and a
gate is exactly what tests/test_no_capability_gate.py exists to keep out of a
self-hosted app the owner is running on their own hardware.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import zipfile

from . import audit, brand, settings

FORMAT = "ava-brand/1"

# Zip limits. zipfile enforces none of these.
MAX_ENTRIES = 16
MAX_TOTAL_UNCOMPRESSED = 12 * 1024 * 1024
MAX_RATIO = 200          # compressed:uncompressed — beyond this it is a bomb

# The ONLY keys a pack may set. Everything else in its brand.yaml is ignored —
# silently, because a pack naming a key we do not know is far more likely to be
# a newer generator than an attack, and refusing the whole pack for it would
# make the format unextendable. What is NOT ignorable is a key outside `brand.*`
# entirely; those never reach the patch because this list is an allowlist rather
# than a denylist.
ALLOWED_KEYS = ("name", "tagline", "accent", "accent_light", "chrome")

# Slot -> filename inside the pack.
PACK_FILES = {s: f"{s}.png" for s in brand.SLOTS}


class PackError(ValueError):
    """A pack that cannot be trusted or cannot be read. The message is shown to
    the owner, so it says what is wrong rather than which line raised."""


# ---- Export ----------------------------------------------------------------
def export_pack() -> bytes:
    """This install's brand, as a pack. Colours and images only."""
    import yaml

    d = settings.brand_dir()
    files: list[dict] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        block = {k: v for k, v in (
            ("name", brand.name()),
            ("tagline", brand.tagline()),
            ("accent", brand.accent()),
            ("accent_light", settings.brand_accent_light()),
            ("chrome", brand.chrome()),
        ) if v}
        z.writestr("brand.yaml", yaml.safe_dump({"brand": block}, sort_keys=False))

        for slot, arc in PACK_FILES.items():
            fname = brand.asset_name(slot)
            if not fname:
                continue
            path = os.path.join(d, fname)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                raw = fh.read()
            z.writestr(arc, raw)
            entry = {"name": arc, "slot": slot, "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest(), "type": "image/png"}
            try:
                from PIL import Image
                with Image.open(io.BytesIO(raw)) as im:
                    entry["w"], entry["h"] = im.size
            except Exception:  # noqa: BLE001 — dimensions are informational
                pass
            files.append(entry)

        z.writestr("MANIFEST.json", json.dumps({
            "format": FORMAT,
            "created": time.time(),
            "generator": "ava",
            "name": brand.name(),
            "files": files,
            "note": ("Colours and images only. A brand pack never carries "
                     "credentials, paths or server settings."),
        }, indent=2))

    data = buf.getvalue()
    audit.record("brand_export", bytes=len(data), files=len(files))
    return data


# ---- Import ----------------------------------------------------------------
def _safe_entries(z: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES:
        raise PackError(f"pack has {len(infos)} entries (max {MAX_ENTRIES})")
    total = 0
    for i in infos:
        n = i.filename
        # A zip may contain any string as a name, including an absolute path or
        # one that walks upward. Nothing here is ever joined onto a directory —
        # entries are read by name and written through ingest_asset — but the
        # check stays because the next person to touch this may not know that.
        if n.startswith("/") or ".." in n.replace("\\", "/").split("/") or "\\" in n:
            raise PackError(f"unsafe entry name in pack: {n!r}")
        if i.is_dir():
            continue
        if i.file_size > brand.MAX_ASSET_BYTES:
            raise PackError(f"{n} is larger than "
                            f"{brand.MAX_ASSET_BYTES // (1024 * 1024)} MB uncompressed")
        if i.compress_size and i.file_size / max(i.compress_size, 1) > MAX_RATIO:
            raise PackError(f"{n} decompresses {i.file_size // max(i.compress_size, 1)}x — "
                            "refusing a compression bomb")
        total += i.file_size
    if total > MAX_TOTAL_UNCOMPRESSED:
        raise PackError(f"pack expands to {total // (1024 * 1024)} MB "
                        f"(max {MAX_TOTAL_UNCOMPRESSED // (1024 * 1024)} MB)")
    return infos


def _read_manifest(z: zipfile.ZipFile) -> dict:
    try:
        man = json.loads(z.read("MANIFEST.json"))
    except KeyError:
        raise PackError("not a brand pack — MANIFEST.json is missing") from None
    except (ValueError, OSError):
        raise PackError("MANIFEST.json is not readable JSON") from None
    if not isinstance(man, dict):
        raise PackError("MANIFEST.json is not an object")
    fmt = man.get("format")
    if fmt != FORMAT:
        # Naming the version is the point: silently ignoring an unknown one is
        # how a format stops being versionable.
        raise PackError(f"unsupported pack format {fmt!r} — this Ava reads {FORMAT}")
    return man


def _brand_block(z: zipfile.ZipFile) -> dict:
    """The allowlisted brand values. See rule 1 in the module docstring."""
    import yaml
    try:
        raw = z.read("brand.yaml")
    except KeyError:
        return {}
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        raise PackError("brand.yaml in the pack does not parse") from None
    if not isinstance(doc, dict):
        raise PackError("brand.yaml is not a mapping")
    block = doc.get("brand")
    if not isinstance(block, dict):
        return {}

    out: dict = {}
    for key in ALLOWED_KEYS:
        if key not in block:
            continue
        val = str(block.get(key) or "").strip()
        if key in ("accent", "accent_light", "chrome"):
            if val and not brand.normalize_hex(val):
                raise PackError(f"{key} in the pack is not a #rrggbb colour")
            out[key] = brand.normalize_hex(val) if val else ""
        else:
            cap = brand.NAME_MAX if key == "name" else brand.TAGLINE_MAX
            out[key] = val[:cap]
    return out


def import_pack(data: bytes, *, enforce_contrast: bool = True) -> dict:
    """Validate a pack completely, then apply it. Raises PackError on refusal."""
    if len(data) > MAX_TOTAL_UNCOMPRESSED:
        raise PackError("that file is too large to be a brand pack")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackError("that file is not a zip archive") from None

    with z:
        _safe_entries(z)
        man = _read_manifest(z)
        block = _brand_block(z)

        if enforce_contrast:
            check = brand.check_accent(block.get("accent", ""),
                                       block.get("accent_light", ""))
            if not check["ok"]:
                raise PackError("this pack's accent is not readable: "
                                + "; ".join(check["blocking"]))

        # Checksums BEFORE ingest. The pack came over a wire.
        declared = {f.get("name"): f.get("sha256") for f in man.get("files", [])
                    if isinstance(f, dict)}
        names = {i.filename for i in z.infolist()}
        staged: dict[str, bytes] = {}
        for slot, arc in PACK_FILES.items():
            if arc not in names:
                continue
            raw = z.read(arc)
            want = declared.get(arc)
            if not want:
                raise PackError(f"{arc} is in the pack but not in its MANIFEST")
            if hashlib.sha256(raw).hexdigest() != want:
                raise PackError(f"{arc} does not match its MANIFEST checksum")
            staged[slot] = raw

    # Ingest everything into the store BEFORE touching config, so a pack that
    # fails on its fourth image leaves the previous brand intact rather than
    # three-fifths replaced. ingest_asset is content-addressed, so writing a file
    # that is then abandoned costs a stray blob, not a corrupted slot.
    written: dict[str, str] = {}
    for slot, raw in staged.items():
        fname, err = brand.ingest_asset(slot, raw)
        if err:
            raise PackError(f"{PACK_FILES[slot]}: {err}")
        written[slot] = fname

    patch = dict(block)
    patch.update(written)
    if not patch:
        raise PackError("this pack sets nothing — no colours and no images")

    previous = {s: brand.asset_name(s) for s in written}
    settings.save_patch({"brand": patch})
    for slot, old in previous.items():
        if old and old != written.get(slot):
            brand.discard_file(old, slot=slot, reason="replaced by brand pack")

    audit.record("brand_import", generator=str(man.get("generator", ""))[:64],
                 pack_name=str(man.get("name", ""))[:64],
                 fields=sorted(block.keys()), assets=sorted(written.keys()))
    return {"ok": True, "applied": sorted(patch.keys()),
            "assets": sorted(written.keys())}
