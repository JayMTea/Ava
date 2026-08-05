"""Setup -> Hardware: the operator-stated memory pool, and freeing model memory.

Two things live here because both are the same question — *what is this machine's
memory doing* — asked in the two directions an owner can ask it. The stated pool says
how much there is; the alloc routes below say what is holding it and let the owner
take some back.

Ava measures what it can and refuses to guess at what it cannot. Two boxes it
genuinely cannot size:

  * an engine on the HOST while Ava runs in a container - it draws on the
    machine's memory and the machine's GPU, and Ava can see it answering
    without being able to measure the box it lives on;
  * any pool behind a probe that does not answer here.

In both, Ava withholds a tier rather than sizing one from the wrong machine.
That is right, and it left an owner who KNOWS the answer with no way to say it.
This route is that door, and `hwinfo.stated_fit_gb` is where precedence lives.

**It sets advice, not actuation.** The value reaches `hwinfo.fit_pool()`, which
recommends a model tier and nothing else. It deliberately never reaches
`fit_memory()`, the oracle `alloc/capacity.py` governs on - see the boundary
note in `hwinfo.fit_pool`.

`restart_required` is False, and that is engineered rather than hoped for:
`settings.save_patch` updates the in-process config, `stated_fit_gb()` reads it
per call, and this route drops the fit cache so the next read is the new value.
Same guarantee hub/branding.py makes, for the same reason - a setting an owner
is expected to tune is a setting that must not cost them a restart to tune.
"""
import contextlib
import threading
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import audit, auth, hwinfo, settings

router = APIRouter()


@router.get("/hardware/pool")
def pool_get():
    """What is stated, what was measured, and whether this form may change it."""
    import os
    stated = hwinfo.stated_fit_gb()
    env = os.environ.get(hwinfo._STATED_ENV)
    return {
        "stated_gb": stated,
        "source": ("env" if env else "config") if stated is not None else "",
        "env_var": hwinfo._STATED_ENV,
        # An env override cannot be edited away from a web form - the process
        # would go on ignoring ava.yaml and the owner would be left changing a
        # number that does nothing. Say so instead.
        "editable": stated is None or not env,
        "min_gb": hwinfo._STATED_MIN_GB,
        "max_gb": hwinfo._STATED_MAX_GB,
    }


@router.post("/hardware/pool")
async def pool_set(request: Request):
    """Set or clear the stated pool. `{"gb": null}` restores what Ava measures."""
    import os
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)

    if os.environ.get(hwinfo._STATED_ENV):
        return JSONResponse(
            {"ok": False, "error": f"{hwinfo._STATED_ENV} is set in this "
                                   "environment and takes precedence — change it "
                                   "there, or unset it to manage this here",
             "field": "gb"}, status_code=409)

    raw = body.get("gb")
    if raw in (None, "", 0):
        gb = None                                   # back to measured
    else:
        try:
            gb = round(float(raw), 1)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "memory must be a number",
                                 "field": "gb"}, status_code=400)
        # Refused, not clamped. A silently corrected value is one the owner
        # cannot see is wrong, and this number's whole job is to be believed.
        if not (hwinfo._STATED_MIN_GB <= gb <= hwinfo._STATED_MAX_GB):
            return JSONResponse(
                {"ok": False, "field": "gb",
                 "error": f"must be between {hwinfo._STATED_MIN_GB:g} and "
                          f"{hwinfo._STATED_MAX_GB:g} GB"}, status_code=400)

    try:
        settings.save_patch({"hardware": {"fit_memory_gb": gb}})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"could not write ava.yaml: {e}"},
                            status_code=500)
    # The pool is TTL-cached, so without this the owner saves a value and watches
    # the old one for another three seconds — which reads as the save not working.
    hwinfo.reset_cache()
    return {"ok": True, "stated_gb": gb, "restart_required": False}


# --------------------------------------------------------------------------- #
# Freeing a model's memory, because the owner said so.
#
# Every route here is a `def`, not `async def`, on purpose: `alloc.report()` can
# shell out to `docker inspect` / `systemctl` once per declared model, and a
# blocking call on the event loop stalls the whole bridge. Starlette runs a sync
# handler on the threadpool, which is exactly what this wants.
#
# One job slot for the whole surface, not one per model — the same shape as the
# model pull in hub/models.py. Two concurrent releases is precisely the race the
# allocator's per-model flock exists to serialise, and the breaker's action budget
# is global anyway, so a second slot would buy nothing and hide the queueing.
# Server-side, so it survives the owner closing the panel or opening a second tab.
# --------------------------------------------------------------------------- #
_alloc_job: dict = {"status": "idle", "model": None, "verb": None,
                    "log": deque(maxlen=100), "result": None,
                    "started_at": None, "ended_at": None}
_alloc_lock = threading.Lock()


def _job_view() -> dict:
    return {**{k: v for k, v in _alloc_job.items() if k != "log"},
            "log": list(_alloc_job["log"])}


@router.get("/hardware/alloc")
def alloc_report():
    """What is holding this machine's memory, and what Ava could free. Read-only."""
    from .. import alloc
    if not alloc.spec.enabled():
        # Not a 404: the panel needs to say WHY there is nothing here, and an
        # absent route cannot be distinguished from a broken one.
        return {"enabled": False, "models": [], "declared_count": 0}
    rep = alloc.report()
    leases = rep.get("leases") or {}
    return {
        "enabled": True,
        "actuating": rep.get("actuating"),
        "gating": rep.get("gating"),
        "platform": rep.get("platform"),
        "pool": rep.get("pool"),
        "models": rep.get("models"),
        "declared_count": rep.get("declared_count"),
        "driver_errors": rep.get("driver_errors"),
        # The thrash guard's own state. Without it the panel cannot explain why a
        # button did nothing, and an owner's only clue would be a log line.
        "breaker": leases.get("breaker") or {},
        "job": _job_view(),
    }


