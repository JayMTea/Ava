"""Setup → Branding — make this install look like yours.

Backs exactly one panel (frontend/src/components/hub/panels/BrandingPanel.tsx),
the rule this package states in its __init__. Everything writable goes to
$AVA_HOME/ava.yaml through settings.save_patch; no source edits, ever.

**Nothing here is gated.** Re-branding your own install is the premise of a
self-hosted assistant, not an upsell, and `tests/test_no_capability_gate.py`
fails the build if any tracked file ever conditions a UI capability on a licence,
plan or subscription value. That guard exists so the promise is checkable by a
stranger rather than merely stated in a README.

`restart_required` is False, and that is engineered rather than hoped for — see
`save_branding`.
"""
from __future__ import annotations

from fastapi import APIRouter, UploadFile
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import audit, brand, brand_pack, config, features, settings

router = APIRouter()

# Text fields the POST accepts, with their caps. Colours and the two booleans are
# handled separately because they validate differently.
_TEXT_LIMITS = {"name": brand.NAME_MAX, "tagline": brand.TAGLINE_MAX}

_ENV_KEYS = {
    "name": "AVA_NAME", "tagline": "AVA_TAGLINE",
    "accent": "AVA_BRAND_ACCENT", "accent_light": "AVA_BRAND_ACCENT_LIGHT",
    "chrome": "AVA_BRAND_CHROME", "public": "AVA_BRAND_PUBLIC",
}


def _env_overrides() -> dict:
    """Which fields an env var is currently shadowing.

    The same honesty field PersonaPanel and SystemPanel already render, and the
    reason "I saved it and it reverted" has a visible cause instead of being a
    silent env-beats-yaml mystery. A control plane that pins a brand from outside
    the container shows up here, so the panel can say the field is managed rather
    than letting the save look like it worked.
    """
    return {k: v for k, v in
            ((k, settings.env_override(e)) for k, e in _ENV_KEYS.items()) if v}


def _state() -> dict:
    a, al = brand.accent(), settings.brand_accent_light()
    return {
        "name": brand.name(),
        "tagline": brand.tagline(),
        "accent": a,
        "accent_light": al,
        "chrome": brand.chrome(),
        "public": settings.brand_public(),
        "accessibility_check": settings.brand_a11y_check(),
        "branded": brand.is_branded(),
        # Four facts per slot, because the panel's toggle needs all four: what
        # is in force, whether that is the owner's or Ava's, whether there is a
        # parked image to toggle BACK to, and what Ava's own looks like. A bare
        # `set` could not distinguish "never uploaded" from "uploaded, currently
        # toggled off", and those render as different controls.
        "assets": {s: {"set": brand.asset_set(s),
                       "url": f"/brand/asset/{s}" if brand.asset_set(s) else None,
                       "stashed": bool(brand.stashed_name(s)),
                       "default_url": brand.default_asset(s)}
                   for s in brand.SLOTS},
        # Served from the backend so the panel never hardcodes a number the
        # backend enforces — the reason hub/persona.py returns `style_max` and
        # hub/system.py returns `retention_choices`.
        "defaults": {
            "name": "Ava",
            "tagline": "your private, self-hosted assistant",
            "accent": brand.DEFAULT_ACCENT,
            "accent_light": brand.DEFAULT_ACCENT_LIGHT,
            "chrome_dark": brand.DARK_BG,
            "chrome_light": brand.LIGHT_BG,
        },
        "limits": {
            "name_max": brand.NAME_MAX,
            "tagline_max": brand.TAGLINE_MAX,
            "slots": list(brand.SLOTS),
            "text_on_accent_min": brand.TEXT_ON_ACCENT_MIN,
            "accent_on_canvas_min": brand.ACCENT_ON_CANVAS_MIN,
        },
        "contrast": brand.check_accent(a, al),
        "env_overrides": _env_overrides(),
        # False = this install may not change its own brand. The panel
        # renders read-only rather than letting a save fail at the door.
        "editable": features.enabled("branding"),
    }


def _gate() -> JSONResponse | None:
    """Refuse a WRITE when the branding switch is off.

    Reads are untouched on purpose: an instance whose brand was pinned from
    outside still renders it, and the panel still shows what it is — it just
    cannot be changed here. `preflight` gives the refusal a regular
    `branding_off` code and a message naming where the switch lives, which is
    the convention every other capability follows.
    """
    pf = features.preflight("branding")
    if pf is None:
        return None
    code, msg = pf
    # preflight's stock message points at Setup → System, which is the right
    # advice when the owner turned this off themselves — and a wild goose chase
    # when an env var is holding it off, because env outranks ava.yaml and the
    # checkbox they are being sent to will not stick. Say which it is.
    if settings.env_override("AVA_BRANDING"):
        msg = ("Branding is managed outside this instance (AVA_BRANDING is set "
               "in its environment), so it cannot be changed from here. Whoever "
               "provisioned this instance controls it.")
    return JSONResponse({"ok": False, "error": msg, "error_code": code},
                        status_code=403)


