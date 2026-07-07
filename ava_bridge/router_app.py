"""Ava inference router — OpenAI-compatible failover proxy (app factory).

The router is the stable control point between everything that wants a model
(the bridge's direct chat path, the sandboxed agent, any local client) and the
actual OpenAI-compatible backends declared in ava.yaml `inference.backends` —
vLLM, Ollama (:11434/v1), llama.cpp server (:8080/v1), or a cloud endpoint.
It owns:
  • per-completion token/throughput perf logging,
  • the hardware-fit `/fit` view (workload hint + live memory),
  • failover across declared backends (5xx/404/connection error), and
  • the `/which` + `/route` control surface for the UI model pill/picker.

Deployment shapes (both use this same factory):
  • EMBEDDED (default): the bridge starts the router in-process on
    127.0.0.1:8010 at boot — no extra service (see ava_bridge/router_host.py).
  • STANDALONE: `uvicorn ava_router:app --host 127.0.0.1 --port 8010`
    (e.g. an always-on systemd unit). The embedded starter detects it and
    yields.

Auth: /which, /route and /fit always require the shared router token.
/v1/* is open on loopback; when the router is bound to a non-loopback host
(or `inference.router.require_auth: true`) every /v1/* call must carry
`Authorization: Bearer <token>` or `X-Ava-Router-Token: <token>`.
/healthz is always open.

Keep this module import-light: it must be usable standalone (no heavy bridge
imports at module load).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:  # perf_log lives at the repo root; best-effort only.
    import perf_log
except Exception:  # noqa: BLE001 — router must run even if logging is missing
    perf_log = None

try:  # hardware-aware fit/selection; router must run even if it's missing.
    from ava_bridge import model_fit
except Exception:  # noqa: BLE001
    model_fit = None

# --- defaults / legacy fallback ----------------------------------------------
OMNI_URL = os.environ.get("AVA_OMNI_URL", "http://127.0.0.1:8002/v1").rstrip("/")
OMNI_MODEL = os.environ.get(
    "AVA_OMNI_MODEL", "nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8")
OMNI_LABEL = os.environ.get("AVA_OMNI_LABEL", "open-model 30B")

# Statuses that mean "this backend can't serve right now, try the next one".
_FAILOVER_STATUS = {404, 500, 502, 503, 504}

_LEGACY_BACKENDS = [
    {"id": "omni", "url": OMNI_URL, "model": OMNI_MODEL, "label": OMNI_LABEL,
     "engine": "vllm", "api_key": None, "tools": "native",
     "fit": {"weight_gb": 35, "tier": "large", "min_free_gb": 10,
             "workloads": ["chat", "reasoning", "code", "vision", "audio",
                           "fast", "cheap", "utility", "classify"]}},
]

# Engines whose OpenAI-compatible streaming endpoint accepts
# `stream_options: {include_usage: true}` (needed so usage/token counts appear
# in streamed responses — Ollama omits usage otherwise). llama.cpp builds may
# reject unknown params, so it is excluded; usage is then simply absent.
_STREAM_USAGE_ENGINES = {"vllm", "ollama", "openai"}


def _resolve_reasoning() -> str:
    """`inference.reasoning` (ava.yaml) / AVA_REASONING (env): the default
    thinking control injected as vLLM `chat_template_kwargs`. See ava_router.py
    history: budget:128 keeps tool decisions correct while staying snappy."""
    val = os.environ.get("AVA_REASONING")
    if val is None:
        try:
            from ava_bridge import settings
            val = settings.get("inference.reasoning")
        except Exception:  # noqa: BLE001 — never let config break the router
            val = None
    return str(val if val is not None else "budget:128").strip().lower()


def _reasoning_kwargs(reasoning: str) -> dict | None:
    """The chat_template_kwargs to inject by default, or None for 'on'."""
    r = reasoning
    if r in ("on", "", "default", "none"):
        return None
    if r in ("off", "false", "no", "0"):
        return {"enable_thinking": False}
    budget = r.split(":", 1)[1] if r.startswith("budget:") else r
    try:
        return {"reasoning_budget": max(0, int(budget))}
    except (ValueError, TypeError):
        return {"enable_thinking": False}  # unrecognised -> the snappy default


def _env_backend() -> dict | None:
    """A single backend declared purely via env (container-friendly):
    AVA_BACKEND_URL (+ AVA_BACKEND_ENGINE, AVA_BACKEND_MODEL, AVA_INFERENCE_KEY).
    Checked before the legacy Omni fallback so `docker compose --profile cpu`
    can point at an Ollama service with zero ava.yaml edits."""
    base = os.environ.get("AVA_BACKEND_URL", "").rstrip("/")
    if not base:
        return None
    engine = os.environ.get("AVA_BACKEND_ENGINE", "openai").strip().lower()
    model = os.environ.get("AVA_BACKEND_MODEL", "")
    return {"id": "backend", "url": base, "model": model,
            "label": os.environ.get("AVA_BACKEND_LABEL", model or engine),
            "engine": engine,
            "api_key": os.environ.get("AVA_INFERENCE_KEY") or None,
            "tools": os.environ.get("AVA_BACKEND_TOOLS", "native"),
            "fit": None}


def load_backends() -> list:
    """Inference backends from ava.yaml `inference.backends`, ordered
    primary -> fallback -> the rest; else a single env-declared backend
    (AVA_BACKEND_URL); else the always-on model default — a fresh install works.

    Each backend is ANY OpenAI-compatible endpoint. `api_key_env` names an env
    var sent as a Bearer token. `tools: native|none` says whether the endpoint
    can return structured tool_calls — requests containing `tools` skip
    `tools: none` backends so a tool turn never lands on an engine that would
    return the call as prose.
    """
    try:
        from ava_bridge import settings
        cfg = settings.get("inference.backends")
        primary = settings.get("inference.primary")
        fallback = settings.get("inference.fallback")
    except Exception:  # noqa: BLE001 — never let config break the router
        cfg = primary = fallback = None
    if not isinstance(cfg, dict) or not cfg:
        env_b = _env_backend()
        return [env_b] if env_b else list(_LEGACY_BACKENDS)
    order: list = []
    for n in (primary, fallback):
        if n in cfg and n not in order:
            order.append(n)
    for n in cfg:
        if n not in order:
            order.append(n)
    out = []
    for name in order:
        b = cfg.get(name) or {}
        base = str(b.get("base_url", "")).rstrip("/")
        if not base:
            continue
        key = os.environ.get(b["api_key_env"]) if b.get("api_key_env") else None
        out.append({
            "id": name, "url": base, "model": b.get("model") or "",
            "label": b.get("label") or name, "engine": b.get("engine", "openai"),
            "api_key": key or None,
            "tools": str(b.get("tools", "native")).lower(),
            # Optional hardware-fit profile (weight_gb / tier / workloads /
            # min_free_gb); model_fit.py supplies permissive defaults if absent.
            "fit": b.get("fit") or None,
        })
    if out:
        return out
    env_b = _env_backend()
    return [env_b] if env_b else list(_LEGACY_BACKENDS)


def _resolve_token(token: str | None) -> str | None:
    """Shared router token: explicit -> env -> secrets store -> bridge token."""
    if token:
        return token
    tok = os.environ.get("AVA_ROUTER_TOKEN")
    if tok:
        return tok
    try:
        from ava_bridge import settings
        tok = settings.secret("router_token")
        if tok:
            return tok
    except Exception:  # noqa: BLE001
        pass
    try:
        from ava_bridge import config as _bridge_config
        return _bridge_config.INTERNAL_TOKEN
    except Exception:  # noqa: BLE001 — router must still boot without the bridge
        return None


def _is_loopback(host: str | None) -> bool:
    if not host:
        return True
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_require_v1_auth(require_v1_auth, bind_host: str | None) -> bool:
    """require_auth: true|false|auto (auto = required iff bound off-loopback)."""
    if require_v1_auth is not None:
        return bool(require_v1_auth)
    try:
        from ava_bridge import settings
        raw = settings.get("inference.router.require_auth", "auto",
                           env="AVA_ROUTER_REQUIRE_AUTH")
    except Exception:  # noqa: BLE001
        raw = os.environ.get("AVA_ROUTER_REQUIRE_AUTH", "auto")
    raw = str(raw).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return not _is_loopback(bind_host)


# Sampling / decoding parameters we surface in the perf log.
_PARAM_KEYS = (
    "temperature", "top_p", "top_k", "min_p", "max_tokens",
    "max_completion_tokens", "presence_penalty", "frequency_penalty",
    "repetition_penalty", "stream", "n", "seed", "stop",
)

_PROMPT_RE = re.compile(rb'"prompt_tokens"\s*:\s*(\d+)')
_TOTAL_RE = re.compile(rb'"total_tokens"\s*:\s*(\d+)')
_COMPLETION_RE = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')

_GUARDED_PATHS = ("/which", "/route", "/fit")
_WORKLOAD_HEADER = "x-ava-workload"


def _workload_hint(headers, body_obj) -> str | None:
    wl = headers.get(_WORKLOAD_HEADER)
    if not wl and isinstance(body_obj, dict):
        wl = body_obj.get("workload") or body_obj.get("ava_workload")
    wl = (wl or "").strip().lower()
    return wl or None


def _extract_params(raw: bytes, is_json: bool) -> dict:
    """Pull the tunable request parameters (+ requested model) for logging."""
    if not is_json or not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(obj, dict):
        return {}
    params = {k: obj.get(k) for k in _PARAM_KEYS if k in obj}
    params["model"] = obj.get("model")
    msgs = obj.get("messages")
    if isinstance(msgs, list):
        params["n_messages"] = len(msgs)
    return params


class RouterState:
    """Per-app mutable state (was module globals; app-scoped so tests can build
    isolated apps and the embedded + standalone shapes don't share records)."""

    def __init__(self, backends: list, mode: str | None = None):
        self.backends = backends
        self.mode = (mode or os.environ.get("AVA_ROUTER_MODE")
                     or (backends[0]["id"] if backends else "omni"))
        # Which backend served the most recent completion (single-user app, so
        # a simple last-write-wins record is enough for the UI pill).
        self.last = {"id": None, "model": None, "label": None, "ts": 0.0}
        # Real token usage + throughput of the most recent completion.
        self.last_usage = {"prompt_tokens": None, "total_tokens": None, "ts": 0.0}
        self.last_perf = {"tokens_per_sec": None, "ttft_ms": None,
                          "gen_seconds": None}
        # Why the router ordered the last request the way it did (for /fit).
        self.last_fit = {"order": None, "workload": None, "ts": 0.0}

    def ordered_backends(self, workload: str | None = None,
                         wants_tools: bool = False) -> list:
        """Backends in try-order: hardware-fit + workload match when model_fit
        is available, else primary-first. Backends declaring `tools: none` are
        skipped for requests that carry `tools` (unless that would leave none)."""
        cands = None
        if model_fit is not None:
            try:
                sel = model_fit.select(self.backends, workload=workload,
                                       primary_id=self.mode)
                if sel:
                    self.last_fit["order"] = [
                        {"id": c.backend["id"], "fits": c.fits,
                         "handles": c.handles, "reason": c.reason} for c in sel]
                    self.last_fit["workload"] = workload
                    self.last_fit["ts"] = time.time()
                    cands = [c.backend for c in sel]
            except Exception:  # noqa: BLE001 — fit must never break serving
                cands = None
        if cands is None:
            primary = [b for b in self.backends if b["id"] == self.mode]
            rest = [b for b in self.backends if b["id"] != self.mode]
            cands = (primary + rest) if primary else list(self.backends)
        if wants_tools:
            with_tools = [b for b in cands if b.get("tools", "native") != "none"]
            # Never brick a single-backend install: if nothing declares tool
            # support, fall through and let the caller see the raw behavior.
            cands = with_tools or cands
        return cands

    def record_usage(self, tail: bytes) -> tuple:
        """(prompt, completion, total) token counts from a response tail; also
        updates the last_usage record the UI counter reads."""
        prompt = completion = total = None
        try:
            pm = _PROMPT_RE.findall(tail)
            cm = _COMPLETION_RE.findall(tail)
            tm = _TOTAL_RE.findall(tail)
            if pm:
                prompt = int(pm[-1])
            if cm:
                completion = int(cm[-1])
            if tm:
                total = int(tm[-1])
            if completion is None and total is not None and prompt is not None:
                completion = total - prompt
            if prompt is not None:
                self.last_usage.update(prompt_tokens=prompt, total_tokens=total,
                                       ts=time.time())
        except Exception:  # noqa: BLE001
            pass
        return prompt, completion, total

    def log_completion(self, backend: dict, params: dict, failover: bool,
                       endpoint: str, status: int, t_start: float,
                       t_first: float | None, t_end: float, tail: bytes,
                       nchunks: int) -> None:
        """Record one served LLM completion to the unified performance log."""
        prompt_tok, completion_tok, total_tok = self.record_usage(tail)
        gen_seconds = (t_end - t_first) if t_first else None
        ttft = (t_first - t_start) if t_first else None
        tps = perf_log.tok_per_sec(completion_tok, gen_seconds) if perf_log else None
        self.last_perf.update(
            tokens_per_sec=tps,
            ttft_ms=round(ttft * 1000, 1) if ttft else None,
            gen_seconds=round(gen_seconds, 3) if gen_seconds else None)
        if perf_log is None:
            return
        perf_log.log_perf(
            "llm",
            serving=backend.get("engine", "openai"),
            endpoint=endpoint,
            served_by=backend["id"],
            served_label=backend["label"],
            served_model=backend["model"],
            served_url=backend["url"],
            requested_model=params.get("model"),
            failover=failover or None,
            status=status,
            prompt_tokens=prompt_tok,
            completion_tokens=completion_tok,
            total_tokens=total_tok,
            tokens_per_sec=tps,
            ttft_ms=self.last_perf["ttft_ms"],
            gen_seconds=self.last_perf["gen_seconds"],
            total_seconds=round(t_end - t_start, 3),
            stream_chunks=nchunks or None,
            params={k: v for k, v in params.items()
                    if k != "model" and v is not None},
        )


def _rewrite_body(raw: bytes, is_json: bool, backend: dict, reasoning: str,
                  is_comp: bool = False) -> bytes:
    """Force the request's model id to match the backend we're forwarding to,
    inject the default reasoning control for local vLLM chat completions, and
    ask usage-capable engines to include usage in streamed responses."""
    if not is_json or not raw:
        return raw
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001 — not JSON we understand; pass through
        return raw
    if not isinstance(obj, dict):
        return raw
    changed = False
    # Drop Ava-private routing hints so the upstream API never sees them.
    hint = obj.pop("workload", None)
    hint = obj.pop("ava_workload", None) or hint
    if hint is not None:
        changed = True
    if "model" in obj:
        obj["model"] = backend["model"]
        changed = True
    # Default reasoning control — only for local vLLM chat completions (the
    # `chat_template_kwargs` field is a vLLM extension; other engines would
    # reject it). Never override a caller that set its own.
    if is_comp and backend.get("engine") == "vllm" and "chat_template_kwargs" not in obj:
        kw = _reasoning_kwargs(reasoning)
        if kw:
            obj["chat_template_kwargs"] = kw
            changed = True
    # Streamed completions: ask for usage in the final chunk so token counts /
    # tok-per-sec logging work on engines that omit usage in streams (Ollama).
    if (is_comp and obj.get("stream")
            and backend.get("engine") in _STREAM_USAGE_ENGINES
            and "stream_options" not in obj):
        obj["stream_options"] = {"include_usage": True}
        changed = True
    return json.dumps(obj).encode("utf-8") if changed else raw


def create_app(backends: list | None = None, *, token: str | None = None,
               require_v1_auth: bool | None = None, bind_host: str | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Build a router app. All arguments are optional — the defaults resolve
    from ava.yaml/env exactly like the old module-level router did. Tests pass
    `backends` + `transport` (httpx.MockTransport) + `token` for isolation."""
    state = RouterState(backends if backends is not None else load_backends())
    router_token = _resolve_token(token)
    v1_auth = _resolve_require_v1_auth(require_v1_auth, bind_host)
    reasoning = _resolve_reasoning()

    app = FastAPI(title="ava-router")
    app.state.router = state
    app.state.require_v1_auth = v1_auth

    # Generation can legitimately take a while (cold prefix cache ~20s, long
    # replies): generous read timeout; short connect so a dead backend fails fast.
    timeout = httpx.Timeout(connect=3.0, read=600.0, write=30.0, pool=5.0)
    client = httpx.AsyncClient(timeout=timeout, transport=transport)

    def _authed(request: Request) -> bool:
        tok = request.headers.get("x-ava-router-token", "")
        if not tok:
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                tok = auth[7:].strip()
        return bool(router_token and hmac.compare_digest(tok, router_token))

    @app.middleware("http")
    async def _guard(request: Request, call_next):
        path = request.url.path
        # /which, /route and /fit reveal or change Ava's active brain: always
        # token-guarded. /v1/* additionally requires the token when the router
        # is exposed beyond loopback (see module docstring). /healthz is open.
        if path in _GUARDED_PATHS and not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if v1_auth and path.startswith("/v1/") and not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.on_event("shutdown")
    async def _shutdown():
        await client.aclose()

    @app.get("/which")
    async def which():
        """The backend that served the most recent completion + its token use."""
        return {**state.last,
                "prompt_tokens": state.last_usage["prompt_tokens"],
                "total_tokens": state.last_usage["total_tokens"],
                "tokens_per_sec": state.last_perf["tokens_per_sec"],
                "ttft_ms": state.last_perf["ttft_ms"]}

    @app.get("/route")
    async def get_route():
        """Current model choice + the selectable backends (chat dropdown)."""
        return {"mode": state.mode,
                "backends": [{"id": b["id"], "label": b["label"],
                              "model": b["model"]} for b in state.backends]}

    @app.post("/route")
    async def set_route(request: Request):
        """Pick which backend the router prefers as primary."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        m = str((body or {}).get("mode", "")).strip()
        if m in {b["id"] for b in state.backends}:
            state.mode = m
            return {"ok": True, "mode": state.mode}
        return JSONResponse({"error": f"unknown mode {m!r}"}, status_code=400)

    @app.get("/fit")
    async def fit():
        """Live hardware-fit snapshot. Read-only; decides nothing."""
        if model_fit is None:
            return {"enabled": False, "reason": "model_fit unavailable"}
        report = model_fit.fit_report(state.backends)
        return {"enabled": True, "mode": state.mode, "last": state.last_fit,
                **report}

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "backends": [b["id"] for b in state.backends]}

    @app.api_route("/v1/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def proxy(path: str, request: Request):
        raw = await request.body()
        ctype = request.headers.get("content-type", "")
        is_json = "application/json" in ctype.lower()

        # Strip hop-by-hop / length headers; httpx sets its own. The router's
        # own credentials (X-Ava-Router-Token, or a Bearer that IS the router
        # token) must never leak upstream; any other Authorization passes
        # through (a caller may be forwarding its own provider key).
        auth_hdr = request.headers.get("authorization", "")
        auth_is_router_cred = bool(
            router_token and auth_hdr.lower().startswith("bearer ")
            and hmac.compare_digest(auth_hdr[7:].strip(), router_token))
        drop = {"host", "content-length", "connection", "accept-encoding",
                "x-ava-router-token"}
        if auth_is_router_cred:
            drop.add("authorization")
        fwd_headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in drop}

        is_comp = path.startswith("chat/completions") or path.startswith("completions")
        req_params = _extract_params(raw, is_json) if is_comp else {}
        t_start = time.time()

        body_obj = None
        if is_json and raw:
            try:
                body_obj = json.loads(raw)
            except Exception:  # noqa: BLE001
                body_obj = None
        workload = _workload_hint(request.headers, body_obj) if is_comp else None
        wants_tools = bool(isinstance(body_obj, dict) and body_obj.get("tools"))

        last_err = None
        ordered = state.ordered_backends(workload, wants_tools=wants_tools)
        for backend in ordered:
            url = f"{backend['url']}/{path}"
            body = _rewrite_body(raw, is_json, backend, reasoning, is_comp)
            # Per-backend headers: inject a Bearer token for backends that
            # declared an api_key_env (local vLLM / Ollama need none).
            b_headers = dict(fwd_headers)
            if backend.get("api_key"):
                b_headers["authorization"] = f"Bearer {backend['api_key']}"
            try:
                req = client.build_request(
                    request.method, url, headers=b_headers, content=body,
                    params=request.query_params)
                resp = await client.send(req, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                    httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
                last_err = f"{backend['id']}: {type(e).__name__}"
                continue

            # Backend reachable but not able to serve -> try the next brain.
            if resp.status_code in _FAILOVER_STATUS and backend is not ordered[-1]:
                await resp.aclose()
                last_err = f"{backend['id']}: HTTP {resp.status_code}"
                continue

            # Commit to this backend: record it and relay.
            if is_comp:
                state.last.update(id=backend["id"], model=backend["model"],
                                  label=backend["label"], ts=time.time())

            resp_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in ("content-length", "content-encoding",
                                     "transfer-encoding", "connection")
            }
            resp_headers["x-ava-backend"] = backend["id"]

            failover = backend is not ordered[0]
            status = resp.status_code

            async def _stream(r=resp, cap=is_comp, be=backend, fo=failover,
                              st=status):
                tail = b""
                t_first = None
                nchunks = 0
                try:
                    async for chunk in r.aiter_raw():
                        if t_first is None:
                            t_first = time.time()
                        if cap:
                            tail = (tail + chunk)[-8192:]
                            nchunks += 1
                        yield chunk
                finally:
                    await r.aclose()
                    if cap:
                        state.log_completion(be, req_params, fo, path, st,
                                             t_start, t_first, time.time(),
                                             tail, nchunks)

            return StreamingResponse(
                _stream(), status_code=resp.status_code,
                headers=resp_headers,
                media_type=resp.headers.get("content-type"))

        return JSONResponse(
            {"error": {"message": f"all inference backends unavailable ({last_err})",
                       "type": "router_unavailable"}},
            status_code=503)

    return app
