"""Allocation watchdog — makes a degraded model impossible to miss.

`alloc.report()` tells the truth, but only when somebody asks. That is not enough:
the failure this layer exists to catch is *quiet by construction*. A model whose
warm-up hit an out-of-memory error, caught it, and let its HTTP server bind anyway
looks healthy to every liveness check there is — the port answers, the service
manager says active, the process is alive. Observed on the development box: a voice
sidecar served nothing for **six days** while its own `/health` reported
`{"ok": false, ...}` the entire time, because nothing polled it.

So this scheduler polls it. Each cycle it re-reads every declared model and pushes a
persistent alert for anything wrong, using the same `alerts.push_external` +
`ttl = INTERVAL * 1.5` idiom as the architecture watchdog: the alert stays active for
as long as the condition persists and **self-clears once it is fixed**, with no
acknowledge step and no stale-alert cleanup to remember.

What it raises, worst first:

  * **degraded** — resident but NOT ready. The dangerous one: the port is open, so
    nothing else notices. Critical.
  * **absent** — declared, but its container/unit does not exist here. Usually a
    typo in `alloc.models`, or a config carried over from another machine.
  * **unfit** — could not be brought up right now for lack of memory. Informational
    while something else legitimately holds the pool; an alert when it persists,
    because a model that can never start is indistinguishable from a broken one.
  * **misconfigured** — a driver's own `validate()` complaints, e.g. an unbounded
    container restart policy that will fight the allocator, or no readiness probe
    (which makes the degraded state above undetectable).
  * **unknown-hog** — undeclared processes holding a large share of the pool while a
    declared model does not fit. Observed and named, never touched.

**Announcing is most of what this does, and the point of it is that every state becomes
either fine or loud, with no third category.** It takes exactly one action: firing a
restore whose cooldown has lapsed. That belongs here because a release arms an in-process
timer, and a timer dies with the process that armed it — leaving a model released with
nobody holding it, which looks exactly like an outage. See `broker.restore_due`.

**Absent config is a no-op.** With no `alloc.models` declared the scheduler does not
start at all, exactly as the architecture watchdog no-ops on an install with no
deployment manifest. A fork that has not opted in pays nothing and sees nothing.

Same in-process pattern as `perf_store.start_scheduler` / `arch_watch.start_scheduler`:
a daemon thread, no systemd timer, portable to a host, a container, or a laptop.
"""
from __future__ import annotations

import os
import threading
import time

from .. import alerts, audit

INTERVAL = float(os.environ.get("AVA_ALLOC_WATCH_INTERVAL", "300"))  # 5 min

# A declared model that cannot fit is normal *while something else holds the pool* —
# that is the system working. It only deserves an alert once it has been true for a
# while, because a model that can never start looks the same as a broken one.
UNFIT_GRACE_S = float(os.environ.get("AVA_ALLOC_UNFIT_GRACE", "3600"))  # 1 h

# Undeclared memory only matters when it is both large and in the way.
HOG_MIN_GIB = float(os.environ.get("AVA_ALLOC_HOG_MIN_GIB", "8"))

_started = False
_lock = threading.RLock()
# Last observed state per model, so the audit ledger records TRANSITIONS rather
# than one row per model per cycle (which would bury the signal it exists to keep).
_last: dict[str, str] = {}
_unfit_since: dict[str, float] = {}


def _wants_start(row: dict) -> bool:
    """Is this a model we would actually want to bring up right now?

    Only a model that is **known not resident** qualifies. This distinction matters
    because `cold_load_ok` asks "could this be loaded from nothing", which is
    naturally False for a model that is already running — its own weights are part
    of the memory currently in use. Counting that as "cannot start" would report
    every healthy resident model as a problem, and would name it in the
    undeclared-memory alert as something being blocked when nothing is blocking it.

    Unknown residency (`None`) also does not qualify: if we cannot see whether it is
    running, we must not claim it is being held back.

    Nor does a model the owner freed by hand. It is down because they said so, and
    "has not been able to start for 60 min" about a state someone chose is the
    watchdog nagging them for using the feature.
    """
    return (bool(row.get("local")) and not row.get("observe_only")
            and not row.get("released_by_owner")
            and row.get("resident") is False)


