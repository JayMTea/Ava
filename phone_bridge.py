#!/usr/bin/env python3
"""Phone voice bridge for Ava — talk to Ava from your phone's browser.

Pipeline:  phone mic (browser MediaRecorder)
           --> POST /api/talk --> ffmpeg decode --> voiceprint gate (your voice only)
           --> faster-whisper (STT, CPU) --> Ava (local LLM engine, :8002)
           --> Piper (TTS) --> wav returned to the phone and played in the browser.

Serve it to your phone over Tailscale (TLS + tailnet-only) with run_bridge.sh.
Every stage above runs on your own machine. Nothing in this pipeline calls out;
what leaves the box is whatever you configure elsewhere (a cloud inference
backend, an Anthropic key for governed code changes) and nothing else.
"""

import asyncio
import base64
import json
import os
import threading

from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import (JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketDisconnect
import websockets

import speaker as spk
import voice_ava as va

# The Experience plane is now a thin face over the ava_bridge package; each module
# owns one concern. Routes below only authenticate, serve, capture input, and
# forward to Ava-the-agent. See ava_bridge/__init__.py for the map.
from ava_bridge import brand, config, settings, state
from ava_bridge.version import version as _ava_version
from ava_bridge.config import (
    RATE, UPLOAD_DIR, PHONE_THRESHOLD,
)
from ava_bridge.agent import (run_turn as _agent_run_turn, warm_openclaw,
                              get_route, set_route, which_model)
from ava_bridge.chat_store import history_for as _history_for
from ava_bridge.documents import augment, parse_ids
from ava_bridge.audio import decode_to_pcm, tts_wav_bytes, gpu_transcribe
from ava_bridge.chat_store import (
    chat_append, chat_session, atts_meta,
)
from ava_bridge.auth import (
    auth_gate, needs_setup, claim_token, in_container,
)
from ava_bridge import features, internal
from ava_bridge import memory_store
from ava_bridge import connectors, perf_store, devices
from ava_bridge import arch_watch
from ava_bridge import ws_auth
from ava_bridge import hardware
from ava_bridge import distill
_AVA_VERSION = _ava_version()

HERE = config.ROOT


# The page assets live in ava_bridge/pages.py, which is the only module that
# renders them. Imported here for the /assets mount alone: mounting is app
# construction and belongs in this file, but "where is the SPA" and "was it
# built" are page facts and should not be duplicated.
from ava_bridge import pages as _pages  # noqa: E402

app = FastAPI(title="Ava phone bridge")
if _pages.SPA_PAGE is not None:
    app.mount("/assets",
              StaticFiles(directory=os.path.join(_pages.FRONTEND_DIST, "assets")),
              name="assets")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


# Step-zero auth gate for the whole Experience plane (see ava_bridge/auth.py).
app.middleware("http")(auth_gate)


@app.exception_handler(settings.ConfigParseError)
async def _config_parse_error(request: Request, exc: settings.ConfigParseError):
    """A refused config write is a 409 with instructions, not a 500.

    Registered once rather than at each of the dozen save_patch/save_config call
    sites: every one of them wants the same answer, and a handler that has to be
    remembered per route is a handler that gets forgotten. 409 Conflict because
    the request is valid — the state on disk is what blocks it.
    """
    return JSONResponse(
        {"ok": False, "error": str(exc), "error_code": "config_unparseable",
         "config_path": str(settings.CONFIG_PATH)},
        status_code=409)


# Lazily-initialised heavy objects (loaded once on first request / startup).
# Voice (STT + speaker gate) is an OPTIONAL extra — requirements-voice.txt.
# Without it the bridge boots and serves normally, just voice-less.
def _ensure_loaded() -> tuple:
    """Load what is missing and return `(whisper, verifier, voiceprint)`.

    Loading and reading are one step, under one lock, for two reasons:

      * **Exactly once.** Every load here was an unguarded check-then-set. Two
        concurrent voice turns could both find `whisper is None` and both build a
        WhisperModel; it was masked only because the startup call always won the
        race first. The owner can now free these models at runtime, which unmasks it
        exactly when memory is short.
      * **No torn read.** Returning the objects the caller will use, rather than
        letting it re-read `state.heavy` later, is what stops a release landing
        mid-turn from turning a dereference into an unhandled 500.
    """
    with state.heavy_lock:
        if state.heavy["voice_unavailable"]:
            return (None, None, None)
        try:
            if state.heavy["whisper"] is None:
                from faster_whisper import WhisperModel
                state.heavy["whisper"] = WhisperModel(va.WHISPER_MODEL, device="cpu", compute_type="int8")
            if state.heavy["voiceprint"] is None:
                state.heavy["voiceprint"] = spk.load_voiceprint()
            if state.heavy["verifier"] is None and state.heavy["voiceprint"] is not None and PHONE_THRESHOLD > 0:
                state.heavy["verifier"] = spk.SpeakerVerifier()
        except ImportError:
            state.heavy["voice_unavailable"] = True
            print("[ava-bridge] voice disabled — optional STT deps not installed "
                  "(pip install -r requirements-voice.txt to enable).", flush=True)
        # Reentrant: `heavy_lock` is an RLock, and one definition of "these three,
        # read together" beats two that can drift.
        return state.voice_snapshot()


@app.on_event("startup")
def _startup():
    _ensure_loaded()
    gate = "ON" if state.heavy["verifier"] is not None else "OFF"
    print(f"[ava-bridge] ready. speaker gate {gate} (threshold={PHONE_THRESHOLD}).", flush=True)
    # First run, unclaimed: print the claim token. A browser on this machine
    # never needs it (loopback claims freely) — this is for the headless case,
    # where the alternative to a printed token is SSH port-forwarding just to
    # reach a password form.
    if needs_setup():
        _tok = claim_token()
        if _tok:
            print("[ava-bridge] NOT YET CLAIMED.", flush=True)
            # Under Docker the published port arrives via the bridge gateway, so
            # even a browser on the host is NOT a loopback peer and the plain
            # /setup URL is refused. Everyone needs the token there, not just
            # the headless case — say so rather than sending them to a 403.
            if not in_container():
                print("[ava-bridge]   from this machine, open "
                      f"http://localhost:{config.SERVER_PORT}/setup", flush=True)
            print("[ava-bridge]   open: "
                  f"http://localhost:{config.SERVER_PORT}/setup?claim={_tok}", flush=True)
    # Report the agent runtime and enforce the `agent.required` policy loudly.
    from ava_bridge import runtime
    rt, err = runtime.gate()
    if err:
        print(f"[ava-bridge] WARNING: AGENT RUNTIME REQUIRED BUT MISSING — {err}", flush=True)
        print("[ava-bridge]   chat turns will error until you provision it "
              "(`ava agent provision --install`).", flush=True)
    elif rt.is_floor():
        print("[ava-bridge] agent runtime absent -> DIRECT (tool-less) chat. "
              "Install the agent runtime for tools/memory/skills "
              "(`ava agent provision`).", flush=True)
    else:
        print(f"[ava-bridge] agent runtime: {rt.name} (full agent — tools + memory + skills).", flush=True)
    threading.Thread(target=warm_openclaw, daemon=True).start()
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
    # Start the in-process memory distiller (mines chat history for durable facts
    # about the owner). Local-only. No-op if features.memory is false.
    distill.start_scheduler()
    # Architecture drift watchdog: periodic SSOT check between commits — heals
    # stale diagrams, alerts on structural drift. No-op without a manifest.
    arch_watch.start_scheduler()
    # Daily KPI collector for the domain layer. No-op unless features.domains is
    # on, and it refuses any app tool above `sensitive` or needing confirmation,
    # so enabling the flag alone never starts reading anything the owner has not
    # separately granted.
    try:
        from ava_bridge import kpi_collect
        kpi_collect.start_scheduler()
    except Exception as e:  # noqa: BLE001 — optional subsystem; never block boot
        print(f"[ava-bridge] domain collector unavailable: {e}", flush=True)
    # Allocation watchdog: polls each declared model's readiness, so "the service is
    # up but its model never loaded" raises an alert instead of going unnoticed —
    # that state answers its own port, so no liveness check detects it. No-op until
    # models are declared in alloc.models.
    try:
        from ava_bridge.alloc import watch as alloc_watch
        alloc_watch.start_scheduler()
    except Exception as e:  # noqa: BLE001 — optional subsystem; never block boot
        print(f"[ava-bridge] allocation watchdog unavailable: {e}", flush=True)
    # Brain drift watchdog: compares what ava.yaml asks for against what the
    # engine is actually serving. Unconditional, and NOT folded into the
    # allocation watchdog above — that one no-ops until alloc.models is declared,
    # and the box where a router served a model its engine did not have for
    # twelve days had nothing declared at all.
    try:
        from ava_bridge import brain_watch
        brain_watch.start_scheduler()
    except Exception as e:  # noqa: BLE001 — optional subsystem; never block boot
        print(f"[ava-bridge] brain watchdog unavailable: {e}", flush=True)


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

# Browser-facing pages: login, first-run setup, logout, password change, the
# SPA shell and the PWA assets. Registered first so the shell is in place before
# the API routers.
app.include_router(_pages.router)

# Media + turns (cookie-gated /api/upload, /api/turn/*, /api/turns). Registers
# after the /uploads StaticFiles mount above, preserving the original
# declaration order.
from ava_bridge.media_api import router as _media_router  # noqa: E402
app.include_router(_media_router)

# The live hardware snapshot (cookie-gated /api/hardware) — what the floating
# hardware monitor polls on every view.
from ava_bridge.perf_api import router as _perf_router  # noqa: E402
app.include_router(_perf_router)

# Domain grouping + the per-domain KPI series. Off unless features.domains is on;
# the routes stay mounted either way so a previously-enabled instance's store is
# still readable.
from ava_bridge.domains_api import router as _domains_router  # noqa: E402
app.include_router(_domains_router)

# Chat API (cookie-gated /api/chats/*, /api/chat-stream, /api/ghost/discard).
from ava_bridge.chats_api import router as _chats_router  # noqa: E402
app.include_router(_chats_router)

# Agent gateway control plane: /api/gateway/* plus the /ws/gateway event relay.
#
# NOTE FOR ANYONE ADDING A WEBSOCKET ROUTE: `auth_gate` below is registered
# `app.middleware("http")`, and Starlette forwards non-HTTP scopes past it
# untouched — so a websocket route is UNGATED unless it awaits
# `ws_auth.guard(ws)` before `accept()`. `qa/test_01_auth_surface.py` cannot see
# these routes either (it enumerates by `methods`). `tests/test_websocket_auth.py`
# is what enforces it.
from ava_bridge.gateway_api import router as _gateway_router  # noqa: E402
app.include_router(_gateway_router)

# Sandbox->bridge callback surface (/internal/* — token-gated, scope-enforced).
# Lives in ava_bridge/internal.py alongside the token logic and the ROUTE_SCOPES
# table the middleware consults, so a new route's author sees the scope entry
# they have to add. auth.auth_gate refuses any /internal path it cannot classify.
app.include_router(internal.router)

# Mount the optional overlay personal-app routes now that `app` exists.
try:
    from overlay.ava_bridge import personal_routes as _personal_routes
    _personal_routes.register(app)
except Exception:  # noqa: BLE001 — no overlay (fork) or a broken overlay: core still boots
    pass


def _resolved_model() -> str:
    """The brain this instance is actually using — the agent sandbox model when
    that runtime is active, else the configured chat backend, else whatever the
    router would actually serve. /api/health feeds the client token counter, so
    this must track reality rather than the standalone AVA_MODEL constant (which
    advertised Omni regardless of what was configured).

    That walk is now models.effective_brain()'s, not this function's. This one
    hand-rolled its own — sandbox, then `inference.backends`, then AVA_MODEL —
    and the middle step read ONLY ava.yaml. A Docker install has no inference
    block at all (install.sh writes AVA_BACKEND_MODEL to deploy/.env and compose
    passes it in; router_app builds that env backend precisely because ava.yaml
    declares none), so this fell through to the Omni constant and /api/health
    reported a model the box was not running — while the hardware monitor, which
    asks effective_brain(), named the right one on the same screen. The resolver
    reads the env backend because it asks the router what it would serve.

    When the resolver reports `none` — no backend anywhere — the answer is the
    EMPTY string, and callers must render that as "no model". This used to fall
    back to the standalone AVA_MODEL constant, so a public, pre-auth endpoint
    advertised a specific model on a box that had none and never would: the same
    invented-brain problem the built-in default backend caused, surviving in the
    one place anybody can read without logging in.

    Lazily imported, and every failure swallowed, for the same reason as before
    — health must never fail on a probe.
    """
    try:
        from ava_bridge import models as _models
        return str(_models.effective_brain().get("model_id") or "").strip()
    except Exception:  # noqa: BLE001 — health must never fail on a probe
        return ""


@app.get("/api/health")
def health():
    # Public (pre-auth) endpoint: expose only what the unauthenticated client
    # needs for the token counter; keep speaker-gate internals private.
    # `brand` was removed here on purpose. This endpoint is PUBLIC (auth.py
    # _PUBLIC_PATHS), so it named the instance to anyone who could reach the
    # port — and it had ZERO consumers: lib/api.ts does not declare the field,
    # qa/test_02_api_contracts requires only ["ok"], and no fixture reads it.
    # A pre-auth disclosure with no caller is not a feature to preserve. The
    # sign-in page still shows the name unless `brand.public` is false, which is
    # a deliberate, documented choice rather than an accident of a health probe.
    return {
        "ok": True,
        "model": _resolved_model(),
        "ctx_max": config.CTX_MAX,
        "ctx_base": config.CTX_BASE,
        "version": _AVA_VERSION,
    }


@app.get("/api/brand")
def brand_api():
    """The brand the SPA renders: name, tagline, colours, and which asset slots
    are filled. Lets a fork re-brand the whole UI without editing React.

    Reads the LIVE accessors, not the import-time `config.AVA_*` constants, so a
    save from Setup → Branding is visible on the next request instead of after a
    restart. That split is also why `settings.brand_tagline()` had to exist: the
    name was live here and the tagline was frozen there, and the two disagreed.

    Still cookie-gated (it is under /api and not in auth._PUBLIC_PATHS). The
    pre-auth surfaces are server-rendered and already hold the config in-process,
    so nothing needs this endpoint before sign-in — making it public would be
    attack surface bought for no caller.
    """
    return {**brand.payload(), "version": _AVA_VERSION}


# The app registry the sidebar renders. Cookie-gated /api/* (browser auth);
# read-first wrappers over ava_bridge modules.


@app.get("/api/apps")
async def api_apps():
    """Left-rail app registry (connectors with a `ui:` block). Drives the SPA nav
    so a new app appears by dropping a connector folder — no frontend edits.

    `apps_origin` tells the shell whether embedded UIs are isolated on their own
    browser origin, and where. When it is null the frame loads same-origin, which
    means the app's JS runs with the owner's session — `warning()` carries the
    sentence the Setup page shows about that."""
    from ava_bridge import apps_origin
    return await run_in_threadpool(lambda: {
        "apps": connectors.apps(),
        "apps_origin": apps_origin.warning(),
    })


@app.get("/api/apps/health")
async def api_apps_health():
    """Per-app readiness for the sidebar dot — see `dashboard.apps_health`.

    Split from `/api/apps` rather than folded into it: the registry is what the
    shell needs to PAINT the nav and must stay instant, while this probes
    services (15s-cached, but still I/O) and is polled on its own clock. A nav
    that waited on health would blank the sidebar every time an app hung."""
    from ava_bridge import dashboard
    return await run_in_threadpool(dashboard.apps_health)


@app.post("/api/apps/order")
async def api_apps_order(body: dict):
    """Save the owner's hand-arranged sidebar order.

    Takes the FULL list of app ids in display order, not a move instruction: a
    drag produces an arrangement, and replaying "moved X above Y" against a list
    that changed underneath (an app connected in another tab) reorders the wrong
    thing. Unknown ids are dropped rather than rejected — an app removed in
    another tab must not make an otherwise valid arrangement un-saveable.
    """
    raw = body.get("order")
    if not isinstance(raw, list):
        return JSONResponse({"ok": False, "error": "order must be a list of app ids"},
                            status_code=400)
    known = {a["id"] for a in connectors.apps() if a.get("section") != "core"}
    seen, order = set(), []
    for x in raw:
        cid = str(x).strip()
        if cid in known and cid not in seen:
            seen.add(cid)
            order.append(cid)
    connectors.save_order(order)
    # No restart_required: `connectors.apps()` reads the key at request time, so
    # the next /api/apps already reflects this.
    return {"ok": True, "order": order}


@app.get("/api/apps/{cid}/actions")
async def api_app_actions(cid: str, live: bool = True):
    """The agent actions one connector exposes, for the `ui.embed: none` console.

    Split from `/api/apps` because the nav does not need it and must stay
    instant. This replaces the read `ActionConsole` used to make against
    `/api/ops/connectors`, which carried live probe results, perf-file presence
    and egress counts the console never rendered — and which went with the
    Operations page.

    For a DYNAMIC connector (a real `mcp:` server, or the ava-tools/1 facade)
    this asks the app, because the manifest holds one synthetic bridge row and
    the owner needs the tool names. That is a network hop, so it goes to the
    threadpool like every other blocking call here — and it never fails the
    request: an unreachable app falls back to the last list it served, with
    `source` and `error` saying so. `?live=0` takes the cached path alone.

    Cookie-gated like every other /api route (it is not in auth._PUBLIC_PATHS),
    so the tool names of the owner's apps are not readable by anyone who can
    reach the port.

    `actions` stays a flat list of names for anything still reading that shape;
    `tools` carries the description and the tier the console renders.
    """
    if not connectors.app(cid):
        return JSONResponse({"error": f"unknown app '{cid}'"}, status_code=404)
    surface = await run_in_threadpool(connectors.app_actions, cid, live)
    return {"ok": True,
            "actions": [t["name"] for t in surface["tools"]],
            "tools": surface["tools"],
            "transport": surface["transport"],
            "source": surface["source"],
            "error": surface["error"]}


@app.get("/api/apps/{cid}/embed")
async def api_app_embed(cid: str, request: Request):
    """The URL the shell should point an app's iframe at.

    Called from Ava's OWN origin, so the session cookie has already authorised the
    caller by the time this runs. It returns an absolute URL on the apps origin
    carrying a short-lived, cid-bound token, because the apps origin has no session
    of its own — see ava_bridge/apps_origin.py.

    When `apps.origin` is unset this returns the same relative path the shell has
    always used, so the SPA needs no branch of its own for the unconfigured case.
    """
    from ava_bridge import apps_origin
    if not connectors.app(cid):
        return JSONResponse({"error": f"unknown app '{cid}'"}, status_code=404)
    q = str(request.url.query or "")
    url = apps_origin.embed_url(cid, q)
    return {"url": url or f"/apps/{cid}/?{q}", "isolated": bool(url)}


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
# Two proxies serve EVERY connector that declares a `ui:` block, so a
# third-party app renders in Ava's shell with no bespoke bridge code:
#   /apps/<id>/api/<path>  — browser data-proxy: forwards to the app's API base
#                            with its bearer token injected (browser never sees it)
#   /apps/<id>/<path>      — iframe proxy: reverse-proxies the app's own web UI
#                            under Ava's roof — same-origin by default, or on the
#                            isolated apps origin (ava_bridge/apps_origin.py)
# The /api/ route is declared first so it wins over the catch-all below. Both
# carry every method a web app uses, HEAD and OPTIONS included — an app that
# preflights its own API or HEADs a download used to get Ava's 405, which reads
# as the app being broken.
#
# GATING is auth_gate, registered `app.middleware("http")` above: Ava's session
# on the main origin, a cid-bound embed token on the apps origin. The routes
# themselves therefore assume an authorised caller.
#
# TRANSPORT. Both proxies are native async httpx streams on the event loop, and
# that is load-bearing, not a style choice. They used to be blocking `requests`
# calls under run_in_threadpool — and Starlette runs those threads with
# abandon_on_cancel=False, so a long-lived response (SSE, a long poll, an app
# that simply stalls) pinned a worker thread that cancellation could NEVER
# reclaim. anyio's default limiter is 40 tokens for the whole process, shared
# by every run_in_threadpool call site in the bridge, so ~40 abandoned streams
# froze every threadpool route — chat, login, the dashboard — until restart.
# Measured, not theoretical: 40 open app streams and /api/devices stopped
# answering. Streaming natively has the opposite property: when the browser
# goes away the response task is cancelled and the upstream connection is
# closed with it (the StreamingResponse's background task), so an app's stream
# costs a pooled connection, never a thread.
#
# `follow_redirects=False` on the shared client, for the same reason the old
# code set allow_redirects=False on every branch: a connector is trusted to
# ANSWER, not to re-point the bridge somewhere else. Following a redirect here
# is an SSRF primitive — the app returns `302 Location: http://127.0.0.1:8010/…`
# and the bridge fetches loopback on its behalf and hands the body back.
# `web_fetch` re-validates every hop against an SSRF guard (ava_bridge/web.py);
# this path has no guard, so it refuses hops instead. A 3xx is forwarded to the
# browser (Location rewritten, see below), which is what a same-origin fetch
# would see anyway.
#
# HEADER CONTRACT, both directions, in one place:
#   request  -> forward the browser's headers EXCEPT hop-by-hop (RFC 9110
#               §7.6.1 — they describe THIS connection, not the next one), Host
#               (httpx sets the upstream's own), and Ava's own cookies: the session cookie
#               and the apps-origin embed cookie are stripped out of Cookie,
#               because the app is a separate trust domain and must never hold
#               a credential for Ava. Every OTHER cookie forwards — the app's
#               own session has to survive the hop or its login breaks.
#   response -> forward everything EXCEPT hop-by-hop and the two framing
#               headers (Content-Length / Transfer-Encoding — streaming
#               re-frames), with two rewrites so the app keeps working from
#               under /apps/<cid>/: a Location that points inside the app
#               (root-relative, or absolute on the app's own host:port) is
#               re-prefixed onto the proxy path, and a Set-Cookie has its Path
#               moved under /apps/<cid>/ and any Domain dropped — a cookie
#               scoped to the app's internal path would never come back through
#               the proxy, and Domain names a host the browser isn't on.
# The old response side was an ALLOWLIST of four cache headers, which silently
# ate Content-Disposition, WWW-Authenticate, Location and every Set-Cookie an
# app issued — a login inside an embedded app could never stick. Forward-by-
# default with a named excludes list is the posture a reverse proxy owes the
# thing it fronts.
import httpx  # noqa: E402 — the proxy block below is this import's only user
from fastapi.responses import StreamingResponse  # noqa: E402
from starlette.background import BackgroundTask  # noqa: E402

# RFC 9110 §7.6.1 hop-by-hop headers, plus `proxy-connection` (the legacy
# Netscape spelling some stacks still emit). Relaying any of these re-states
# OUR connection's terms as the NEXT connection's.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade",
    "proxy-authenticate", "proxy-authorization", "proxy-connection",
})

