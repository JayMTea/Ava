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
import subprocess
import sys
import threading
from collections import deque

from fastapi import APIRouter, Request, UploadFile
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import audit, config, connectors, runtime, settings
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
    from . import approvals
    return {"ok": approvals.decide(aid, decision != "deny")}


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
        "docker": bool(__import__("shutil").which("docker")),
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
            "mcp": connectors._mcp_spec(m) is not None,
            "has_policy": os.path.exists(os.path.join(pol_dir, f"{cid}.yaml")),
            "has_tools": has_tools,
            "renders_policy": connectors.render_egress_policy(cid) is not None,
            # When no `connectors:` list is configured, everything ships enabled.
            "enabled": (cid in enabled) if enabled else True,
        })
    return {"connectors": out}


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
    try:
        _bench_job["result"] = bench.bench(prompt, only=only, max_tokens=max_tokens)
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
# Voice — status / enroll from browser recordings / test similarity
# --------------------------------------------------------------------------- #
@router.get("/voice/status")
def voice_status():
    from . import voice_enroll
    st = voice_enroll.status()
    st["enabled"] = settings.get_bool("features.voice", False, env="AVA_VOICE")
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
    return {"ok": True, "restart_required": True}


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


def _probe(url: str, command: str, token_env: str | None) -> dict:
    """Figure out how to talk to an app so the user doesn't have to classify it:
    try MCP (a start command = stdio, or the URL = MCP-over-HTTP), then a
    discovery endpoint (GET <url>/tools). Returns the detected kind + the tools
    we found, or kind='rest' when nothing is auto-discoverable (the caller then
    asks the user to declare actions — the one thing we can't infer)."""
    from . import mcp_client
    import uuid
    cid = "__probe_" + uuid.uuid4().hex[:6]

    # 1) A start command is unambiguously an MCP stdio server.
    if command:
        spec = {"transport": "stdio", "url": None, "command": command.split(),
                "env": None, "token_env": token_env}
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
        headers = {}
        if token_env and os.environ.get(token_env):
            headers["Authorization"] = "Bearer " + os.environ[token_env]
        r = requests.get(url.rstrip("/") + "/tools", headers=headers, timeout=8)
        data = r.json()
        tools = data.get("tools") if isinstance(data, dict) else (
            data if isinstance(data, list) else None)
        if isinstance(tools, list) and tools:
            return {"ok": True, "kind": "discover", "tools": _slim_tools(tools)}
    except Exception:  # noqa: BLE001
        pass

    # 4) A plain web API — Ava can't guess its endpoints; the user declares them.
    return {"ok": True, "kind": "rest", "tools": [],
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
    return await run_in_threadpool(_probe, url, command, token_env)


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
        tenv = str(mcp_in.get("token_env") or "").strip()
        if tenv:
            mcp["token_env"] = tenv
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
        tenv = str(disc_in.get("token_env") or "").strip()
        if tenv:
            d["token_env"] = tenv
        manifest["actions"] = {"discover": d}

    actions = []
    for a in ([] if (mcp_in or disc_in) else (body.get("actions") or []))[:32]:
        aid = str(a.get("id", "")).strip().lower()
        path = str(a.get("path", "")).strip()
        if not (_ID_RE.match(aid) and path.startswith("/")):
            continue
        act = {"id": aid,
               "description": str(a.get("description") or aid.replace("_", " ")).strip(),
               "method": "POST" if str(a.get("method", "POST")).upper() == "POST" else "GET",
               "path": path}
        if a.get("confirm"):
            act["confirm"] = True              # per-action human-in-the-loop gate
        actions.append(act)
    if actions:
        manifest["actions"] = actions
        # The app's own bearer token (named env var) — so Ava can authenticate
        # to it. The secret value stays in $AVA_HOME's env, never in the manifest.
        rest_token = str(body.get("token_env") or "").strip()
        if rest_token:
            manifest["auth"] = {"token_env": rest_token}
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

    d = os.path.join(settings.home("connectors"), cid)
    path = os.path.join(d, "connector.yaml")
    try:
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Generated by the Setup Hub — edit freely.\n"
                    "# Full schema: connectors/_template/connector.yaml + docs/CONNECTOR_SDK.md\n")
            _yaml.safe_dump(manifest, f, sort_keys=False)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not write manifest: {e}"},
                            status_code=500)
    connectors.load(force=True)  # pick it up without a restart
    return {"ok": True, "path": path, "manifest": manifest,
            "actions": len(actions)}


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
