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
import re
import shutil
import subprocess
import sys
import threading
from collections import deque

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import audit, config, connectors, features, perf_mgmt, runtime, settings
from .version import __version__

router = APIRouter(prefix="/api/hub")


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


@router.get("/connectors/{cid}/grants")
def grants_list(cid: str):
    """One connector's permission sheet (settings page): every static action —
    plus, for an MCP/discover connector, its live dynamic tools (best-effort;
    a down server just means the live set is omitted) — with capability group,
    access tier, and grant state."""
    from . import connectors as _c, grants
    granted = grants.for_connector(cid)
    m = {x["id"]: x for x in _c.all()}.get(cid) or {}
    acts = []
    for a in _c._static_actions(m):
        if not a.get("id"):
            continue
        acts.append({"id": a["id"], "access": _c._infer_access(a),
                     "capability": _c.action_capability(a),
                     "method": str(a.get("method") or "POST").upper(),
                     "path": a.get("path", ""),
                     "description": a.get("description", ""),
                     "granted": a["id"] in granted,
                     "grantable": _c.grantable(cid, a["id"])})
    if _c._mcp_spec(m) or _c._discover_spec(m):
        seen = {a["id"] for a in acts}
        for t in (_c.discover_tools(cid).get("tools") or []):
            name = t.get("name")
            if not name or name in seen:
                continue
            acts.append({"id": name, "access": _c.action_access(cid, name),
                         "capability": "live tools", "method": "", "path": "",
                         "description": (t.get("description") or "")[:160],
                         "granted": name in granted,
                         "grantable": _c.grantable(cid, name)})
    return {"grants": granted, "actions": acts}


@router.post("/connectors/{cid}/grants/{action}")
def grants_add(cid: str, action: str):
    """Pre-grant from the settings page — same rule as the "Always allow"
    prompt: write tier only; destructive/author-gated actions can't be granted."""
    from . import connectors as _c, grants
    if not _c.grantable(cid, action):
        return JSONResponse({"ok": False, "error":
                             "this action asks every time and can't be always-allowed"},
                            status_code=400)
    grants.grant(cid, action)
    return {"ok": True}


@router.delete("/connectors/{cid}/grants/{action}")
def grants_revoke(cid: str, action: str):
    from . import grants
    return {"ok": grants.revoke(cid, action)}


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
        # Legacy per-feature booleans (existing consumers) + the registry
        # snapshot the Optional-features panel renders from — one source
        # (features.REGISTRY), so a new capability appears here automatically.
        "voice": features.enabled("voice"),
        "voiceprint": voiceprint,
        "web_search": features.enabled("web_search"),
        "image": features.enabled("image"),
        "features": features.snapshot(),
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
# Connectors — list generation state + generate/preview tools & egress policy
# --------------------------------------------------------------------------- #
@router.get("/connectors")
def list_connectors():
    """The connector **management** view for the Setup Hub: every manifest via
    `connectors.catalog()` (INCLUDING disabled ones, so they can be re-enabled),
    each with its edit/deploy state (`enabled`, `builtin`, `has_tools`,
    `has_policy`, `renders_policy`) plus any malformed-manifest `errors`.

    Distinct from `/api/ops/connectors` (`dashboard.connectors_info`), which is the
    read-only dashboard **telemetry** view: enabled connectors only, with health /
    perf / egress-count fields for the Ops panel. Two endpoints, two audiences —
    management vs monitoring — so neither UI carries the other's fields."""
    pol_dir = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated")
    tool_root = os.path.join(settings.CODE_ROOT, "agent", "mcp_server_content", "connectors")
    user_root = settings.home("connectors")
    out = []
    for m in connectors.catalog():  # includes disabled, so they can be re-enabled
        cid = m["id"]
        actions = _proxy_actions(m)
        expected = connectors.tool_files(cid)
        has_tools = bool(expected) and all(
            os.path.exists(os.path.join(tool_root, cid, t["name"]))
            for t in expected)
        # Credential state — the env-var NAME the app authenticates with (if any),
        # and whether a value is available (a real env var OR one saved once via
        # the Hub) so the UI can show "credential saved" and never re-prompt on
        # redeploy. The value itself is never returned.
        auth = connectors.auth_env(m)
        out.append({
            "id": cid,
            "label": m.get("label", cid),
            "kind": m.get("kind", "app"),
            "actions": len(actions),
            "mcp": connectors._mcp_spec(m) is not None,
            "discover": connectors._discover_spec(m) is not None,
            # HOW its tools arrive — mcp | discover | rest | none. The honest
            # name for the wire protocol; `mcp` here means a real MCP server,
            # not "has tools". See connectors.transport().
            "transport": connectors.transport(m),
            # The two connect surfaces (the doctrine: an app is a UI, a tool
            # facade, or both — never raw endpoints): ui block => APP tile.
            "app": isinstance(m.get("ui"), dict),
            # Device identity: `role: device` and/or an inbound `ingest:` block.
            # Setup groups by what a connector IS to the owner, and a device's
            # defining trait is that it pushes to Ava rather than being visited.
            "role": str(m.get("role") or "") or None,
            "ingest": connectors.ingest_enabled(cid),
            # Rail identity, for the Appearance picker (null = uses the auto-pick).
            "icon": (m.get("ui") or {}).get("icon") if isinstance(m.get("ui"), dict) else None,
            "color": str((m["ui"]).get("color")) if isinstance(m.get("ui"), dict) and (m["ui"]).get("color") else None,
            "has_policy": os.path.exists(os.path.join(pol_dir, f"{cid}.yaml")),
            "has_tools": has_tools,
            "renders_policy": connectors.render_egress_policy(cid) is not None,
            "enabled": bool(m.get("enabled", True)),  # from the manifest
            # User-added connectors are editable/removable; shipped ones are read-only.
            "builtin": not os.path.isdir(os.path.join(user_root, cid)),
            # Credential status (never the value): the env-var name it uses, and
            # whether a credential is available / saved in the store.
            "auth_env": auth,
            "auth_set": settings.has_env_secret(auth) if auth else False,
            "auth_stored": settings.env_secret_stored(auth) if auth else False,
        })
    return {"connectors": out, "errors": connectors.load_errors()}


