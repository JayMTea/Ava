"""Setup -> Agent panel: provisioning and status of the agent runtime.

Ava runs without an agent — DirectRuntime is the tool-less floor — so this
panel is where an owner turns her from a chat model into something with tools,
skills and memory. The routes report which runtime is active, provision the
NemoClaw sandbox, and surface what went wrong when provisioning fails, which is
the failure a fresh install is most likely to hit.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, runtime, settings
from .. import skills

router = APIRouter()

# --------------------------------------------------------------------------- #
# Agent runtime — status + provision
# --------------------------------------------------------------------------- #
@router.get("/agent/status")
def agent_status():
    # `configured()`, not `nemoclaw()`. This hardcoded the LOCAL runtime, so on
    # `agent.runtime: remote` — which is what the agent and full profiles use — the
    # bridge container reported its OWN missing CLI and the panel rendered
    # "CLI: not installed / Sandbox: none / Tools: unavailable" with a not-ready
    # badge, while the remote agent was serving turns correctly. It then told the
    # owner to run `ava agent provision --install`, which is the wrong machine.
    #
    # `location` is what lets the panel stop showing CLI/sandbox rows that describe
    # a host the operator is not on.
    rt = runtime.configured()
    st = rt.status()
    st["location"] = "local" if rt is runtime.nemoclaw() else "remote"
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
    return {"skills": skills.catalog(), "errors": skills.load_errors(),
            "summary": skills.summary(), "category_order": skills.category_order()}

@router.get("/agent/skills/{skill_id}")
def agent_skill_detail(skill_id: str):
    """The full SKILL.md markdown of one skill — lazy-loaded when the UI expands
    a card, so the list endpoint stays light as skills grow."""
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
    body = await request.json()
    if not skills.create_category(str(body.get("name") or "")):
        return JSONResponse({"ok": False, "error": "a category needs a name"},
                            status_code=400)
    return {"ok": True}

@router.post("/agent/skills/categories/delete")
async def agent_skills_category_delete(request: Request):
    """Delete a category; any skills still filed under it fall back to their
    author hint or the General bucket (the UI only offers this on empty groups)."""
    if not skills.delete_category(str((await request.json()).get("name") or "")):
        return JSONResponse({"ok": False, "error": "unknown category"}, status_code=404)
    return {"ok": True}

@router.post("/agent/skills/{skill_id}/category")
async def agent_skill_set_category(skill_id: str, request: Request):
    """Recategorize one skill (Hub UI drag-and-drop). Writes the owner-owned
    `skills.categories` map in ava.yaml; null/empty clears the override."""
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