_app_client_obj: "httpx.AsyncClient | None" = None
_app_client_loop = None


def _app_client() -> "httpx.AsyncClient":
    """The ONE AsyncClient both proxies share, created on first use.

    Module-level and lazy rather than per-request or lifespan-created: a client
    owns the connection pool, so a client per request is a TCP (and TLS)
    handshake per request and an unbounded number of pools — and lifespan
    creation would cost every install the object whether or not it embeds any
    apps, while tests import this module without running the ASGI lifespan at
    all. First use is on the event loop, which is single-threaded, so the
    check-then-set needs no lock.

    The timeout SHAPE is the fix for the thread-starvation defect above:
      * connect=5 — a dead app must fail fast enough to render the
        unreachable page instead of a hanging tile;
      * read=None — an SSE / long-poll stream is SUPPOSED to sit quiet for
        minutes, and any read ceiling here kills every quiet stream at the
        ceiling. The idle cost is a pooled connection, reclaimed the moment
        the browser goes away — never a thread.
    The pool limits are the corresponding brake: embedded apps can hold at
    most max_connections upstream sockets between them, so one app hoarding
    streams exhausts ITS proxy, not the bridge — and pool=15 turns "the pool
    is full" into a visible 502 instead of a silent queue.

    trust_env=False: app upstreams are the owner's own addresses from a
    manifest, and routing them through an HTTP_PROXY inherited from the
    service environment would be silent egress nobody configured.
    """
    global _app_client_obj, _app_client_loop
    loop = asyncio.get_running_loop()
    if _app_client_obj is None or _app_client_loop is not loop:
        # Re-created whenever the RUNNING LOOP changes, not only on first use.
        # The pool holds sockets registered on the loop that opened them, so a
        # keep-alive connection reused from a later loop dies with "Event loop
        # is closed". Under uvicorn one loop lives for the whole process and
        # this branch runs exactly once; under TestClient — which spins a
        # fresh loop per request — it is what keeps one request's pooled
        # upstream from poisoning the next. The superseded client cannot be
        # aclose()d from here (its loop is gone); dropping the reference lets
        # GC close the raw sockets.
        _app_client_obj = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=None, pool=15.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=20),
            follow_redirects=False, trust_env=False)
        _app_client_loop = loop
    return _app_client_obj


