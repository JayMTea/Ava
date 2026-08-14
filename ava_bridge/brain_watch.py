"""Brain drift watchdog: config and reality must not disagree in silence.

The incident this exists for ran from 2026-08-01 to 2026-08-13. A router process
held a model id its engine did not have, so `_rewrite_body` stamped that id onto
every completion and the engine answered 404 to all of them. Twelve days. Nothing
said anything, because:

  * the hardware panel reads the ENGINE, so it showed a green, correctly-named
    brain — it had no idea what the config claimed;
  * the router read the CONFIG and never once asked the engine what it held;
  * the 404 was relayed to the caller with no error code, so even the failing
    requests carried no signal anyone could act on;
  * and nobody had the panel open at the moment it broke anyway.

The first three are fixed where they live (`models.serving_truth`, the hardware
row's `drift` fact, the router's `model_unknown` re-answer). This module is the
fourth: something that looks even when nobody is looking.

**Deliberately not part of `alloc/watch.py`.** That scheduler returns early when
`alloc.models` is empty, and the box this happened on had none declared. Hanging
the brain guard off an unrelated opt-in would put it exactly where it is not
needed and remove it from every install that needs it most.

**Two disagreements, two severities.**

  * `drifted` — CRITICAL. The engine answered and does not hold the configured
    model, so every turn fails.
  * `mismatched` — WARN. Two config surfaces name different models. Turns still
    succeed, because the router rewrites the model id on the way through; what
    breaks is the truth. Seen for real on 2026-08-13: swapping Ava's backend from
    the FP8 build to NVFP4 left the agent sandbox onboarded with the old id
    (`NEMOCLAW_MODEL` is baked into its container), so the hardware panel named a
    model that was no longer served and filed the engine that WAS serving under
    "Ava's other engines". Nothing failed; the display simply lied.

**What it will not do.** It says nothing about `unobservable`, `elsewhere` or
`unconfigured`, and waits a grace cycle before `unreachable`. A watchdog that
pages because an answer could not be obtained is one people switch off, and then
it is not a watchdog at all.

`run_cycle()` takes an injected verdict, so every branch runs with no engine, no
network and no hardware.

Run: .venv/bin/python -m pytest tests/test_brain_watch.py -q
"""
from __future__ import annotations

import os
import threading

from . import alerts

INTERVAL = float(os.environ.get("AVA_BRAIN_WATCH_INTERVAL", "300"))  # 5 min

DRIFT_ALERT = "brain_drift"
UNREACHABLE_ALERT = "brain_unreachable"
MISMATCH_ALERT = "brain_mismatch"

_started = False
#: Verdict from the previous cycle, so `unreachable` gets one grace cycle before
#: it is announced: a restart of the engine (or of Ava) must not page anybody.
_last: str = ""


def _fmt(ids: list[str], limit: int = 3) -> str:
    if not ids:
        return "nothing"
    head = ", ".join(ids[:limit])
    return head + (f" (+{len(ids) - limit} more)" if len(ids) > limit else "")


def run_cycle(truth: dict | None = None) -> dict:
    """One comparison. Returns the verdict dict it acted on."""
    from . import models

    global _last
    t = truth if truth is not None else models.serving_truth()
    verdict = str(t.get("verdict") or "")

    if verdict == "drifted":
        want = t.get("want") or "?"
        alerts.push_external(
            DRIFT_ALERT,
            # Both ids, and both ways out. One id alone is not actionable, and
            # "something is wrong with your model" is how an alert gets ignored.
            f"Ava is set to think with {want}, but the engine at "
            f"{t.get('base_url') or 'its endpoint'} is serving {_fmt(t.get('serving') or [])}. "
            f"Every message fails until they agree — either point Ava at what the "
            f"engine has (Setup → Agent → Brain), or load {want} on the engine.",
            severity="critical", ttl=INTERVAL * 1.5, metric="brain_drift_count",
            value=1)
    elif verdict == "mismatched":
        # Warn, not critical: turns still succeed because the router rewrites the
        # model id. What is broken is the TRUTH — a surface reading the sandbox
        # names a model that is not answering, which is the exact class of lie
        # this module exists to end.
        alerts.push_external(
            MISMATCH_ALERT,
            f"{t.get('detail') or 'two surfaces name different models'}. Chat still "
            f"works — the router rewrites the model id — but anything reading the "
            f"agent sandbox will name a model that is not answering. Re-onboard the "
            f"sandbox onto the current model (`nemoclaw onboard`), or turn the agent "
            f"runtime off until you do.",
            severity="warn", ttl=INTERVAL * 1.5, metric="brain_mismatch_count",
            value=1)
    elif verdict == "unreachable":
        # One grace cycle. An engine restart, a cold load, or Ava starting
        # before its engine are all normal and all transiently unreachable.
        if _last == "unreachable":
            alerts.push_external(
                UNREACHABLE_ALERT,
                f"Ava's brain endpoint {t.get('base_url') or ''} has not answered "
                f"for {int(INTERVAL / 60)} min or more. Chat cannot work until it does.",
                severity="warn", ttl=INTERVAL * 1.5)
    # `unobservable`, `elsewhere`, `unconfigured`, `agrees`: nothing to say.
    # Silence is not evidence of a mismatch, and an unconfigured install is a
    # setup step, not a fault — `ava doctor` and the setup wizard own that.

    _last = verdict
    return t


def metrics() -> dict:
    """Gauges for config/alerts.yaml. Zeroes on an install with no brain.

    Reads the comparison directly rather than through `run_cycle`: a metrics
    scrape must not push alerts as a side effect, or the alert's lifetime would
    depend on how often the dashboard is open.

    Never raises — a gauge that throws takes the dashboard down with it.
    """
    try:
        from . import models
        v = models.serving_truth().get("verdict")
        return {"brain_drift_count": 1 if v == "drifted" else 0,
                "brain_mismatch_count": 1 if v == "mismatched" else 0}
    except Exception:  # noqa: BLE001 — a gauge is never worth an exception
        return {"brain_drift_count": 0, "brain_mismatch_count": 0}


def start_scheduler() -> None:
    """Start the periodic comparison.

    Unconditional, unlike the allocation watchdog: there is no opt-in to check,
    because every install has a brain (or should) and the failure this catches
    needs no configuration to occur.
    """
    global _started
    if _started:
        return
    _started = True

    def loop() -> None:
        import time

        while True:
            try:
                run_cycle()
            except Exception as e:  # noqa: BLE001 — the watchdog must never die
                print(f"[brain-watch] cycle failed: {e}", flush=True)
            time.sleep(INTERVAL)

    threading.Thread(target=loop, name="brain-watch", daemon=True).start()
    print(f"[ava-bridge] brain drift watchdog: every {INTERVAL:.0f}s", flush=True)