@router.get("/connectors/{cid}/live")
def connector_live(cid: str):
    """Actually talk to <cid> right now and report what came back.

    This is what keeps the Setup UI's transport label honest. `list_connectors`
    reads the manifest — it can only tell you what a connector *claims*. This
    performs the real handshake (MCP `initialize` + `tools/list`, or a GET on
    the ava-tools/1 facade) so the UI can say "MCP · 18 tools" only when Ava
    genuinely spoke MCP to it a moment ago, and show the transport error when
    it didn't.

    ``verified`` is the distinction that matters: true means we round-tripped
    just now; false means the count is declared in the manifest (a `rest`
    connector has nothing to hand-shake with) and no promise is being made.
    """
    m = {x["id"]: x for x in connectors.catalog()}.get(cid)
    if not m:
        return {"ok": False, "error": f"no connector {cid}"}
    kind = connectors.transport(m)
    base = {"ok": True, "transport": kind, "verified": False,
            "tools": None, "error": None}

    if not m.get("enabled", True):
        # Never dial a connector the owner switched off — "disabled" is an
        # answer, not a failure.
        return {**base, "ok": False, "error": "disabled"}

    if kind == "rest":
        return {**base, "tools": len(connectors._static_actions(m))}
    if kind == "none":
        return {**base, "tools": 0}

    r = connectors.discover_tools(cid)
    if isinstance(r, dict) and r.get("error"):
        return {**base, "ok": False, "error": str(r["error"])[:300]}
    return {**base, "verified": True, "tools": len(r.get("tools") or [])}


@router.post("/connectors/{cid}/secret")
async def connector_set_secret(cid: str, body: dict):
    """Save (or clear) a connected app's credential VALUE so redeploy never
    re-prompts. Keyed by the manifest's ``token_env`` NAME, the value goes to the
    server-side secret store (``$AVA_HOME/secrets/env/<NAME>``, 0600) — never the
    manifest, never the agent. An empty value clears it. A real env var of the
    same name still wins at read time."""
    m = {x["id"]: x for x in connectors.catalog()}.get(cid)
    if not m:
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    name = connectors.auth_env(m)
    if not name:
        return JSONResponse({"ok": False, "error":
                             "This app declares no auth token. Add "
                             "`auth: { token_env: NAME }` via Edit (or reconnect "
                             "with a token) first."}, status_code=400)
    value = str(body.get("value") or "")
    if value.strip():
        settings.set_env_secret(name, value)
    else:
        settings.clear_env_secret(name)
    return {"ok": True, "auth_env": name,
            "auth_set": settings.has_env_secret(name),
            "auth_stored": settings.env_secret_stored(name)}


# --------------------------------------------------------------------------- #
# Models — manifest listing + background pull with live log (wraps the CLI so
# the download logic stays in ONE place: `ava models pull`)
# --------------------------------------------------------------------------- #
_pull_job: dict = {"status": "idle", "role": None, "log": deque(maxlen=200),
                   "rc": None}
_pull_lock = threading.Lock()


def _cli_models():
    """The CLI's model helpers (manifest/dirs/present/tier) — single source of
    truth shared with `ava models`. Imported lazily to keep bridge boot lean."""
    sys.path.insert(0, settings.CODE_ROOT) if settings.CODE_ROOT not in sys.path else None
    import ava_cli
    return ava_cli


@router.get("/models")
def models_list():
    cli = _cli_models()
    manifest = cli._models_manifest()
    dirs = cli._model_dirs()
    tier, avail = cli._detected_tier()
    roles = []
    for role, spec in manifest.items():
        roles.append({"role": role, "id": spec.get("id"),
                      "engine": spec.get("engine"), "tier": spec.get("tier"),
                      "present": cli._model_present(spec, dirs)})
    return {"roles": roles, "detected_tier": tier,
            "available_gb": round(avail, 0) if avail else None,
            "store": dirs["root"]}


