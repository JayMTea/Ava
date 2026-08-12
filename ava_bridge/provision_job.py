"""One provisioning run at a time, observable while it happens.

`POST /api/hub/agent/provision` used to block for up to ten minutes and return the
whole step list at the end. The Hub's only feedback was a disabled button reading
"Provisioning…", which cannot distinguish slow from hung — and install.sh has a
documented hang mode (§7's `recover` spawns a detached `ssh -f` that inherits
stdout). A ten-minute synchronous POST is also fragile through any proxy or
tailnet hop.

Shape copied deliberately from `hub/models.py`'s model pull: a single-slot dict
under a lock, a bounded deque of output lines, `Popen` line iteration, and 409 on
a concurrent start. It is a module rather than route-local state because three
callers need it — the hub routes, `ava_cli.py`, and the agent-runtime shim.

**Why a private slot and not the /api/stream/ops SSE**, which would have been
free: that channel carries turn, hardware, device and alert deltas, all of which
a browser subscribes to for the Operations page. A provisioning run is neither —
it belongs to whoever started it, it is single-slot by construction, and its
output lines are shell chatter that no dashboard should diff at 1 Hz.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

from . import provision

# install.sh's machine-readable progress channel. Prose is never parsed: a
# reworded `echo` must not be able to silently stop the progress view.
_STEP_PREFIX = "[ava::step]"

_LOG_MAX = 400

_job: dict[str, Any] = {
    "status": "idle",       # idle | running | done | error
    "id": None,
    "scope": None,
    "connector": None,      # set when a run deploys ONE connected app
    "started_at": None,
    "ended_at": None,
    "rc": None,
    "steps": [],            # [{scope, id, ok, detail}]
    "log": deque(maxlen=_LOG_MAX),
    "detail": "",
    "result": None,
    "observable": True,     # False on a runtime that cannot stream (remote)
    "seq": 0,               # monotonic cursor for incremental polling
}
_lock = threading.Lock()


def _parse_step(line: str) -> dict | None:
    if not line.startswith(_STEP_PREFIX):
        return None
    parts = line.split("\t")
    if len(parts) < 4:
        return None
    _, scope, ident, outcome, *rest = parts
    return {"scope": scope, "id": ident, "ok": outcome == "ok",
            "detail": (rest[0] if rest else "").strip()}


def is_running() -> bool:
    with _lock:
        return _job["status"] == "running"


def snapshot(since: int = 0) -> dict:
    """The poll payload. `since` slices the log so a poller ships only new lines."""
    with _lock:
        return _snapshot_locked(since)


def _snapshot_locked(since: int = 0) -> dict:
    """Caller already holds `_lock`.

    Split out because `_lock` is a plain Lock, not an RLock: `start()` needs a
    snapshot for its 409 response while still holding the lock it used to test the
    slot, and calling the public `snapshot()` there deadlocked the request thread
    — on the concurrency path, which is the one that only shows up under load.
    """
    log = list(_job["log"])
    total = _job["seq"]
    start = max(0, len(log) - (total - min(since, total)))
    return {
        "status": _job["status"],
        "id": _job["id"],
        "scope": _job["scope"],
        "connector": _job["connector"],
        "started_at": _job["started_at"],
        "ended_at": _job["ended_at"],
        "rc": _job["rc"],
        "steps": list(_job["steps"]),
        "log": log[start:],
        "seq": total,
        "detail": _job["detail"],
        "observable": _job["observable"],
        "result": _job["result"],
    }


def start(scope: str = "all", auto_install: bool = False,
          runner: Callable[..., dict] | None = None,
          connector: str | None = None) -> tuple[bool, dict]:
    """Begin a run. Returns (started, snapshot); (False, …) means one is already
    in flight and the caller should 409.

    `connector` narrows a `policies,servers` run to one connected app. It rides
    on the job rather than in the scope string because a scope is a domain of the
    desired manifest and a connector's material already lives inside two of them
    — see `provision.parse_scope`.
    """
    if provision.parse_scope(scope) is None:
        return False, {**snapshot(), "error": f"unknown scope {scope!r}",
                       "error_code": "unknown_scope"}

    with _lock:
        if _job["status"] == "running":
            return False, _snapshot_locked()
        _job.update(
            status="running", id=uuid.uuid4().hex[:12], scope=scope,
            connector=connector or None,
            started_at=time.time(), ended_at=None, rc=None, steps=[], detail="",
            result=None, seq=0, observable=True,
        )
        _job["log"].clear()
        job_id = _job["id"]

    def _on_line(line: str) -> None:
        step = _parse_step(line)
        with _lock:
            if _job["id"] != job_id:        # a later run took the slot
                return
            if step is not None:
                _job["steps"].append(step)  # steps never pollute the log
            else:
                _job["log"].append(line)
                _job["seq"] += 1

    def _work() -> None:
        result: dict = {}
        try:
            if runner is not None:
                # The injected runner sees exactly what the real runtime sees,
                # narrowing included — a test seam that is handed less than
                # production is a seam that can pass while production is wrong.
                result = runner(scope=scope, auto_install=auto_install,
                                on_line=_on_line, connector=connector) or {}
            else:
                from . import runtime as _runtime
                rt = _runtime.configured()
                with _lock:
                    # RemoteRuntime has no streaming primitive: steps arrive only
                    # at the end. The UI must know, so it renders an honest
                    # "no step-by-step progress" note instead of a checklist that
                    # never fills.
                    _job["observable"] = hasattr(rt, "_stream")
                result = rt.provision(auto_install=auto_install, scope=scope,
                                      on_line=_on_line, connector=connector) or {}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "detail": f"provisioning crashed: {e}"}
        else:
            # Assert what actually landed, and let it VETO a green run. install.sh
            # exiting 0 means "I ran", not "it worked": a rejected egress policy
            # is reported and skipped by design, so without this a deploy that
            # silently dropped a policy still reads as success.
            try:
                # Scoped AND narrowed the same way the run was: a connector
                # deploy that reported failure because a DIFFERENT connector is
                # undeployed would be vetoing work it never claimed to do.
                verdict = provision.verify(scope=scope, connector=connector)
                result["verify"] = verdict
                step = provision.verify_step(verdict)
                if step is not None:
                    result.setdefault("steps", []).append(step)
                    if not step["ok"]:
                        result["ok"] = False
                        result["detail"] = step["detail"]
            except Exception:  # noqa: BLE001 — a broken check must not fail a good deploy
                pass
        finally:
            # Exactly-once, and in a `finally`, so a runner that raises can never
            # strand the slot at "running" and lock out every later attempt.
            # Capture the provenance INSIDE the guarded block, under the same
            # `_job["id"] == job_id` check that guards the update.
            #
            # It used to be read afterwards and unguarded, so a second run
            # starting in that window produced a record mixing this run's scope
            # with the NEXT run's timestamps and exit code — a provenance file
            # describing something that never happened, which is worse than no
            # provenance at all. `mine` is None when the slot moved on, and then
            # there is simply nothing of ours left to record.
            mine = None
            with _lock:
                if _job["id"] == job_id and _job["status"] == "running":
                    ok = bool(result.get("ok"))
                    for s in (result.get("steps") or []):
                        _job["steps"].append({"scope": "runtime", "id": s.get("step", ""),
                                              "ok": bool(s.get("ok")),
                                              "detail": str(s.get("detail") or "")})
                    _job.update(status="done" if ok else "error",
                                ended_at=time.time(),
                                rc=0 if ok else 1,
                                detail=str(result.get("detail") or ""),
                                result=result)
                    mine = {"started": float(_job["started_at"] or 0),
                            "ended": float(_job["ended_at"] or 0),
                            "rc": int(_job["rc"] or 0),
                            "steps": list(_job["steps"])}
            if mine is not None:
                try:
                    provision.record_run(scope=scope, source="bridge",
                                         verify=(result or {}).get("verify"),
                                         **mine)
                except Exception:  # noqa: BLE001 — provenance is best-effort
                    provision.invalidate()

    threading.Thread(target=_work, daemon=True, name="ava-provision").start()
    return True, snapshot()


def reset_for_tests() -> None:
    with _lock:
        _job.update(status="idle", id=None, scope=None, connector=None,
                    started_at=None,
                    ended_at=None, rc=None, steps=[], detail="", result=None,
                    seq=0, observable=True)
        _job["log"].clear()
