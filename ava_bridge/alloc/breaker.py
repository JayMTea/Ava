"""Circuit breaker and action budget — the allocator's own safety limits.

Two failures this prevents, both observed in the wild.

**The retry storm.** A container supervised with an unbounded restart policy was
asked to start a model that could not fit. Its startup check failed, the supervisor
retried, and because nothing capped the retries it did that **7,997 times**. The
engine was never broken; it was being started at an impossible moment, forever.

Two independent mechanisms answer that, and the first matters more than the second:

  1. **Refuse to start what provably cannot fit.** Before any start, if the pool is
     readable and short, do not attempt it. While an outstanding lease explains the
     shortage this is not a fault at all and consumes no failure budget — it is the
     system working. *Not attempting* is the real fix; capping retries only limits
     the damage of attempting.
  2. **Bound the retries that do happen.** Exponential backoff with a give-up, so a
     genuinely broken model costs a handful of attempts over half an hour instead of
     thousands forever.

**The allocator itself thrashing.** A coordinator that stops and starts models in a
tight loop is worse than no coordinator. A global action budget caps state-changing
actions across every model *and every process*; exceeding it puts the allocator into
`QUIESCED`, where it stops actuating entirely and every lease becomes advisory. **Its
failure mode is to become a no-op, never a loop.**

Two details that look like implementation trivia and are not:

  * **A failure counter is reset only after a stability window of continuous
    readiness**, not by one successful start. Otherwise a model that crash-loops but
    looks healthy for three seconds resets the counter on every cycle and the loop
    runs forever — the classic breaker bug.
  * **State lives in the ledger, not in memory.** Several processes may drive the
    same models; a per-process counter would let each of them independently spend the
    full budget.

**Scope.** Decides whether an action is *allowed* right now. It does not decide which
action to take (`policy`), perform it (`drivers`), or sequence it (`broker`).
"""
from __future__ import annotations

import json
import os
import random

from .. import settings
from . import ledger

# Backoff for a model whose start genuinely fails. Worst case with the defaults:
# 6 attempts spread over ~35 minutes, then stop — versus an uncapped loop.
BASE_BACKOFF_S = 15.0
MAX_BACKOFF_S = 1800.0
MAX_ATTEMPTS = 6
MAX_WINDOW_S = 1800.0

# A start is only "good" once it has stayed ready this long. Shorter than this and a
# crash-loop keeps clearing its own record.
STABILITY_WINDOW_S = 120.0

# Global ceiling on state-changing actions, across all models and processes.
BUDGET_ACTIONS = 20
BUDGET_WINDOW_S = 600.0
QUIESCE_COOLOFF_S = 1800.0

# Causes that are not the model's fault and must not consume its failure budget.
#
# `pool_unmeasurable` is the one that is easy to get wrong. A release reports `ok`
# only once the pool has MEASURABLY dropped — but on a box whose accelerator probe
# failed, `hwinfo.fit_memory()` falls back to system RAM (a documented gap, see its
# comment), so freeing a GPU model moves the number not at all. `wait_free` then
# times out and a perfectly successful release arrives here as a failure. Counting
# it meant two clicks half an hour apart gave up on a working model, curable only
# from a terminal. The release did not fail; the box cannot see that it worked.
CAUSE_NO_FAULT = {"insufficient_memory", "declined", "quiesced", "not_enforcing",
                  "pool_unmeasurable"}


def _cfg(key: str, default):
    return settings.get(f"alloc.breaker.{key}", default)


def _path() -> str:
    return os.path.join(ledger._dir(), "breaker.json")


# Fields holding CLOCK_MONOTONIC readings. Monotonic time restarts near zero at
# boot, so a value written before a reboot lands arbitrarily far in the future
# afterwards — and `allowed()` compares against exactly these.
_MONOTONIC_FIELDS = ("quiesced_at",)
_MONOTONIC_MODEL_FIELDS = ("next_attempt_at", "first_fail_at", "last_fail_at",
                           "given_up_at", "last_success_at", "ready_since")


def _scrub_foreign_boot(obj: dict) -> dict:
    """Drop monotonic timestamps written before this boot, keep the judgements.

    `ledger.live_leases()` already does this for leases; breaker.json and the
    model-state records did not, so after a reboot the allocator could refuse
    every action for as long as the previous uptime — silently, because
    `watch._breaker_alerts` only fires on quiesced/given_up, not on "backing off
    until a time that will never arrive".

    What survives is deliberate: `fails`, `given_up`, `reason` and `cause` are
    judgements about a model, and a reboot does not repair a broken model. Only
    the CLOCKS are meaningless.
    """
    if obj.get("boot_id") == ledger.boot_id():
        return obj
    out = {k: v for k, v in obj.items() if k not in _MONOTONIC_FIELDS}
    models = out.get("models")
    if isinstance(models, dict):
        out["models"] = {
            mid: {k: v for k, v in (rec or {}).items()
                  if k not in _MONOTONIC_MODEL_FIELDS}
            for mid, rec in models.items() if isinstance(rec, dict)
        }
    out["boot_id"] = ledger.boot_id()
    return out