def _run_pull(args: list[str]) -> None:
    """Worker: run `ava models pull …` as a subprocess, streaming stdout into
    the job log so the UI can poll progress. One pull at a time."""
    try:
        proc = subprocess.Popen(
            [sys.executable, os.path.join(settings.CODE_ROOT, "ava_cli.py"),
             "models", "pull", *args],
            cwd=settings.CODE_ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        for line in proc.stdout:
            line = ansi.sub("", line).rstrip()
            if line:
                _pull_job["log"].append(line)
        rc = proc.wait()
        _pull_job["rc"] = rc
        _pull_job["status"] = "done" if rc == 0 else "error"
    except Exception as e:  # noqa: BLE001
        _pull_job["log"].append(f"pull failed: {e}")
        _pull_job["rc"] = 1
        _pull_job["status"] = "error"


@router.post("/models/pull")
def models_pull(role: str = ""):
    """Start a model download in the background. role='' or 'auto' picks the
    model that fits the detected hardware tier (same as `ava models pull --auto`)."""
    with _pull_lock:
        if _pull_job["status"] == "running":
            return JSONResponse({"ok": False, "error": "a pull is already running"},
                                status_code=409)
        role = (role or "").strip().lower()
        args = ["--auto"] if role in ("", "auto") else [role]
        _pull_job.update(status="running", role=role or "auto", rc=None)
        _pull_job["log"].clear()
        threading.Thread(target=_run_pull, args=(args,), daemon=True,
                         name="hub-model-pull").start()
    return {"ok": True, "status": "running"}


@router.get("/models/pull/status")
def models_pull_status():
    return {"status": _pull_job["status"], "role": _pull_job["role"],
            "rc": _pull_job["rc"], "log": list(_pull_job["log"])}


# --- Model bench/compare (background — same prompt on each backend) ---------
_bench_job: dict = {"status": "idle", "result": None}
_bench_lock = threading.Lock()


def _run_bench(prompt: str, only, max_tokens: int) -> None:
    from . import bench

    def on_result(partial, total):
        # Publish a running snapshot so the panel fills row-by-row as each
        # backend finishes, with a live "fastest" leader.
        ok = [r for r in partial if r.get("ok")]
        winner = max(ok, key=lambda r: r["tok_s"])["id"] if ok else None
        _bench_job["result"] = {
            "prompt": prompt, "max_tokens": max_tokens,
            "results": list(partial), "winner": winner,
            "backend_count": total, "pending": total - len(partial),
        }

    try:
        _bench_job["result"] = bench.bench(prompt, only=only, max_tokens=max_tokens,
                                           on_result=on_result)
        _bench_job["status"] = "done"
    except Exception as e:  # noqa: BLE001
        _bench_job["result"] = {"error": str(e)[:200], "results": []}
        _bench_job["status"] = "error"


@router.post("/models/bench")
async def models_bench(request: Request):
    from . import bench
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    with _bench_lock:
        if _bench_job["status"] == "running":
            return JSONResponse({"ok": False, "error": "a benchmark is already running"},
                                status_code=409)
        prompt = str(body.get("prompt") or bench.DEFAULT_PROMPT)[:2000]
        only = body.get("models") if isinstance(body.get("models"), list) else None
        max_tokens = min(max(int(body.get("max_tokens") or 200), 16), 1000)
        _bench_job.update(status="running", result=None)
        threading.Thread(target=_run_bench, args=(prompt, only, max_tokens),
                         daemon=True, name="hub-model-bench").start()
    return {"ok": True, "status": "running"}


@router.get("/models/bench/status")
def models_bench_status():
    return {"status": _bench_job["status"], "result": _bench_job["result"]}


# --------------------------------------------------------------------------- #
# Inference backends — the multi-model "brain" manager
#
# ava.yaml holds a MAP of named OpenAI-compatible backends; inference.primary +
# inference.roles.chat pick which one is Ava's brain. These routes let the Hub
# list / add / test / select / remove them without hand-editing YAML. Cloud keys
# live in the secrets store (secrets/<id>.key, resolved by router_app.load_backends
# via settings.backend_key), never in ava.yaml. Writes return restart_required so
# the UI reloads the router with the new config.
# --------------------------------------------------------------------------- #
# Local engines that need no API key (all serve an OpenAI-compatible endpoint).
_LOCAL_ENGINES_UI = {"vllm", "ollama", "llamacpp", "mlx", "lmstudio"}


def _inference() -> dict:
    inf = settings.get("inference") or {}
    return inf if isinstance(inf, dict) else {}


def _brain_id(inf: dict) -> str | None:
    """Which backend is the conversational brain: roles.chat, else primary."""
    roles = inf.get("roles") or {}
    return roles.get("chat") or inf.get("primary")


@router.get("/models/backends")
def backends_list():
    """Configured inference backends + which one is the brain."""
    inf = _inference()
    backends = inf.get("backends") or {}
    primary = inf.get("primary")
    brain = _brain_id(inf)
    out = []
    for bid, b in backends.items():
        if not isinstance(b, dict):
            continue
        eng = str(b.get("engine", "openai")).strip().lower()
        out.append({
            "id": bid,
            "engine": eng,
            "base_url": b.get("base_url", ""),
            "model": b.get("model", ""),
            "label": b.get("label") or bid,
            "local": eng in _LOCAL_ENGINES_UI,
            "is_brain": bid == brain,
            "is_primary": bid == primary,
            "has_key": bool(b.get("api_key_env")) or bool(settings.backend_key(bid)),
        })
    return {"backends": out, "brain": brain, "primary": primary}


def _test_backend(base: str, model: str, key: str) -> dict:
    """One tiny completion against an OpenAI-compatible endpoint. Read-only —
    used to validate a candidate model BEFORE the user commits to saving it."""
    import time as _t
    try:
        import requests
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"requests unavailable: {e}"}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "max_tokens": 16,
               "messages": [{"role": "user", "content": "Reply with the word: ok"}]}
    t0 = _t.time()
    try:
        r = requests.post(base + "/chat/completions", json=payload,
                          headers=headers, timeout=20)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not reach {base}: {e}"}
    ms = int((_t.time() - t0) * 1000)
    if r.status_code >= 400:
        return {"ok": False, "status": r.status_code, "ms": ms,
                "error": (r.text or f"HTTP {r.status_code}")[:300]}
    reply = ""
    try:
        reply = ((r.json().get("choices") or [{}])[0]
                 .get("message", {}).get("content") or "")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "ms": ms, "reply": reply.strip()[:200]}


@router.post("/models/backends/test")
async def backends_test(request: Request):
    """Probe a candidate {engine, base_url, model, api_key?} without saving it.
    If no key is supplied but one is already stored for this id, reuse it."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    base = str(body.get("base_url", "")).strip().rstrip("/")
    model = str(body.get("model", "")).strip()
    key = str(body.get("api_key", "")).strip()
    bid = settings._safe_backend_id(str(body.get("id", "")))
    if not base or not model:
        return JSONResponse({"ok": False, "error": "base_url and model are required"},
                            status_code=400)
    if not key and bid:
        key = settings.backend_key(bid) or ""
    return await run_in_threadpool(_test_backend, base, model, key)


@router.post("/models/backends")
async def backends_save(request: Request):
    """Add or update a named backend. `make_brain` also points primary +
    roles.chat at it. A cloud key goes to the secrets store, never ava.yaml."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    bid = settings._safe_backend_id(str(body.get("id", "")))
    engine = str(body.get("engine", "")).strip().lower() or "openai"
    base = str(body.get("base_url", "")).strip().rstrip("/")
    model = str(body.get("model", "")).strip()
    key = str(body.get("api_key", "")).strip()
    make_brain = bool(body.get("make_brain"))
    if not bid:
        return JSONResponse({"ok": False, "error": "a name is required"}, status_code=400)
    if not base or not model:
        return JSONResponse({"ok": False, "error": "base_url and model are required"},
                            status_code=400)
    cfg = settings.current_config()
    inf = cfg.setdefault("inference", {})
    backends = inf.setdefault("backends", {})
    entry = backends.get(bid) if isinstance(backends.get(bid), dict) else {}
    entry.update({"engine": "llamacpp" if engine == "gguf" else engine,
                  "base_url": base, "model": model})
    backends[bid] = entry
    if not inf.get("primary"):
        inf["primary"] = bid          # first backend becomes the default brain
    if make_brain:
        inf["primary"] = bid
        inf.setdefault("roles", {})["chat"] = bid
    settings.save_config(cfg)
    # Cloud key -> secrets store (local engines need none); router resolves it.
    if key and engine not in _LOCAL_ENGINES_UI:
        try:
            settings.write_backend_key(bid, key)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": f"saved config but key write "
                                 f"failed: {e}"}, status_code=500)
    return {"ok": True, "id": bid, "restart_required": True}


