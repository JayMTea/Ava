"""Hub API — cookie-gated `/api/hub/*` routes powering the setup & control portal.

Thin wrappers over existing machinery (runtime status/provision, the connector
generators, settings.save_patch). Mounted into the bridge, so `auth_gate` covers
these automatically (they are NOT in auth._PUBLIC_PATHS — no middleware change).
Everything writable is written to $AVA_HOME/ava.yaml via settings.save_patch;
no source edits, ever. Model configuration reuses the proven /api/setup/* routes
(setup_wizard.py); this module adds the agent, connector, and system surfaces.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import audit, config, features, runtime, settings
from .version import __version__

router = APIRouter(prefix="/api/hub")

# Panels live in ava_bridge/hub/, one module per Setup tab. They expose
# prefix-less routers so the /api/hub prefix is declared exactly once, here.
from .hub import connectors as _hub_connectors  # noqa: E402
from .hub import models as _hub_models  # noqa: E402
router.include_router(_hub_connectors.router)
router.include_router(_hub_models.router)


# --------------------------------------------------------------------------- #
# Flight recorder — the durable append-only audit ledger
# --------------------------------------------------------------------------- #
@router.get("/audit")
def audit_log(limit: int = 200, kind: str = ""):
    """Recent audit events (newest first): agent turns + self-edit outcomes,
    from $AVA_HOME/logs/audit.jsonl — survives restarts, unlike the ops views."""
    limit = max(1, min(int(limit), 1000))
    return {"events": audit.tail(limit, kind=kind or None)}


# --------------------------------------------------------------------------- #
# Approvals — the agent parked a sensitive connector action; the operator OKs it
# --------------------------------------------------------------------------- #
@router.get("/approvals")
def approvals_list():
    from . import approvals
    return {"pending": approvals.pending()}


@router.post("/approvals/{aid}")
def approvals_decide(aid: str, decision: str = "approve"):
    """decision: approve (once) | always (approve + durable grant) | deny."""
    from . import approvals
    return {"ok": approvals.decide(aid, decision != "deny",
                                   remember=decision == "always")}










# --------------------------------------------------------------------------- #
# System — brand, version, governance mode, feature flags
# --------------------------------------------------------------------------- #
@router.get("/system")
def system():
    voiceprint = any(
        os.path.exists(os.path.join(base, "models", "voiceprint.npy"))
        for base in (settings.AVA_HOME, settings.CODE_ROOT))
    return {
        "brand": settings.brand_name(),
        "version": __version__,
        "code_approval": config.CODE_APPROVAL,
        "learning_enabled": config.LEARNING_ENABLED,
        "learning_interval_h": config.LEARNING_INTERVAL_H,
        # Legacy per-feature booleans (existing consumers) + the registry
        # snapshot the Optional-features panel renders from — one source
        # (features.REGISTRY), so a new capability appears here automatically.
        "voice": features.enabled("voice"),
        "voiceprint": voiceprint,
        "web_search": features.enabled("web_search"),
        "image": features.enabled("image"),
        "features": features.snapshot(),
        # "" when ava.yaml is fine. When it is not, EVERY setting on this page
        # is silently showing its default and no save will be accepted, so the
        # UI has to say so rather than letting the owner toggle things that
        # cannot persist. connectors/skills surface their load errors the same
        # way — see connectors.load_errors().
        "config_error": settings.load_error(),
        "config_path": str(settings.CONFIG_PATH),
        "docker": bool(__import__("shutil").which("docker")),
        "retention_days": settings.data_retention_days(),
        "retention_choices": list(settings.DATA_RETENTION_CHOICES),
        # Editable keys currently shadowed by env vars: a yaml write from the
        # UI "works" but the env value wins again on the next boot. Surfacing
        # the active overrides is the only way the UI can say WHY.
        "env_overrides": {k: v for k, v in {
            "code_approval": settings.env_override("AVA_CODE_APPROVAL"),
            "retention_days": settings.env_override("AVA_DATA_RETENTION_DAYS"),
            "voice": settings.env_override("AVA_VOICE"),
            "voice_threshold": settings.env_override("AVA_PHONE_THRESHOLD"),
            "agent_enabled": settings.env_override("AVA_AGENT_ENABLED"),
            "learning": settings.env_override("AVA_LEARNING"),
        }.items() if v},
    }


@router.get("/cost")
def cost_get():
    """Current electricity rate, currency, and spend/energy budgets + live
    daily totals (for the Setup hub Budgets editor + the Vitals budget bar)."""
    from . import dashboard
    settings_ = dashboard.cost_settings()
    day = dashboard.perf_cost("1d")
    settings_["daily_spend_usd"] = day["spend_usd"]
    settings_["daily_energy_kwh"] = day["energy_kwh"]
    settings_["power_measured"] = day["power_measured"]
    return settings_


@router.post("/cost")
async def cost_set(request: Request):
    """Persist cost/budget settings to ava.yaml (cost.*) — no source edits."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    patch: dict = {}
    if "electricity_rate_per_kwh" in body:
        try:
            patch["electricity_rate_per_kwh"] = max(0.0, float(body["electricity_rate_per_kwh"]))
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "rate must be a number"}, status_code=400)
    if body.get("currency"):
        patch["currency"] = str(body["currency"])[:3]
    if isinstance(body.get("budgets"), dict):
        b = {}
        for k in ("daily_usd", "monthly_usd", "daily_kwh"):
            v = body["budgets"].get(k)
            if v in (None, "", 0):
                b[k] = None                       # clear the budget
            else:
                try:
                    b[k] = round(max(0.0, float(v)), 2)
                except (TypeError, ValueError):
                    return JSONResponse({"ok": False, "error": f"{k} must be a number"},
                                        status_code=400)
        patch["budgets"] = b
    if not patch:
        return JSONResponse({"ok": False, "error": "nothing to set"}, status_code=400)
    try:
        settings.save_patch({"cost": patch})
        from . import dashboard
        dashboard.invalidate_cost_cache()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True}


