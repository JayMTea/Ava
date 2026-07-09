#!/usr/bin/env python3
"""Phone voice bridge for Ava — talk to Ava from your phone's browser.

Pipeline:  phone mic (browser MediaRecorder)
           --> POST /api/talk --> ffmpeg decode --> voiceprint gate (your voice only)
           --> faster-whisper (STT, CPU) --> Ava (vLLM open-model 30B :8002)
           --> Piper (TTS) --> wav returned to the phone and played in the browser.

Serve it to your phone over Tailscale (TLS + tailnet-only) with run_bridge.sh.
Everything runs locally on the Spark; nothing leaves the tailnet.
"""

import base64
import asyncio
import hmac
import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

import requests
from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import speaker as spk
import voice_ava as va

# The Experience plane is now a thin face over the ava_bridge package; each module
# owns one concern. Routes below only authenticate, serve, capture input, and
# forward to Ava-the-agent. See ava_bridge/__init__.py for the map.
from ava_bridge import config, state
from ava_bridge.version import version as _ava_version
from ava_bridge.config import (
    RATE, MEDIA_DIR, UPLOAD_DIR, MAX_UPLOAD_BYTES, MAX_DOC_CHARS,
    OC_SESSION, PHONE_THRESHOLD, COOKIE_NAME, IMAGE_EXTS,
)
from ava_bridge.agent import (run_turn as _agent_run_turn, _warm_openclaw,
                              discard_session, get_route, set_route, which_model)
from ava_bridge.chat_store import history_for as _history_for
from ava_bridge.gpu_jobs import (start_image_job, start_upscale_job,
                                   _pickup_image_since, cancel_job)
from ava_bridge.turns import start_turn
from ava_bridge.documents import extract_text, _augment, _parse_ids, _safe_name
from ava_bridge.audio import decode_to_pcm, tts_wav_bytes
from ava_bridge.chat_store import (
    _chat_new, _chat_append, _chat_summary, _chat_session, _atts_meta, _chats_persist,
)
from ava_bridge.auth import (
    auth_gate, _is_authed, _set_session_cookie, _login_locked, _login_record,
    current_password, needs_setup, set_password,
)
from ava_bridge import internal, architecture, learning_mgmt, log_mgmt, config_mgmt, policy_mgmt, perf_mgmt
from ava_bridge import audit, approvals
from ava_bridge import memory_store
from ava_bridge import dashboard, connectors, perf_store, devices
from ava_bridge import arch_watch
from ava_bridge import code_agent
from ava_bridge import hardware
from ava_bridge import web as web_access
from ava_bridge import learning
from ava_bridge.learning import CodeLearner, ChatLearner
_AVA_VERSION = _ava_version()
# Shared-state aliases so the route bodies below read naturally (same objects).
_ATTACH = state.attachments
_ATTACH_LOCK = state.attachments_lock
_CHATS = state.chats
_CHATS_LOCK = state.chats_lock
_JOBS = state.jobs
_JOBS_LOCK = state.jobs_lock
_TURNS = state.turns
_TURNS_LOCK = state.turns_lock
_state = state.heavy
_CODE_LEARNER = CodeLearner()
_CHAT_LEARNER = ChatLearner()

HERE = config.ROOT

# ---- Web templates (externalised to ava_bridge/web/*.html) -------------------
with open(os.path.join(config.WEB_DIR, "index.html"), encoding="utf-8") as _f:
    LEGACY_PAGE = _f.read()
with open(os.path.join(config.WEB_DIR, "login.html"), encoding="utf-8") as _f:
    LOGIN_PAGE = _f.read().replace("Ava", config.AVA_NAME)
with open(os.path.join(config.WEB_DIR, "setup.html"), encoding="utf-8") as _f:
    SETUP_PAGE = _f.read().replace("__BRAND__", config.AVA_NAME)

# ---- New Vite + React SPA (frontend/dist) -----------------------------------
# The primary UI is the built SPA. The single-file legacy UI stays reachable at
# /legacy so nothing breaks while views are ported (strangler-fig migration).
FRONTEND_DIST = os.path.join(config.ROOT, "frontend", "dist")
_spa_index_path = os.path.join(FRONTEND_DIST, "index.html")
if os.path.isfile(_spa_index_path):
    with open(_spa_index_path, encoding="utf-8") as _f:
        SPA_PAGE = _f.read()
else:
    SPA_PAGE = None

app = FastAPI(title="Ava phone bridge")
if SPA_PAGE is not None:
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
# Step-zero auth gate for the whole Experience plane (see ava_bridge/auth.py).
app.middleware("http")(auth_gate)


# --- Authentication routes ---------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request):
    if _is_authed(request):
        return RedirectResponse("/", status_code=303)
    if needs_setup():   # no password yet -> first-run onboarding, not a login wall
        return RedirectResponse("/setup", status_code=303)
    return HTMLResponse(LOGIN_PAGE.replace("<!--MSG-->", ""))


@app.post("/login")
def login_post(request: Request, password: str = Form("")):
    ip = request.client.host if request.client else "?"
    if _login_locked(ip):
        return HTMLResponse(
            LOGIN_PAGE.replace("<!--MSG-->", "Too many attempts &mdash; wait a minute."),
            status_code=429)
    pw = current_password()
    if pw and hmac.compare_digest(password, pw):
        _login_record(ip, ok=True)
        resp = RedirectResponse("/", status_code=303)
        _set_session_cookie(resp)
        return resp
    _login_record(ip, ok=False)
    time.sleep(0.5)
    return HTMLResponse(
        LOGIN_PAGE.replace("<!--MSG-->", "Incorrect password."), status_code=401)


@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    """First-run: create the admin password. Only reachable until one is set."""
    if not needs_setup():
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(SETUP_PAGE.replace("<!--MSG-->", ""))


@app.post("/setup")
def setup_post(request: Request, password: str = Form(""), confirm: str = Form("")):
    if not needs_setup():   # password already set -> can't reset via this screen
        return RedirectResponse("/login", status_code=303)
    password = (password or "").strip()
    if len(password) < 8:
        return HTMLResponse(
            SETUP_PAGE.replace("<!--MSG-->", "Password must be at least 8 characters."),
            status_code=400)
    if password != (confirm or "").strip():
        return HTMLResponse(
            SETUP_PAGE.replace("<!--MSG-->", "Passwords do not match."), status_code=400)
    set_password(password)
    # Fresh install -> continue into the onboarding wizard (hardware, backend,
    # features, connectors). A pre-existing config skips it (setup_completed()).
    from ava_bridge.setup_wizard import setup_completed
    dest = "/" if setup_completed() else "/setup/wizard"
    resp = RedirectResponse(dest, status_code=303)
    _set_session_cookie(resp)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# Lazily-initialised heavy objects (loaded once on first request / startup).
# Voice (STT + speaker gate) is an OPTIONAL extra — requirements-voice.txt.
# Without it the bridge boots and serves normally, just voice-less.
def _ensure_loaded():
    if _state["voice_unavailable"]:
        return
    try:
        if _state["whisper"] is None:
            from faster_whisper import WhisperModel
            _state["whisper"] = WhisperModel(va.WHISPER_MODEL, device="cpu", compute_type="int8")
        if _state["voiceprint"] is None:
            _state["voiceprint"] = spk.load_voiceprint()
        if _state["verifier"] is None and _state["voiceprint"] is not None and PHONE_THRESHOLD > 0:
            _state["verifier"] = spk.SpeakerVerifier()
    except ImportError:
        _state["voice_unavailable"] = True
        print("[ava-bridge] voice disabled — optional STT deps not installed "
              "(pip install -r requirements-voice.txt to enable).", flush=True)


