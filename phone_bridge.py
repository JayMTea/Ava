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

import base64
import json
import os
import threading

import requests
from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import (JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

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
from ava_bridge import dashboard, connectors, perf_store, devices
from ava_bridge import arch_watch
from ava_bridge import hardware
from ava_bridge import learning
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
def _ensure_loaded():
    if state.heavy["voice_unavailable"]:
        return
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
    elif rt.name == "direct":
        print("[ava-bridge] agent runtime absent -> DIRECT (tool-less) chat. "
              "Install NemoClaw for tools/memory/skills (`ava agent provision`).", flush=True)
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
    # Start the in-process self-analysis/learning scheduler (local-first; parks
    # improvement proposals for approval). No-op if features.learning is false.
    learning.start_scheduler()
    # Architecture drift watchdog: periodic SSOT check between commits — heals
    # stale diagrams, alerts on structural drift. No-op without a manifest.
    arch_watch.start_scheduler()
    # Allocation watchdog: polls each declared model's readiness, so "the service is
    # up but its model never loaded" raises an alert instead of going unnoticed —
    # that state answers its own port, so no liveness check detects it. No-op until
    # models are declared in alloc.models.
    try:
        from ava_bridge.alloc import watch as alloc_watch
        alloc_watch.start_scheduler()
    except Exception as e:  # noqa: BLE001 — optional subsystem; never block boot
        print(f"[ava-bridge] allocation watchdog unavailable: {e}", flush=True)


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

# Data page API (cookie-gated /api/data/* — the on-disk store inventory).
from ava_bridge.data_api import router as _data_router  # noqa: E402
app.include_router(_data_router)

# Browser-facing pages: login, first-run setup, logout, password change, the
# SPA shell and the PWA assets. Registered first so the shell is in place before
# the API routers.
app.include_router(_pages.router)

# Media + turns (cookie-gated /api/upload, /api/turn/*, /api/turns). Registers
# after the /uploads StaticFiles mount above, preserving the original
# declaration order.
from ava_bridge.media_api import router as _media_router  # noqa: E402
app.include_router(_media_router)

# Telemetry reads (cookie-gated /api/perf/*, /api/hardware*) — what Vitals polls.
from ava_bridge.perf_api import router as _perf_router  # noqa: E402
app.include_router(_perf_router)

# Chat API (cookie-gated /api/chats/*, /api/chat-stream, /api/ghost/discard).
from ava_bridge.chats_api import router as _chats_router  # noqa: E402
app.include_router(_chats_router)

# Operations API + the live SSE ops stream (cookie-gated /api/ops/*,
# /api/stream/ops). The poll endpoints and the push channel are one contract.
from ava_bridge.ops_api import router as _ops_router  # noqa: E402
app.include_router(_ops_router)

# Owner-facing learning review API (cookie-gated /api/learning/*). Its sandbox
# counterpart, /internal/learning/*, lives in internal.py with the rest of the
# token-gated surface — same feature, different trust boundary.
from ava_bridge.learning_api import router as _learning_router  # noqa: E402
app.include_router(_learning_router)

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

    AVA_MODEL stays as the floor for the case the resolver reports as `none`
    (no backend anywhere): a PUBLIC health probe must still answer with a
    string. Lazily imported, and every failure swallowed, for the same reason as
    before — health must never fail on a probe.
    """
    try:
        from ava_bridge import models as _models
        m = str(_models.effective_brain().get("model_id") or "").strip()
        if m:
            return m
    except Exception:  # noqa: BLE001 — health must never fail on a probe
        pass
    return va.AVA_MODEL


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


# Dashboard routes (Vitals + Operations) moved to ava_bridge/dashboard.py and
# ava_bridge/ops_api.py. "Command Center" was retired as a name: it rhymed with
# the Control Center, which is a different surface (Operations -> Control).
# All cookie-gated /api/* (browser auth); read-first wrappers over ava_bridge
# modules.


@app.get("/api/code-turns")
async def api_code_turns(limit: int = 30):
    return await run_in_threadpool(dashboard.code_turns_list, limit)


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
    ctype = r.headers.get("content-type", "")
    if "text/html" in ctype:
        # An embedded app's HTML entry must always revalidate. Many app servers
        # (Starlette StaticFiles included) send Last-Modified but no
        # Cache-Control, so browsers cache the page heuristically — pinning the
        # iframe to a stale bundle across the app's rebuilds. ETag stays, so
        # unchanged pages are still cheap 304s; hashed assets stay long-cached.
        fwd.pop("expires", None)
        fwd["cache-control"] = "no-cache"
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type"), headers=fwd)


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


@app.api_route("/apps/{cid}/api/{path:path}",
               methods=["GET", "POST", "DELETE", "PATCH", "PUT"])
async def app_api_proxy(cid: str, path: str, request: Request):
    cfg = await run_in_threadpool(connectors.app_api, cid)
    if not cfg:
        # No declared ui.api (token injection / separate API base). The common
        # case is an app serving its API same-origin with its UI — so /api/*
        # falls through to the generic UI proxy instead of 404ing.
        return await app_ui_proxy(cid, f"api/{path}", request)
    url = f"{cfg['base']}{cfg['prefix']}/{path}"
    params = dict(request.query_params)
    headers = {"content-type": request.headers.get("content-type", "application/json")}
    if cfg["token"]:
        headers["Authorization"] = "Bearer " + cfg["token"]
    else:
        # The connector's saved credential is authoritative (it survives a stale
        # token in the app's own storage). Only when none is saved do we forward
        # the app's OWN session — an app with a login but no stored token, where
        # dropping its bearer would log the user out. '' leaves it unauthenticated.
        tok = await run_in_threadpool(connectors.app_token, cid)
        if tok:
            headers["Authorization"] = "Bearer " + tok
        elif request.headers.get("authorization"):
            headers["Authorization"] = request.headers["authorization"]
    body = await request.body() if request.method in ("POST", "PATCH", "PUT") else None

    # allow_redirects=False on every branch: a connector's own API is trusted to
    # answer, not to re-point the bridge somewhere else. Following a redirect here
    # is an SSRF primitive — the app returns `302 Location:
    # http://127.0.0.1:8010/...` and the bridge fetches loopback on its behalf and
    # hands the body back. `web_fetch` re-validates every hop against an SSRF guard
    # (ava_bridge/web.py:108); this path had no guard at all, so it refuses hops
    # instead. A 3xx is forwarded to the browser, which is what a same-origin
    # fetch would do anyway.
    def _do():
        if request.method in ("POST", "PATCH", "PUT"):
            return requests.request(request.method, url, params=params, data=body,
                                    headers=headers, timeout=200,
                                    allow_redirects=False)
        if request.method == "DELETE":
            return requests.delete(url, params=params, headers=headers, timeout=60,
                                   allow_redirects=False)
        return requests.get(url, params=params, headers=headers, timeout=60,
                            allow_redirects=False)

    try:
        r = await run_in_threadpool(_do)
    except Exception as e:  # noqa: BLE001
        return _unreachable_response(cid, e, request, url=url, what="api")
    return _proxy_response(r)


@app.api_route("/apps/{cid}/{path:path}",
               methods=["GET", "POST", "DELETE", "PATCH", "PUT"])
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
    params = dict(request.query_params)
    body = await request.body() if request.method in ("POST", "PATCH", "PUT") else None
    # Keep the owner signed in to an app they already connected. The connector's
    # saved credential is authoritative and wins — so a stale token still sitting in
    # the embedded app's storage can't cause a 401/login flash; only if none is saved
    # do we forward the app's OWN session (an app with a login but no stored token).
    # Resolved on the bridge, never handed to the browser (Ava-never-has-passwords).
    fwd = {}
    tok = await run_in_threadpool(connectors.app_token, cid)
    if tok:
        fwd["Authorization"] = "Bearer " + tok
    elif request.headers.get("authorization"):
        fwd["Authorization"] = request.headers["authorization"]

    # allow_redirects=False — see the note on the data-proxy above. This path is
    # the more exposed of the two: `fwd` can carry the app's own bearer, and
    # `requests` strips Authorization only across HOSTS, so a same-host redirect
    # would resend the credential to an attacker-chosen path on that host.
    def _do():
        if request.method in ("POST", "PATCH", "PUT"):
            ct = request.headers.get("content-type", "application/json")
            return requests.request(request.method, url, params=params, data=body,
                                    headers={"content-type": ct, **fwd}, timeout=180,
                                    allow_redirects=False)
        if request.method == "DELETE":
            return requests.delete(url, params=params, headers=fwd or None, timeout=60,
                                   allow_redirects=False)
        return requests.get(url, params=params, headers=fwd or None, timeout=60,
                            allow_redirects=False)

    try:
        r = await run_in_threadpool(_do)
    except Exception as e:  # noqa: BLE001
        return _unreachable_response(cid, e, request, url=url, what="app")
    return _proxy_response(r)


@app.get("/api/model")
async def api_model_get():
    """Current model choice + selectable backends for the chat dropdown.

    When the agent runtime is active, chat turns are served by the SANDBOX
    model and bypass the router entirely — so the router pick below only
    governs the tool-less fallback path. `agent_model` lets the picker say so
    instead of promising "which model answers" while changing nothing."""
    def _load():
        r = get_route() or {}
        if not isinstance(r, dict):
            r = {}
        # Normalize even a foreign/unauthorized router reply to the contract.
        r.setdefault("mode", None)
        r.setdefault("backends", [])
        try:
            from ava_bridge import runtime as _runtime
            rt = _runtime.active()
            if rt.name == "nemoclaw":
                r["agent_model"] = (rt.sandbox_info(wait=False) or {}).get("model")
            else:
                r["agent_model"] = None
        except Exception:  # noqa: BLE001
            r["agent_model"] = None
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
    _ensure_loaded()
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
    if state.heavy["verifier"] is not None:
        emb = await run_in_threadpool(state.heavy["verifier"].embed_pcm, pcm)
        sim = spk.cosine(state.heavy["voiceprint"], emb)
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
        text = await run_in_threadpool(va.transcribe, state.heavy["whisper"], pcm)
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


# ---- Code mode endpoints ----

@app.get("/api/code/models")
def code_models():
    """Available Claude models for governed code edits.

    Delegates to coder.list_models(), which queries the live /v1/models and falls
    back to config.CODE_MODELS_FALLBACK. This route used to hardcode its own copy
    of that same three-id list, so adding a model to the fallback updated the SPA
    and silently left the legacy UI's dropdown a version behind.
    """
    from ava_bridge import coder
    return {"models": coder.list_models()}


# ---- Learning system endpoints (code improvement proposals + feedback) --------