def _foreign_cookies(header: str, cid: str) -> str:
    """The browser's Cookie header minus Ava's own two.

    `config.COOKIE_NAME` is Ava's session — handing it to an embedded app would
    let the app act as the owner against every /api/* route, which is the exact
    capability the apps-origin split exists to remove. The embed cookie
    (`apps_origin.cookie_name`) is likewise Ava's infrastructure, not the
    app's: it authorises this proxy and is nothing an upstream should see or
    replay. Everything else is the app's own state (its session above all) and
    forwards untouched. Split on ';' rather than a cookie parser: a parser
    normalises or drops values it dislikes, and a proxy has no business
    editing cookies it is merely carrying.
    """
    from ava_bridge import apps_origin
    mine = {config.COOKIE_NAME, apps_origin.cookie_name(cid)}
    kept = []
    for part in header.split(";"):
        part = part.strip()
        if part and part.split("=", 1)[0].strip() not in mine:
            kept.append(part)
    return "; ".join(kept)


def _upstream_headers(request: Request, cid: str,
                      auth_override: str | None) -> list:
    """The header pairs to send upstream — the request half of the contract.

    Works on `headers.raw` because Starlette's mapping view de-duplicates keys,
    and HTTP/2 browsers legally split Cookie across several header lines; those
    are merged here per RFC 6265bis before Ava's own names are stripped. The
    Connection header may NAME additional per-hop headers, so its tokens join
    the drop set before it is dropped itself.

    Content-Length rides THROUGH, deliberately. The body streams upstream
    byte-identical, so the browser's length is still the truth — and httpx
    cannot derive a length from a stream, so dropping the header forces
    chunked request framing, which plain WSGI/http.server upstreams do not
    decode: the app would read a zero-length body from a POST that plainly
    had one. (Transfer-Encoding is still dropped as hop-by-hop; a browser
    that chunked its side gets re-chunked by httpx on ours.)

    `auth_override` is the connector's saved credential resolved on the bridge
    (Ava-never-has-passwords). When present it REPLACES any Authorization the
    browser sent — the saved credential is authoritative, surviving a stale
    token in the app's own storage. When absent the browser's own header rides
    through with everything else: an app with a login but no stored token must
    not be logged out by the hop.
    """
    drop = set(_HOP_BY_HOP) | {"host"}
    for tok in (request.headers.get("connection") or "").split(","):
        tok = tok.strip().lower()
        if tok:
            drop.add(tok)
    if auth_override is not None:
        drop.add("authorization")
    out: list[tuple[bytes, bytes]] = []
    cookies: list[str] = []
    for k, v in request.headers.raw:
        name = k.decode("latin-1").lower()
        if name in drop:
            continue
        if name == "cookie":
            cookies.append(v.decode("latin-1"))
            continue
        out.append((k, v))
    if cookies:
        kept = _foreign_cookies("; ".join(cookies), cid)
        if kept:
            out.append((b"cookie", kept.encode("latin-1")))
    if auth_override is not None:
        out.append((b"authorization", auth_override.encode("latin-1")))
    return out