@app.on_event("startup")
def _startup():
    _ensure_loaded()
    gate = "ON" if _state["verifier"] is not None else "OFF"
    print(f"[ava-bridge] ready. speaker gate {gate} (threshold={PHONE_THRESHOLD}).", flush=True)
    # Report the agent runtime and enforce the `agent.required` policy loudly.
    from ava_bridge import runtime
    rt, err = runtime.gate()
    if err:
        print(f"[ava-bridge] WARNING: AGENT RUNTIME REQUIRED BUT MISSING — {err}", flush=True)
        print("[ava-bridge]   chat turns will error until you provision it "
              "(`ava agent provision --install`).", flush=True)
    elif rt.name == "direct":
        print("[ava-bridge] agent runtime absent -> DIRECT (tool-less) chat. "
              "Install NemoClaw for tools/memory/skills (`ava agent provision`).", flush=True)
    else:
        print(f"[ava-bridge] agent runtime: {rt.name} (full agent — tools + memory + skills).", flush=True)
    threading.Thread(target=_warm_openclaw, daemon=True).start()
    # Provide the inference router: embedded in-process unless a standalone
    # unit already owns the port (or config disables it). Same in-process
    # pattern as the samplers below — no extra service on a fresh install.
    from ava_bridge import router_host
    mode = router_host.start_embedded()
    print(f"[ava-bridge] inference router: {mode} "
          f"(:{config.ROUTER_PORT})", flush=True)
    # Start the dashboard's hardware time-series sampler (ring buffer).
    hardware.start_sampler()
    # Start the in-process perf-log rollup (bounds raw storage + serves cold history
    # for month-range charts). Portable — no systemd timer. See ava_bridge/perf_store.
    perf_store.start_scheduler()
    # Start the in-process self-analysis/learning scheduler (local-first; parks
    # improvement proposals for approval). No-op if features.learning is false.
    learning.start_scheduler()
    # Architecture drift watchdog: periodic SSOT check between commits — heals
    # stale diagrams, alerts on structural drift. No-op without a manifest.
    arch_watch.start_scheduler()


@app.get("/", response_class=HTMLResponse)
def index():
    # Serve index.html fresh from disk so a frontend rebuild (which changes the
    # hashed asset filenames) is picked up immediately — without this, a cached
    # page can reference a bundle that the rebuild deleted, giving a black screen.
    if SPA_PAGE is not None:
        try:
            with open(_spa_index_path, encoding="utf-8") as _f:
                return _f.read()
        except OSError:
            return SPA_PAGE
    return LEGACY_PAGE