@router.post("/system/approval")
def set_approval(mode: str):
    """Set code.approval (all|policy|none) for Ava's self-editing agent.

    Applies LIVE: code_agent reads config.CODE_APPROVAL at call time, so updating
    it here gates the very next code-change request without a restart (the setting
    is also persisted to ava.yaml so it survives one)."""
    mode = (mode or "").strip().lower()
    if mode not in ("all", "policy", "none"):
        return JSONResponse({"ok": False, "error": "mode must be all|policy|none"},
                            status_code=400)
    try:
        settings.save_patch({"code": {"approval": mode}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    config.CODE_APPROVAL = mode  # take effect immediately, no restart
    return {"ok": True, "restart_required": False,
            # Honest caveat: with the env var set, this live value reverts to
            # the env's on the next boot — the security gate silently flips.
            "env_override": settings.env_override("AVA_CODE_APPROVAL")}


@router.post("/system/retention")
def set_retention(days: int):
    """Set data.retention_days — how long telemetry/history is kept (0 = forever).
    Applies to perf rollups + hardware history on the next restart."""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "days must be an integer"},
                            status_code=400)
    if days not in settings.DATA_RETENTION_CHOICES:
        return JSONResponse(
            {"ok": False, "error": f"days must be one of {list(settings.DATA_RETENTION_CHOICES)}"},
            status_code=400)
    try:
        settings.save_patch({"data": {"retention_days": days}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True, "restart_required": True,
            "env_override": settings.env_override("AVA_DATA_RETENTION_DAYS")}


# --------------------------------------------------------------------------- #
# Agent runtime — status + provision (thin wrappers over runtime.nemoclaw())
# --------------------------------------------------------------------------- #
@router.get("/agent/status")
def agent_status():
    st = runtime.nemoclaw().status()
    st["runtime"] = config.AGENT_RUNTIME
    st["required"] = config.AGENT_REQUIRED
    st["tools"] = bool(st.get("available"))
    # Distinguish "turned off by config" from "configured but not working".
    # nemoclaw.available() returns False either way, so without this the Hub
    # can't tell the operator whether to flip a switch or fix an install.
    st["enabled"] = config.AGENT_ENABLED
    st["enabled_env_override"] = settings.env_override("AVA_AGENT_ENABLED")
    return st


@router.get("/agent/skills")
def agent_skills():
    """The agent's skills, auto-discovered from agent/skills + the overlay (drop
    a folder → it appears; no registration). Each carries its deploy state
    (deployed/stale/undeployed/unknown) so the UI shows what's actually live in
    the sandbox vs newly added. See ava_bridge/skills.py."""
    from . import skills
    return {"skills": skills.catalog(), "errors": skills.load_errors(),
            "summary": skills.summary(), "category_order": skills.category_order()}


@router.get("/agent/skills/{skill_id}")
def agent_skill_detail(skill_id: str):
    """The full SKILL.md markdown of one skill — lazy-loaded when the UI expands
    a card, so the list endpoint stays light as skills grow."""
    from . import skills
    detail = skills.body(skill_id)
    if not detail:
        return JSONResponse({"error": "unknown skill"}, status_code=404)
    return detail


@router.post("/agent/skills/categories/rename")
async def agent_skills_rename_category(request: Request):
    """Rename a skill category (Hub UI inline edit). Applies an owner override
    to every skill whose effective category matches, so it also works on groups
    that only exist via author frontmatter hints. Persists to ava.yaml
    `skills.categories` — never a source edit."""
    from . import skills
    body = await request.json()
    old = str(body.get("from") or "")
    new = str(body.get("to") or "")
    if not old.strip() or not new.strip():
        return JSONResponse({"ok": False, "error": "both 'from' and 'to' are required"},
                            status_code=400)
    return {"ok": True, "renamed": skills.rename_category(old, new)}


@router.post("/agent/skills/categories/order")
async def agent_skills_category_order(request: Request):
    """Persist the owner's category order (Hub UI drag-to-reorder). The stored
    list also registers owner-created categories that hold no skills yet."""
    from . import skills
    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list):
        return JSONResponse({"ok": False, "error": "'order' must be a list"},
                            status_code=400)
    return {"ok": True, "order": skills.set_category_order(order)}