def _strip_base_path(path: str, base: str) -> str:
    """`path` with the upstream's own base path removed, always root-relative.

    `base` is the connector's `ui.url`; when it carries a path component the
    app lives under that prefix and emits it in its own redirects and cookie
    paths. The proxy path is `/apps/<cid>` + (path relative to that prefix).
    """
    from urllib.parse import urlsplit
    try:
        up_path = (urlsplit(base).path or "").rstrip("/")
    except ValueError:
        up_path = ""
    if up_path and (path == up_path or path.startswith(up_path + "/")):
        path = path[len(up_path):] or "/"
    return path if path.startswith("/") else "/" + path


def _rewrite_location(loc: str, cid: str, base: str) -> str:
    """Re-point an app-internal redirect at the proxy path.

    Apps redirect as if they lived at their own root — a trailing-slash bounce
    (`/panel` -> `/panel/`), a login flow, a framework's canonical-URL rule.
    Forwarded verbatim, a root-relative Location walks the browser out of
    /apps/<cid>/ onto Ava's own routes, and an absolute one onto a host:port
    the browser may not even resolve. So: root-relative gets the proxy prefix;
    absolute (or scheme-relative) URLs are rewritten ONLY when the authority is
    the app's own upstream, with the upstream's base path stripped so the
    remainder maps onto the proxy the way every other path does. Anything
    pointing at a different host is the app's business — an OAuth hop, a docs
    link — and passes through untouched.
    """
    from urllib.parse import urlsplit
    if loc.startswith("/") and not loc.startswith("//"):
        # An app served under its own basePath (Next.js `basePath`, a mounted
        # sub-app) redirects with that prefix already present — forwarding
        # `/apps/healthapp` -> `/apps/healthapp/apps/healthapp` doubled the mount and 404'd
        # the very first iframe load. Strip the upstream's base path first, the
        # same way the absolute branch below has always done.
        return f"/apps/{cid}" + _strip_base_path(loc, base)
    try:
        target, up = urlsplit(loc), urlsplit(base)
    except ValueError:
        return loc
    if not target.netloc:
        return loc   # relative-to-here already resolves inside the proxy path

    def _hostport(u, fallback_scheme):
        scheme = (u.scheme or fallback_scheme or "").lower()
        port = u.port or {"http": 80, "https": 443}.get(scheme)
        return ((u.hostname or "").lower(), port)

    if _hostport(target, up.scheme) != _hostport(up, "http"):
        return loc
    path = target.path or "/"
    up_path = (up.path or "").rstrip("/")
    if up_path and path.startswith(up_path):
        path = path[len(up_path):] or "/"
    new = f"/apps/{cid}" + (path if path.startswith("/") else "/" + path)
    if target.query:
        new += "?" + target.query
    if target.fragment:
        new += "#" + target.fragment
    return new