# PWA shell files live at the dist ROOT (not dist/assets/), so the /assets
# mount doesn't cover them. Served explicitly; both are in auth._PUBLIC_PATHS.
@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    p = os.path.join(FRONTEND_DIST, "manifest.webmanifest")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    return FileResponse(p, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def pwa_sw():
    p = os.path.join(FRONTEND_DIST, "sw.js")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    # no-cache: the worker itself must always revalidate, or updates stall.
    return FileResponse(p, media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    p = os.path.join(FRONTEND_DIST, "favicon.ico")
    if not os.path.isfile(p):
        return JSONResponse({"error": "not built"}, status_code=404)
    return FileResponse(p, media_type="image/x-icon")


@app.get("/legacy", response_class=HTMLResponse)
def legacy_index():
    return LEGACY_PAGE


# --- Optional personal-app routes (overlay) ---------------------------------
# App-specific routes (a media-app reverse-proxy + an app draft-preview surface)
# are specific to the author's apps, so they live in the gitignored overlay
# (overlay/ava_bridge/personal_routes.py). They are mounted below (once `app`
# exists); a fork with no overlay simply skips them.


# First-run onboarding wizard (server-rendered; cookie-gated sub-routes).
from ava_bridge.setup_wizard import router as _wizard_router  # noqa: E402
app.include_router(_wizard_router)

# Setup & control Hub API (cookie-gated /api/hub/* — agent, connectors, system).
from ava_bridge.hub_api import router as _hub_router  # noqa: E402
app.include_router(_hub_router)

# Mount the optional overlay personal-app routes now that `app` exists.
try:
    from overlay.ava_bridge import personal_routes as _personal_routes
    _personal_routes.register(app)
except Exception:  # noqa: BLE001 — no overlay (fork) or a broken overlay: core still boots
    pass


@app.get("/api/health")
def health():
    # Public (pre-auth) endpoint: expose only what the unauthenticated client
    # needs for the token counter; keep speaker-gate internals private.
    return {
        "ok": True,
        "model": va.AVA_MODEL,
        "ctx_max": config.CTX_MAX,
        "ctx_base": config.CTX_BASE,
        "brand": config.AVA_NAME,
        "version": _AVA_VERSION,
    }


@app.get("/api/brand")
def brand():
    """Assistant name/tagline (config brand.* -> ava.yaml/env). Lets a fork
    re-brand the whole UI without editing React — the SPA reads this."""
    return {"name": config.AVA_NAME, "tagline": config.AVA_TAGLINE,
            "version": _AVA_VERSION}


@app.get("/api/hardware")
async def api_hardware():
    """Live hardware snapshot for the app's floating monitor: GPU
    utilisation/temperature, unified/system memory used/free, and CPU
    utilisation. Auth-gated like every other /api route."""
    return await run_in_threadpool(hardware.stats)


# ===== DASHBOARD — Ava Command Center (Vitals + Operations) ==================
# All cookie-gated /api/* (browser auth); read-first wrappers over ava_bridge
# modules.

@app.get("/api/perf/summary")
async def api_perf_summary(app: str | None = None, category: str | None = None,
                           since: str | None = None):
    return await run_in_threadpool(dashboard.perf_summary, app, category, since)


@app.get("/api/perf/series")
async def api_perf_series(metric: str = "tokens_per_sec", bucket: str = "1h",
                          since: str = "24h", app: str | None = None,
                          category: str | None = None):
    return await run_in_threadpool(dashboard.perf_series, metric, bucket, since,
                                   app, category)


@app.get("/api/perf/recent")
async def api_perf_recent(limit: int = 50, app: str | None = None,
                          category: str | None = None):
    return await run_in_threadpool(dashboard.perf_recent, limit, app, category)


@app.get("/api/perf/cost")
async def api_perf_cost(since: str = "7d", group: str = "model"):
    return await run_in_threadpool(dashboard.perf_cost, since, group)


@app.get("/api/hardware/history")
async def api_hardware_history(since: str = "1d", bucket: str = "5m"):
    return {"ok": True, "samples": await run_in_threadpool(hardware.history_series, since, bucket)}


@app.get("/api/jobs")
async def api_jobs(status: str | None = None, kind: str | None = None,
                   limit: int = 100):
    return await run_in_threadpool(dashboard.jobs_list, status, kind, limit)


@app.get("/api/turns")
async def api_turns(limit: int = 50, active: bool = False):
    return await run_in_threadpool(dashboard.turns_list, limit, active)


@app.get("/api/code-turns")
async def api_code_turns(limit: int = 30):
    return await run_in_threadpool(dashboard.code_turns_list, limit)


@app.get("/api/ops/summary")
async def api_ops_summary():
    return await run_in_threadpool(dashboard.ops_summary)


@app.get("/api/ops/schedule")
async def api_ops_schedule():
    return await run_in_threadpool(dashboard.ops_schedule)


@app.get("/api/ops/services")
async def api_ops_services():
    return await run_in_threadpool(dashboard.ops_services)


@app.get("/api/ops/tools")
async def api_ops_tools(limit: int = 15):
    return await run_in_threadpool(dashboard.ops_tools, limit)


@app.get("/api/ops/connectors")
async def api_ops_connectors():
    return await run_in_threadpool(dashboard.connectors_info)


@app.api_route("/internal/connector/{cid}/{action}", methods=["POST", "GET"])
async def internal_connector(cid: str, action: str, request: Request):
    """Generic connector-action proxy: forwards a generated tool's call to the
    connector's own host-local API. ONE route serves every connector, so adding
    an app needs no new bridge code — just a connector manifest.

    Two reserved actions bridge a connector's DYNAMIC tool set (see the manifest
    `actions.discover` block): __tools lists them, __call invokes one by name."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if action == "__tools":
        q = request.query_params.get("q", "")
        try:
            limit = int(request.query_params.get("limit", "0"))
        except ValueError:
            limit = 0
        return JSONResponse(await run_in_threadpool(connectors.discover_tools, cid, q, limit))
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if action == "__call":
        name = (body.get("name") or "").strip()
        if not name:
            return JSONResponse({"error": "missing tool name"}, status_code=400)
        gate = await run_in_threadpool(approvals.gate, cid, name, body.get("arguments") or {})
        if gate not in ("skip", "approved"):
            audit.record("egress", connector=cid, tool=name, status=f"blocked:{gate}")
            return JSONResponse({"error": f"not run — awaiting-approval {gate}"}, status_code=403)
        data, status = await run_in_threadpool(
            connectors.call_discovered, cid, name, body.get("arguments") or {})
        # Egress record for the flight recorder: the agent reached out to a
        # connector/MCP tool. This is the closest in-process egress signal we
        # have (the sandbox's network denials live in openclaw, not here).
        audit.record("egress", connector=cid, tool=name, status=status)
        return JSONResponse(data, status_code=status)
    # Merge query params (GET-style tools) with any JSON body so declared actions
    # get their args regardless of how the tool passed them.
    args = dict(request.query_params)
    if isinstance(body, dict):
        args.update(body)
    gate = await run_in_threadpool(approvals.gate, cid, action, args)
    if gate not in ("skip", "approved"):
        audit.record("egress", connector=cid, tool=action, status=f"blocked:{gate}")
        return JSONResponse({"error": f"not run — awaiting-approval {gate}"}, status_code=403)
    data, status = await run_in_threadpool(connectors.call_action, cid, action, args)
    audit.record("egress", connector=cid, tool=action, status=status)
    return JSONResponse(data, status_code=status)


@app.get("/api/apps")
async def api_apps():
    """Left-rail app registry (connectors with a `ui:` block). Drives the SPA nav
    so a new app appears by dropping a connector folder — no frontend edits."""
    return await run_in_threadpool(lambda: {"apps": connectors.apps()})


# --- Devices: inbound "app → Ava" event channel ------------------------------
# The one direction the connector proxy above can't do: let a user's own device
# app hand Ava an event when IT decides to (motion detected, threshold crossed),
# rather than only being polled. The app owns all device I/O; Ava receives, stores
# (ava_bridge/devices.py), surfaces it live (device.event on the ops SSE stream),
# and — for notify/critical — raises a dashboard alert. Auth is a per-connector
# bearer token (internal.ingest_token), NOT the sandbox internal token: an app
# that can push events can't reach the /internal/* tool surface. This route is
# exempted from the cookie gate in ava_bridge/auth.py (auth._is_ingest).
@app.post("/api/connectors/{cid}/events")
async def connector_events(cid: str, request: Request):
    if not internal.verify_ingest(cid, internal.bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not await run_in_threadpool(connectors.ingest_enabled, cid):
        return JSONResponse(
            {"error": f"connector {cid} has not enabled ingest"}, status_code=404)
    if not devices.allow(cid):   # per-connector token bucket — flood protection
        return JSONResponse({"error": "rate limited"}, status_code=429)
    raw = await request.body()
    if len(raw) > 64 * 1024:   # a single event is tiny; cap chatty/hostile apps
        return JSONResponse({"error": "event too large"}, status_code=413)
    try:
        payload = json.loads(raw or b"{}")
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    try:
        rec = await run_in_threadpool(devices.record_event, cid, payload)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "seq": rec["seq"]}


@app.get("/api/devices")
async def api_devices():
    """Device connectors + their recent pushed events (dashboard Devices view)."""
    def _build():
        rows = connectors.devices()
        for r in rows:
            r["last_event"] = devices.last_event_ts(r["id"])
            r["events"] = devices.recent(r["id"], limit=20)
        return {"devices": rows}
    return await run_in_threadpool(_build)


# --- Generic app surface -----------------------------------------------------
# Two same-origin proxies serve EVERY connector that declares a `ui:` block, so
# a third-party app renders in Ava's shell with no bespoke bridge code:
#   /apps/<id>/api/<path>  — browser data-proxy: forwards to the app's API base
#                            with its bearer token injected (browser never sees it)
#   /apps/<id>/<path>      — iframe proxy: reverse-proxies the app's own web UI
#                            same-origin, so it inherits Ava's session cookie
# The /api/ route is declared first so it wins over the catch-all below.
def _proxy_response(r: "requests.Response") -> Response:
    fwd = {}
    for hkey in ("cache-control", "etag", "last-modified", "expires"):
        hv = r.headers.get(hkey)
        if hv:
            fwd[hkey] = hv
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type"), headers=fwd)


@app.api_route("/apps/{cid}/api/{path:path}",
               methods=["GET", "POST", "DELETE", "PATCH", "PUT"])
async def app_api_proxy(cid: str, path: str, request: Request):
    cfg = await run_in_threadpool(connectors.app_api, cid)
    if not cfg:
        return JSONResponse({"error": f"connector {cid} has no ui.api"}, status_code=404)
    url = f"{cfg['base']}{cfg['prefix']}/{path}"
    params = dict(request.query_params)
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if cfg["token"]:
        headers["Authorization"] = "Bearer " + cfg["token"]
    body = await request.body() if request.method in ("POST", "PATCH", "PUT") else None

    def _do():
        if request.method in ("POST", "PATCH", "PUT"):
            return requests.request(request.method, url, params=params, data=body,
                                    headers=headers, timeout=200)
        if request.method == "DELETE":
            return requests.delete(url, params=params, headers=headers, timeout=60)
        return requests.get(url, params=params, headers=headers, timeout=60)

    try:
        r = await run_in_threadpool(_do)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{cid} api unreachable: {e}"}, status_code=502)
    return _proxy_response(r)


@app.api_route("/apps/{cid}/{path:path}",
               methods=["GET", "POST", "DELETE", "PATCH", "PUT"])
async def app_ui_proxy(cid: str, path: str, request: Request):
    meta = await run_in_threadpool(connectors.app, cid)
    if not meta or meta.get("embed") != "iframe" or not meta.get("url"):
        return JSONResponse({"error": f"connector {cid} is not an iframe app"},
                            status_code=404)
    url = f"{meta['url'].rstrip('/')}/{path}"
    params = dict(request.query_params)
    body = await request.body() if request.method in ("POST", "PATCH", "PUT") else None

    def _do():
        if request.method in ("POST", "PATCH", "PUT"):
            ct = request.headers.get("content-type", "application/json")
            return requests.request(request.method, url, params=params, data=body,
                                    headers={"content-type": ct}, timeout=180)
        if request.method == "DELETE":
            return requests.delete(url, params=params, timeout=60)
        return requests.get(url, params=params, timeout=60)

    try:
        r = await run_in_threadpool(_do)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{cid} app unreachable: {e}"}, status_code=502)
    return _proxy_response(r)


@app.get("/api/ops/alerts")
async def api_ops_alerts():
    return await run_in_threadpool(dashboard.ops_alerts)


@app.get("/api/stream/ops")
async def api_stream_ops(request: Request):
    """Server-Sent Events: live turn/job/hardware/alert deltas for the Operations
    tab. A 1 Hz producer snapshots state, diffs it, and emits only changes; alerts
    re-evaluate every ~5s; a heartbeat keeps the connection alive."""
    async def gen():
        last_turns: dict = {}
        last_jobs: dict = {}
        last_alerts: set = set()
        last_beat = time.time()
        tick = 0
        # Only stream device events that arrive AFTER this client connects (don't
        # replay history into a fresh toast storm).
        last_dev_seq = devices.current_seq()
        # Prime clients with the current alert set on connect.
        try:
            init = await run_in_threadpool(dashboard.ops_alerts)
            yield f"event: alert.state\ndata: {json.dumps(init.get('active', []))}\n\n"
        except Exception:  # noqa: BLE001
            pass
        while True:
            if await request.is_disconnected():
                break
            try:
                snap = await run_in_threadpool(dashboard.live_snapshot)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1.0)
                continue

            for tid, cur in snap["turns"].items():
                if last_turns.get(tid) != cur:
                    yield f"event: turn.update\ndata: {json.dumps({'id': tid, **cur})}\n\n"
            last_turns = snap["turns"]

            for jid, cur in snap["jobs"].items():
                if last_jobs.get(jid) != cur:
                    yield f"event: job.update\ndata: {json.dumps({'id': jid, **cur})}\n\n"
            last_jobs = snap["jobs"]

            if snap.get("hw"):
                yield f"event: hardware.tick\ndata: {json.dumps(snap['hw'])}\n\n"

            # Live device events pushed by connector apps (ava_bridge/devices.py).
            last_dev_seq, new_events = devices.live_since(last_dev_seq)
            for ev in new_events:
                yield f"event: device.event\ndata: {json.dumps(ev)}\n\n"

            # Alerts every ~5 ticks (5s).
            if tick % 5 == 0:
                try:
                    res = await run_in_threadpool(dashboard.ops_alerts)
                    active = res.get("active", [])
                    ids = {a["id"] for a in active}
                    for a in active:
                        if a["id"] not in last_alerts:
                            yield f"event: alert.raise\ndata: {json.dumps(a)}\n\n"
                    for gone in last_alerts - ids:
                        yield f"event: alert.clear\ndata: {json.dumps({'id': gone})}\n\n"
                    last_alerts = ids
                except Exception:  # noqa: BLE001
                    pass

            now = time.time()
            if now - last_beat > 15:
                last_beat = now
                yield ": keepalive\n\n"

            tick += 1
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/model")
async def api_model_get():
    """Current model choice + selectable backends for the chat dropdown."""
    r = await run_in_threadpool(get_route)
    return r or {"mode": None, "backends": []}


@app.post("/api/model")
async def api_model_set(mode: str = Form(...)):
    """Pick which backend the router prefers as primary (a backend id, e.g.
    'omni', or a declared cloud-fallback id)."""
    r = await run_in_threadpool(set_route, (mode or "").strip())
    if not r or r.get("error"):
        return JSONResponse(
            {"error": (r or {}).get("error", "inference router unreachable")},
            status_code=400)
    return r


# ---- Internal capability surface (Ava's sandboxed MCP tools call back here) --
# Token-gated; reached from the sandbox via host.openshell.internal:8096 under
# the ava-knowledge egress policy. Lets Ava read the text of files the user uploaded.
@app.get("/internal/documents")
def internal_documents(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return internal.documents_payload()


@app.post("/internal/extract")
async def internal_extract(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    file_id = str(body.get("file_id", "")).strip()
    if not file_id:
        return JSONResponse({"error": "file_id required"}, status_code=400)
    try:
        max_chars = int(body.get("max_chars") or 0)
    except (TypeError, ValueError):
        max_chars = 0
    payload = internal.extract_payload(file_id, max_chars)
    if payload is None:
        return JSONResponse({"error": "no such document"}, status_code=404)
    return payload


@app.get("/internal/devices/events")
def internal_device_events(request: Request):
    """Recent pushed device events, for Ava's `device_events` MCP tool. Token-gated
    (reuses the ava-knowledge /internal egress; scoped to the `content` group)."""
    if not internal.authorized(request, scope="content"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = (request.query_params.get("connector") or "").strip() or None
    try:
        limit = min(int(request.query_params.get("limit") or 50), 200)
    except (TypeError, ValueError):
        limit = 50
    return {"events": devices.recent(cid, limit=limit)}


@app.post("/internal/run-gpu-job")
async def internal_run_gpu_job(request: Request):
    """Render an image on Ava's behalf so the CHAT gets a live, real progress %.

    Ava's `run_gpu_job` MCP tool calls this instead of hitting the GPU service directly.
    The bridge then owns the the GPU service render (start_image_job -> gpu_service, tracked
    over the the GPU service websocket), so /api/job/{id} reports a TRUE percentage the chat
    bar polls — exactly like the manual Generate button. Token-gated
    (reuses the ava-knowledge /internal egress; no new policy).
    """
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    prompt = str(body.get("prompt", "")).strip()
    if not prompt:
        return JSONResponse({"error": "prompt required"}, status_code=400)
    try:
        width = int(body.get("width") or 1024)
        height = int(body.get("height") or 1024)
    except (TypeError, ValueError):
        width, height = 1024, 1024
    kw = {"width": width, "height": height}
    neg = str(body.get("negative", "")).strip()
    if neg:
        kw["negative"] = neg
    # Fidelity knobs (all optional): cfg/guidance = how literally the render obeys
    # the prompt, steps = detail, seed = reproducibility. Clamped to safe ranges.
    try:
        if body.get("cfg") is not None:
            kw["cfg"] = min(max(float(body["cfg"]), 1.0), 20.0)
    except (TypeError, ValueError):
        pass
    try:
        if body.get("steps") is not None:
            kw["steps"] = min(max(int(body["steps"]), 8), 60)
    except (TypeError, ValueError):
        pass
    try:
        if body.get("seed") is not None:
            kw["seed"] = int(body["seed"]) & 0x7FFFFFFF
    except (TypeError, ValueError):
        pass
    # Multi-person regional prompts: list of {text,x,y,w,h,weight} sub-prompts,
    # each pinned to its own area so distinct people don't blend together.
    regions = body.get("regions")
    if isinstance(regions, list) and regions:
        clean = []
        for r in regions:
            if isinstance(r, dict) and str(r.get("text", "")).strip():
                clean.append(r)
        if clean:
            kw["regions"] = clean
    # Optional checkpoint/model key from the router (e.g. juggernaut, ponyreal).
    mdl = body.get("model")
    if mdl and str(mdl).strip():
        kw["model"] = str(mdl).strip()
    job_id = start_image_job(prompt, source="agent", **kw)
    return {"job": {"id": job_id, "kind": "image", "status": "running"}}


# Personal-app routes (reverse proxies + their /internal/* read-backs) live in
# the optional gitignored overlay (overlay/ava_bridge/*), registered at boot.


# ---- Architecture capability (Ava reads/updates her own SSOT diagrams+code) --
# Same token gate. Lets Ava get the architecture manifest + drift report, render
# the diagrams, and apply manifest edits (auto-committed). Backed by arch.py.
@app.get("/internal/architecture")
def internal_architecture(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return architecture.summary_payload()


@app.post("/internal/architecture/describe")
async def internal_arch_describe(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name required"}, status_code=400)
    return architecture.describe_payload(name)


@app.post("/internal/architecture/check")
def internal_arch_check(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return architecture.check_payload()


@app.post("/internal/architecture/sync")
async def internal_arch_sync(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    commit = bool(body.get("commit", True))
    return architecture.sync_payload(commit=commit, message=body.get("message"))


@app.post("/internal/architecture/update")
async def internal_arch_update(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    new_yaml = body.get("yaml") or ""
    if not new_yaml.strip():
        return JSONResponse({"error": "yaml required (the full new manifest)"}, status_code=400)
    commit = bool(body.get("commit", True))
    return architecture.update_payload(new_yaml, message=body.get("message"), commit=commit)


@app.get("/internal/learning/scripts")
async def internal_learning_read(request: Request):
    """Allow Ava to read current learning digest scripts (read-only access)."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return learning_mgmt.read_digest_scripts()


@app.post("/internal/learning/update")
async def internal_learning_update(request: Request):
    """Allow Ava to update learning digest scripts with improved descriptions/rationales."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    
    script_type = body.get("script_type") or ""
    updates = body.get("updates") or {}
    content = body.get("content")
    
    if not script_type:
        return JSONResponse({"error": "script_type required (daily, weekly, or both)"}, status_code=400)
    
    result = learning_mgmt.update_digest_scripts(script_type, updates, content)
    status = 200 if result.get("success") else 400
    return JSONResponse(result, status_code=status)


@app.get("/internal/learning/state")
async def internal_learning_state(request: Request, state_type: str = "all", limit: int = 10, patterns: bool = True):
    """Allow Ava to read her own learning state (code/chat cycles, patterns)."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    limit = min(max(limit, 1), 100)
    result = {}
    
    if state_type in ["code", "all"]:
        with state.code_learning_state_lock:
            cycles = state.code_learning_state.get("cycles", [])[-limit:]
            inline_fixes = state.code_learning_state.get("inline_fixes", [])[-limit:]
            result["code"] = {
                "cycles": cycles,
                "inline_fixes": inline_fixes,
                "total_cycles": len(state.code_learning_state.get("cycles", [])),
                "total_fixes": len(state.code_learning_state.get("inline_fixes", [])),
            }
    
    if state_type in ["chat", "all"]:
        with state.chat_learning_state_lock:
            cycles = state.chat_learning_state.get("cycles", [])[-limit:]
            result["chat"] = {
                "cycles": cycles,
                "total_cycles": len(state.chat_learning_state.get("cycles", [])),
            }
    
    return JSONResponse(result)


@app.get("/internal/learning/chats")
async def internal_learning_chats(request: Request, action: str = "list", chat_id: str = None, limit: int = 50, metadata: bool = True):
    """Allow Ava to read chat history for reflection and pattern analysis."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    limit = min(max(limit, 1), 200)
    
    if action == "list":
        # Return list of chats with metadata
        import json
        chats_file = Path('data/chats.json')
        if not chats_file.exists():
            return JSONResponse({"chats": []})
        try:
            with open(chats_file) as f:
                data = json.load(f)
            chats = []
            for cid, chat in data.items():
                chats.append({
                    "id": cid,
                    "title": chat.get("title", "Untitled"),
                    "created": chat.get("created"),
                    "updated": chat.get("updated"),
                    "message_count": len(chat.get("messages", [])),
                })
            return JSONResponse({"chats": chats[:limit]})
        except Exception:
            return JSONResponse({"chats": []})
    
    elif action == "read":
        if not chat_id:
            return JSONResponse({"error": "chat_id required"}, status_code=400)
        import json
        chats_file = Path('data/chats.json')
        if not chats_file.exists():
            return JSONResponse({"error": "no chats"}, status_code=404)
        try:
            with open(chats_file) as f:
                data = json.load(f)
            chat = data.get(chat_id)
            if not chat:
                return JSONResponse({"error": "chat not found"}, status_code=404)
            
            messages = chat.get("messages", [])[-limit:]
            if not metadata:
                # Strip metadata, keep only role and content
                messages = [{"role": m.get("role"), "content": m.get("content")} for m in messages]
            
            return JSONResponse({
                "chat_id": chat_id,
                "title": chat.get("title"),
                "created": chat.get("created"),
                "updated": chat.get("updated"),
                "messages": messages,
            })
        except Exception:
            return JSONResponse({"error": "failed to read chat"}, status_code=500)
    
    else:
        return JSONResponse({"error": "action must be 'list' or 'read'"}, status_code=400)


@app.get("/internal/learning/code-turns")
async def internal_learning_code_turns(request: Request, cycle_id: str = None, status_filter: str = "all", diffs: bool = True, reasoning: bool = True, limit: int = 10):
    """Allow Ava to read her code editing turns and proposals for learning."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    limit = min(max(limit, 1), 100)
    
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        if cycle_id:
            cycles = [c for c in cycles if c.get("id") == cycle_id]
        cycles = cycles[-limit:]
    
    result = []
    for cycle in cycles:
        proposals = cycle.get("proposals", [])
        
        # Filter by status if requested
        if status_filter != "all":
            proposals = [p for p in proposals if p.get("status") == status_filter]
        
        cycle_data = {
            "id": cycle.get("id"),
            "timestamp": cycle.get("timestamp"),
            "proposals": proposals,
        }
        
        # Strip diffs if not requested
        if not diffs:
            for p in cycle_data["proposals"]:
                p.pop("diff", None)
        
        # Strip reasoning if not requested
        if not reasoning:
            for p in cycle_data["proposals"]:
                p.pop("reasoning", None)
        
        result.append(cycle_data)
    
    return JSONResponse({"code_turns": result, "count": len(result)})


# === LOG MANAGEMENT ENDPOINTS ===

@app.post("/internal/logs")
async def internal_logs(request: Request):
    """Allow Ava to read system logs for debugging."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
    except:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    result = log_mgmt.read_logs(
        source=body.get("source", "systemd"),
        service=body.get("service"),
        component=body.get("component"),
        lines=body.get("lines", 50),
        level=body.get("level"),
        since=body.get("since", "1h")
    )
    
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.post("/internal/perf")
async def internal_perf(request: Request):
    """Let Ava read + summarise generation-performance across all her apps."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    result = perf_mgmt.read_performance(
        app=body.get("app"),
        category=body.get("category"),
        since=body.get("since"),
        limit=body.get("limit", 50),
        summary=body.get("summary", True),
    )
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


# === CONFIGURATION MANAGEMENT ENDPOINTS ===

@app.get("/internal/config")
async def internal_config_get(request: Request, component: str):
    """Allow Ava to read configuration."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    result = config_mgmt.read_config(component)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.post("/internal/config")
async def internal_config_post(request: Request):
    """Allow Ava to update configuration."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
    except:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    result = config_mgmt.update_config(
        component=body.get("component"),
        updates=body.get("updates", {}),
        reason=body.get("reason", "No reason provided")
    )
    
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


# === POLICY MANAGEMENT ENDPOINTS ===

@app.get("/internal/policies")
async def internal_policies_get(request: Request):
    """Allow Ava to list all policies."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    result = policy_mgmt.list_policies()
    return JSONResponse(result, status_code=200)


@app.get("/internal/policies/{policy_name}")
async def internal_policy_read(request: Request, policy_name: str):
    """Allow Ava to read a specific policy."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    result = policy_mgmt.read_policy(policy_name)
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.post("/internal/policies")
async def internal_policies_post(request: Request):
    """Allow Ava to update a policy."""
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
    except:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    
    result = policy_mgmt.update_policy(
        name=body.get("name"),
        updates=body.get("updates", {}),
        reason=body.get("reason", "No reason provided")
    )
    
    status = 200 if result.get("ok") else 400
    return JSONResponse(result, status_code=status)


@app.post("/internal/code-change")
async def internal_code_change(request: Request):
    """Hand off a code-change request to Claude (uses ANTHROPIC_API_KEY).

    Ava's `code_change_request` MCP tool calls this. code_agent drives Claude
    through the repo, then applies the access policy: safe files are written +
    committed autonomously; protected files (auth/config/policy/deploy) are
    parked as a pending proposal in the user's approval bucket on the Learning page;
    secrets/binaries are refused. The Claude tool loop runs synchronously and can
    take a while, so run it off the event loop.
    """
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)

    request_text = (body.get("request") or "").strip()
    context_text = (body.get("context") or "").strip()
    files_list = body.get("files") or []
    project = (body.get("project") or "ava").strip() or "ava"
    actor = (body.get("actor") or "Ava").strip() or "Ava"
    if not request_text:
        return JSONResponse({"error": "empty request"}, status_code=400)

    print(f"[ava-bridge] [DEBUG] /internal/code-change received: "
          f"project={project!r} actor={actor!r} request={request_text[:80]!r} files={files_list}", flush=True)

    try:
        result = await run_in_threadpool(
            code_agent.run_change_request,
            request_text, context_text, files_list, "code_change_request",
            None, project, actor,
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"code change failed: {e}"}, status_code=500)

    status = 500 if result.get("status") == "error" else 200
    return JSONResponse(result, status_code=status)