@router.get("/hardware/alloc/job")
def alloc_job():
    """The in-flight release/restore, if any. Polled only while one is running."""
    return _job_view()


def _run_alloc(verb: str, model_id: str, mode: str | None, actor: str) -> None:
    """Worker: one owner-initiated actuation, off the request thread.

    A `docker stop` plus waiting for the kernel to hand memory back takes minutes,
    which no HTTP timeout survives — hence the job. `actor` is passed in rather than
    read here because `audit`'s actor is a contextvar set by the auth middleware, and
    a fresh thread starts with an empty context: read from inside, every owner action
    would be recorded as "unknown".
    """
    from .. import alloc
    from ..alloc import broker
    try:
        fn = alloc.owner_release if verb == "release" else alloc.owner_restore
        kw = {"mode": mode} if verb == "release" else {}
        res = fn(model_id, log=lambda m: _alloc_job["log"].append(str(m)), **kw)
        _alloc_job["result"] = res
        _alloc_job["status"] = "done" if res.get("ok") else "error"
        audit.record(kind=f"alloc.owner_{verb}", model=model_id, actor=actor,
                     ok=bool(res.get("ok")), code=res.get("code"),
                     freed_gib=res.get("freed_gib"))
    except Exception as e:  # noqa: BLE001 — a failed actuation is a result, not a 500
        _alloc_job["log"].append(f"{verb} failed: {e}")
        _alloc_job["result"] = {"ok": False, "code": "driver_error", "detail": str(e)}
        _alloc_job["status"] = "error"
    finally:
        _alloc_job["ended_at"] = time.time()
        # Register with the broker's actuation set so `wait_for_actuations()` covers
        # this thread too — a release outliving a test fixture writes into the real
        # ledger, which is the leak tests/test_alloc_isolation.py exists to stop.
        with contextlib.suppress(Exception):
            broker._actuating.discard(threading.current_thread())


def _start(verb: str, model_id: str, request: Request, mode: str | None = None):
    """Shared guard + launch for both verbs. Returns a response dict or JSONResponse."""
    from .. import alloc
    from ..alloc import broker

    ok, why = auth.same_site_write(request)
    if not ok:
        # The first /api/hub route that can stop a running process. The session
        # cookie is samesite=lax, which does NOT ride a cross-site POST — but this
        # is cheap, and "it happened to be blocked by something else" is how the
        # /setup hole got there in the first place.
        return JSONResponse({"ok": False, "code": "cross_site", "error": why},
                            status_code=403)
    if not alloc.spec.enabled():
        return JSONResponse({"ok": False, "code": "alloc_disabled"}, status_code=409)

    # Refuse while Ava is mid-answer. Nothing in the serving path takes a lease yet
    # (ADR-0005's follow-up), so this register is the only thing that knows, and
    # unloading the brain under a streaming turn would break it silently.
    if verb == "release" and _turn_in_flight():
        return JSONResponse({"ok": False, "code": "turn_in_flight"}, status_code=409)

    with _alloc_lock:
        if _alloc_job["status"] == "running":
            return JSONResponse({"ok": False, "code": "job_running",
                                 "job": _job_view()}, status_code=409)
        _alloc_job.update(status="running", model=model_id, verb=verb,
                          result=None, started_at=time.time(), ended_at=None)
        _alloc_job["log"].clear()
        t = threading.Thread(target=_run_alloc, name=f"hub-alloc-{verb}",
                             args=(verb, model_id, mode, audit.actor()), daemon=True)
        with contextlib.suppress(Exception):
            broker._actuating.add(t)
        t.start()
    return {"ok": True, "job": _job_view()}


def _turn_in_flight() -> bool:
    """Is Ava mid-answer right now?"""
    try:
        from .. import state
        with state.turns_lock:
            return any((v or {}).get("status") == "running"
                       for v in state.turns.values())
    except Exception:  # noqa: BLE001 — if we cannot tell, do not block the owner
        return False


@router.post("/hardware/alloc/{model_id}/release")
def alloc_release(model_id: str, request: Request, mode: str = ""):
    """Free one declared model's memory. POST only — see the CSRF note in `_start`."""
    return _start("release", model_id, request, mode=mode or None)


@router.post("/hardware/alloc/{model_id}/restore")
def alloc_restore(model_id: str, request: Request):
    """Bring back a model the owner freed. Only ever one Ava itself released."""
    return _start("restore", model_id, request)


@router.post("/hardware/alloc/{model_id}/reset")
def alloc_reset(model_id: str, request: Request):
    """Clear a model's failure record.

    Exists so the owner is never stranded behind a terminal. Before this, the only
    escape from a breaker that had given up was `ava alloc reset <id>` — which is a
    poor answer for a control whose whole point is not needing one.
    """
    ok, why = auth.same_site_write(request)
    if not ok:
        return JSONResponse({"ok": False, "code": "cross_site", "error": why},
                            status_code=403)
    from ..alloc import breaker
    breaker.reset(model_id or None)
    audit.record(kind="alloc.breaker_reset", model=model_id, ok=True)
    return {"ok": True}