def _rewrite_set_cookie(value: str, cid: str, base: str = "") -> str:
    """Scope an app's cookie to its proxy prefix; keep every other attribute.

    Three edits and no more. Path moves under /apps/<cid>/ (Path=/ ->
    /apps/<cid>/, Path=/x -> /apps/<cid>/x, absent -> added) because the path
    the app scoped it to does not exist on this origin — the browser would
    never send the cookie back through the proxy. Domain is dropped because it
    names the app's host, which the browser is not on; a Domain that does not
    cover the request host makes the whole Set-Cookie rejected silently. And
    the prefix is per-cid on purpose: it is what keeps one embedded app's
    session from being offered to another's proxy, the same isolation
    apps_origin.apply_cookie draws for the embed cookie. HttpOnly, Secure,
    SameSite, Max-Age, Expires and anything future ride through verbatim —
    they are the app's security posture, not ours to edit.
    """
    parts = value.split(";")
    out = [parts[0].strip()]
    saw_path = False
    for attr in parts[1:]:
        attr = attr.strip()
        if not attr:
            continue
        key = attr.split("=", 1)[0].strip().lower()
        if key == "domain":
            continue
        if key == "path":
            saw_path = True
            val = attr.split("=", 1)[1].strip() if "=" in attr else ""
            out.append("Path=" + (f"/apps/{cid}" + _strip_base_path(val, base)
                                  if val.startswith("/") else f"/apps/{cid}/"))
            continue
        out.append(attr)
    if not saw_path:
        out.append(f"Path=/apps/{cid}/")
    return "; ".join(out)


def _proxy_response(r: "httpx.Response", cid: str, base: str) -> Response:
    """Wrap an upstream response for the browser — the response half of the
    contract: stream the body, forward the headers, apply the two rewrites.

    `aiter_raw` rather than `aiter_bytes` because Content-Encoding is
    forwarded: the browser negotiated gzip/br with the APP, so the proxy must
    relay the encoded bytes untouched instead of decoding what it will then
    claim is still encoded. The background task closes the upstream response
    when the browser-side response finishes OR is torn down by a disconnect —
    that close is what releases the pooled connection and, mid-stream, what
    actually hangs up on the app. Without it an abandoned SSE panel would hold
    its upstream socket until the app noticed on its own.
    """
    drop = set(_HOP_BY_HOP) | {"content-length"}
    for tok in (r.headers.get("connection") or "").split(","):
        tok = tok.strip().lower()
        if tok:
            drop.add(tok)
    html = "text/html" in r.headers.get("content-type", "")
    headers: list[tuple[bytes, bytes]] = []
    for k, v in r.headers.raw:
        name = k.decode("latin-1").lower()
        if name in drop:
            continue
        if html and name in ("cache-control", "expires"):
            # An embedded app's HTML entry must always revalidate. Many app
            # servers (Starlette StaticFiles included) send Last-Modified but
            # no Cache-Control, so browsers cache the page heuristically —
            # pinning the iframe to a stale bundle across the app's rebuilds.
            # ETag stays, so unchanged pages are still cheap 304s; hashed
            # assets stay long-cached.
            continue
        if name == "location":
            v = _rewrite_location(v.decode("latin-1"), cid, base).encode("latin-1")
        elif name == "set-cookie":
            # Rewritten one header AT A TIME: folding Set-Cookie into a single
            # comma-joined value corrupts it (Expires dates contain commas),
            # so each upstream header survives as its own header here.
            v = _rewrite_set_cookie(v.decode("latin-1"), cid, base).encode("latin-1")
        headers.append((k.decode("latin-1").lower().encode("latin-1"), v))
    if html:
        headers.append((b"cache-control", b"no-cache"))
    resp = StreamingResponse(r.aiter_raw(), status_code=r.status_code,
                             background=BackgroundTask(r.aclose))
    resp.raw_headers = headers
    return resp