@router.post("/models/backends/{bid}/brain")
def backends_set_brain(bid: str):
    """Make this backend Ava's brain (inference.primary + roles.chat)."""
    bid = settings._safe_backend_id(bid)
    cfg = settings.current_config()
    inf = cfg.get("inference") or {}
    if bid not in (inf.get("backends") or {}):
        return JSONResponse({"ok": False, "error": f"no backend '{bid}'"},
                            status_code=404)
    inf["primary"] = bid
    inf.setdefault("roles", {})["chat"] = bid
    cfg["inference"] = inf
    settings.save_config(cfg)
    return {"ok": True, "brain": bid, "restart_required": True}


@router.post("/models/backends/{bid}/delete")
def backends_delete(bid: str):
    """Remove a backend; repoint the brain to a survivor if we removed it."""
    bid = settings._safe_backend_id(bid)
    cfg = settings.current_config()
    inf = cfg.get("inference") or {}
    backends = inf.get("backends") or {}
    if bid not in backends:
        return JSONResponse({"ok": False, "error": f"no backend '{bid}'"},
                            status_code=404)
    del backends[bid]
    survivor = next(iter(backends), None)
    if inf.get("primary") == bid:
        inf["primary"] = survivor
    roles = inf.get("roles") or {}
    if roles.get("chat") == bid:
        if survivor:
            roles["chat"] = inf.get("primary") or survivor
        else:
            roles.pop("chat", None)
        inf["roles"] = roles
    cfg["inference"] = inf
    settings.save_config(cfg)
    settings.delete_backend_key(bid)
    return {"ok": True, "restart_required": True}


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
# Connectors — scaffold a new manifest from the GUI form
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


def _slim_tools(tools) -> list[dict]:
    out = []
    for t in (tools or [])[:60]:
        if isinstance(t, dict) and t.get("name"):
            out.append({"name": str(t["name"])[:80],
                        "description": str(t.get("description") or "")[:200]})
    return out


def _actions_from_openapi(spec: dict, limit: int = 50) -> list[dict]:
    """Turn an OpenAPI/Swagger paths object into ready-to-use connector actions
    so a plain web app is zero-config: the user reviews a pre-filled list instead
    of hand-typing every endpoint. Each operation -> {id, method, path, description}.
    Destructive verbs (DELETE, or a *delete* path) default to confirm=True."""
    if not isinstance(spec, dict):
        return []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            if not isinstance(op, dict):
                op = {}
            # A clean, human-readable id from the method + last path segments,
            # dropping params and a leading api/v1 prefix (e.g. GET /api/persona/{key}
            # -> get_persona). The verbose operationId only feeds the description.
            segs = [s for s in str(path).split("/") if s and not s.startswith("{")]
            if len(segs) > 1 and segs[0] in ("api", "v1", "v2", "rest"):
                segs = segs[1:]
            tail = "_".join(segs[-2:]) if segs else "root"
            aid = re.sub(r"[^a-z0-9]+", "_", f"{m}_{tail}".lower()).strip("_")[:32] or f"{m.lower()}_{len(out)}"
            base, n = aid, 2
            while aid in seen:
                aid = f"{base[:29]}_{n}"
                n += 1
            seen.add(aid)
            desc = str(op.get("summary") or op.get("description")
                       or op.get("operationId") or "").strip()[:200]
            # Access tier drives JIT consent (read silent / write asks once /
            # destructive asks always). confirm mirrors destructive for the 🔒.
            if m in ("GET", "HEAD"):
                access = "read"
            elif m == "DELETE" or "delete" in str(path).lower():
                access = "destructive"
            else:
                access = "write"
            out.append({"id": aid, "method": m, "path": str(path),
                        "description": desc, "access": access,
                        "confirm": access == "destructive"})
            if len(out) >= limit:
                return out
    return out


