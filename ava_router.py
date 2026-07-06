"""Ava inference router.

A tiny OpenAI-compatible reverse proxy in front of Ava's single always-on local
brain — Nemotron open-model 30B-A3B (FP8), served by vLLM at :8002. Ava keeps
ONE model loaded at all times (no scaling models up/down): the Omni model is her
everyday chat brain and backs her background multimodal capabilities.

The OpenClaw inference route (`inference.local` -> OPENAI_BASE_URL) points here
instead of straight at vLLM, so the router still owns:
  • per-completion token/throughput perf logging,
  • the hardware-fit `/fit` view (workload hint + live memory), and
  • optional failover: if you declare a second backend in ava.yaml
    `inference.backends` (e.g. a cloud model) it becomes an automatic fallback
    when the local engine is down / still loading (5xx/404/connection error).

With a single backend there is nothing to fail over TO — the point of the Omni
model is that it is always resident. `GET /which` still reports which backend
served the most recent completion for the chat "which model" pill.

Run (see ava-router.service):
    uvicorn ava_router:app --host 0.0.0.0 --port 8010
"""
import hmac
import json
import os
import re
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

try:  # perf_log lives at the repo root next to this file; best-effort only.
    import perf_log
except Exception:  # noqa: BLE001 — router must run even if logging is missing
    perf_log = None

try:  # hardware-aware fit/selection; router must run even if it's missing.
    from ava_bridge import model_fit
except Exception:  # noqa: BLE001
    model_fit = None

# --- backend: config-declared (ava.yaml `inference.backends`) with an env
# fallback so a fresh install serves Ava's always-on model brain out of the box.
# Nemotron open-model 30B-A3B (FP8) is a multimodal MoE (3B active) — small
# enough to stay resident on the GB10 unified pool, capable enough to be Ava's
# only conversational + background model.
OMNI_URL = os.environ.get("AVA_OMNI_URL", "http://127.0.0.1:8002/v1").rstrip("/")
OMNI_MODEL = os.environ.get(
    "AVA_OMNI_MODEL", "nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8")
OMNI_LABEL = os.environ.get("AVA_OMNI_LABEL", "open-model 30B")

# Statuses that mean "this backend can't serve right now, try the next one".
# vLLM returns these fast (503 = engine dead / not ready, 404 = model not loaded).
_FAILOVER_STATUS = {404, 500, 502, 503, 504}

# Ava's single always-on brain. It handles every workload (it's the only model),
# so it is never "off-workload". Declare extra backends (e.g. a cloud fallback)
# in ava.yaml `inference.backends` to add automatic failover.
_LEGACY_BACKENDS = [
    {"id": "omni", "url": OMNI_URL, "model": OMNI_MODEL, "label": OMNI_LABEL,
     "engine": "vllm", "api_key": None,
     "fit": {"weight_gb": 35, "tier": "large", "min_free_gb": 10,
             "workloads": ["chat", "reasoning", "code", "vision", "audio",
                           "fast", "cheap", "utility", "classify"]}},
]


def _load_backends():
    """Inference backends from ava.yaml `inference.backends`, ordered
    primary -> fallback -> the rest; falls back to the single always-on model
    backend when no config is present (so a fresh install just works).

    Each backend is ANY OpenAI-compatible endpoint — vLLM, Ollama
    (:11434/v1), llama.cpp/llamafile (:8080/v1), OpenAI, OpenRouter, Together, …
    `api_key_env` names an env var whose value is sent as a Bearer token, so
    cloud backends work for users without a local GPU.
    """
    try:
        from ava_bridge import settings
        cfg = settings.get("inference.backends")
        primary = settings.get("inference.primary")
        fallback = settings.get("inference.fallback")
    except Exception:  # noqa: BLE001 — never let config break the router
        cfg = primary = fallback = None
    if not isinstance(cfg, dict) or not cfg:
        return list(_LEGACY_BACKENDS)
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
            # Optional hardware-fit profile (weight_gb / tier / workloads /
            # min_free_gb); model_fit.py supplies permissive defaults if absent.
            "fit": b.get("fit") or None,
        })
    return out or list(_LEGACY_BACKENDS)