def _classifiable(e: Exception) -> Exception:
    """Give an httpx failure a chain `connectors.unreachable` can classify.

    The classifier walks `__cause__`/`__context__` for the innermost real
    OSError (connectors._errno_cause) — a shape chosen for `requests`, whose
    chain ends AT the ConnectionRefusedError. httpx-via-anyio ends somewhere
    else: a refused connect surfaces as a plain OSError("All connection
    attempts failed") whose ConnectionRefusedError sits INSIDE an
    ExceptionGroup, so the linear walk finds the aggregate first and the app
    classifies as a generic `_error` instead of "isn't running" — losing the
    `<cid>_down` code the frontend's fix-it link keys on
    (tests/test_connector_unreachable.py explains why that spelling is load
    bearing). So before handing over, find the most SPECIFIC OSError anywhere
    in the chain — group members included — and re-link it as the direct
    cause. Where the chain is already specific (httpx DNS misses end at a bare
    gaierror) this re-links the same object the classifier would have found.
    """
    stack: list = [e]
    seen: set[int] = set()
    while stack and len(seen) < 50:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, OSError) and type(cur) is not OSError:
            e.__cause__ = cur
            return e
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        stack.extend((cur.__cause__, cur.__context__))
    return e


async def _proxy_stream(cid: str, request: Request, url: str, *, base: str,
                        auth_override: str | None, what: str) -> Response:
    """One hop, browser -> app, streamed in both directions.

    The request body is `request.stream()` — an upload passes through without
    ever being held whole in memory — but ONLY when the browser declared one:
    handing httpx a stream on a bodyless GET makes it frame an empty chunked
    body, which some app servers refuse on safe methods. The query string is
    relayed raw rather than re-encoded, so whatever encoding the app's own
    frontend chose survives the hop byte-for-byte.
    """
    q = str(request.url.query or "")
    target = f"{url}?{q}" if q else url
    has_body = request.headers.get("transfer-encoding") is not None or \
        (request.headers.get("content-length") or "0") not in ("", "0")
    client = _app_client()
    req = client.build_request(
        request.method, target,
        headers=_upstream_headers(request, cid, auth_override),
        content=request.stream() if has_body else None)
    try:
        r = await client.send(req, stream=True)
    except Exception as e:  # noqa: BLE001 — an app being down is not a bridge error
        return _unreachable_response(cid, _classifiable(e), request,
                                     url=url, what=what)
    return _proxy_response(r, cid, base)


# A failed proxy hop has TWO audiences and they need different media types.
# The iframe's own navigation renders whatever body we return AS the frame
# document — so a JSON 502 is displayed to the owner as literal JSON, which is
# how `{"error":"<id> app unreachable: HTTPConnectionPool(...)"}` ended up on
# screen as the app's UI. That is also why AppFrame's error state never fired:
# the frame DID load (a document arrived), so `onLoad` reported ready.
#
# The frontend cannot fix this from outside. With `apps.origin` configured the
# frame is deliberately cross-origin, so React can neither read its body nor
# distinguish "app UI" from "proxy error". The page has to come from here.
# That makes this the documented exception to backend-returns-facts: owner-facing
# copy lives in the frontend EXCEPT where the browser renders our bytes directly.
#
# Subresource requests (fetch/XHR from inside the app, sec-fetch-dest: empty)
# keep getting JSON — a script parsing our HTML would be a worse failure.
def _wants_document(request: Request) -> bool:
    dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if dest:
        return dest in ("iframe", "document", "frame", "embed", "object")
    # No Sec-Fetch-Dest (older browsers, curl): fall back to Accept.
    return "text/html" in (request.headers.get("accept") or "")


def _unreachable_page(info: dict, label: str, theme: str) -> str:
    import html as _html
    dark = theme != "light"
    bg, fg, dim = ("#16181d", "#e8eaed", "#9aa0a6") if dark else ("#fbfbfc", "#1f2124", "#5f6368")
    card, edge = ("#1e2127", "#2c3038") if dark else ("#ffffff", "#e3e5e9")
    # `detail` is collapsed, not dropped: the owner asked what is wrong, not for
    # a stack trace, but the trace is what makes a NON-obvious failure fixable.
    detail = _html.escape(info.get("detail") or "")
    return f"""<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html.escape(label)} unavailable</title>
<style>
 html,body{{margin:0;height:100%;background:{bg};color:{fg};
   font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
 .wrap{{height:100%;display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box}}
 .card{{max-width:30rem;background:{card};border:1px solid {edge};border-radius:12px;padding:22px 24px}}
 h1{{margin:0 0 8px;font-size:15px;font-weight:600}}
 p{{margin:0;color:{dim}}}
 details{{margin-top:16px}}
 summary{{color:{dim};font-size:12px;cursor:pointer}}
 pre{{margin:8px 0 0;padding:10px;background:{bg};border:1px solid {edge};border-radius:8px;
   font-size:11px;color:{dim};white-space:pre-wrap;word-break:break-all;overflow-x:auto}}
</style>
<div class="wrap"><div class="card">
 <h1>{_html.escape(label)} is unavailable</h1>
 <p>{_html.escape(info.get("error") or "")}</p>
 {f'<details><summary>Technical detail</summary><pre>{detail}</pre></details>' if detail else ''}
</div></div>"""


def _unreachable_response(cid: str, e: Exception, request: Request,
                          url: str = "", what: str = "app") -> Response:
    info = connectors.unreachable(cid, e, url=url, what=what)
    if not _wants_document(request):
        return JSONResponse(info, status_code=502)
    label = (connectors.app(cid) or {}).get("label") or cid
    theme = (request.query_params.get("theme") or "dark").lower()
    return Response(content=_unreachable_page(info, label, theme), status_code=502,
                    media_type="text/html; charset=utf-8",
                    headers={"cache-control": "no-store"})


# Every verb a web app uses. HEAD (a download probe) and OPTIONS (an app
# preflighting or introspecting its own API) used to fall outside the list and
# came back as Ava's 405 — listed explicitly because FastAPI does not add them.
_PROXY_METHODS = ["GET", "POST", "DELETE", "PATCH", "PUT", "HEAD", "OPTIONS"]


@app.api_route("/apps/{cid}/api/{path:path}", methods=_PROXY_METHODS)
async def app_api_proxy(cid: str, path: str, request: Request):
    cfg = await run_in_threadpool(connectors.app_api, cid)
    if not cfg:
        # No declared ui.api (token injection / separate API base). The common
        # case is an app serving its API same-origin with its UI — so /api/*
        # falls through to the generic UI proxy instead of 404ing. EXCEPT
        # across the origin split: on the main host the api path is admitted
        # (apps_origin.is_app_api_path) precisely because a declared data-proxy
        # returns the app's DATA, and letting the fall-through serve the app's
        # UI DOCUMENTS on Ava's own origin would reopen the same-origin hole
        # the split exists to close.
        from ava_bridge import apps_origin
        if apps_origin.configured() and not apps_origin.on_apps_host(request):
            return JSONResponse(
                {"error": f"connector {cid} declares no browser API"},
                status_code=404)
        return await app_ui_proxy(cid, f"api/{path}", request)
    url = f"{cfg['base']}{cfg['prefix']}/{path}"
    # The connector's saved credential is authoritative (it survives a stale
    # token in the app's own storage): manifest ui.api token first, then the
    # app's proxy token. Only when NEITHER is saved does the browser's own
    # Authorization ride through with the other forwarded headers — an app
    # with a login but no stored token, where dropping its bearer would log
    # the user out. Resolved on the bridge, never handed to the browser
    # (Ava-never-has-passwords).
    auth_override = None
    if cfg["token"]:
        auth_override = "Bearer " + cfg["token"]
    else:
        tok = await run_in_threadpool(connectors.app_token, cid)
        if tok:
            auth_override = "Bearer " + tok
    return await _proxy_stream(cid, request, url, base=cfg["base"],
                               auth_override=auth_override, what="api")