# An app draft-preview route moved to the optional overlay
# (overlay/ava_bridge/personal_routes.py), registered above when the overlay is
# present. A fork without it simply has no such route.


# Bespoke connected-app routes were removed: connected apps ride the generic
# connector proxy (/internal/connector/<id>/* for agent tools, /apps/<id>/api/*
# for the browser tab) — see each connector's connector.yaml.
# --- Web access (self-hosted search + SSRF-guarded reader) -------------------
# HOST-MEDIATED: Ava's sandbox tools call these token-gated routes (reusing the
# ava-knowledge egress policy — no new open egress). The HOST runs the private
# SearXNG search and the SSRF-guarded page fetch; Ava never touches the internet
# directly and no API keys ever enter the sandbox.
@app.post("/internal/web/search")
async def internal_web_search(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    query = (body or {}).get("query") or (body or {}).get("q") or ""
    count = (body or {}).get("count")
    try:
        return JSONResponse(await run_in_threadpool(web_access.search, query, count))
    except web_access.WebAccessError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"web search failed: {e}"}, status_code=502)


@app.post("/internal/web/fetch")
async def internal_web_fetch(request: Request):
    if not internal.authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    url = (body or {}).get("url") or ""
    try:
        return JSONResponse(await run_in_threadpool(web_access.fetch, url))
    except web_access.WebAccessError as e:
        # Blocked/invalid target — a 400 so Ava learns the URL was refused.
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"web fetch failed: {e}"}, status_code=502)