def _probe(url: str, command: str, token_env: str | None,
           token_value: str | None = None, *, sandbox: str | None = None,
           image: str | None = None, allow_unsandboxed: bool = False) -> dict:
    """Figure out how to talk to an app so the user doesn't have to classify it:
    try MCP (a start command = stdio, or the URL = MCP-over-HTTP), then a
    discovery endpoint (GET <url>/tools). Returns the detected kind + the tools
    we found, or kind='rest' when nothing is auto-discoverable (the caller then
    asks the user to declare actions — the one thing we can't infer).

    ``token_value`` is a credential the owner just pasted in the connect form (not
    yet saved) so the detection call can authenticate; otherwise fall back to the
    saved/env value resolved from ``token_env``."""
    from . import mcp_client
    import uuid
    cid = "__probe_" + uuid.uuid4().hex[:6]
    # Credential for the detection request: the just-pasted value wins, else the
    # value already saved for this env-var name (or a real env var).
    tok = (token_value or "").strip() or settings.env_secret(token_env)

    # 1) A start command is unambiguously an MCP stdio server.
    if command:
        # Detection RUNS the command. It used to run it bare, with the bridge's
        # whole environment, before the owner had made any isolation choice — the
        # isolation checkbox only rendered after the probe returned. So "paste its
        # address, click Detect" executed an internet-sourced command on the host.
        # Default to contained, and refuse rather than silently downgrade.
        want_sandbox = "docker" if sandbox is None else sandbox
        if want_sandbox == "docker" and not shutil.which("docker"):
            if not allow_unsandboxed:
                return {"ok": False, "needs": "docker", "kind": "mcp",
                        "error": "This is a start command, so detecting it means "
                                 "RUNNING it. Docker isn't available to contain "
                                 "it. Install Docker, or re-run detection with "
                                 "'run it directly on this host' if you trust "
                                 "this command."}
            want_sandbox = "none"
        spec = {"transport": "stdio", "url": None, "command": command.split(),
                "env": None, "token_env": token_env,
                "sandbox": want_sandbox, "image": image or None}
        try:
            res = mcp_client.list_tools(cid, spec)
        finally:
            mcp_client.reset(cid)
        if res.get("tools"):
            return {"ok": True, "kind": "mcp", "transport": "stdio",
                    "tools": _slim_tools(res["tools"])}
        return {"ok": True, "kind": "unknown", "tools": [],
                "detail": res.get("error", "that command didn't expose MCP tools")}

    if not url:
        return {"ok": False, "error": "give a web address or a start command"}

    def _serves_html() -> bool:
        """Does the app have its own web UI at the base URL? Drives the
        "Add it to Ava's sidebar" offer (ui.embed: iframe — the embedded-app
        tier from CONNECTOR_SDK.md §3)."""
        try:
            import requests
            r = requests.get(url, timeout=5, headers={"Accept": "text/html"})
            ct = r.headers.get("content-type", "")
            return r.status_code < 400 and (
                "text/html" in ct
                or r.text[:200].lstrip().lower().startswith(("<!doctype", "<html")))
        except Exception:  # noqa: BLE001
            return False

    # 1.5) The app may self-describe: GET /.well-known/ava.json (SDK §5) names
    # the app, its health probe, its UI, and where the facade routes live — the
    # most authoritative signal, so it goes first and prefills the whole form.
    try:
        import requests
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        wk = requests.get(url.rstrip("/") + "/.well-known/ava.json",
                          headers=headers, timeout=5)
        meta = wk.json() if wk.status_code == 200 else None
        if isinstance(meta, dict) and str(meta.get("facade", "")).startswith("ava-tools/"):
            list_path = "/" + str(meta.get("tools") or "/tools").lstrip("/")
            call_path = "/" + str(meta.get("call") or "/call").lstrip("/")
            r = requests.get(url.rstrip("/") + list_path, headers=headers, timeout=8)
            data = r.json()
            tools = data.get("tools") if isinstance(data, dict) else None
            if isinstance(tools, list) and tools:
                health = str(meta.get("health") or "").strip()
                # Self-described token name (SDK §3 Single sign-on): prefill the
                # connect form so the owner pastes only the VALUE, and Ava then
                # presents it to the agent tools AND the embedded UI.
                auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
                token_env_hint = str(auth_meta.get("token_env") or "").strip() or None
                return {"ok": True, "kind": "discover", "tools": _slim_tools(tools),
                        "label": str(meta.get("label") or "").strip()[:60] or None,
                        "health": (url.rstrip("/") + "/" + health.lstrip("/")) if health else None,
                        "discover": {"list": list_path, "call": call_path},
                        "token_env": token_env_hint,
                        "has_ui": bool(meta.get("ui")) or _serves_html()}
    except Exception:  # noqa: BLE001
        pass

    # 2) Try MCP over HTTP at the URL.
    spec = {"transport": "http", "url": url, "command": None, "env": None,
            "token_env": token_env}
    try:
        res = mcp_client.list_tools(cid, spec)
    finally:
        mcp_client.reset(cid)
    if res.get("tools"):
        return {"ok": True, "kind": "mcp", "transport": "http",
                "tools": _slim_tools(res["tools"])}

    # 3) Try a discovery endpoint (our list+call facade / FastMCP-style).
    try:
        import requests
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        r = requests.get(url.rstrip("/") + "/tools", headers=headers, timeout=8)
        data = r.json()
        tools = data.get("tools") if isinstance(data, dict) else (
            data if isinstance(data, list) else None)
        if isinstance(tools, list) and tools:
            return {"ok": True, "kind": "discover", "tools": _slim_tools(tools),
                    "has_ui": _serves_html()}
    except Exception:  # noqa: BLE001
        pass

    # 3.5) Most web apps (FastAPI/Swagger/anything OpenAPI) publish a
    # machine-readable spec. Read it and pre-fill the actions so the user
    # reviews a list instead of hand-typing endpoints — zero-config.
    try:
        import requests
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        for suffix in ("/openapi.json", "/swagger.json", "/openapi"):
            try:
                r = requests.get(url.rstrip("/") + suffix, headers=headers, timeout=8)
            except Exception:  # noqa: BLE001 — try the next well-known path
                continue
            if r.status_code != 200:
                continue
            try:
                spec = r.json()
            except Exception:  # noqa: BLE001 — not JSON, not a spec
                continue
            actions = _actions_from_openapi(spec)
            if actions:
                return {"ok": True, "kind": "rest", "tools": [], "actions": actions,
                        "has_ui": _serves_html(),
                        "detail": f"Read its API — found {len(actions)} actions."}
    except Exception:  # noqa: BLE001
        pass

    # 4) A plain web API with no discoverable spec — the user declares actions.
    return {"ok": True, "kind": "rest", "tools": [], "has_ui": _serves_html(),
            "detail": "No tools to auto-discover — tell Ava what this app can do."}