@app.api_route("/apps/{cid}/{path:path}", methods=_PROXY_METHODS)
async def app_ui_proxy(cid: str, path: str, request: Request):
    meta = await run_in_threadpool(connectors.app, cid)
    if not meta or meta.get("embed") != "iframe" or not meta.get("url"):
        return JSONResponse({"error": f"connector {cid} is not an iframe app"},
                            status_code=404)
    # A TOP-LEVEL visit to the iframe's src (typed/bookmarked /apps/<id>/)
    # strands the user in the bare app with no Ava shell around it. Bounce them
    # to the app's tile inside the shell; the embedded iframe itself fetches
    # with Sec-Fetch-Dest: iframe and passes through untouched.
    if not path and request.method == "GET" \
            and request.headers.get("sec-fetch-dest") == "document":
        return RedirectResponse(f"/#{cid}")
    url = f"{meta['url'].rstrip('/')}/{path}"
    # Keep the owner signed in to an app they already connected — same
    # credential rule as the data-proxy above, resolved on the bridge and
    # never handed to the browser.
    tok = await run_in_threadpool(connectors.app_token, cid)
    return await _proxy_stream(cid, request, url, base=meta["url"],
                               auth_override=("Bearer " + tok) if tok else None,
                               what="app")


@app.websocket("/apps/{cid}/{path:path}")
async def app_ws_proxy(ws: WebSocket, cid: str, path: str):
    """The websocket half of the app proxy, beside its two HTTP twins.

    They are one contract — same path, same connector resolution, same
    credential rule — so they live in one file. Only the transport differs, and
    it differs enough to need its own route: the HTTP proxies are streaming
    `httpx` calls with `follow_redirects=False`, and `httpx` cannot speak
    WebSocket at all. A `GET` carrying `Upgrade: websocket` would match the
    HTTP catch-all and be executed as an ordinary request, which never
    performs the handshake.

    GATING: `auth_gate` is registered `app.middleware("http")` and Starlette
    forwards non-HTTP scopes past it untouched, so this route is PUBLIC unless
    it gates itself. `ws_auth.guard` re-runs every check `auth_gate` makes, in
    the same order, calling the same functions. See tests/test_websocket_auth.py.

    TOKEN LIFETIME, stated rather than discovered: `apps_origin.TOKEN_TTL_S` is
    300 seconds, but a socket is authorized once at the handshake and never
    re-checked — so a panel left open for an hour outlives its token by 55
    minutes. That is ordinary websocket behaviour rather than a bug, and an
    owner who wants periodic re-authentication sets
    `agent.gateway.ws_max_lifetime_s`.
    """
    why = await ws_auth.guard(ws)
    if why:
        await ws_auth.refuse(ws, why)
        return

    meta = await run_in_threadpool(connectors.app, cid)
    if not meta or meta.get("embed") != "iframe" or not meta.get("url"):
        await ws_auth.refuse(ws, f"connector {cid} is not an iframe app")
        return

    upstream = _ws_url(meta["url"], path, str(ws.url.query or ""))
    fwd = {}
    tok = await run_in_threadpool(connectors.app_token, cid)
    if tok:
        # Resolved on the bridge and never handed to the browser — the same
        # Ava-never-has-passwords rule the two HTTP proxies follow.
        fwd["Authorization"] = "Bearer " + tok

    # Offer exactly what the browser offered, and accept exactly what upstream
    # chose. Never guess: `accept()` with no subprotocol against a browser that
    # offered one is a silent immediate close, which reads as "the app is
    # broken" rather than as a negotiation failure.
    offered = list(ws.scope.get("subprotocols") or [])
    try:
        async with websockets.connect(
                upstream, additional_headers=fwd or None,
                subprotocols=offered or None,
                open_timeout=10, ping_interval=20, ping_timeout=20,
                close_timeout=5, max_size=None) as up:
            await ws.accept(subprotocol=up.subprotocol)
            await _ws_pump(ws, up)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001 — an app being down is not a bridge error
        # Refused BEFORE accept when the upstream never came up, so the browser
        # sees a failed handshake it can retry rather than an immediate close it
        # has to guess about.
        await ws_auth.refuse(ws, f"{cid} websocket unreachable: {e}")


def _ws_url(base: str, path: str, query: str) -> str:
    """`http(s)://host/base` + path -> `ws(s)://host/base/path`."""
    root = base.rstrip("/")
    if root.startswith("https://"):
        root = "wss://" + root[len("https://"):]
    elif root.startswith("http://"):
        root = "ws://" + root[len("http://"):]
    url = f"{root}/{path.lstrip('/')}" if path else root + "/"
    return f"{url}?{query}" if query else url


async def _ws_pump(ws: WebSocket, up) -> None:
    """Relay both directions until either end closes.

    Two tasks under one `gather`, cancelled together — the same shape
    `ava_bridge/gw_forward.py` already uses for raw TCP, so this is a house
    pattern rather than a new idea. Text and binary are relayed as they arrive;
    the proxy does not decode either, because an app's protocol is not Ava's
    business.
    """
    async def _down() -> None:
        async for msg in up:
            if isinstance(msg, (bytes, bytearray)):
                await ws.send_bytes(bytes(msg))
            else:
                await ws.send_text(msg)

    async def _upstream() -> None:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                return
            if msg.get("text") is not None:
                await up.send(msg["text"])
            elif msg.get("bytes") is not None:
                await up.send(msg["bytes"])

    tasks = [asyncio.create_task(_down()), asyncio.create_task(_upstream())]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


@app.get("/api/model")
async def api_model_get():
    """Current model choice + selectable backends for the chat dropdown.

    When the agent runtime is active, chat turns are served by the SANDBOX
    model and bypass the router entirely — so the router pick below only
    governs the tool-less fallback path. `agent_model` lets the picker say so
    instead of promising "which model answers" while changing nothing.

    Both fields come from `models.effective_brain()`. They used to be derived
    here, independently, by asking `sandbox_info()` directly — and the two
    derivations did not agree: this one gated on `rt.name == "nemoclaw"` while
    the resolver gates on `rt.name != "direct"`, so on a `remote` runtime the
    resolver said the sandbox WAS the brain and this endpoint said `null`, and
    the header pill went blank on a perfectly working install. That is the
    second-answer problem the one-resolver rule exists to prevent, and
    tests/test_one_brain_resolver.py now fails any new instance of it.
    """
    def _load():
        r = get_route() or {}
        if not isinstance(r, dict):
            r = {}
        # Normalize even a foreign/unauthorized router reply to the contract.
        r.setdefault("mode", None)
        r.setdefault("backends", [])
        try:
            from ava_bridge import models as _models
            brain = _models.effective_brain()
            # Kept for the existing contract (qa/test_02_api_contracts.py asserts
            # the key; useChat.ts reads it). Only its DERIVATION changes.
            r["agent_model"] = (brain.get("model_id") or None
                                if brain.get("source") == "agent" else None)
            # The whole answer, so the header can name the brain in EVERY shape
            # rather than only when a sandbox happens to be running.
            r["brain"] = {k: brain.get(k) for k in
                          ("source", "model_id", "label", "engine", "implicit")}
        except Exception:  # noqa: BLE001
            r["agent_model"] = None
            r["brain"] = None
        return r
    return await run_in_threadpool(_load)


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


