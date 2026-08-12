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
from .. import audit
from .. import bench
from .. import model_fit
from .. import models as model_store

router = APIRouter()

# --------------------------------------------------------------------------- #
# Models — manifest listing + background pull with live log (wraps the CLI so
# the download logic stays in ONE place: `ava models pull`)
# --------------------------------------------------------------------------- #
_pull_job: dict = {"status": "idle", "role": None, "log": deque(maxlen=200),
                   "rc": None}

_pull_lock = threading.Lock()


def _pool_kind() -> str | None:
    """'vram' | 'unified' | 'system' | None — which pool a model would land in.

    The fit verdict needs it because spilling only exists on a discrete
    accelerator: on a unified pool there is nowhere to spill TO, so
    `model_fit.assess` must never describe one there. Read from the HAL rather
    than inferred, and never fatal — an unreadable pool yields `unknown`, which
    the UI renders as silence.
    """
    try:
        from .. import hwinfo
        return hwinfo.fit_pool().kind
    except Exception:  # noqa: BLE001 — advisory only; never break the model list
        return None


@router.get("/models")
def models_list():
    # `from .. import models` — no sys.path injection and no importing the CLI
    # script from inside the package. The helpers this route needs moved to
    # ava_bridge/models.py, so `ava models` and Setup → Models are now two
    # callers of one public API instead of two callers of one private one.
    manifest = model_store.manifest()
    dirs = model_store.dirs()
    tier, avail = model_store.detected_tier()
    pool_kind = _pool_kind()
    roles = []
    for role, spec in manifest.items():
        here = model_store.present(spec, dirs)
        # MEASURED first, estimated only as a fallback. Once the weights are on
        # disk their size is a fact; before that, all we have is what the name
        # says, and `size_from_id` returns None rather than guess when the name
        # says nothing — which the UI renders as silence.
        need = model_store.disk_size_gb(spec, dirs) if here else None
        if need is None:
            need = model_fit.size_from_id(spec.get("id"), spec.get("engine"))
        roles.append({"role": role, "id": spec.get("id"),
                      "engine": spec.get("engine"), "tier": spec.get("tier"),
                      "present": here,
                      "fit": model_fit.assess(need, avail, pool_kind)})
    return {"roles": roles, "detected_tier": tier,
            "available_gb": round(avail, 0) if avail else None,
            "store": dirs["root"]}

def model_in_use(engine: str, model_id: str) -> list[str]:
    """Why this model must not be deleted yet, in the owner's terms. [] = free.

    Deleting weights an engine is serving does not fail loudly: the process
    keeps running on the file handles it already holds, and the failure arrives
    on the next restart, hours later, with nothing connecting it to the deletion.
    So the check happens BEFORE, and names what is holding it.
    """
    reasons = []
    inf = settings.current_config().get("inference") or {}
    backends = inf.get("backends") or {}
    brain = (inf.get("roles") or {}).get("chat") or inf.get("primary")
    for bid, b in backends.items():
        if str(b.get("model") or "").strip() != model_id:
            continue
        reasons.append(f"Ava's brain is served from it (backend '{bid}')"
                       if bid == brain else f"the backend '{bid}' serves it")
    for role, spec in model_store.manifest().items():
        if (spec.get("id") == model_id
                and str(spec.get("engine") or "") == engine):
            reasons.append(f"ava.yaml declares it as the '{role}' model")
    return reasons


@router.get("/models/store")
def models_store():
    """Every model ON DISK, with what is holding it and what it costs.

    A different question from GET /models, which lists what ava.yaml declares.
    A model pulled outside Ava, or pulled and since undeclared, is real and
    occupies real space — and was invisible to every surface, so it could
    neither be selected nor reclaimed.
    """
    rows = []
    total = 0.0
    for m in model_store.installed_models():
        held = model_in_use(m["engine"], m["id"])
        rows.append({**m, "in_use": bool(held), "held_by": held})
        total += m.get("size_gb") or 0.0
    return {"models": rows, "total_gb": round(total, 2),
            "store": model_store.dirs()["root"]}


def _local_serve_driver(model_id: str, weight_gb=None):
    """The driver that can start/stop a vLLM this box owns."""
    from ..alloc import capacity, drivers
    from ..alloc.base import DriverContext
    ctx = DriverContext(model_id=model_id, weight_gb=weight_gb,
                        config={"port": int(os.environ.get("AVA_SERVE_PORT") or 8002)},
                        free_gib=lambda: capacity.free_gib(),
                        wait_free=lambda target, **kw: capacity.wait_free(target, **kw),
                        log=lambda m: _swap_log(m))
    return drivers.for_spec("local-serve", ctx)