@router.post("/connectors/probe")
async def connector_probe(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    url = str(body.get("url") or "").strip()
    command = str(body.get("command") or "").strip()
    token_env = str(body.get("token_env") or "").strip() or None
    token_value = str(body.get("token_value") or "")
    # The isolation choice is a property of HOW to run the command, so it has to
    # arrive with the request that runs it — not be confirmed afterwards.
    sandbox = body.get("sandbox")
    sandbox = str(sandbox).strip().lower() if sandbox is not None else None
    image = str(body.get("image") or "").strip() or None
    allow_unsandboxed = bool(body.get("allow_unsandboxed"))
    return await run_in_threadpool(_probe, url, command, token_env, token_value,
                                   sandbox=sandbox, image=image,
                                   allow_unsandboxed=allow_unsandboxed)


@router.post("/connectors/new")
async def connector_new(body: dict):
    """Write $AVA_HOME/connectors/<id>/connector.yaml from the Hub's form.
    Refuses to overwrite an existing manifest. The generated file uses the same
    schema as `ava connector new` + docs/CONNECTOR_SDK.md."""
    import yaml as _yaml
    cid = str(body.get("id", "")).strip().lower()
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error":
                             "id must be 2-32 chars: a-z 0-9 _ - (starting with a letter)"},
                            status_code=400)
    if any(m["id"] == cid for m in connectors.all()):
        return JSONResponse({"ok": False, "error": f"connector '{cid}' already exists"},
                            status_code=409)
    label = str(body.get("label") or cid.title()).strip()
    kind = body.get("kind") if body.get("kind") in ("core", "inference", "media", "app") else "app"
    manifest: dict = {"id": cid, "label": label, "kind": kind, "enabled": True}
    if body.get("confirm"):  # connector-level "require my approval for every action"
        manifest["confirm"] = True
    if str(body.get("role") or "").lower() == "device":
        manifest["role"] = "device"  # groups it under Devices; enables the push flow

    # Credential (Ava-never-has-passwords): the manifest stores only the NAME of
    # an env var (token_env). If the owner pasted the actual VALUE in the connect
    # form, we save it ONCE to the server-side secret store below — never to the
    # manifest, never to the agent — so redeploy never re-prompts. When they gave
    # a value but no name, derive a stable one from the id (e.g. NOTES_TOKEN) so a
    # forker never has to think about env-var naming.
    _mcp_body = body.get("mcp") if isinstance(body.get("mcp"), dict) else {}
    _disc_body = body.get("discover") if isinstance(body.get("discover"), dict) else {}
    token_env_name = (str(body.get("token_env") or "").strip()
                      or str(_mcp_body.get("token_env") or "").strip()
                      or str(_disc_body.get("token_env") or "").strip())
    token_value = str(body.get("token_value") or "")
    if token_value and not token_env_name:
        token_env_name = re.sub(r"[^A-Za-z0-9]", "_", cid).upper() + "_TOKEN"

    probe = str(body.get("probe") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    if probe:
        manifest["service"] = {"name": label, "probe": probe}
    if base_url:
        manifest["base_url"] = base_url

    # MCP server mode — wrap a Model Context Protocol server (url or command).
    # Mutually exclusive with REST actions for a scaffold; edit the YAML to mix.
    mcp_in = body.get("mcp") if isinstance(body.get("mcp"), dict) else None
    if mcp_in:
        mcp: dict = {}
        url = str(mcp_in.get("url") or "").strip()
        command = str(mcp_in.get("command") or "").strip()
        if url:
            mcp["url"] = url
        elif command:
            mcp["command"] = command.split()
        else:
            return JSONResponse({"ok": False,
                                 "error": "mcp needs a url (http) or a command (stdio)"},
                                status_code=400)
        if token_env_name:
            mcp["token_env"] = token_env_name
        if command and mcp_in.get("sandbox") == "docker":
            mcp["sandbox"] = "docker"          # run the stdio server contained
            if mcp_in.get("image"):
                mcp["image"] = str(mcp_in["image"])
        manifest["mcp"] = mcp

    # Auto-discovered facade (GET /tools + POST /call) — from the probe's
    # 'discover' result. Ava lists + calls the app's tools live; the agent still
    # reaches only the policed __tools/__call bridge routes.
    disc_in = body.get("discover") if isinstance(body.get("discover"), dict) else None
    if disc_in and not mcp_in:
        d = {"list": str(disc_in.get("list") or "/tools"),
             "call": str(disc_in.get("call") or "/call")}
        b = str(disc_in.get("base") or base_url or "").strip()
        if b:
            d["base"] = b
        if token_env_name:
            d["token_env"] = token_env_name
        manifest["actions"] = {"discover": d}

    # A token but no mcp/discover block to carry it (a plain iframe or REST app):
    # record it as top-level `auth.token_env` so Ava can present it to the agent
    # tools and the embedded UI (SDK §3 Single sign-on).
    if token_env_name and "mcp" not in manifest and "actions" not in manifest:
        manifest["auth"] = {"token_env": token_env_name}

    # Embedded-app tier (CONNECTOR_SDK.md §3): the app has its own web UI and the
    # user asked for a sidebar tile — Ava reverse-proxies it same-origin under
    # /apps/<id>/ so it inherits the session cookie. No app code changes needed.
    if body.get("ui"):
        ui_url = base_url or str((disc_in or {}).get("base") or "").strip()
        if ui_url:
            manifest["ui"] = {"label": label, "icon": "grid",
                              "embed": "iframe", "url": ui_url}
    # Split-container apps (nginx SPA + separate API port): the UI lives at a
    # DIFFERENT address than the tool surface. ui.api routes the embedded UI's
    # /api calls to the API origin — /apps/<id>/api/<p> is reconstructed as
    # <api base>/api/<p>, which is the identity mapping for any UI that calls
    # /api/* paths (the only ones this proxy route matches).
    ui_url2 = str(body.get("ui_url") or "").strip()
    if ui_url2:
        manifest["ui"] = {"label": label, "icon": "grid",
                          "embed": "iframe", "url": ui_url2}
        api_base = base_url or str((disc_in or {}).get("base") or "").strip()
        if api_base and api_base.rstrip("/") != ui_url2.rstrip("/"):
            manifest["ui"]["api"] = {"base": api_base, "prefix": "/api"}

    actions = []
    # Cap matches the probe's discovery limit — a silently-dropped tail would
    # read as "connected everything" when it didn't.
    for a in ([] if (mcp_in or disc_in) else (body.get("actions") or []))[:50]:
        aid = str(a.get("id", "")).strip().lower()
        path = str(a.get("path", "")).strip()
        if not (_ID_RE.match(aid) and path.startswith("/")):
            continue
        act = {"id": aid,
               "description": str(a.get("description") or aid.replace("_", " ")).strip(),
               "method": "POST" if str(a.get("method", "POST")).upper() == "POST" else "GET",
               "path": path}
        # Persist the explicit tier: the proxy collapses methods to GET/POST, so
        # without it a DELETE endpoint would infer as read. Tier drives JIT
        # consent (read silent / write asks once / destructive asks always).
        acc = str(a.get("access") or "").lower()
        if acc in ("read", "write", "destructive"):
            act["access"] = acc
        if a.get("confirm"):
            act["confirm"] = True              # per-action human-in-the-loop gate
        actions.append(act)
    if actions:
        manifest["actions"] = actions
        # The app's own bearer token (named env var) — so Ava can authenticate
        # to it. The secret value stays in the server-side secret store, never in
        # the manifest (stored below via settings.set_env_secret).
        if token_env_name:
            manifest["auth"] = {"token_env": token_env_name}
        egress: dict = {}
        if base_url:  # actions call base_url server-side; allow it for the agent
            from urllib.parse import urlparse
            u = urlparse(base_url if "//" in base_url else "http://" + base_url)
            if u.hostname:
                # Scheme-aware default port: https://api.example.com -> :443,
                # not the policy renderer's :80 fallback.
                port = u.port or (443 if u.scheme == "https" else 80)
                egress["hosts"] = [f"{u.hostname}:{port}"]
        manifest["egress"] = egress or {"routes": []}

    # Device push channel — the app may POST events with its ingest token.
    if body.get("ingest"):
        manifest["ingest"] = {"enabled": True}

    # Every Hub-created connector gets a perf source by default, so it shows in
    # Vitals from its first proxied call (app_perf.py writes here). The path is
    # OUTSIDE $AVA_HOME/connectors/<id> on purpose: deleting the connector keeps
    # its history, and re-adding the same id resumes it. Apps that keep their own
    # SDK perf log can point `perf.path` elsewhere by editing the manifest.
    manifest["perf"] = {"app": cid,
                        "path": "${AVA_LOGS}/apps/%s/performance.jsonl" % cid}

    d = os.path.join(settings.home("connectors"), cid)
    path = os.path.join(d, "connector.yaml")
    def _write_manifest() -> None:
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Generated by the Setup Hub — edit freely.\n"
                    "# Full schema: connectors/_template/connector.yaml + docs/CONNECTOR_SDK.md\n")
            _yaml.safe_dump(manifest, f, sort_keys=False)

    try:
        # Threadpooled like every other disk write on an async route — see
        # tests/test_no_blocking_routes.py. run_in_threadpool re-raises, so the
        # OSError handling below still applies.
        await run_in_threadpool(_write_manifest)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not write manifest: {e}"},
                            status_code=500)
    # Save the pasted credential ONCE to the server-side store, keyed by the
    # env-var name the manifest references. Must precede discover seeding below so
    # the very first (authenticated) tool-list call can reach the app. A real env
    # var of the same name still wins at read time (settings.env_secret).
    if token_value and token_env_name:
        settings.set_env_secret(token_env_name, token_value)
    connectors.load(force=True)   # pick it up without a restart
    perf_mgmt.refresh_sources()   # …and let Vitals see its perf source now
    if disc_in or mcp_in:
        # Seed the JIT tier cache (ava-tools/1 `access`) so the app's declared
        # tiers apply from the very first agent call — best-effort; a down app
        # just seeds later, on the next discovery.
        await run_in_threadpool(connectors.discover_tools, cid)
    return {"ok": True, "path": path, "manifest": manifest,
            "actions": len(actions),
            "auth_env": token_env_name or None,
            "auth_saved": bool(token_value and token_env_name)}


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
    # tool_files decides the shape: find/call meta tools for dynamic or large
    # static connectors, else one tool per action.
    tools = connectors.tool_files(cid)
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