@app.post("/api/talk")
async def talk(audio: UploadFile = File(...), history: str = Form("[]"),
               attachments: str = Form("[]"), chat_id: str = Form("")):
    _ensure_loaded()
    if _state["voice_unavailable"]:
        return JSONResponse(
            {"error": "voice not installed — pip install -r requirements-voice.txt"},
            status_code=503)
    raw = await audio.read()
    if not raw:
        return JSONResponse({"error": "empty audio"}, status_code=400)

    try:
        pcm = decode_to_pcm(raw)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"decode failed: {e}"}, status_code=400)

    if len(pcm) < RATE:  # < ~0.5s of audio
        return {"accepted": True, "text": "", "reply": "", "sim": None,
                "note": "Too short — hold the button and speak a full sentence."}

    # Temporary diagnostic: dump the exact audio the browser sent so we can check
    # the capture level / device. Enabled only when AVA_DEBUG_TALK=1.
    if os.environ.get("AVA_DEBUG_TALK") == "1":
        try:
            import wave as _wave
            import numpy as _np
            _samples = _np.frombuffer(pcm, dtype=_np.int16).astype(_np.float32) / 32768.0
            _rms = float(_np.sqrt(_np.mean(_samples * _samples))) if _samples.size else 0.0
            _peak = float(_np.max(_np.abs(_samples))) if _samples.size else 0.0
            _dur = _samples.size / RATE
            _dbg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "logs", "last_talk.wav")
            os.makedirs(os.path.dirname(_dbg_path), exist_ok=True)
            with _wave.open(_dbg_path, "wb") as _w:
                _w.setnchannels(1)
                _w.setsampwidth(2)
                _w.setframerate(RATE)
                _w.writeframes(pcm)
            print(f"[ava-debug] talk audio: dur={_dur:.1f}s rms={_rms:.4f} "
                  f"peak={_peak:.3f} -> {_dbg_path}", flush=True)
        except Exception as _e:  # noqa: BLE001
            print(f"[ava-debug] capture failed: {_e}", flush=True)

    # Speaker gate — your voice only.
    sim = None
    if _state["verifier"] is not None:
        sim = spk.cosine(_state["voiceprint"], _state["verifier"].embed_pcm(pcm))
        if sim < PHONE_THRESHOLD:
            return {"accepted": False, "sim": round(float(sim), 3),
                    "threshold": PHONE_THRESHOLD,
                    "reply": "Sorry, I only respond to the enrolled voice."}

    text = va.transcribe(_state["whisper"], pcm)
    if not text:
        return {"accepted": True, "text": "", "reply": "",
                "sim": round(float(sim), 3) if sim is not None else None,
                "note": "I didn't catch that — try again."}

    sim_out = round(float(sim), 3) if sim is not None else None

    # Route through Ava — she decides whether to call the run_gpu_job tool.
    ids = _parse_ids(attachments)
    agent_text = _augment(text, ids)
    agent_text = memory_store.augment_with_recall(agent_text, text, chat_id)
    sid = _chat_session(chat_id) if chat_id else None
    if chat_id:
        _chat_append(chat_id, "user", text, _atts_meta(ids))
    t0 = time.time()
    tools: list[str] = []
    try:
        reply, tools = _agent_run_turn(agent_text, session_id=sid,
                                       history=_history_for(chat_id))
    except Exception as e:  # noqa: BLE001
        # The turn may have timed out *after* the image tool rendered — salvage
        # the finished picture so it still appears on the user's screen.
        job = _pickup_image_since(t0, wait=120)
        if job:
            reply = "Here's the image you asked for."
            m = which_model()
            if chat_id:
                _chat_append(chat_id, "assistant", reply, model=m, tools_used=tools)
            return {
                "accepted": True, "text": text, "reply": reply, "sim": sim_out,
                "audio": base64.b64encode(tts_wav_bytes(reply)).decode(),
                "model": m,
                "tools_used": tools,
                "job": job,
            }
        return JSONResponse({"error": f"Ava unreachable: {e}"}, status_code=502)

    m = which_model()
    if chat_id:
        _chat_append(chat_id, "assistant", reply, model=m, tools_used=tools)
    resp = {
        "accepted": True,
        "text": text,
        "reply": reply,
        "sim": sim_out,
        "audio": base64.b64encode(tts_wav_bytes(reply)).decode(),
        "model": m,
        "tools_used": tools,
    }
    if any("run_gpu_job" in t for t in tools):
        job = _pickup_image_since(t0, wait=120)
        if job:
            resp["job"] = job
    return resp