_swap_job: dict = {"status": "idle", "model": None, "log": deque(maxlen=200),
                   "error": None}
_swap_lock = threading.Lock()


def _swap_log(msg: str) -> None:
    with _swap_lock:
        _swap_job["log"].append(str(msg)[:400])


def _run_swap(model_id: str, engine: str, weight_gb) -> None:
    """Stop whatever is serving, start the new model, then repoint config.

    ORDER MATTERS and this order is the safe one. Config is written LAST, only
    after the engine reports it is holding the model, so a failed start leaves
    ava.yaml pointing at the brain that still works. Repointing first and then
    starting would, on any failure, leave Ava configured for a model nothing
    serves — an install that is broken until someone edits YAML by hand.
    """
    try:
        drv = _local_serve_driver(model_id, weight_gb)
        if not drv.usable():
            raise RuntimeError(drv.unusable_reason())

        _swap_log("stopping the current engine")
        rel = drv.release("stop", timeout=120)
        if rel.acted and not rel.ok:
            raise RuntimeError(f"could not stop the running engine: {rel.detail}")

        _swap_log(f"starting {model_id}")
        got = drv.acquire(timeout=900)
        if not got.ok:
            raise RuntimeError(got.detail or "the engine did not come up")

        port = int(os.environ.get("AVA_SERVE_PORT") or 8002)
        cfg = settings.current_config()
        inf = cfg.setdefault("inference", {})
        backends = inf.setdefault("backends", {})
        bid = "local"
        backends[bid] = {**(backends.get(bid) or {}), "engine": "vllm",
                         "base_url": f"http://127.0.0.1:{port}/v1", "model": model_id}
        inf["primary"] = bid
        inf.setdefault("roles", {})["chat"] = bid
        settings.save_config(cfg)
        _swap_log("Ava's brain now points at it")
        audit.record("brain_swap", model=model_id, engine=engine, backend=bid)
        with _swap_lock:
            _swap_job.update(status="done", error=None)
    except Exception as e:  # noqa: BLE001 — the job reports; it must not raise here
        _swap_log(f"failed: {e}")
        with _swap_lock:
            _swap_job.update(status="error", error=str(e)[:300])


@router.post("/models/store/brain")
def models_store_brain(payload: dict):
    """Load a store model into Ava's brain: stop, start, health-gate, repoint.

    Runs in the background — a vLLM boot is minutes, not a request — and the
    caller polls /models/store/brain/status for the live log.
    """
    model_id = str(payload.get("id") or "").strip()
    engine = str(payload.get("engine") or "vllm").strip().lower()
    if not model_id:
        return JSONResponse({"ok": False, "error": "id is required"}, status_code=400)
    if engine != "vllm":
        # Ollama loads on demand, so a swap there is a repoint with nothing to
        # start. Saying so is better than pretending this path handled it.
        return JSONResponse(
            {"ok": False, "error": f"'{engine}' models are not started by Ava; "
                                   "point a backend at it instead"}, status_code=400)
    with _swap_lock:
        if _swap_job["status"] == "running":
            return JSONResponse({"ok": False, "error": "a swap is already running"},
                                status_code=409)
        _swap_job.update(status="running", model=model_id, error=None)
        _swap_job["log"].clear()
    weight = model_store.disk_size_gb({"engine": engine, "id": model_id},
                                      model_store.dirs())
    threading.Thread(target=_run_swap, args=(model_id, engine, weight),
                     daemon=True, name="hub-brain-swap").start()
    return {"ok": True, "status": "running"}


@router.get("/models/store/brain/status")
def models_store_brain_status():
    with _swap_lock:
        return {"status": _swap_job["status"], "model": _swap_job["model"],
                "error": _swap_job["error"], "log": list(_swap_job["log"])}