def _read() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            obj = json.load(fh)
        return _scrub_foreign_boot(obj) if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(obj: dict) -> None:
    path = _path()
    obj = dict(obj)
    obj["boot_id"] = ledger.boot_id()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
    except OSError:
        pass        # a read-only ledger disables the breaker's memory, not the app


def _update(fn):
    """Read-modify-write under the pool lock, held for microseconds only.

    Never held across a driver call: the whole point of the ordering rule is that a
    slow stop cannot block a peer's bookkeeping.
    """
    try:
        with ledger.flock("pool.lock", wait_s=2.0):
            obj = _read()
            out = fn(obj)
            _write(obj)
            return out
    except (ledger.LockBusy, OSError):
        # Cannot coordinate: fail toward caution for permission questions, and skip
        # the bookkeeping rather than blocking the caller.
        return fn(_read())


# --- per-model breaker ------------------------------------------------------- #
def state(model_id: str) -> dict:
    return (_read().get("models") or {}).get(model_id) or {}


def allowed(model_id: str, *, human: bool = False) -> tuple[bool, str]:
    """May we attempt a state-changing action on this model right now?

    `human=True` means a person named this model and asked for this action. Every
    guard here exists to stop a LOOP — an unattended retry, a thrashing coordinator —
    and a person clicking is bounded by the person. So the thrash guards yield:
    `quiesced`, `given_up` and the backoff window are all skipped, because refusing a
    deliberate request on the grounds that Ava was previously too eager leaves the
    owner stranded behind `ava alloc reset` in a terminal they may not have open.

    What does NOT yield: the caller still calls `record_attempt`, so a human action
    spends budget and stays visible in the same accounting as everything else, and the
    memory check in `_restore_locked` is untouched — that one is the actual cure for
    the restart storm, and a person clicking "bring it back" on a full box would
    reintroduce it exactly.
    """
    if not human:
        q = quiesced()
        if q[0]:
            return False, q[1]
    st = state(model_id)
    if st.get("given_up") and not human:
        return False, (f"breaker open for {model_id}: gave up after "
                       f"{st.get('fails', 0)} attempts — last error: "
                       f"{st.get('reason') or 'unknown'}. Clear with "
                       f"`ava alloc reset {model_id}`.")
    nxt = float(st.get("next_attempt_at") or 0.0)
    if nxt and ledger.now() < nxt and not human:
        return False, (f"backing off {model_id} for "
                       f"{int(nxt - ledger.now())}s after "
                       f"{st.get('fails', 0)} failed attempt(s)")
    ok, why = budget_ok()
    if not ok and not human:
        return False, why
    return True, "ok"


def record_failure(model_id: str, reason: str, cause: str = "") -> dict:
    """Note a failed action. No-fault causes do not count against the budget.

    `insufficient_memory` is the important exemption: a model that could not start
    because something else legitimately held the pool is not broken, and counting it
    would eventually give up on a perfectly good model.
    """
    if cause in CAUSE_NO_FAULT:
        return _update(lambda o: _touch(o, model_id, deferred=reason, cause=cause))

    def fn(obj):
        m = _touch(obj, model_id)
        now = ledger.now()
        m["fails"] = int(m.get("fails", 0)) + 1
        m["reason"] = reason[:400]
        m["cause"] = cause or "error"
        m.setdefault("first_fail_at", now)
        m["last_fail_at"] = now
        m.pop("deferred", None)
        attempts = int(_cfg("max_attempts", MAX_ATTEMPTS))
        window = float(_cfg("max_window_s", MAX_WINDOW_S))
        elapsed = now - float(m["first_fail_at"])
        if m["fails"] >= attempts or elapsed >= window:
            m["given_up"] = True
            m["given_up_at"] = now
            m["next_attempt_at"] = 0.0
        else:
            base = float(_cfg("base_backoff_s", BASE_BACKOFF_S))
            cap = float(_cfg("max_backoff_s", MAX_BACKOFF_S))
            delay = min(base * (2 ** (m["fails"] - 1)), cap)
            delay *= 0.8 + random.random() * 0.4      # jitter: avoid lockstep retries
            m["next_attempt_at"] = now + delay
        return dict(m)
    return _update(fn)