@app.post("/api/generate")
async def generate(prompt: str = Form(...), width: int = Form(1024),
                   height: int = Form(1024), steps: int = Form(28),
                   chat_id: str = Form(""), chat_text: str = Form("")):
    """Directly start an image render from a text prompt (no voice).

    GOVERNANCE NOTE: this is the one sanctioned direct path that bypasses the
    OpenClaw agent. All conversational turns (/api/talk, /api/talk-text) route
    through Ava-the-agent (ask_openclaw) so persona, memory and tool-policies
    apply, and agent-initiated images go via her `run_gpu_job` MCP tool. This
    endpoint is a deliberate UX fast-path for an EXPLICIT user "generate" action
    (a button, not a sentence): there is no reasoning to govern, and it stays on
    the same local host the GPU service behind the same egress boundary. Keep this the
    exception — do not add conversational logic here; send that through the agent.
    """
    prompt = prompt.strip()
    if not prompt:
        return JSONResponse({"error": "empty prompt"}, status_code=400)
    if chat_id:
        _chat_append(chat_id, "user", (chat_text or prompt).strip())
    job_id = start_image_job(prompt, width=width, height=height, steps=steps)
    return {"job": {"id": job_id, "kind": "image", "prompt": prompt}}


@app.post("/api/upscale")
async def upscale(filename: str = Form(...)):
    """Optionally upscale a generated image ~4x (the refiner) to ~4K."""
    name = os.path.basename(filename.strip())
    if not name or not name.lower().endswith(".png"):
        return JSONResponse({"error": "bad filename"}, status_code=400)
    src = os.path.join(MEDIA_DIR, name)
    if not os.path.isfile(src):
        return JSONResponse({"error": "image not found"}, status_code=404)
    job_id = start_upscale_job(src)
    return {"job": {"id": job_id, "kind": "upscale"}}