def _state_of(row: dict) -> str:
    """Collapse a report row into one word. Order matters: worst wins."""
    if row.get("released_by_owner"):
        # FIRST, ahead of every problem state, because this is not a problem — it is
        # the state that was asked for. It has to outrank `degraded` in particular:
        # an engine that drops its weights on request keeps serving its port and
        # answers its own readiness probe with "no model loaded", which is byte for
        # byte the six-day silent-fallback shape below. The difference is not
        # observable at the probe; it is only knowable from who asked.
        return "released"
    if row.get("resident") and row.get("ready") is False:
        return "degraded"
    if row.get("observe_only") or not row.get("local"):
        return "observed"
    if row.get("resident") is None and "absent" in (row.get("detail") or ""):
        return "absent"
    if _wants_start(row) and not row.get("cold_load_ok"):
        return "unfit"
    if row.get("resident"):
        return "ready" if row.get("ready") is not False else "degraded"
    return "stopped"


def run_cycle(report: dict | None = None) -> dict:
    """One observe pass. Returns `{model_id: state}`; pushes alerts as needed.

    `report` is injectable so a test can drive every branch with no hardware.
    """
    from . import report as _report
    rep = report if report is not None else _report()
    rows = rep.get("models") or []
    states: dict[str, str] = {}
    now = time.time()

    for row in rows:
        mid = row.get("id") or "?"
        state = _state_of(row)
        states[mid] = state

        # Feed observed readiness to the breaker. This is the ONLY path that clears a
        # failure record, and it deliberately requires sustained readiness rather than
        # a single successful start — otherwise a model that crash-loops but looks
        # healthy for a few seconds resets its own counter forever. The watchdog is
        # already polling, so it is the natural observer.
        try:
            from . import breaker
            breaker.note_ready(mid, row.get("ready"))
        except Exception:  # noqa: BLE001 — bookkeeping must never break the watch
            pass

        if state == "degraded":
            # The six-day failure. Say so in terms an operator can act on, and name
            # the probe, because "not ready" is only useful if you know who said it.
            alerts.push_external(
                f"alloc_degraded_{mid}",
                f"{row.get('label') or mid} is running but its model is NOT loaded "
                f"({row.get('detail') or 'readiness probe says not ready'}). "
                "It is serving nothing, or has silently fallen back.",
                severity="critical", ttl=INTERVAL * 1.5, metric="alloc_degraded_count")
        elif state == "absent":
            alerts.push_external(
                f"alloc_absent_{mid}",
                f"{row.get('label') or mid} is declared in alloc.models but not "
                f"installed here ({row.get('detail') or 'driver reports absent'}).",
                severity="warn", ttl=INTERVAL * 1.5)

        # Unfit is only alarming once it persists — see UNFIT_GRACE_S.
        if state == "unfit":
            first = _unfit_since.setdefault(mid, now)
            if now - first >= UNFIT_GRACE_S:
                alerts.push_external(
                    f"alloc_unfit_{mid}",
                    f"{row.get('label') or mid} has not been able to start for "
                    f"{int((now - first) / 60)} min — {row.get('cold_load_reason')}.",
                    severity="warn", ttl=INTERVAL * 1.5, metric="alloc_unfit_count")
        else:
            _unfit_since.pop(mid, None)

        problems = row.get("problems") or []
        if problems:
            more = f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""
            alerts.push_external(
                f"alloc_config_{mid}", f"{problems[0]}{more}",
                severity="warn", ttl=INTERVAL * 1.5)

    _hog_alert(rep, rows)
    _breaker_alerts()
    _fire_due_restores()
    _audit_transitions(states, rows)
    return states


def _fire_due_restores() -> None:
    """Bring back anything the allocator released whose cooldown has lapsed.

    The durable half of the restore pair. A release arms an in-process timer, but a timer
    dies with the process that armed it — and a model left released with nobody holding
    it is the worst state this layer can leave behind, because it looks exactly like an
    outage. This is the net under that.
    """
    try:
        from . import broker
        back = broker.restore_due()
        if back:
            print(f"[alloc-watch] restored {', '.join(back)}", flush=True)
    except Exception as e:  # noqa: BLE001 — the watchdog must never die
        print(f"[alloc-watch] restore pass failed: {e}", flush=True)