BACKENDS = _load_backends()

app = FastAPI(title="ava-router")

# --- control-endpoint auth ---------------------------------------------------
# /which and /route reveal or change Ava's active brain, so they require a shared
# token (the bridge sends it as X-Ava-Router-Token). The OpenAI-compatible
# inference proxy (/v1/*) and /healthz stay open: the sandbox agent and health
# probes reach them without the bridge's secret.
_ROUTER_TOKEN = os.environ.get("AVA_ROUTER_TOKEN")
if not _ROUTER_TOKEN:
    try:
        from ava_bridge import config as _bridge_config
        _ROUTER_TOKEN = _bridge_config.INTERNAL_TOKEN
    except Exception:  # noqa: BLE001 — router must still boot without the bridge pkg
        _ROUTER_TOKEN = None

_GUARDED_PATHS = ("/which", "/route", "/fit")


@app.middleware("http")
async def _guard_control_endpoints(request: Request, call_next):
    if request.url.path in _GUARDED_PATHS:
        tok = request.headers.get("x-ava-router-token", "")
        if not (_ROUTER_TOKEN and hmac.compare_digest(tok, _ROUTER_TOKEN)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)

# Generation can legitimately take a while (cold prefix cache ~20s, long replies),
# so the read timeout is generous; connect is short so a dead backend fails fast.
_TIMEOUT = httpx.Timeout(connect=3.0, read=600.0, write=30.0, pool=5.0)
_client = httpx.AsyncClient(timeout=_TIMEOUT)

# Which backend served the most recent completion (single-user app, so a simple
# last-write wins record is enough for the UI pill).
_last = {"id": None, "model": None, "label": None, "ts": 0.0}

# Real token usage from the most recent completion (vLLM returns `usage` in the
# response; prompt_tokens = how many tokens went INTO the model = current context
# size). Scraped from the response bytes so the chat can show a real counter.
_last_usage = {"prompt_tokens": None, "total_tokens": None, "ts": 0.0}
_PROMPT_RE = re.compile(rb'"prompt_tokens"\s*:\s*(\d+)')
_TOTAL_RE = re.compile(rb'"total_tokens"\s*:\s*(\d+)')
_COMPLETION_RE = re.compile(rb'"completion_tokens"\s*:\s*(\d+)')

# Throughput of the most recent completion (for the UI speed pill + /which).
_last_perf = {"tokens_per_sec": None, "ttft_ms": None, "gen_seconds": None}

# Which backend the user picked as PRIMARY (the other stays as a safety fallback).
# Defaults to the first backend — the always-on model brain. Only meaningful if
# you declare extra backends (e.g. a cloud fallback). Set live via POST /route.
_mode = os.environ.get("AVA_ROUTER_MODE") or (BACKENDS[0]["id"] if BACKENDS else "omni")


# Where a caller can name the kind of work so the router sends it to the
# right-sized brain: an `X-Ava-Workload: code` header, or a `workload`/`ava_workload`
# field in the JSON body. Unset -> plain primary-first ordering (unchanged).
_WORKLOAD_HEADER = "x-ava-workload"


def _workload_hint(headers, body_obj) -> str | None:
    wl = headers.get(_WORKLOAD_HEADER)
    if not wl and isinstance(body_obj, dict):
        wl = body_obj.get("workload") or body_obj.get("ava_workload")
    wl = (wl or "").strip().lower()
    return wl or None