async def _read_upload(f: UploadFile) -> bytes | None:
    """Read one upload with a hard cap (None = too large). Same shape as
    hub/voice.py:_read_clip — read the cap PLUS ONE so an oversize file is
    detected without buffering all of it."""
    data = await f.read(brand.MAX_ASSET_BYTES + 1)
    return None if len(data) > brand.MAX_ASSET_BYTES else data


@router.post("/branding/asset/{slot}")
async def upload_brand_asset(slot: str, file: UploadFile):
    """Store one branding image.

    Decoding runs in a worker thread: Pillow on the event loop blocks every
    other request for the duration, which media_api.py's own comment already
    warns about.
    """
    blocked = _gate()
    if blocked:
        return blocked
    if slot not in brand.SLOTS:
        return JSONResponse({"ok": False, "error": f"unknown slot '{slot}'"},
                            status_code=404)
    raw = await _read_upload(file)
    if raw is None:
        mb = brand.MAX_ASSET_BYTES // (1024 * 1024)
        return JSONResponse({"ok": False, "error": f"image must be under {mb} MB"},
                            status_code=413)

    fname, err = await run_in_threadpool(brand.ingest_asset, slot, raw)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=422)

    old = brand.asset_name(slot)
    # A slot holds ONE "yours". Uploading replaces whatever was parked by a
    # toggle as well as whatever was in force, so the parked name is cleared in
    # the same write and its file dropped below — otherwise toggling to Ava's
    # default, uploading a replacement, then toggling back would resurrect the
    # image the owner thought they had just overwritten.
    parked = brand.stashed_name(slot)
    try:
        settings.save_patch({"brand": {slot: fname, brand.stash_key(slot): ""}})
    except settings.ConfigParseError as e:
        return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                            status_code=409)
    # Remove the file the slot USED to point at, now that the new one is
    # committed to config. Ordering matters: unlinking first would leave the
    # slot pointing at nothing if the config write then failed.
    if old and old != fname:
        brand.discard_file(old, slot=slot, reason="superseded")
    if parked and parked not in (fname, old):
        brand.discard_file(parked, slot=slot, reason="superseded_stashed")
    audit.record("brand_asset", slot=slot, action="set", bytes=len(raw), file=fname)
    return {"ok": True, "slot": slot, "url": f"/brand/asset/{slot}",
            "restart_required": False}


@router.delete("/branding/asset/{slot}")
def clear_brand_asset(slot: str):
    """Delete a slot's image for good: both files AND both config keys.

    This is the destructive verb. The non-destructive one is
    `set_brand_asset_source` below, which is what the panel's toggle calls.
    """
    blocked = _gate()
    if blocked:
        return blocked
    if slot not in brand.SLOTS:
        return JSONResponse({"ok": False, "error": f"unknown slot '{slot}'"},
                            status_code=404)
    brand.clear_asset(slot)   # audits both deletions itself
    try:
        settings.save_patch({"brand": {slot: "", brand.stash_key(slot): ""}})
    except settings.ConfigParseError as e:
        return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                            status_code=409)
    return {"ok": True, "slot": slot, "restart_required": False}


@router.post("/branding/asset/{slot}/source")
def set_brand_asset_source(slot: str, body: dict):
    """Toggle one slot between Ava's shipped default and the owner's own image.

    Deliberately NOT a delete. `default` parks the active filename in
    `brand.<slot>_stashed` and leaves the file where it is; `custom` puts it
    back. That is what makes the control in the panel a toggle the owner can
    flip both ways rather than a remove-and-re-upload wearing a switch's
    clothing.

    Idempotent in both directions: asking for the state you are already in is a
    no-op that still returns ok, so a double-click or a stale panel cannot
    destroy anything or 400.
    """
    blocked = _gate()
    if blocked:
        return blocked
    if slot not in brand.SLOTS:
        return JSONResponse({"ok": False, "error": f"unknown slot '{slot}'"},
                            status_code=404)
    source = str(body.get("source") or "").strip()
    if source not in ("default", "custom"):
        return JSONResponse({"ok": False, "error": "source must be 'default' or 'custom'"},
                            status_code=400)

    patch = brand.stash_patch(slot) if source == "default" else brand.restore_patch(slot)
    if patch:
        try:
            settings.save_patch({"brand": patch})
        except settings.ConfigParseError as e:
            return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                                status_code=409)
        # No icon cache to invalidate any more: the favicon and every PWA size
        # are Ava's shipped files, so a slot toggle cannot change them.
        audit.record("brand_asset", slot=slot, action=f"source:{source}")

    return {"ok": True, "slot": slot, "source": source,
            "set": brand.asset_set(slot), "stashed": bool(brand.stashed_name(slot)),
            "url": f"/brand/asset/{slot}" if brand.asset_set(slot) else None,
            "restart_required": False}


@router.get("/branding/export")
def export_branding():
    """This install's brand as a portable `ava-brand/1` pack."""
    data = brand_pack.export_pack()
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="brand.zip"'})