def _breaker_alerts() -> None:
    """Surface a model the allocator has given up on, and its own thrash guard.

    Giving up is a dead end until a human acts, so it must be loud and must carry the
    command that clears it. A quiesced allocator is louder still: it means memory is no
    longer being managed at all, which an operator needs to know rather than discover.
    """
    try:
        from . import breaker
        snap = breaker.snapshot()
    except Exception:  # noqa: BLE001 — optional
        return
    if snap.get("quiesced"):
        alerts.push_external(
            "alloc_quiesced",
            f"Model allocation has disabled itself: {snap.get('quiesce_reason')} "
            "Memory is no longer being managed. Investigate, then `ava alloc resume`.",
            severity="critical", ttl=INTERVAL * 1.5)
    for mid, st in (snap.get("models") or {}).items():
        if st.get("given_up"):
            alerts.push_external(
                f"alloc_giveup_{mid}",
                f"Allocation gave up on {mid} after {st.get('fails')} attempts "
                f"(last error: {st.get('reason') or 'unknown'}). It will not be "
                f"retried until you fix it and run `ava alloc reset {mid}`.",
                severity="critical", ttl=INTERVAL * 1.5)


def _hog_alert(rep: dict, rows: list) -> None:
    """Undeclared memory is only worth mentioning when it is blocking something.

    Reporting every idle desktop session as a hog would train an operator to ignore
    the alert, so this requires both a meaningful amount AND a declared model that
    cannot start because of it.
    """
    unknown = (rep.get("pool") or {}).get("unknown_gib")
    if not unknown or unknown < HOG_MIN_GIB:
        return
    blocked = [r.get("label") or r.get("id") for r in rows
               if _wants_start(r) and not r.get("cold_load_ok")]
    if not blocked:
        return
    alerts.push_external(
        "alloc_unknown_hog",
        f"{unknown:.0f} GB is held by processes that are not declared in "
        f"alloc.models, and {', '.join(str(b) for b in blocked[:3])} cannot start. "
        "Declare them to let Ava manage that memory, or stop them.",
        severity="warn", ttl=INTERVAL * 1.5, metric="alloc_unknown_hold_gb",
        value=round(unknown, 1))


def _audit_transitions(states: dict, rows: list) -> None:
    """Record only CHANGES to the durable ledger.

    This is the "what happened at 3 a.m." trail: it has to survive restarts and stay
    readable, so a row per model per cycle would defeat it. `audit.record` is
    best-effort and never raises into the caller.
    """
    by_id = {r.get("id"): r for r in rows}
    with _lock:
        for mid, state in states.items():
            if _last.get(mid) == state:
                continue
            was, _last[mid] = _last.get(mid), state
            row = by_id.get(mid) or {}
            audit.record(
                kind=f"alloc.{state}", model=mid, previous=was,
                driver=row.get("driver"), resident=row.get("resident"),
                resident_gib=row.get("resident_gib"), ready=row.get("ready"),
                detail=row.get("detail"))
        for mid in [m for m in _last if m not in states]:
            _last.pop(mid, None)


def metrics() -> dict:
    """Alert-rule metrics for `dashboard.build_alert_metrics()`.

    Zero when nothing is declared, so the rules in `config/alerts.yaml` stay dormant
    on an install that has not opted in — the same convention the cost budgets use.
    """
    out = {"alloc_degraded_count": 0, "alloc_unfit_count": 0,
           "alloc_unknown_hold_gb": 0}
    try:
        from . import report as _report
        from .spec import load_models
        if not load_models():
            return out
        rep = _report()
        rows = rep.get("models") or []
        out["alloc_degraded_count"] = sum(
            1 for r in rows if _state_of(r) == "degraded")
        out["alloc_unfit_count"] = sum(
            1 for r in rows if _wants_start(r) and not r.get("cold_load_ok"))
        unknown = (rep.get("pool") or {}).get("unknown_gib")
        out["alloc_unknown_hold_gb"] = round(unknown, 1) if unknown else 0
    except Exception:  # noqa: BLE001 — metrics must never break the SSE producer
        pass
    return out


def start_scheduler() -> None:
    """Start the periodic watchdog. No-op when no models are declared."""
    global _started
    if _started:
        return
    try:
        from .spec import enabled, load_models
        if not enabled() or not load_models():
            return  # nothing declared: nothing to watch, nothing to pay for
    except Exception:  # noqa: BLE001 — unreadable config: stay out of the way
        return
    _started = True

    def loop() -> None:
        while True:
            try:
                run_cycle()
            except Exception as e:  # noqa: BLE001 — the watchdog must never die
                print(f"[alloc-watch] cycle failed: {e}", flush=True)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name="alloc-watch", daemon=True).start()
    print(f"[ava-bridge] allocation watchdog: every {INTERVAL:.0f}s", flush=True)