def _ordered_backends(workload: str | None = None) -> list:
    """Backends in try-order.

    With a hardware-fit layer available, order by fit + workload match + tier so
    work lands on the right-sized model and sheds off the big brain when the
    unified-memory pool is under pressure (see model_fit.select). The user-picked
    primary (`_mode`) breaks remaining ties. Falls back to plain primary-first
    ordering if model_fit is unavailable, so the router is never worse off.
    """
    if model_fit is not None:
        try:
            cands = model_fit.select(BACKENDS, workload=workload, primary_id=_mode)
            if cands:
                _last_fit["order"] = [
                    {"id": c.backend["id"], "fits": c.fits, "handles": c.handles,
                     "reason": c.reason} for c in cands]
                _last_fit["workload"] = workload
                _last_fit["ts"] = time.time()
                return [c.backend for c in cands]
        except Exception:  # noqa: BLE001 — fit must never break serving
            pass
    primary = [b for b in BACKENDS if b["id"] == _mode]
    rest = [b for b in BACKENDS if b["id"] != _mode]
    return (primary + rest) if primary else BACKENDS


# Why the router ordered the last request the way it did (for /fit + debugging).
_last_fit = {"order": None, "workload": None, "ts": 0.0}


def _record_usage(tail: bytes) -> tuple:
    """Pull prompt/completion/total token counts out of a completion's response.

    Returns ``(prompt_tokens, completion_tokens, total_tokens)`` (any may be None)
    and also updates the `_last_usage` record the UI counter reads.
    """
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
            _last_usage.update(prompt_tokens=prompt, total_tokens=total,
                               ts=time.time())
    except Exception:  # noqa: BLE001
        pass
    return prompt, completion, total


# Sampling / decoding parameters we surface in the perf log. These are the knobs
# that actually move throughput and output, so they're captured verbatim.
_PARAM_KEYS = (
    "temperature", "top_p", "top_k", "min_p", "max_tokens",
    "max_completion_tokens", "presence_penalty", "frequency_penalty",
    "repetition_penalty", "stream", "n", "seed", "stop",
)


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


def _log_completion(backend: dict, params: dict, failover: bool, endpoint: str,
                    status: int, t_start: float, t_first: float | None,
                    t_end: float, tail: bytes, nchunks: int) -> None:
    """Record one served LLM completion to the unified performance log."""
    prompt_tok, completion_tok, total_tok = _record_usage(tail)
    gen_seconds = (t_end - t_first) if t_first else None
    ttft = (t_first - t_start) if t_first else None
    tps = perf_log.tok_per_sec(completion_tok, gen_seconds) if perf_log else None
    _last_perf.update(
        tokens_per_sec=tps,
        ttft_ms=round(ttft * 1000, 1) if ttft else None,
        gen_seconds=round(gen_seconds, 3) if gen_seconds else None)
    if perf_log is None:
        return
    perf_log.log_perf(
        "llm",
        serving="vllm",
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
        ttft_ms=_last_perf["ttft_ms"],
        gen_seconds=_last_perf["gen_seconds"],
        total_seconds=round(t_end - t_start, 3),
        stream_chunks=nchunks or None,
        params={k: v for k, v in params.items()
                if k != "model" and v is not None},
    )


@app.on_event("shutdown")
async def _shutdown():
    await _client.aclose()


@app.get("/which")
async def which():
    """The backend that served the most recent completion, plus its real token use."""
    return {**_last,
            "prompt_tokens": _last_usage["prompt_tokens"],
            "total_tokens": _last_usage["total_tokens"],
            "tokens_per_sec": _last_perf["tokens_per_sec"],
            "ttft_ms": _last_perf["ttft_ms"]}


@app.get("/route")
async def get_route():
    """Current model choice + the selectable backends (for the chat dropdown)."""
    return {"mode": _mode,
            "backends": [{"id": b["id"], "label": b["label"], "model": b["model"]}
                         for b in BACKENDS]}


@app.post("/route")
async def set_route(request: Request):
    """Pick which backend the router prefers as primary (a backend id, e.g.
    'omni', or a declared cloud-fallback id)."""
    global _mode
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    m = str((body or {}).get("mode", "")).strip()
    if m in {b["id"] for b in BACKENDS}:
        _mode = m
        return {"ok": True, "mode": _mode}
    return JSONResponse({"error": f"unknown mode {m!r}"}, status_code=400)