@router.get("/connectors/{cid}/ingest-token")
def connector_ingest_token(cid: str):
    """The inbound push token a device app presents (Authorization: Bearer …) to
    POST events — the same value as `ava device token <cid>`, surfaced in the UI so
    a non-technical user never needs a terminal. Returned only for a known connector."""
    from . import internal
    if not any(x["id"] == cid for x in connectors.all()):
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    return {"ok": True, "token": internal.ingest_token(cid),
            "enabled": connectors.ingest_enabled(cid),
            "url": f"/api/connectors/{cid}/events"}


@router.get("/connectors/{cid}/last-event")
def connector_last_event(cid: str):
    """Most recent pushed event for a connector — powers the 'waiting for the first
    reading…' verify step so the user sees their device come alive without a terminal."""
    from . import devices as _devices
    rows = _devices.recent(cid, limit=1)
    return {"ok": True, "event": rows[0] if rows else None}


@router.post("/connectors/{cid}/delete")
def delete_connector(cid: str):
    """Remove a user-added connector (its manifest + any generated tools/policy).
    Built-in connectors shipped in the repo are not removable from here."""
    import shutil
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    user_dir = os.path.join(settings.home("connectors"), cid)
    if not os.path.isdir(user_dir):
        if any(x["id"] == cid for x in connectors.all()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and can't be deleted here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    try:
        shutil.rmtree(user_dir)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not remove: {e}"},
                            status_code=500)
    # Best-effort cleanup of generated agent surface so the sandbox can be re-synced.
    tdir = os.path.join(settings.CODE_ROOT, "agent", "mcp_server_content", "connectors", cid)
    pol = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated", f"{cid}.yaml")
    try:
        if os.path.isdir(tdir):
            shutil.rmtree(tdir)
    except OSError:
        pass
    try:
        if os.path.isfile(pol):
            os.remove(pol)
    except OSError:
        pass
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
    return {"ok": True}


@router.post("/connectors/{cid}/deploy")
def deploy_connector(cid: str):
    """One-button version of `ava connector generate` + `cd agent && ./install.sh`:
    render + write this connector's tools and egress policy, then load them into the
    agent sandbox. Push-only devices need no deploy (they work the moment they have a
    token), so we say so rather than pretend to do work."""
    m = {x["id"]: x for x in connectors.all()}.get(cid)
    if not m:
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    steps: list[dict] = []
    gen = generate_connector(cid, write=1)
    if isinstance(gen, JSONResponse):
        return gen
    steps.append({"step": "render", "ok": True,
                  "detail": f"wrote {len(gen.get('wrote') or [])} file(s)"})

    # Nothing for the sandbox to load? Then this is a push-only device.
    if not gen.get("policy") and not gen.get("tools"):
        return {"ok": True, "deployed": False, "steps": steps,
                "detail": "Nothing to deploy — this device only pushes events, which "
                          "works as soon as it has its token."}

    rt = runtime.configured()
    if getattr(rt, "name", "") != "nemoclaw" or not rt.available():
        steps.append({"step": "install", "ok": False,
                      "detail": "The agent sandbox isn't reachable from here. Run "
                                "`cd agent && ./install.sh` on the agent host."})
        return {"ok": False, "deployed": False, "steps": steps,
                "detail": "Wrote the files, but couldn't reach the agent sandbox to load them."}

    install = os.path.join(settings.CODE_ROOT, "agent", "install.sh")
    try:
        cp = subprocess.run(["bash", install], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=600)
        ok = cp.returncode == 0
        tail = cp.stdout.decode(errors="ignore")[-300:]
        steps.append({"step": "install", "ok": ok,
                      "detail": "agent/install.sh" if ok else f"rc={cp.returncode}: {tail}"})
        return {"ok": ok, "deployed": ok, "steps": steps,
                "detail": "Deployed into the agent sandbox." if ok
                          else "install.sh failed — see steps."}
    except Exception as e:  # noqa: BLE001
        steps.append({"step": "install", "ok": False, "detail": str(e)})
        return {"ok": False, "deployed": False, "steps": steps, "detail": "deploy failed"}