# Personal-app routes (reverse proxies + their /internal/* read-backs) live in
# the optional gitignored overlay (overlay/ava_bridge/*), registered at boot.


# === LOG MANAGEMENT ENDPOINTS ===


# === CONFIGURATION MANAGEMENT ENDPOINTS ===


# === POLICY MANAGEMENT ENDPOINTS ===


# An app draft-preview route moved to the optional overlay
# (overlay/ava_bridge/personal_routes.py), registered above when the overlay is
# present. A fork without it simply has no such route.


@app.post("/api/talk")
async def talk(audio: UploadFile = File(...), history: str = Form("[]"),
               attachments: str = Form("[]"), chat_id: str = Form("")):
    # Honor a DELIBERATE off (yaml features.voice:false / AVA_VOICE=0) — the
    # toggle the wizard and Hub write must actually gate this endpoint, not be
    # decorative. Unset stays permissive so installs that never wrote the key
    # (where voice works purely because the deps are installed) keep working.
    if features.explicitly_off("voice"):
        return JSONResponse(
            {"error": "voice is turned off in settings (features.voice)",
             "error_code": "voice_off"},
            status_code=503)
    # Bind the three heavy objects ONCE, here, and use these locals for the rest of
    # the turn. Reading state.heavy at each point of use instead left three unguarded
    # dereferences spread across a window that spans an ffmpeg decode (up to 30s) and
    # a sidecar round-trip — and the owner can now free these models from the hardware
    # panel, on a background thread, at any moment inside it. Landing there gave an
    # unhandled 500, not the lazy reload the design intends. Holding them also keeps
    # them alive for the turn, so a release mid-turn frees nothing until it ends,
    # which is honest and is what the allocator will measure.
    whisper, verifier, voiceprint = _ensure_loaded()
    if state.heavy["voice_unavailable"]:
        return JSONResponse(
            {"error": "voice not installed — pip install -r requirements-voice.txt"},
            status_code=503)
    raw = await audio.read()
    if not raw:
        return JSONResponse({"error": "empty audio"}, status_code=400)

    try:
        # Threadpool, not the loop: decode_to_pcm shells out to ffmpeg with a 30s
        # ceiling (config.AUDIO_DECODE_TIMEOUT). Every heavy step in this route is
        # handed off the same way — a voice turn can legitimately run for minutes
        # (the agent turn alone carries a 600s ceiling), and on the loop that
        # froze SSE, the dashboard and the login gate for the whole turn.
        pcm = await run_in_threadpool(decode_to_pcm, raw)
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
            # config.LOGS_DIR, not __file__/logs: the latter is the code root,
            # which on Docker is the ephemeral container layer, not the volume.
            _dbg_path = os.path.join(config.LOGS_DIR, "last_talk.wav")
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
    if verifier is not None and voiceprint is not None:
        emb = await run_in_threadpool(verifier.embed_pcm, pcm)
        sim = spk.cosine(voiceprint, emb)
        if sim < PHONE_THRESHOLD:
            return {"accepted": False, "sim": round(float(sim), 3),
                    "threshold": PHONE_THRESHOLD,
                    "reply": "Sorry, I only respond to the enrolled voice."}

    # Transcribe on the GPU sidecar when enabled (AVA_STT=gpu), falling back to
    # the local CPU Whisper if that service is unreachable or errors — so a
    # stopped sidecar slows STT but never breaks voice.
    text = None
    if os.environ.get("AVA_STT", "").strip().lower() == "gpu":
        try:
            text = await run_in_threadpool(gpu_transcribe, pcm)
        except Exception as e:  # noqa: BLE001 — degrade to CPU, don't fail voice
            print(f"[ava] GPU STT unavailable ({e}); falling back to CPU Whisper",
                  flush=True)
    if text is None:
        if whisper is None:
            return JSONResponse(
                {"error": "speech-to-text is not loaded right now",
                 "error_code": "voice_released"}, status_code=503)
        text = await run_in_threadpool(va.transcribe, whisper, pcm)
    if not text:
        return {"accepted": True, "text": "", "reply": "",
                "sim": round(float(sim), 3) if sim is not None else None,
                "note": "I didn't catch that — try again."}

    sim_out = round(float(sim), 3) if sim is not None else None

    # Route through Ava — she decides which of her tools the turn needs.
    ids = parse_ids(attachments)
    agent_text = augment(text, ids)
    agent_text = memory_store.augment_with_recall(agent_text, text, chat_id)
    sid = chat_session(chat_id) if chat_id else None
    if chat_id:
        chat_append(chat_id, "user", text, atts_meta(ids))
    tools: list[str] = []
    try:
        reply, tools = await run_in_threadpool(
            _agent_run_turn, agent_text, session_id=sid,
            history=_history_for(chat_id))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"Ava unreachable: {e}"}, status_code=502)

    m = which_model()
    if chat_id:
        chat_append(chat_id, "assistant", reply, model=m, tools_used=tools)
    wav = await run_in_threadpool(tts_wav_bytes, reply)
    return {
        "accepted": True,
        "text": text,
        "reply": reply,
        "sim": sim_out,
        "audio": base64.b64encode(wav).decode(),
        "model": m,
        "tools_used": tools,
    }


@app.post("/api/talk-text")
async def talk_text(text: str = Form(...), history: str = Form("[]"),
                    attachments: str = Form("[]"), chat_id: str = Form("")):
    """Typed chat (no voice gate, no TTS)."""
    text = text.strip()
    ids = parse_ids(attachments)
    if not text and not ids:
        return JSONResponse({"error": "empty text"}, status_code=400)

    agent_text = augment(text, ids)
    agent_text = memory_store.augment_with_recall(agent_text, text, chat_id)
    sid = chat_session(chat_id) if chat_id else None
    if chat_id:
        chat_append(chat_id, "user", text, atts_meta(ids))
    tools: list[str] = []
    try:
        # Same threadpool hand-off as /api/talk: the agent turn carries a 600s
        # ceiling and must not sit on the event loop.
        reply, tools = await run_in_threadpool(
            _agent_run_turn, agent_text, session_id=sid,
            history=_history_for(chat_id))
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"Ava unreachable: {e}"}, status_code=502)
    m = which_model()
    if chat_id:
        chat_append(chat_id, "assistant", reply, model=m, tools_used=tools)
    return {"reply": reply, "model": m, "tools_used": tools}


@app.get("/api/artifact/weather")
def artifact_weather(location: str = "", days: int = 7):
    """Rebuild a fresh weather artifact (used by the panel's Refresh button)."""
    from ava_bridge.artifacts import build_weather_artifact
    art = build_weather_artifact((location or "").strip() or None, days)
    if not art:
        return JSONResponse({"error": "could not fetch weather"}, status_code=502)
    return art