@router.post("/models/store/delete")
def models_store_delete(payload: dict):
    """Delete a model's weights from disk and reclaim the space.

    Refuses while anything is holding it unless `force` is set, and says WHAT is
    holding it rather than just declining. Audited, because removing tens of GB
    is not recoverable and the flight recorder should be able to answer "where
    did that model go".
    """
    engine = str(payload.get("engine") or "").strip().lower()
    model_id = str(payload.get("id") or "").strip()
    force = bool(payload.get("force"))
    if not engine or not model_id:
        return JSONResponse({"ok": False, "error": "engine and id are required"},
                            status_code=400)
    held = model_in_use(engine, model_id)
    if held and not force:
        return JSONResponse({"ok": False, "error": "in use", "held_by": held},
                            status_code=409)
    try:
        out = model_store.delete_model(engine, model_id,
                                       forced=force, held_by=held)
    except model_store.ModelDeleteError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"delete failed: {e}"},
                            status_code=500)
    if not out["removed"]:
        return JSONResponse({"ok": False, "error": "not found on disk"},
                            status_code=404)
    # The ledger record is written by `models.delete_model`, at the destruction
    # itself — see tests/test_destructive_paths_audited for why not here.
    return {"ok": True, **out}


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
                # Under the lock like every other write to this job. `deque` is
                # append-safe, but the status route does `list(_pull_job["log"])`
                # and iterating a deque another thread is appending to raises
                # "deque mutated during iteration" — a 500 on the poll that draws
                # the progress the owner is watching, at the moment of most
                # traffic through it.
                with _pull_lock:
                    _pull_job["log"].append(line)
        rc = proc.wait()
        with _pull_lock:
            _pull_job.update(rc=rc, status="done" if rc == 0 else "error")
    except Exception as e:  # noqa: BLE001
        with _pull_lock:
            _pull_job["log"].append(f"pull failed: {e}")
            _pull_job.update(rc=1, status="error")

@router.post("/models/pull")
def models_pull(role: str = "", engine: str = "", id: str = ""):
    """Start a model download in the background.

    Three shapes, one implementation behind all of them (`ava models pull`):
    `role=<name>` for a declared role, `role=auto` for whatever fits this box,
    and `engine=+id=` for ANY model the owner wants in their store — which is
    what makes the store a library rather than a fixed set of roles.
    """
    with _pull_lock:
        if _pull_job["status"] == "running":
            return JSONResponse({"ok": False, "error": "a pull is already running"},
                                status_code=409)
        role = (role or "").strip().lower()
        model_id = (id or "").strip()
        eng = (engine or "").strip().lower()
        if model_id:
            if eng not in ("vllm", "ollama", "gguf", "llamacpp"):
                return JSONResponse(
                    {"ok": False, "error": "engine must be vllm, ollama or gguf"},
                    status_code=400)
            args = ["--engine", eng, "--id", model_id]
            label = f"{eng}:{model_id}"
        else:
            args = ["--auto"] if role in ("", "auto") else [role]
            label = role or "auto"
        _pull_job.update(status="running", role=label, rc=None)
        _pull_job["log"].clear()
        threading.Thread(target=_run_pull, args=(args,), daemon=True,
                         name="hub-model-pull").start()
    return {"ok": True, "status": "running"}

@router.get("/models/pull/status")
def models_pull_status():
    # Under the lock. The worker thread writes `status` and `rc` as two separate
    # statements, so a poll landing between them read `status: "done"` with
    # `rc: None` — which the panel renders as a finished, successful pull. The
    # log deque is thread-safe on its own; the pair is not.
    with _pull_lock:
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
        with _bench_lock:
            _bench_job["result"] = {
                "prompt": prompt, "max_tokens": max_tokens,
                "results": list(partial), "winner": winner,
                "backend_count": total, "pending": total - len(partial),
            }

    try:
        result = bench.bench(prompt, only=only, max_tokens=max_tokens,
                             on_result=on_result)
        with _bench_lock:
            _bench_job.update(result=result, status="done")
    except Exception as e:  # noqa: BLE001
        with _bench_lock:
            _bench_job.update(result={"error": str(e)[:200], "results": []},
                              status="error")

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
    # Under the lock, for the same reason as the pull status above: the worker
    # sets `result` and `status` separately, so a poll could see `done` with the
    # PREVIOUS run's result still in place.
    with _bench_lock:
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

