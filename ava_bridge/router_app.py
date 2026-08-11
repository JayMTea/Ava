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

# --- no built-in model -------------------------------------------------------
# Ava ships NO default backend. There was one — a hardcoded `omni` pointed at
# http://127.0.0.1:8002/v1 serving Qwen/Qwen2.5-7B-Instruct — synthesized on any
# install with no `inference` block and no AVA_BACKEND_URL.
#
# It was there so a fresh install "worked", and the cost was that Ava asserted a
# brain nobody had chosen. On a box where nothing had ever listened on 8002 the
# hardware monitor still listed it, offline, beside real models — a phantom the
# owner could not remove because it was not in any config to remove it from. A
# model Ava invents cannot be a model the owner picked, and the panel had no way
# to say so.
#
# Nothing replaces it. `load_backends()` returns [] and every surface says the
# brain is unset, which is true and actionable, where the phantom was neither.
# An explicitly-declared env backend (AVA_BACKEND_URL) is still honoured — that
# IS configuration, just not via ava.yaml.

# Statuses that mean "this backend can't serve right now, try the next one".
_FAILOVER_STATUS = {404, 500, 502, 503, 504}

# Engines whose OpenAI-compatible streaming endpoint accepts
# `stream_options: {include_usage: true}` (needed so usage/token counts appear in
# streamed responses — Ollama omits usage otherwise).
#
# Derived from ava_bridge/engines.py rather than restated, because this set used to
# carry the *reason* for each exclusion in a prose comment that nobody could test:
# "llama.cpp builds may reject unknown params" was a plausible guess presented as
# settled. Each engine now records its decision AND whether anyone verified it, and
# tests/test_engine_parity.py fails an entry that has neither.
from . import engines as _engines  # noqa: E402 — local import, cycle-free

_STREAM_USAGE_ENGINES = _engines.usage_engines()


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
    # .strip() like every sibling read on this function. A trailing \r from a
    # .env written on Windows survives into the OpenAI `model` field otherwise,
    # and the engine answers `model 'llama3.2\r' not found` — a missing-model
    # error for a model that is right there.
    model = os.environ.get("AVA_BACKEND_MODEL", "").strip()
    return {"id": "backend", "url": base, "model": model,
            "label": os.environ.get("AVA_BACKEND_LABEL", model or engine),
            "engine": engine,
            "api_key": os.environ.get("AVA_INFERENCE_KEY") or None,
            "tools": os.environ.get("AVA_BACKEND_TOOLS", "native"),
            "fit": None}


