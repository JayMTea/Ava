"""Setup -> Agent panel: provisioning and status of the agent runtime.

Ava runs without an agent — DirectRuntime is the tool-less floor — so this
panel is where an owner turns her from a chat model into something with tools,
skills and memory. The routes report which runtime is active, provision the
NemoClaw sandbox, and surface what went wrong when provisioning fails, which is
the failure a fresh install is most likely to hit.
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, provision, provision_job, runtime, settings
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
    the sandbox vs newly added. See ava_bridge/skills.py.

    The state is overlaid from ava_bridge/provision.py rather than taken from
    skills.py directly: skills.py can only consult install.sh's manifest, which
    records what a deploy ATTEMPTED. provision.py prefers what the sandbox
    actually holds. Same response shape either way."""
    return {"skills": provision.apply_skill_states(skills.catalog()),
            "errors": skills.load_errors(),
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

@router.get("/agent/provision/state")
def agent_provision_state(force: int = 0):
    """What the sandbox is actually running vs what this checkout declares.

    A sync `def` so FastAPI runs it in a threadpool, and it reads only files —
    the NemoClaw registry, the policy/skill/server trees, the rendered persona.
    It never execs into the sandbox, so it is safe to poll and it still answers
    correctly when the container is stopped (which is precisely when the owner
    most wants to know what is live).
    """
    return provision.state(force=bool(force))


@router.post("/agent/provision")
def agent_provision(scope: str = "all"):
    # auto_install=False on purpose: never run the curl|bash CLI installer from a
    # browser. This checks the CLI + sandbox and deploys Ava's tools/policies if
    # both are present; installing the CLI stays a deliberate terminal step
    # (`ava agent provision --install`).
    #
    # `configured()`, not `nemoclaw()` — the same bug agent_status() above already
    # fixed, and for the same reason. This hardcoded the LOCAL runtime, so on
    # `agent.runtime: remote` the panel correctly reported the remote agent host
    # while this button provisioned the bridge container instead: it would fail on
    # a missing CLI, or worse, succeed against the wrong machine.
    if scope not in provision.ALL_SCOPES:
        return JSONResponse({"ok": False, "error": f"unknown scope {scope!r}",
                             "error_code": "unknown_scope",
                             "supported": list(provision.ALL_SCOPES)}, status_code=400)
    started, snap = provision_job.start(scope=scope, auto_install=False)
    if not started:
        # 409, not a queue. Two concurrent deploys into one sandbox would
        # interleave writes to openclaw.json.
        return JSONResponse(
            {"ok": False, "error": "a provisioning run is already in progress",
             "error_code": "provision_running",
             "job_id": snap.get("id"), "scope": snap.get("scope"),
             "status": snap.get("status")}, status_code=409)
    # `steps` and `detail` are present but empty on purpose: a fork shipping an
    # older committed frontend/dist reads those keys, and an absent key would be
    # a crash where an empty list is merely a build that cannot show progress.
    return {"ok": True, "job_id": snap["id"], "scope": scope, "status": "running",
            "steps": [], "detail": "applying changes — watch Setup → Agent → Runtime"}


@router.get("/agent/provision/status")
def agent_provision_status(since: int = 0):
    """Progress of the current (or last) run. `since` ships only new log lines."""
    return provision_job.snapshot(since=since)



@router.get("/agent/inference")
def agent_inference():
    """Can Ava actually answer right now?

    Nothing else answers this. `setup_completed()` reports a flag and says so in
    its own docstring — it stays True for an install whose model was later
    deleted, whose engine stopped, or whose config was hand-edited. So the first
    thing that ever noticed was the engine, one turn later, by which time a new
    owner has already read "Failed" as "this product does not work".

    The `code` field is the point: it feeds frontend/src/lib/fixes.ts, which
    resolves a Setup destination from the code PATTERN. So this needs no mapping
    table of its own and stays correct when new codes appear.

    WHICH model this diagnoses comes from models.effective_brain(), not from
    this route. It used to take the first backend that had a model set, which is
    only the brain by coincidence: with `inference.roles.chat` pointing at the
    second backend, this reported the health of a model no turn would ever
    reach — a green badge for an engine nobody talks to, or a red one naming a
    model that is not the problem. The monitor and Setup now diagnose the same
    model they name.
    """
    from .. import models as _models
    from .. import router_app as _router

    # Parsed here rather than inferred from the resolver, because the resolver
    # deliberately swallows a broken config (a monitor must degrade, never
    # raise) and so cannot tell "no model" from "unreadable yaml" — and telling
    # those apart is this route's entire job.
    try:
        _router.load_backends()
    except Exception as e:  # noqa: BLE001 — a broken config must still answer
        return {"ok": False, "code": "config_unparseable",
                "detail": f"Could not read the inference config: {e}"}

    brain = _models.effective_brain()
    model = str(brain.get("model_id") or "").strip()
    engine = str(brain.get("engine") or "").strip()
    if not model:
        # Distinguishing this from "engine down" matters: nothing is broken to
        # restart, a value is simply missing, and Operations is the wrong place
        # to send someone.
        return {"ok": False, "code": "model_unknown", "model": "", "engine": "",
                "detail": "No model is configured, so there is nothing to answer "
                          "with. Choose one in Setup -> Agent."}

    if brain.get("source") == "agent":
        # The agent runtime serves turns inside its sandbox and owns the model
        # endpoint; we hold no base URL to probe. Probing the empty one would
        # report `inference_down` — a red banner over an install that is
        # answering — so the honest read is the one the resolver already made:
        # runtime.active() returned the sandbox, which means it is live.
        return {"ok": True, "code": "", "model": model, "engine": engine,
                "detail": ""}

    base = str(brain.get("base_url") or "")
    # The key travels with the probe. Without it a token-protected local engine
    # answers 401 and a perfectly healthy brain is reported down — the same bug
    # hardware._loaded_models() already carries a comment about.
    key = str(brain.get("api_key") or "")
    reachable, served = _models.probe_serving(base, engine, key, timeout=2.0)
    if not reachable:
        return {"ok": False, "code": "inference_down", "model": model,
                "engine": engine,
                "detail": f"The {engine or 'inference'} service is not answering, "
                          "so Ava cannot reply yet. It may still be starting."}
    # Reachable but not holding it. Only reported when the list was actually
    # READ — an engine that answered with nothing is a different thing from one
    # that did not answer, and calling a warming engine "missing your model"
    # would send the owner to fix something that is fine.
    if served and not _models.match_served(model, served):
        return {"ok": False, "code": "model_unknown", "model": model,
                "engine": engine,
                "detail": f"{engine or 'The engine'} is running but does not have "
                          f"{model!r}. Choose one it serves, or download it."}
    return {"ok": True, "code": "", "model": model, "engine": engine, "detail": ""}