@router.post("/agent/skills/categories/new")
async def agent_skills_category_new(request: Request):
    """Create an owner category (may start empty — it lives in the order list
    until skills are dragged in)."""
    from . import skills
    body = await request.json()
    if not skills.create_category(str(body.get("name") or "")):
        return JSONResponse({"ok": False, "error": "a category needs a name"},
                            status_code=400)
    return {"ok": True}


@router.post("/agent/skills/categories/delete")
async def agent_skills_category_delete(request: Request):
    """Delete a category; any skills still filed under it fall back to their
    author hint or the General bucket (the UI only offers this on empty groups)."""
    from . import skills
    if not skills.delete_category(str((await request.json()).get("name") or "")):
        return JSONResponse({"ok": False, "error": "unknown category"}, status_code=404)
    return {"ok": True}


@router.post("/agent/skills/{skill_id}/category")
async def agent_skill_set_category(skill_id: str, request: Request):
    """Recategorize one skill (Hub UI drag-and-drop). Writes the owner-owned
    `skills.categories` map in ava.yaml; null/empty clears the override."""
    from . import skills
    body = await request.json()
    category = body.get("category")
    if not skills.set_category(skill_id, None if category is None else str(category)):
        return JSONResponse({"ok": False, "error": "unknown skill"}, status_code=404)
    return {"ok": True}


@router.post("/agent/provision")
def agent_provision():
    # auto_install=False on purpose: never run the curl|bash CLI installer from a
    # browser. This checks the CLI + sandbox and deploys Ava's tools/policies if
    # both are present; installing the CLI stays a deliberate terminal step
    # (`ava agent provision --install`).
    return runtime.nemoclaw().provision(auto_install=False)














































# --------------------------------------------------------------------------- #
# Voice — status / enroll from browser recordings / test similarity
# --------------------------------------------------------------------------- #
@router.get("/voice/status")
def voice_status():
    from . import voice_enroll
    st = voice_enroll.status()
    st["enabled"] = features.enabled("voice")
    return st


# Upload bounds: recordings are short mic clips — a minute of webm/opus is well
# under 1 MB, so 25 MB/clip and 8 clips is generous while stopping OOM abuse.
_MAX_CLIP_BYTES = 25 * 1024 * 1024
_MAX_CLIPS = 8


async def _read_clip(f: UploadFile) -> bytes | None:
    """Read one upload with a hard size cap (None = too large)."""
    data = await f.read(_MAX_CLIP_BYTES + 1)
    return None if len(data) > _MAX_CLIP_BYTES else data