def load_backends() -> list:
    """Inference backends from ava.yaml `inference.backends`, ordered
    primary -> fallback -> the rest; else a single env-declared backend
    (AVA_BACKEND_URL); else NOTHING.

    An empty list is a real, expected answer: a fresh install has no brain until
    the owner connects one. Callers must handle it rather than assume a model —
    see the `model_unknown` code in `proxy()`, which is what turns "nothing is
    configured" into a link to Setup instead of a failure.

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
        # No ava.yaml backends. An AVA_BACKEND_URL is still configuration — the
        # container-friendly way to declare one — so it is honoured, STAMPED
        # implicit so a UI can say where it came from. Nothing else is invented.
        env_b = _env_backend()
        return [dict(env_b, implicit=True)] if env_b else []
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
        if not key:
            # Fall back to a per-backend key from the secrets store (secrets/<id>.key),
            # written by the Hub's model manager — so a UI-linked cloud model
            # authenticates without the user hand-wiring an env var.
            try:
                key = settings.backend_key(name)
            except Exception:  # noqa: BLE001
                key = None
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
    # An `inference.backends` block that declared only unusable entries (every
    # one missing a base_url) is the same situation as declaring none.
    env_b = _env_backend()
    return [dict(env_b, implicit=True)] if env_b else []


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
        # No backends means no mode. This used to fall back to the literal
        # "omni" — the id of the built-in default — so with nothing configured
        # the router advertised a mode naming a backend that could not exist,
        # and the chat header selected it.
        self.mode = (mode or os.environ.get("AVA_ROUTER_MODE")
                     or (backends[0]["id"] if backends else ""))
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
        # An EXPLICIT user pick (via POST /route → self.mode) is a hard
        # preference: pin it to the front so a manual model swap actually
        # takes effect, instead of being just the last tiebreaker inside
        # model_fit's workload/tier ranking (where a chat workload could keep
        # serving the large model despite the user choosing the small one).
        if self.mode:
            picked = [b for b in cands if b["id"] == self.mode]
            if picked:
                cands = picked + [b for b in cands if b["id"] != self.mode]
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
    # Force the model id to the backend we are forwarding to — including when
    # the caller sent NO model at all.
    #
    # This used to be `if "model" in obj`, which made the "force" only an
    # overwrite: a request that omitted the field was forwarded verbatim and the
    # engine answered 400 "model is required". That is every tool-less chat turn
    # on a fresh install, because DirectRuntime posts {messages, stream} and
    # nothing else — so a forker with no agent sandbox (i.e. anyone who has not
    # run `ava agent provision`) got a 400 on their first message, on the cpu
    # profile and on any other. The router chooses the backend, so the router
    # owns the model; a caller-supplied value is overridden anyway.
    #
    # Only for completions. Other proxied routes (/v1/models and friends) take
    # no model, and inventing one for them would be a different bug.
    #
    # An empty backend model means "this backend serves whatever is asked",
    # so leave the caller's value alone rather than blanking it — the old code
    # would rewrite a valid model to "" for any yaml backend declared without a
    # `model:` key, which fails upstream for the same reason.
    want = backend.get("model") or ""
    if want and (is_comp or "model" in obj) and obj.get("model") != want:
        obj["model"] = want
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
        #
        # /lease/* is guarded by PREFIX, not exact match, and unconditionally: unlike
        # /fit it can stop and start models, so an unauthenticated caller could take
        # Ava's brain down. Prefix matching matters because the sub-paths carry a
        # lease id (/lease/<id>/heartbeat), which an exact-match list cannot express.
        if (path in _GUARDED_PATHS or path == "/lease"
                or path.startswith("/lease/")) and not _authed(request):
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
        """Current model choice + the selectable backends (chat dropdown).
        `implicit` marks the env/built-in fallback served when nothing is
        configured — UIs label it "default" rather than user-chosen."""
        return {"mode": state.mode,
                "backends": [{"id": b["id"], "label": b["label"],
                              "model": b["model"],
                              "implicit": bool(b.get("implicit"))}
                             for b in state.backends]}

    @app.post("/route")
    async def set_route(request: Request):
        """Pick which backend the router prefers as primary. The pick lives in
        router memory only (`persisted: false`) — it reverts on restart; a
        durable brain choice is the Hub model manager's job."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        m = str((body or {}).get("mode", "")).strip()
        if m in {b["id"] for b in state.backends}:
            state.mode = m
            return {"ok": True, "mode": state.mode, "persisted": False}
        return JSONResponse({"error": f"unknown mode {m!r}"}, status_code=400)

    @app.get("/fit")
    async def fit():
        """Live hardware-fit snapshot. Read-only; decides nothing."""
        if model_fit is None:
            return {"enabled": False, "reason": "model_fit unavailable"}
        report = model_fit.fit_report(state.backends)
        return {"enabled": True, "mode": state.mode, "last": state.last_fit,
                **report}

    # --- allocation leases -------------------------------------------------- #
    # Mounted here rather than as a separate service because router_host already
    # solves "exactly one instance owns this": it probes /healthz and stands down to
    # a standalone unit if one is running. So this inherits a single owner, an auth
    # story, and a port, instead of adding another of each.
    #
    # Every handler hands the blocking work to a worker thread. Acquiring takes locks
    # and writes files, and awaiting that on the event loop would stall inference for
    # every other caller.
    def _alloc():
        from .alloc import broker
        return broker

    @app.post("/lease")
    async def lease_acquire(request: Request):
        """Acquire a lease for a model on behalf of another process.

        Send `"wait": false` and this answers immediately, with `state: "pending"` if
        a release is still running; poll `GET /lease/{id}` until `ready` is true. That
        is the shape a networked client wants, because the alternative is holding a
        request open across a container stop and letting the client's socket timeout
        decide policy: give up early and it concludes nothing is coordinating, starts
        coordinating for itself, and two components are now acting on one container.
        The default stays `true` so a client written against the older contract is
        never told it may start before the memory is actually free.

        A remote holder cannot prove liveness with a file lock the way a local one
        does (the lock would belong to this process), so the lease carries a deadline
        and must be renewed — see POST /lease/{id}/heartbeat.
        """
        from starlette.concurrency import run_in_threadpool
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — a malformed body is a client error
            body = {}
        model = str(body.get("model") or "").strip()
        if not model:
            return JSONResponse({"error": "model is required"}, status_code=400)
        try:
            out = await run_in_threadpool(
                _alloc().acquire_api, model,
                reason=str(body.get("reason") or ""),
                need_gib=body.get("need_gib"),
                priority=body.get("priority"),
                ttl_s=body.get("ttl_s"),
                holder=str(body.get("holder") or ""),
                wait=body.get("wait", True) is not False)
            return out
        except Exception as e:  # noqa: BLE001 — a client must degrade, not fail
            return {"lease_id": "", "granted": True, "ready": True,
                    "state": "active",
                    "reason": f"allocator unavailable, proceed: {e}",
                    "released": [], "enforced": False}

    @app.post("/lease/{lease_id}/heartbeat")
    async def lease_heartbeat(lease_id: str, request: Request):
        from starlette.concurrency import run_in_threadpool
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        return await run_in_threadpool(_alloc().heartbeat_api, lease_id,
                                       ttl_s=body.get("ttl_s"))

    @app.delete("/lease/{lease_id}")
    async def lease_release(lease_id: str):
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_alloc().release_api, lease_id)

    @app.get("/lease")
    async def lease_status():
        """Read-only allocation snapshot. Decides nothing."""
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_alloc().snapshot)

    @app.get("/lease/{lease_id}")
    async def lease_state(lease_id: str):
        """Where a deferred acquire has got to. Read-only; actuates nothing.

        `state` is `pending` while a release runs, then `active`; `gone` if the lease no
        longer exists, which a poller must treat as terminal rather than retryable.
        """
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(_alloc().state_api, lease_id)

    @app.post("/lease/restore")
    async def lease_restore():
        """Bring back everything the allocator released that nothing is using."""
        from starlette.concurrency import run_in_threadpool
        return {"restored": await run_in_threadpool(_alloc().restore_now)}

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

        # A completion needs a model from SOMEBODY. _rewrite_body forces the
        # backend's id onto the request, but only `if want` — an empty backend
        # model deliberately means "serves whatever is asked", which is right
        # when the CALLER supplied one and leaves a hole when neither did.
        #
        # DirectRuntime posts {messages, stream} and nothing else, so on a fresh
        # install whose backend carries no model the outbound body has no model
        # at all and the engine answers `model is required`. That is the first
        # message a new owner ever sends, and the error is the engine's words for
        # our own misconfiguration: it names nothing they can act on and gets no
        # fix-it link, because the string matches no known code.
        #
        # So when the caller named no model, drop the backends that cannot supply
        # one and fail with a code if that leaves nothing. Filtering rather than
        # checking `any()` matters: a modelless backend ordered FIRST would
        # otherwise still be tried, and failover would never reach the one that
        # works. If the caller DID name a model, every backend can serve it and
        # nothing is filtered.
        if is_comp and not (isinstance(body_obj, dict)
                            and str(body_obj.get("model") or "").strip()):
            servable = [b for b in ordered if str(b.get("model") or "").strip()]
            if not servable:
                return JSONResponse(
                    {"error": "No model is configured, so there is nothing to "
                              "answer with. Choose one in Setup -> Agent.",
                     "error_code": "model_unknown"}, status_code=400)
            ordered = servable

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

        # Two different failures used to share this one line. `last_err` is None
        # when the loop above never ran — there were no backends to try — and
        # the owner was shown "all inference backends unavailable (None)": a
        # report that everything is down, on an install where nothing was ever
        # set up, with no code for the UI to offer a fix from.
        if not ordered:
            return JSONResponse(
                {"error": "No model is configured, so there is nothing to "
                          "answer with. Choose one in Setup -> Agent.",
                 "error_code": "model_unknown"}, status_code=400)
        return JSONResponse(
            {"error": {"message": f"all inference backends unavailable ({last_err})",
                       "type": "router_unavailable"},
             "error_code": "inference_down"},
            status_code=503)

    return app