@app.get("/api/job/{job_id}")
def job_status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    return job


@app.post("/api/job/{job_id}/cancel")
def job_cancel(job_id: str):
    ok = cancel_job(job_id)
    if not ok:
        return JSONResponse({"error": "unknown job"}, status_code=404)
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    return {"ok": True, "job": job}


@app.post("/api/talk-text")
async def talk_text(text: str = Form(...), history: str = Form("[]"),
                    attachments: str = Form("[]"), chat_id: str = Form("")):
    """Typed chat (no voice gate, no TTS). Ava drives GPU workloads herself."""
    text = text.strip()
    ids = _parse_ids(attachments)
    if not text and not ids:
        return JSONResponse({"error": "empty text"}, status_code=400)

    agent_text = _augment(text, ids)
    agent_text = memory_store.augment_with_recall(agent_text, text, chat_id)
    sid = _chat_session(chat_id) if chat_id else None
    if chat_id:
        _chat_append(chat_id, "user", text, _atts_meta(ids))
    t0 = time.time()
    tools: list[str] = []
    try:
        reply, tools = _agent_run_turn(agent_text, session_id=sid,
                                       history=_history_for(chat_id))
    except Exception as e:  # noqa: BLE001
        # Salvage an image the tool may have rendered before the turn timed out.
        job = _pickup_image_since(t0, wait=120)
        if job:
            m = which_model()
            if chat_id:
                _chat_append(chat_id, "assistant", "Here's the image you asked for.", model=m, tools_used=tools)
            return {"reply": "Here's the image you asked for.", "job": job, "model": m, "tools_used": tools}
        return JSONResponse({"error": f"Ava unreachable: {e}"}, status_code=502)
    m = which_model()
    if chat_id:
        _chat_append(chat_id, "assistant", reply, model=m, tools_used=tools)
    resp = {"reply": reply, "model": m, "tools_used": tools}
    if any("run_gpu_job" in t for t in tools):
        job = _pickup_image_since(t0, wait=120)
        if job:
            resp["job"] = job
    return resp


@app.post("/api/chat-stream")
async def chat_stream(text: str = Form(...), history: str = Form("[]"),
                      attachments: str = Form("[]"), chat_id: str = Form("")):
    """Start an Ava turn and stream her live reasoning via /api/turn/{id}."""
    text = text.strip()
    ids = _parse_ids(attachments)
    if not text and not ids:
        return JSONResponse({"error": "empty text"}, status_code=400)
    agent_text = _augment(text, ids)
    agent_text = memory_store.augment_with_recall(agent_text, text, chat_id)
    sid = _chat_session(chat_id) if chat_id else OC_SESSION
    if chat_id:
        _chat_append(chat_id, "user", text, _atts_meta(ids))
    tid = start_turn(agent_text, sid, chat_id)
    return {"turn_id": tid}


@app.post("/api/ghost/discard")
async def ghost_discard(chat_id: str = Form(...)):
    """Wipe a ghost conversation's agent-side session transcript.

    Ghost chats use an unregistered chat id, so they were never written to
    chats.json (host-side persistence is already a no-op). This also deletes the
    OpenClaw session file so the ephemeral conversation leaves no trace when the user
    exits ghost mode or starts a new chat.
    """
    cid = (chat_id or "").strip()
    if not cid:
        return {"ok": False}
    ok = await run_in_threadpool(discard_session, _chat_session(cid))
    return {"ok": bool(ok)}


@app.get("/api/turn/{tid}")
def turn_status(tid: str):
    with _TURNS_LOCK:
        t = _TURNS.get(tid)
        if not t:
            return JSONResponse({"error": "unknown turn"}, status_code=404)
        return dict(t)


@app.get("/api/artifact/weather")
def artifact_weather(location: str = "", days: int = 7):
    """Rebuild a fresh weather artifact (used by the panel's Refresh button)."""
    from ava_bridge.artifacts import build_weather_artifact
    art = build_weather_artifact((location or "").strip() or None, days)
    if not art:
        return JSONResponse({"error": "could not fetch weather"}, status_code=502)
    return art


# Upload types we know how to store/extract; anything else is refused so the
# upload dir can't be used to stash executables, keys, or other arbitrary files.
_ALLOWED_UPLOAD_EXTS = {
    ".pdf", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".rtf",
    ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic",
}