@router.post("/voice/enroll")
async def voice_enroll_ep(files: list[UploadFile]):
    """Build + save the voiceprint from uploaded recordings (any format the
    browser produces — decoded via ffmpeg). Embedding runs in a worker thread."""
    from . import voice_enroll
    if len(files) > _MAX_CLIPS:
        return JSONResponse({"ok": False, "error": f"too many clips (max {_MAX_CLIPS})"},
                            status_code=413)
    clips = []
    for f in files:
        data = await _read_clip(f)
        if data is None:
            return JSONResponse({"ok": False, "error": "a clip exceeds 25 MB"},
                                status_code=413)
        clips.append(data)
    if not clips or all(len(c) == 0 for c in clips):
        return JSONResponse({"ok": False, "error": "no audio uploaded"}, status_code=400)
    res = await run_in_threadpool(voice_enroll.enroll, clips)
    return res if res.get("ok") else JSONResponse(res, status_code=422)


@router.post("/voice/test")
async def voice_test_ep(file: UploadFile):
    """Similarity of one clip against the enrolled voiceprint (gate preview)."""
    from . import voice_enroll
    clip = await _read_clip(file)
    if clip is None:
        return JSONResponse({"ok": False, "error": "clip exceeds 25 MB"}, status_code=413)
    if not clip:
        return JSONResponse({"ok": False, "error": "no audio uploaded"}, status_code=400)
    res = await run_in_threadpool(voice_enroll.test, clip)
    return res if res.get("ok") else JSONResponse(res, status_code=422)


@router.post("/voice/threshold")
def voice_threshold(value: float):
    """Set the speaker-gate threshold (the Hub's 'apply suggested threshold').
    Persists to ava.yaml; a restart applies it to the live gate."""
    if not (0.2 <= value <= 0.95):
        return JSONResponse({"ok": False, "error": "threshold must be 0.2–0.95"},
                            status_code=400)
    try:
        settings.save_patch({"voice": {"threshold": round(float(value), 2)}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True, "restart_required": True,
            "env_override": settings.env_override("AVA_PHONE_THRESHOLD")}




































# --------------------------------------------------------------------------- #
# Memory — the governed long-term store (ava_bridge/memory_store.py).
# The owner can read, correct, and delete everything Ava remembers; recalls
# that influenced a turn are in the audit ledger (kind=memory_recall).
# --------------------------------------------------------------------------- #
@router.get("/memory/export")
def memory_export():
    """The whole store as a JSON download — your data leaves in one click."""
    from . import memory_store
    return JSONResponse(
        memory_store.export_all(),
        headers={"Content-Disposition": 'attachment; filename="ava-memory.json"'})


@router.get("/memory")
def memory_list(q: str = "", kind: str = "", limit: int = 100, offset: int = 0):
    """List (newest first) or free-text search the memory store."""
    from . import memory_store
    if kind not in ("", "fact", "doc"):
        return JSONResponse({"error": "kind must be fact|doc"}, status_code=400)
    items = memory_store.list_items(kind=kind, query=q.strip(),
                                    limit=limit, offset=offset)
    return {"items": items, "counts": memory_store.counts(),
            "enabled": config.MEMORY_ENABLED}


@router.post("/memory")
async def memory_add(request: Request):
    """Add a manual fact: {"text": ...}. Manual facts rank like any other."""
    from . import memory_store
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "empty text"}, status_code=400)
    mid = memory_store.add("fact", text, source="manual")
    if mid is None:
        return JSONResponse({"ok": False, "error": "could not write memory store"},
                            status_code=500)
    audit.record("memory_edit", action="add", id=mid)
    return {"ok": True, "id": mid}


@router.post("/memory/{mid}")
async def memory_update(mid: int, request: Request):
    """Edit one item: {"text": ...} and/or {"pinned": bool}."""
    from . import memory_store
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    text = body.get("text")
    pinned = body.get("pinned")
    if text is None and pinned is None:
        return JSONResponse({"ok": False, "error": "nothing to update"},
                            status_code=400)
    ok = memory_store.update_item(
        mid, text=(str(text) if text is not None else None),
        pinned=(bool(pinned) if pinned is not None else None))
    if not ok:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    audit.record("memory_edit", action="update", id=mid)
    return {"ok": True}


@router.post("/memory/{mid}/delete")
def memory_delete(mid: int):
    from . import memory_store
    if not memory_store.delete_item(mid):
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    audit.record("memory_edit", action="delete", id=mid)
    return {"ok": True}