def _user_manifest_path(cid: str) -> str:
    return os.path.join(settings.home("connectors"), cid, "connector.yaml")


@router.post("/connectors/{cid}/enabled")
def set_connector_enabled(cid: str, body: dict):
    """Flip a user connector's `enabled` flag in its manifest (the switch `load()`
    honors). Built-in connectors shipped in the repo are read-only here."""
    import yaml as _yaml
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    path = _user_manifest_path(cid)
    if not os.path.isfile(path):
        if any(x["id"] == cid for x in connectors.catalog()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and can't be toggled here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    try:
        with open(path, encoding="utf-8") as f:
            m = _yaml.safe_load(f) or {}
        if not isinstance(m, dict):
            return JSONResponse({"ok": False, "error": "manifest is not a mapping"},
                                status_code=400)
        m["enabled"] = bool(body.get("enabled", True))
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Managed by the Setup Hub — edit freely.\n")
            _yaml.safe_dump(m, f, sort_keys=False)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not update: {e}"}, status_code=500)
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
    return {"ok": True, "enabled": m["enabled"]}


# Rail identity: a glyph name (see frontend lib/icons), and a color that is
# either an author's brand #hex or one of Ava's theme-aware palette slots
# (`var(--app-accent-N)`, which the Hub picker writes so the accent still
# adapts between light and dark). Validated before the file is touched, so a
# bad value can never land in YAML.
_ICON_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_COLOR_RE = re.compile(r"^(?:#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})"
                       r"|var\(--app-accent-[0-7]\))$")


@router.post("/connectors/{cid}/appearance")
def set_connector_appearance(cid: str, body: dict):
    """Patch a user app's rail identity — `ui.icon` and/or `ui.color` — in its
    manifest, the single source of truth `apps()` reads for /api/apps. Only keys
    present in the body are touched; a null/empty value CLEARS that key so the
    tile falls back to the frontend's stable auto-pick. Built-in connectors are
    read-only here, and a connector with no `ui:` block has no tile to restyle."""
    import yaml as _yaml
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    path = _user_manifest_path(cid)
    if not os.path.isfile(path):
        if any(x["id"] == cid for x in connectors.catalog()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and can't be restyled here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    has_icon, icon = "icon" in body, body.get("icon")
    has_color, color = "color" in body, body.get("color")
    if not (has_icon or has_color):
        return JSONResponse({"ok": False, "error": "nothing to update"}, status_code=400)
    if has_icon and icon not in (None, "") and not _ICON_RE.match(str(icon)):
        return JSONResponse({"ok": False, "error": "bad icon name"}, status_code=400)
    if has_color and color not in (None, "") and not _COLOR_RE.match(str(color)):
        return JSONResponse({"ok": False, "error": "color must be a #hex value"}, status_code=400)
    try:
        with open(path, encoding="utf-8") as f:
            m = _yaml.safe_load(f) or {}
        if not isinstance(m, dict):
            return JSONResponse({"ok": False, "error": "manifest is not a mapping"},
                                status_code=400)
        ui = m.get("ui")
        if not isinstance(ui, dict):
            return JSONResponse({"ok": False, "error":
                                 "this connector has no app tile to restyle"},
                                status_code=400)
        for key, present, value in (("icon", has_icon, icon), ("color", has_color, color)):
            if not present:
                continue
            if value in (None, ""):
                ui.pop(key, None)          # clear -> tile falls back to auto-pick
            else:
                ui[key] = str(value)
        m["ui"] = ui
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Managed by the Setup Hub — edit freely.\n")
            _yaml.safe_dump(m, f, sort_keys=False)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not update: {e}"}, status_code=500)
    connectors.load(force=True)
    return {"ok": True, "icon": ui.get("icon"), "color": ui.get("color")}


@router.get("/connectors/{cid}/manifest")
def get_connector_manifest(cid: str):
    """The raw connector.yaml for editing. `editable` is false for built-ins
    (shown read-only) — a bad edit shouldn't be possible on a shipped connector."""
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    upath = _user_manifest_path(cid)
    if os.path.isfile(upath):
        try:
            with open(upath, encoding="utf-8") as f:
                return {"ok": True, "yaml": f.read(), "editable": True}
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    bpath = os.path.join(settings.CODE_ROOT, "connectors", cid, "connector.yaml")
    if os.path.isfile(bpath):
        try:
            with open(bpath, encoding="utf-8") as f:
                return {"ok": True, "yaml": f.read(), "editable": False}
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"}, status_code=404)


@router.post("/connectors/{cid}/manifest")
def put_connector_manifest(cid: str, body: dict):
    """Overwrite a user connector's manifest with validated YAML. Rejects invalid
    YAML, non-mappings, id changes, and edits to built-ins (delete+recreate instead)."""
    import yaml as _yaml
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    text = str(body.get("yaml") or "")
    try:
        m = _yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"invalid YAML: {e}"}, status_code=400)
    if not isinstance(m, dict):
        return JSONResponse({"ok": False, "error":
                             "manifest must be a mapping (key: value lines)"}, status_code=400)
    if str(m.get("id") or cid) != cid:
        return JSONResponse({"ok": False, "error":
                             "changing the id isn't supported — delete and recreate instead"},
                            status_code=400)
    upath = _user_manifest_path(cid)
    if not os.path.isfile(upath):
        # No user manifest: either a read-only built-in, or an unknown id.
        if any(x["id"] == cid for x in connectors.catalog()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and is read-only here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"}, status_code=404)
    try:
        with open(upath, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not write: {e}"}, status_code=500)
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
    return {"ok": True}


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