@app.get("/fit")
async def fit():
    """Live hardware-fit snapshot: free memory, each backend's fit/workloads, and
    how the router ordered the most recent request. Read-only; decides nothing."""
    if model_fit is None:
        return {"enabled": False, "reason": "model_fit unavailable"}
    report = model_fit.fit_report(BACKENDS)
    return {"enabled": True, "mode": _mode, "last": _last_fit, **report}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "backends": [b["id"] for b in BACKENDS]}


def _rewrite_body(raw: bytes, is_json: bool, backend: dict) -> bytes:
    """Force the request's model id to match the backend we're forwarding to."""
    if not is_json or not raw:
        return raw
    try:
        obj = json.loads(raw)
    except Exception:  # noqa: BLE001 — not JSON we understand; pass through
        return raw
    # Drop Ava-private routing hints so the upstream OpenAI API never sees them.
    hint = obj.pop("workload", None) if isinstance(obj, dict) else None
    hint = obj.pop("ava_workload", None) or hint if isinstance(obj, dict) else hint
    if isinstance(obj, dict) and ("model" in obj or hint is not None):
        if "model" in obj:
            obj["model"] = backend["model"]
        return json.dumps(obj).encode("utf-8")
    return raw


@app.api_route("/v1/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    raw = await request.body()
    ctype = request.headers.get("content-type", "")
    is_json = "application/json" in ctype.lower()

    # Strip hop-by-hop / length headers; httpx sets its own.
    fwd_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "connection",
                             "accept-encoding")
    }

    # Capture the tunable request parameters once (same for every backend try),
    # and the wall clock so we can measure time-to-first-token + tokens/sec.
    is_comp = path.startswith("chat/completions") or path.startswith("completions")
    req_params = _extract_params(raw, is_json) if is_comp else {}
    t_start = time.time()

    # Send this request to the right-sized brain: honour an X-Ava-Workload header
    # or a workload field in the body, then order backends by fit + workload.
    body_obj = None
    if is_json and raw:
        try:
            body_obj = json.loads(raw)
        except Exception:  # noqa: BLE001
            body_obj = None
    workload = _workload_hint(request.headers, body_obj) if is_comp else None

    last_err = None
    ordered = _ordered_backends(workload)
    for backend in ordered:
        url = f"{backend['url']}/{path}"
        body = _rewrite_body(raw, is_json, backend)
        # Per-backend headers: inject a Bearer token for cloud backends that
        # declared an api_key_env (local vLLM / Ollama need none).
        b_headers = dict(fwd_headers)
        if backend.get("api_key"):
            b_headers["authorization"] = f"Bearer {backend['api_key']}"
        try:
            req = _client.build_request(
                request.method, url, headers=b_headers, content=body,
                params=request.query_params)
            resp = await _client.send(req, stream=True)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.RemoteProtocolError, httpx.PoolTimeout) as e:
            last_err = f"{backend['id']}: {type(e).__name__}"
            continue

        # Backend reachable but not able to serve -> try the next brain.
        if resp.status_code in _FAILOVER_STATUS and backend is not ordered[-1]:
            await resp.aclose()
            last_err = f"{backend['id']}: HTTP {resp.status_code}"
            continue

        # Commit to this backend: record it (for /chat/completions) and relay.
        if is_comp:
            _last.update(id=backend["id"], model=backend["model"],
                         label=backend["label"], ts=time.time())

        resp_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in ("content-length", "content-encoding",
                                 "transfer-encoding", "connection")
        }
        resp_headers["x-ava-backend"] = backend["id"]

        failover = backend is not ordered[0]
        status = resp.status_code

        async def _stream(r=resp, cap=is_comp, be=backend, fo=failover, st=status):
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
                    _log_completion(be, req_params, fo, path, st,
                                    t_start, t_first, time.time(), tail, nchunks)

        return StreamingResponse(
            _stream(), status_code=resp.status_code,
            headers=resp_headers,
            media_type=resp.headers.get("content-type"))

    return JSONResponse(
        {"error": {"message": f"all inference backends unavailable ({last_err})",
                   "type": "router_unavailable"}},
        status_code=503)