def record_attempt(model_id: str) -> None:
    """Count one state-changing action against the global budget."""
    def fn(obj):
        acts = obj.setdefault("actions", [])
        acts.append(ledger.now())
        window = float(_cfg("budget_window_s", BUDGET_WINDOW_S))
        cutoff = ledger.now() - window
        obj["actions"] = [t for t in acts if t >= cutoff]
        limit = int(_cfg("budget_actions", BUDGET_ACTIONS))
        # `>=`, not `>`. Every attempt passes budget_ok() first, which refuses at
        # `>= limit` — so the count could never EXCEED the limit, and this branch
        # was unreachable through the normal path. QUIESCED therefore never
        # happened and the critical `alloc_quiesced` alert could never fire; the
        # existing test only saw it because it called record_attempt directly.
        # The action that spends the last unit of budget is the one that trips
        # the guard, which is also what breaker.py's own docstring describes.
        if len(obj["actions"]) >= limit:
            obj["quiesced_at"] = ledger.now()
            obj["quiesce_reason"] = (
                f"{len(obj['actions'])} state-changing actions in "
                f"{int(window / 60)} min exceeded the limit of {limit}")
        return None
    _update(fn)


def record_success(model_id: str, *, ready: bool | None = None) -> None:
    """Note a successful action. Does NOT clear the failure count on its own.

    Clearing waits for `note_ready()` to observe continuous readiness for the
    stability window — one successful start proves nothing about a crash-loop.
    """
    def fn(obj):
        m = _touch(obj, model_id)
        m["last_success_at"] = ledger.now()
        m["next_attempt_at"] = 0.0
        if ready:
            m.setdefault("ready_since", ledger.now())
        return None
    _update(fn)


def note_ready(model_id: str, ready: bool | None) -> None:
    """Feed observed readiness in; clears the breaker after a stable window.

    Called by the watchdog, which is already polling readiness. This is the only path
    that resets a failure count, and it deliberately requires *duration*.
    """
    def fn(obj):
        m = _touch(obj, model_id)
        now = ledger.now()
        if not ready:
            m.pop("ready_since", None)
            return None
        since = m.setdefault("ready_since", now)
        if now - float(since) >= float(_cfg("stability_window_s", STABILITY_WINDOW_S)):
            for k in ("fails", "first_fail_at", "last_fail_at", "reason", "cause",
                      "given_up", "given_up_at", "next_attempt_at", "deferred"):
                m.pop(k, None)
        return None
    _update(fn)


def reset(model_id: str | None = None) -> None:
    """Clear a model's breaker, or the whole thing including QUIESCED."""
    def fn(obj):
        if model_id:
            (obj.get("models") or {}).pop(model_id, None)
        else:
            obj["models"] = {}
            obj["actions"] = []
            obj.pop("quiesced_at", None)
            obj.pop("quiesce_reason", None)
        return None
    _update(fn)


# --- global budget / quiesce -------------------------------------------------- #
def budget_ok() -> tuple[bool, str]:
    obj = _read()
    window = float(_cfg("budget_window_s", BUDGET_WINDOW_S))
    limit = int(_cfg("budget_actions", BUDGET_ACTIONS))
    cutoff = ledger.now() - window
    recent = [t for t in (obj.get("actions") or []) if t >= cutoff]
    if len(recent) >= limit:
        return False, (f"action budget spent: {len(recent)} actions in "
                       f"{int(window / 60)} min (limit {limit})")
    return True, "ok"


def quiesced() -> tuple[bool, str]:
    """Has the allocator disabled itself? Self-clears after a cool-off."""
    obj = _read()
    at = obj.get("quiesced_at")
    if not at:
        return False, ""
    cool = float(_cfg("quiesce_cooloff_s", QUIESCE_COOLOFF_S))
    if ledger.now() - float(at) >= cool:
        _update(lambda o: (o.pop("quiesced_at", None), o.pop("quiesce_reason", None),
                           o.update(actions=[]))[0])
        return False, ""
    return True, ("allocator QUIESCED (acting as a no-op): "
                  f"{obj.get('quiesce_reason') or 'thrash guard tripped'}. "
                  "Leases are advisory until it clears; "
                  "`ava alloc resume` to clear now.")


def snapshot() -> dict:
    """Read-only view for reports and `ava doctor`. Decides nothing."""
    obj = _read()
    q, why = quiesced()
    window = float(_cfg("budget_window_s", BUDGET_WINDOW_S))
    cutoff = ledger.now() - window
    recent = [t for t in (obj.get("actions") or []) if t >= cutoff]
    models = {}
    for mid, m in (obj.get("models") or {}).items():
        nxt = float(m.get("next_attempt_at") or 0.0)
        models[mid] = {
            "fails": int(m.get("fails", 0)),
            "given_up": bool(m.get("given_up")),
            "reason": m.get("reason"),
            "cause": m.get("cause"),
            "deferred": m.get("deferred"),
            "retry_in_s": max(0, int(nxt - ledger.now())) if nxt else None,
        }
    return {"quiesced": q, "quiesce_reason": why or None,
            "actions_in_window": len(recent),
            "budget": int(_cfg("budget_actions", BUDGET_ACTIONS)),
            "window_s": window, "models": models}


def _touch(obj: dict, model_id: str, **fields) -> dict:
    m = obj.setdefault("models", {}).setdefault(model_id, {})
    m.update(fields)
    return m