def _brain_id() -> str | None:
    """Which backend is the conversational brain — asked of the ONE resolver.

    Still `roles.chat, else primary` in the ordinary case, because that is what
    models.effective_brain() resolves first. What changed is the answer when
    neither is set: this read the two keys straight off `inference` and returned
    None, so Setup reported "nothing is configured" on an install where the
    router was serving its env/built-in default and chat plainly worked — while
    the hardware monitor, which asks effective_brain(), named the real model.
    One question, two derivations, two answers.

    The contract is unchanged — a backend id, or None.

    The agent-sandbox case needs the fall-through below. A sandbox model is not
    a router backend, so the resolver reports it with an EMPTY backend_id — but
    the configured backends are still real, still serve the tool-less fallback,
    and Setup still has to mark which of them that is (AgentPanel renders it as
    "fallback brain"). Returning None there left the default install — where the
    agent runtime IS live — with no row flagged at all, so the badge became
    unreachable and the list lost its selected row. "Which backend would serve"
    stays answerable even while something else is answering.

    An implicit id (the env backend, or the shipped default) can name something
    that is not in this response's `backends` list — that list holds only what
    ava.yaml declares, and an implicit brain exists precisely because it
    declares none.
    """
    bid = model_store.effective_brain().get("backend_id") or ""
    if bid:
        return bid
    inf = _inference()
    roles = inf.get("roles") or {}
    chat = roles.get("chat") if isinstance(roles, dict) else None
    return chat or inf.get("primary") or None

@router.get("/models/backends")
def backends_list():
    """Configured inference backends + which one is the brain."""
    inf = _inference()
    backends = inf.get("backends") or {}
    primary = inf.get("primary")
    brain = _brain_id()
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

def purge_backend(cfg: dict, bid: str) -> list[str]:
    """Remove `bid` and EVERY pointer to it from a config dict, in place.

    Returns the config paths that were changed, so the caller can report what a
    removal actually touched instead of asserting it was clean.

    Deleting a backend used to clear four of the seven places its id can appear —
    the backend itself, `inference.primary`, `inference.roles.chat`, and the key
    file. Everything else was left dangling, and each dangling pointer fails
    differently and quietly:

      * `inference.fallback` is read by router_app on every request. A fallback
        naming a deleted backend is a failover target that cannot answer, so the
        request that needed a fallback is the one that finds out.
      * `inference.roles.<other>` — roles are "chat / fast / embed / …"
        (settings.resolve_role). Only `chat` was repointed, so an embed role
        outlived its backend and every embedding call resolved to nothing.
      * `alloc.models.<id>` is keyed by the same id (alloc/spec.py). A declared
        model with no backend stays governable: the planner counts memory it can
        never actually free, and the Free button drives an address nobody serves.

    Roles are repointed rather than dropped where a survivor exists, matching
    what the brain repoint already did — a role the owner configured should
    survive the removal of one engine behind it.
    """
    touched: list[str] = []
    inf = cfg.get("inference") or {}
    backends = inf.get("backends") or {}
    backends.pop(bid, None)
    touched.append(f"inference.backends.{bid}")
    survivor = next(iter(backends), None)

    for key in ("primary", "fallback"):
        if inf.get(key) == bid:
            # `fallback` falls away rather than moving to the survivor: it is the
            # SECOND choice, and promoting the only remaining backend to be its
            # own fallback is a loop, not a safety net.
            new = survivor if key == "primary" else None
            if new:
                inf[key] = new
            else:
                inf.pop(key, None)
            touched.append(f"inference.{key}")

    roles = inf.get("roles") or {}
    for role in [r for r, v in roles.items() if v == bid]:
        if survivor:
            roles[role] = inf.get("primary") or survivor
        else:
            roles.pop(role, None)
        touched.append(f"inference.roles.{role}")
    if roles:
        inf["roles"] = roles
    elif "roles" in inf:
        inf.pop("roles")

    cfg["inference"] = inf

    alloc = cfg.get("alloc") or {}
    declared = alloc.get("models") or {}
    if bid in declared:
        declared.pop(bid, None)
        alloc["models"] = declared
        cfg["alloc"] = alloc
        touched.append(f"alloc.models.{bid}")
    return touched


@router.post("/models/backends/{bid}/delete")
def backends_delete(bid: str):
    """Remove a backend and every reference to it — config, key, breaker state."""
    bid = settings._safe_backend_id(bid)
    cfg = settings.current_config()
    backends = (cfg.get("inference") or {}).get("backends") or {}
    if bid not in backends:
        return JSONResponse({"ok": False, "error": f"no backend '{bid}'"},
                            status_code=404)
    touched = purge_backend(cfg, bid)
    settings.save_config(cfg)
    settings.delete_backend_key(bid)
    # Runtime state outlives config. The breaker keeps per-model failure counts
    # and give-up timestamps in its own state file, so a re-added backend with
    # the same id inherited the dead one's strikes and could start out already
    # given up on.
    try:
        from ..alloc import breaker
        breaker.reset(bid)
    except Exception:  # noqa: BLE001 — the config removal already succeeded
        pass
    return {"ok": True, "restart_required": True, "removed": touched}