@app.post("/api/upload")
async def upload(files: List[UploadFile] = File(...)):
    """Accept document/image uploads, extract text, stash for the next turn."""
    out = []
    for uf in files:
        raw = await uf.read()
        if not raw:
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            out.append({"error": f"{uf.filename}: too large "
                                 f"(max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"})
            continue
        aid = uuid.uuid4().hex[:12]
        safe = _safe_name(uf.filename)
        ext = os.path.splitext(safe)[1].lower()
        if ext not in _ALLOWED_UPLOAD_EXTS:
            out.append({"error": f"{uf.filename}: file type '{ext or '?'}' not allowed"})
            continue
        stored = f"{aid}_{safe}"
        dest = os.path.join(UPLOAD_DIR, stored)
        with open(dest, "wb") as f:
            f.write(raw)
        is_image = ext in IMAGE_EXTS
        text = (extract_text(dest, ext) or "").strip()
        if len(text) > MAX_DOC_CHARS:
            text = text[:MAX_DOC_CHARS] + "\n\u2026[truncated]"
        rec = {"id": aid, "filename": safe,
               "kind": "image" if is_image else "document",
               "url": f"/uploads/{stored}" if is_image else None,
               "chars": len(text), "ocr": bool(is_image and text), "text": text}
        with _ATTACH_LOCK:
            _ATTACH[aid] = rec
        # Long-term memory: index document text so it stays searchable in
        # future conversations (not just this message). Images skipped unless
        # OCR found real text.
        if not is_image or rec["ocr"]:
            memory_store.index_document(aid, safe, text)
        out.append({k: rec[k] for k in ("id", "filename", "kind", "url", "chars", "ocr")})
    return {"attachments": out}


@app.get("/api/chats")
def chats_list():
    with _CHATS_LOCK:
        items = [_chat_summary(c) for c in _CHATS.values()]
    items.sort(key=lambda x: x["updated"], reverse=True)
    return {"chats": items}


@app.post("/api/chats")
def chats_create():
    return _chat_summary(_chat_new())


@app.get("/api/chats/{cid}")
def chats_get(cid: str):
    with _CHATS_LOCK:
        c = _CHATS.get(cid)
        if not c:
            return JSONResponse({"error": "unknown chat"}, status_code=404)
        return {"id": c["id"], "title": c.get("title") or "New chat",
                "messages": c.get("messages", [])}


@app.delete("/api/chats/{cid}")
def chats_delete(cid: str):
    with _CHATS_LOCK:
        existed = _CHATS.pop(cid, None) is not None
        if existed:
            _chats_persist()
    return {"ok": existed}


@app.patch("/api/chats/{cid}")
def chats_rename(cid: str, title: str = Form(...)):
    title = title.strip()[:80]
    with _CHATS_LOCK:
        c = _CHATS.get(cid)
        if not c:
            return JSONResponse({"error": "unknown chat"}, status_code=404)
        if title:
            c["title"] = title
            _chats_persist()
        return _chat_summary(c)


@app.post("/api/chats/{cid}/image")
def chats_image(cid: str, url: str = Form(...), caption: str = Form(""),
                models: str = Form("")):
    with _CHATS_LOCK:
        if cid not in _CHATS:
            return JSONResponse({"error": "unknown chat"}, status_code=404)
    img_models = None
    if models:
        try:
            parsed = json.loads(models)
            if isinstance(parsed, list) and parsed:
                img_models = parsed
        except (ValueError, TypeError):
            img_models = None
    _chat_append(cid, "assistant", caption, image=url, img_models=img_models)
    return {"ok": True}


# ---- Code mode endpoints ----

@app.get("/api/code/models")
def code_models():
    """Get available Claude models for code editing."""
    # Verified working on this deployment's key (claude-opus-4-7 is not).
    allowed_models = [
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "claude-haiku-4-5",
    ]
    return {"models": allowed_models}


# ---- Learning system endpoints (code improvement proposals + feedback) --------

@app.post("/api/learning/run")
async def learning_run():
    """Manually trigger a learning cycle now (the Learning page 'Run now' button).
    Analyzes recent code + chat activity locally and parks any proposals."""
    summary = await learning.run_all_cycles()
    return {"ok": True, **summary}


@app.get("/api/learning/code/state")
def learning_code_state():
    """Get code-learning cycles and proposals."""
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        return {
            "context": "code",
            "cycles": cycles,
            "last_cycle": state.code_learning_state.get("last_cycle"),
        }


@app.get("/api/learning/chat/state")
def learning_chat_state():
    """Get chat-learning cycles and proposals."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        return {
            "context": "chat",
            "cycles": cycles,
            "last_cycle": state.chat_learning_state.get("last_cycle"),
        }


@app.post("/api/learning/code/apply")
def learning_code_apply(proposal_id: str):
    """Approve a code proposal. If it carries staged code changes (from Ava's
    code_change_request / self-fix), actually write + commit them; otherwise just
    mark the improvement suggestion approved."""
    # Does this proposal carry real staged changes to apply?
    has_changes = False
    with state.code_learning_state_lock:
        for cycle in state.code_learning_state.get("cycles", []):
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    has_changes = bool(prop.get("staged_changes"))
                    break
    if has_changes:
        res = code_agent.apply_approved_proposal(proposal_id)
        if not res.get("ok"):
            err = res.get("error", "apply failed")
            if res.get("test_output"):
                err += f"\n\n{res['test_output']}"
            return {"ok": False, "error": err}
        msg = f"Applied {len(res.get('files', []))} file(s)"
        if res.get("branch"):
            msg += f" to branch {res['branch']}"
            if res.get("tests_passed"):
                msg += " (checks passed)"
        if res.get("commit"):
            msg += f" · commit {res['commit']}"
        if res.get("restart"):
            msg += " · restarting bridge"
        return {"ok": True, "message": msg, "files": res.get("files", []),
                "commit": res.get("commit"), "restart": res.get("restart", False),
                "branch": res.get("branch")}
    # Plain improvement suggestion — just record approval.
    with state.code_learning_state_lock:
        for cycle in state.code_learning_state.get("cycles", []):
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "completed"
                    prop["applied"] = True
                    prop["completed_by"] = prop.get("requested_by") or "Ava"
                    prop["approved_by"] = config.OPERATOR_NAME
                    prop["completed_at"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {"ok": True, "proposal": prop,
                            "message": f"Approved: {prop.get('title')}"}
    return {"ok": False, "error": "Proposal not found"}


@app.post("/api/learning/code/reject")
def learning_code_reject(proposal_id: str):
    """Reject a code-improvement proposal."""
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "rejected"
                    state.save_learning_state()
                    return {"ok": True, "message": "Proposal rejected"}
    return {"ok": False, "error": "Proposal not found"}


@app.post("/api/learning/code/feedback")
def learning_code_feedback(proposal_id: str, rating: int):
    """Record feedback on a code-improvement proposal (1=helpful, 0=not helpful)."""
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["feedback"] = rating
                    prop["feedback_timestamp"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {"ok": True}
    return {"ok": False, "error": "Proposal not found"}


@app.post("/api/learning/chat/apply")
def learning_chat_apply(proposal_id: str):
    """Approve a chat-improvement proposal."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "completed"
                    prop["applied"] = True
                    prop["completed_by"] = prop.get("requested_by") or "Ava"
                    prop["approved_by"] = config.OPERATOR_NAME
                    prop["completed_at"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {
                        "ok": True,
                        "proposal": prop,
                        "message": f"Approved: {prop.get('title')}"
                    }
    return {"ok": False, "error": "Proposal not found"}


@app.post("/api/learning/chat/reject")
def learning_chat_reject(proposal_id: str):
    """Reject a chat-improvement proposal."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "rejected"
                    state.save_learning_state()
                    return {"ok": True, "message": "Proposal rejected"}
    return {"ok": False, "error": "Proposal not found"}


@app.post("/api/learning/chat/feedback")
def learning_chat_feedback(proposal_id: str, rating: int):
    """Record feedback on a chat-improvement proposal (1=helpful, 0=not helpful)."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["feedback"] = rating
                    prop["feedback_timestamp"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {"ok": True}
    return {"ok": False, "error": "Proposal not found"}
