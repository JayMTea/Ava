"""First-run web wizard — extends the existing /setup surface (PACKAGING_PLAN §7).

Server-rendered so it works before the SPA bundle exists / when it's stale, and
so onboarding never couples to the frontend build. Step 1 (the password screen
in phone_bridge) stays the only PRE-auth surface; everything here is behind the
session cookie (these routes are not in auth._PUBLIC_PATHS, so auth_gate covers
them automatically — no middleware change).

Everything the wizard sets is written to $AVA_HOME/ava.yaml via
settings.save_patch — no source edits, ever.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, connectors, settings

router = APIRouter()

with open(os.path.join(config.WEB_DIR, "setup_wizard.html"), encoding="utf-8") as _f:
    _WIZARD_PAGE = _f.read()


def setup_completed() -> bool:
    """True once onboarding is done. A pre-existing install (upgraded from before
    the wizard) counts as completed if it already declares an inference backend,
    so the owner is never dropped back into the wizard."""
    if settings.get_bool("setup.completed", False):
        return True
    backends = settings.get("inference.backends", {}) or {}
    return bool(backends)


@router.get("/setup/wizard", response_class=HTMLResponse)
def wizard_page():
    if setup_completed():
        return RedirectResponse("/", status_code=303)
    brand = settings.brand_name()
    return HTMLResponse(_WIZARD_PAGE.replace("__BRAND__", brand))


@router.get("/api/setup/hardware")
def api_hardware():
    from . import hwinfo, model_fit
    out = {"fit_gb": None, "source": None, "tier": "cloud", "hint": "",
           "gpu": None, "platform": None, "note": ""}
    try:
        out["platform"] = hwinfo.platform_id()
    except Exception:  # noqa: BLE001
        pass
    try:
        mem = hwinfo.fit_memory()
        out["fit_gb"] = round(mem.total_gb, 1) if mem.total_gb else None
        out["source"] = mem.source
        if mem.total_gb:
            tier, hint = model_fit.recommend_tier(mem.total_gb)
            out["tier"], out["hint"] = tier, hint
        else:
            out["hint"] = "no local GPU/unified pool detected — use a cloud key"
    except Exception as e:  # noqa: BLE001
        out["hint"] = f"probe failed: {e}"
    # Apple Silicon: unified memory is the pool; GPU util/temp/power have no
    # unprivileged API (blank meters are expected), and vLLM can't serve here —
    # use Ollama / MLX / LM Studio. Surface this so a Mac first-run isn't puzzling.
    if out["platform"] == "darwin-apple":
        out["note"] = ("Apple Silicon: use Ollama, MLX, or LM Studio (vLLM needs "
                       "an NVIDIA GPU). GPU memory shows; util/temp/power read "
                       "blank — that's expected on a Mac.")
    try:
        from . import hardware
        g = hardware._gpu()
        if g.get("name"):
            out["gpu"] = g["name"]
    except Exception:  # noqa: BLE001
        pass
    return out


def _probe(url: str, timeout: float = 1.5) -> bool:
    try:
        import requests
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:  # noqa: BLE001
        return False


@router.get("/api/setup/backends")
def api_backends():
    """Which local engines are answering right now, so the wizard can preselect."""
    return {
        "vllm": _probe("http://127.0.0.1:8002/v1/models"),
        "ollama": _probe("http://127.0.0.1:11434/api/tags"),
        "router": _probe(f"http://127.0.0.1:{config.ROUTER_PORT}/healthz"),
    }


@router.get("/api/setup/connectors")
def api_connectors():
    out = []
    for m in connectors.load(force=True):
        out.append({"id": m["id"], "label": m.get("label", m["id"]),
                    "kind": m.get("kind", "app")})
    return {"connectors": out}


@router.post("/api/setup/save")
async def api_save(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    inf = body.get("inference") or {}
    mode = inf.get("mode", "local")
    patch: dict = {"setup": {"completed": True}}

    if mode == "cloud":
        base = str(inf.get("base_url", "")).strip()
        model = str(inf.get("model", "")).strip()
        if not base or not model:
            return JSONResponse(
                {"error": "cloud mode needs base_url and model"}, status_code=400)
        patch["inference"] = {
            "primary": "cloud",
            "backends": {"cloud": {"engine": "openai", "base_url": base,
                                   "model": model, "api_key_env": "AVA_INFERENCE_KEY"}},
        }
        key = str(inf.get("api_key", "")).strip()
        if key:
            # Secret goes to the secrets store, never into ava.yaml.
            path = os.path.join(settings.secrets_dir(), "inference_key")
            os.makedirs(settings.secrets_dir(), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(key)
            os.chmod(path, 0o600)
    else:
        engine = str(inf.get("engine", "vllm")).strip()
        base = str(inf.get("base_url", "")).strip()
        model = str(inf.get("model", "")).strip()
        if base and model:
            patch["inference"] = {
                "primary": "local",
                "backends": {"local": {"engine": engine, "base_url": base,
                                       "model": model}},
            }

    feats = body.get("features") or {}
    if isinstance(feats, dict):
        patch["features"] = {k: bool(v) for k, v in feats.items()
                             if k in ("voice", "web_search", "image")}

    enabled = body.get("connectors")
    if isinstance(enabled, list):
        patch["connectors"] = [str(c) for c in enabled]

    try:
        settings.save_patch(patch)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    # config.py reads several of these at import, so a restart applies them fully.
    return {"ok": True, "restart_required": True}
