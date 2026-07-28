"""Setup -> Models panel: the model store, benchmarks, and inference backends.

Covers what the owner does to choose a brain — list and pull models, benchmark
them, and add / test / activate / delete the OpenAI-compatible backends the
inference router fans out to.

The two long-running jobs (pull, bench) keep their module-level state and locks
here rather than in a shared store: each is a single-slot job with a bounded log
deque, polled by its own /status route, and nothing outside this panel reads
them. Verified before the move — all ten helpers this panel uses are referenced
by /models handlers and by nothing else in the hub.
"""
import os
import re
import subprocess
import sys
import threading
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import settings
import time as _t
from .. import bench
from .. import models as model_store

router = APIRouter()

# --------------------------------------------------------------------------- #
# Models — manifest listing + background pull with live log (wraps the CLI so
# the download logic stays in ONE place: `ava models pull`)
# --------------------------------------------------------------------------- #
_pull_job: dict = {"status": "idle", "role": None, "log": deque(maxlen=200),
                   "rc": None}

_pull_lock = threading.Lock()

@router.get("/models")
def models_list():
    # `from .. import models` — no sys.path injection and no importing the CLI
    # script from inside the package. The helpers this route needs moved to
    # ava_bridge/models.py, so `ava models` and Setup → Models are now two
    # callers of one public API instead of two callers of one private one.
    manifest = model_store.manifest()
    dirs = model_store.dirs()
    tier, avail = model_store.detected_tier()
    roles = []
    for role, spec in manifest.items():
        roles.append({"role": role, "id": spec.get("id"),
                      "engine": spec.get("engine"), "tier": spec.get("tier"),
                      "present": model_store.present(spec, dirs)})
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

