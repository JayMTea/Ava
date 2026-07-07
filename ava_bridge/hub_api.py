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

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from . import config, connectors, runtime, settings
from .version import __version__

router = APIRouter(prefix="/api/hub")


def _proxy_actions(m: dict) -> list[dict]:
    """Generic-proxy actions (id + path) — the ones that get a generated tool
    and an auto-allowed egress route."""
    return [a for a in connectors._static_actions(m) if a.get("id") and a.get("path")]


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
        "voice": settings.get_bool("features.voice", False, env="AVA_VOICE"),
        "voiceprint": voiceprint,
        "web_search": settings.get_bool("features.web_search", False),
        "image": settings.get_bool("features.image", True),
    }


@router.post("/system/approval")
def set_approval(mode: str):
    """Set code.approval (all|policy|none) for Ava's self-editing agent."""
    mode = (mode or "").strip().lower()
    if mode not in ("all", "policy", "none"):
        return JSONResponse({"ok": False, "error": "mode must be all|policy|none"},
                            status_code=400)
    try:
        settings.save_patch({"code": {"approval": mode}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    return {"ok": True, "restart_required": True}


# --------------------------------------------------------------------------- #
# Agent runtime — status + provision (thin wrappers over runtime.nemoclaw())
# --------------------------------------------------------------------------- #
@router.get("/agent/status")
def agent_status():
    st = runtime.nemoclaw().status()
    st["runtime"] = config.AGENT_RUNTIME
    st["required"] = config.AGENT_REQUIRED
    st["tools"] = bool(st.get("available"))
    return st


@router.post("/agent/provision")
def agent_provision():
    # auto_install=False on purpose: never run the curl|bash CLI installer from a
    # browser. This checks the CLI + sandbox and deploys Ava's tools/policies if
    # both are present; installing the CLI stays a deliberate terminal step
    # (`ava agent provision --install`).
    return runtime.nemoclaw().provision(auto_install=False)


# --------------------------------------------------------------------------- #
# Connectors — list generation state + generate/preview tools & egress policy
# --------------------------------------------------------------------------- #
@router.get("/connectors")
def list_connectors():
    enabled = set(settings.get("connectors", []) or [])
    pol_dir = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated")
    tool_root = os.path.join(settings.CODE_ROOT, "agent", "mcp_server_content", "connectors")
    out = []
    for m in connectors.all():
        cid = m["id"]
        actions = _proxy_actions(m)
        has_tools = bool(actions) and all(
            os.path.exists(os.path.join(tool_root, cid, f"{cid}_{a['id']}.mjs"))
            for a in actions)
        out.append({
            "id": cid,
            "label": m.get("label", cid),
            "kind": m.get("kind", "app"),
            "actions": len(actions),
            "has_policy": os.path.exists(os.path.join(pol_dir, f"{cid}.yaml")),
            "has_tools": has_tools,
            "renders_policy": connectors.render_egress_policy(cid) is not None,
            # When no `connectors:` list is configured, everything ships enabled.
            "enabled": (cid in enabled) if enabled else True,
        })
    return {"connectors": out}


@router.post("/connectors/{cid}/generate")
def generate_connector(cid: str, write: int = 0):
    """Render (and optionally write) a connector's agent tools + egress policy
    from its manifest. `write=0` previews; `write=1` writes the files (the same
    output as `ava connector tools|policies --write`) — deploy with
    `cd agent && ./install.sh`."""
    import yaml as _yaml
    m = {x["id"]: x for x in connectors.all()}.get(cid)
    if not m:
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    pol = connectors.render_egress_policy(cid)
    policy_yaml = _yaml.safe_dump(pol, sort_keys=False) if pol else ""
    tools = [{"name": f"{cid}_{a['id']}.mjs", "source": connectors.render_tool(cid, a)}
             for a in _proxy_actions(m)]
    wrote: list[str] = []
    if write:
        if pol:
            pdir = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated")
            os.makedirs(pdir, exist_ok=True)
            pp = os.path.join(pdir, f"{cid}.yaml")
            with open(pp, "w", encoding="utf-8") as f:
                f.write(policy_yaml)
            wrote.append(os.path.relpath(pp, settings.CODE_ROOT))
        tdir = os.path.join(settings.CODE_ROOT, "agent", "mcp_server_content", "connectors", cid)
        for t in tools:
            os.makedirs(tdir, exist_ok=True)
            tp = os.path.join(tdir, t["name"])
            with open(tp, "w", encoding="utf-8") as f:
                f.write(t["source"])
            wrote.append(os.path.relpath(tp, settings.CODE_ROOT))
    return {"ok": True, "policy": policy_yaml, "tools": tools, "wrote": wrote}