@router.post("/branding/import")
async def import_branding(file: UploadFile):
    """Apply a brand pack.

    The pack is the least trusted input in the system — it arrives over a wire
    from someone who is not the owner — so `brand_pack` allowlists its keys
    rather than merging its document, re-runs every image through the same
    validator a manual upload uses, and hardens the zip itself. See that
    module's docstring for the full argument.
    """
    blocked = _gate()
    if blocked:
        return blocked
    raw = await file.read(brand_pack.MAX_TOTAL_UNCOMPRESSED + 1)
    if len(raw) > brand_pack.MAX_TOTAL_UNCOMPRESSED:
        return JSONResponse({"ok": False, "error": "that file is too large to be a brand pack"},
                            status_code=413)
    try:
        result = await run_in_threadpool(
            brand_pack.import_pack, raw,
            enforce_contrast=settings.brand_a11y_check())
    except brand_pack.PackError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
    except settings.ConfigParseError as e:
        return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                            status_code=409)
    config.AVA_NAME = brand.name()
    config.AVA_TAGLINE = brand.tagline()
    return {**result, "restart_required": False}


@router.get("/branding")
def get_branding():
    try:
        return _state()
    except settings.ConfigParseError as e:
        return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                            status_code=409)


@router.post("/branding")
def save_branding(body: dict):
    """Patch any subset of the brand.

    Keys are PRESENCE-checked, never truthiness-checked. `""` is a legitimate
    value here — it is how "clear this and go back to Ava's default" is spelled,
    and it is what the panel's Reset button posts for every field. `if
    body.get(k)` would silently refuse exactly that, which is the bug
    hub/persona.py's own comment warns about.
    """
    blocked = _gate()
    if blocked:
        return blocked

    patch: dict = {}

    for key, cap in _TEXT_LIMITS.items():
        if key not in body:
            continue
        val = str(body.get(key) or "").strip()
        if len(val) > cap:
            return JSONResponse(
                {"ok": False, "error": f"{key} must be {cap} characters or fewer"},
                status_code=400)
        # A name of only whitespace would render an empty header and an empty
        # PWA label, which reads as a broken install rather than a default.
        patch[key] = val

    # Colours: validated BEFORE anything is written, the order
    # hub/connectors.py:set_connector_appearance uses, so a bad value can never
    # land in YAML and be served back on the next GET.
    proposed_accent = (str(body.get("accent", brand.accent()) or "")).strip()
    proposed_light = (str(body.get("accent_light",
                                   settings.brand_accent_light()) or "")).strip()
    for key, val in (("accent", proposed_accent), ("accent_light", proposed_light),
                     ("chrome", str(body.get("chrome", "") or "").strip())):
        if key not in body:
            continue
        if val and not brand.normalize_hex(val):
            return JSONResponse({"ok": False, "error": f"{key} must be a #rrggbb colour"},
                                status_code=400)
        patch[key] = brand.normalize_hex(val) if val else ""

    contrast = brand.check_accent(proposed_accent, proposed_light)
    if not contrast["ok"] and settings.brand_a11y_check():
        return JSONResponse(
            {"ok": False,
             "error": "; ".join(contrast["blocking"]),
             "error_code": "brand_contrast",
             "contrast": contrast},
            status_code=400)

    for key in ("public", "accessibility_check"):
        if key in body:
            patch[key] = bool(body.get(key))

    if not patch:
        return JSONResponse({"ok": False, "error": "nothing to update"}, status_code=400)

    name_changed = "name" in patch and patch["name"] != brand.name()
    try:
        settings.save_patch({"brand": patch})
    except settings.ConfigParseError as e:
        return JSONResponse({"error": str(e), "error_code": "config_unparseable"},
                            status_code=409)

    # Make it true rather than hoping. `settings.save_patch` rebinds the live
    # config, which covers every `settings.brand_*()` reader — but ava_bridge/
    # config.py bound AVA_NAME/AVA_TAGLINE as module constants at import, and
    # four RUNTIME prompt builders read those (turns.py, coder.py, code_agent.py,
    # runtime/direct.py). Without this line the assistant would keep calling
    # itself by the old name on the very next turn while the UI showed the new
    # one. Same move hub/system.py makes for config.CODE_APPROVAL, for the same
    # reason and with the same comment.
    config.AVA_NAME = brand.name()
    config.AVA_TAGLINE = brand.tagline()

    audit.record("brand_change", fields=sorted(patch.keys()))

    return {
        "ok": True,
        # Everything that renders the brand now reads it live: /api/brand, the
        # request-time pre-auth render, the request-time PWA manifest, and the
        # prompt builders above.
        "restart_required": False,
        # The one thing a save cannot reach. agent/render_persona.py bakes the
        # name into the sandbox system prompt at provision time, so the agent
        # keeps its old self-reference until re-provisioned. Colours and logos
        # never touch the agent, so this is False unless the NAME moved.
        "reprovision_required": name_changed,
        "contrast": contrast,
        "env_overrides": _env_overrides(),
        # False = this install may not change its own brand. The panel
        # renders read-only rather than letting a save fail at the door.
        "editable": features.enabled("branding"),
    }
