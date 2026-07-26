"""Ava model allocation — fit-checked, declared-model memory management.

A box that runs Ava plus any second model oversubscribes its memory. `model_fit`
already knows how to answer *"would this fit right now"* — including a complete
cold-load check, `fits_now(..., assume_loaded=False)` — but nothing consumed that
answer, and nothing could act on it. This package is the acting half.

It answers two questions, and this module is the whole public surface for both:

  1. `admit(model_id)` -> can this model be brought up right now, and if not, what
     is short. Pure, non-blocking, actuates nothing.
  2. `report()` -> the full picture: the pool, every declared model with its
     measured residency and readiness, everything else that is holding memory, and
     any config problems. Read-only — decides nothing.

  3. `lease(model_id)` -> hold memory for a block of work, stating a *need* rather
     than naming a victim. **Advisory by default** (`alloc.lease.enforce: false`):
     the full decision is computed and recorded, and no driver is touched. See
     `broker.py`.

**What is governable is declared, never discovered.** Only models under
`alloc.models` in `ava.yaml` are ever candidates for actuation. Everything else
holding memory is *observed* — counted, so the planner does not promise memory
someone else has, and named, so an operator can see it — but never touched. Blast
radius is bounded by config, not by heuristics.

**Portability is a hard requirement, not a later concern:**

  - **Discrete GPU** — the HAL reports free VRAM; declared models are measured from
    their own control planes.
  - **Unified memory** (Apple Silicon, Grace-class) — a GPU-memory query returns
    nothing, so the HAL reports the system pool. Per-process residency still works.
  - **CPU-only / no container runtime / unreadable memory** — every driver whose
    tooling is missing degrades to observe-only, and unreadable memory means
    *unknown*, which means **never gate**. Behaviour is exactly what it was before
    this layer existed.

**Absent `alloc:` block means an empty registry**: nothing is declared, so nothing
is governed and nothing changes. An operator opts in one model at a time.

Reads `alloc:` from ava.yaml — see `spec.py` for the full block and
`config.example.yaml` for a commented example.

**Scope.** This package resolves declarations (`spec`), measures capacity
(`capacity`), decides (`policy`), arbitrates across processes (`ledger`), sequences
(`broker`), and probes/actuates one model at a time (`drivers`). It does not
choose which backend serves a request (`model_fit.select`), download weights (the
`models:` manifest), or allocate on another host — a model declared with a `host:`
is reported and never actuated.
"""
from __future__ import annotations

import time

from . import capacity, spec
from .base import DriverContext, Residency
from .broker import admit_plan, enforcing, lease, restore_now
from .drivers import for_spec

__all__ = ["admit", "admit_plan", "lease", "restore_now", "enforcing",
           "report", "residency_of", "drivers_for", "spec", "capacity"]

# `free_gib=None` has to mean "measured, and the answer is UNKNOWN" — that is the
# reading a box with no readable memory source actually produces, and it must admit.
# So "caller did not supply a reading" needs its own value.
_UNSET = object()


def admit(model_id: str, *, free_gib: "float | None" = _UNSET) -> tuple[bool, str]:
    """Could `model_id` be brought up right now? Returns (ok, human reason).

    This is the consumer `model_fit.fits_now(..., assume_loaded=False)` was written
    for and never had. `assume_loaded=False` is the important half: it requires room
    for the model's *weights* as well as its serving headroom, which is exactly the
    question a loader has to answer and a router does not.

    Never blocks and never actuates. Unknown memory admits — a box we cannot measure
    must behave as it did before this layer.
    """
    specs = spec.by_id()
    s = specs.get(model_id)
    if s is None:
        return True, f"{model_id!r} is not declared — not governed"
    if not s.local:
        return True, f"declared on host {s.host!r} — does not use this box's memory"
    free = capacity.free_gib() if free_gib is _UNSET else free_gib
    if free is None:
        return True, "memory unknown — not gated"
    if s.need_gib is None:
        return True, f"weight_gb unknown for {model_id!r} — not gated"

    ok, reason = _fits(s, free)
    if ok:
        return True, reason
    return False, reason


def residency_of(model_id: str) -> Residency:
    """What one declared model is holding, via its driver. Read-only."""
    s = spec.by_id().get(model_id)
    if s is None:
        return Residency(resident=None, detail="not declared")
    return _driver(s).residency()


def drivers_for() -> dict:
    """`{model_id: driver}` for every declared model. Mostly for tests/doctor."""
    return {s.id: _driver(s) for s in spec.load_models()}


def report() -> dict:
    """Everything known about allocation on this box. **Read-only — decides nothing.**

    Shaped like `model_fit.fit_report()` so the two read alike: a `gating` field that
    says plainly whether memory is being used to make decisions, the pool with its
    source, then one row per declared model. `ava doctor` and the dashboard render
    this; nothing consumes it to make a choice.
    """
    specs = spec.load_models()
    rows, holders = [], []
    for s in specs:
        drv = _driver(s)
        res = drv.residency()
        free = capacity.free_gib()
        ok, reason = (True, "not gated")
        if s.local and s.need_gib is not None and free is not None:
            ok, reason = _fits(s, free)
        rows.append({
            "id": s.id, "label": s.label, "driver": drv.name,
            "source": s.source, "implicit": s.implicit,
            "priority": s.priority, "rank": s.rank, "pinned": s.pinned,
            "via": s.via, "host": s.host, "local": s.local,
            "observe_only": drv.observe_only,
            "weight_gb": s.weight_gb, "min_free_gb": s.min_free_gb,
            "tier": s.tier or None,
            "workloads": sorted(s.workloads) if s.workloads else [],
            "resident": res.resident, "resident_gib": res.gib,
            "measured": res.measured, "ready": res.ready,
            "detail": res.detail,
            "cold_load_ok": ok, "cold_load_reason": reason,
            "problems": drv.validate(),
            "notes": list(s.notes),
        })
        if s.local:
            holders.append(capacity.Holder(
                id=s.id, gib=res.gib if res.resident else 0.0,
                measured=res.measured, declared=True, detail=res.detail))

    pool = capacity.snapshot(holders)
    from . import broker as _broker
    return {
        "enabled": spec.enabled(),
        # Named and valued exactly like model_fit.fit_report so the two read alike.
        "gating": "enabled" if pool.readable else "disabled",
        # False = advisory: decisions are computed and recorded, nothing is actuated.
        "actuating": _broker.enforcing(),
        "leases": _broker.snapshot(),
        "platform": pool.platform,
        "pool": {
            "free_gib": _r(pool.free_gib), "total_gib": _r(pool.total_gib),
            "source": pool.source, "baseline_gib": _r(pool.baseline_gib),
            "declared_gib": _r(pool.declared_gib),
            "unknown_gib": _r(pool.unknown_gib),
        },
        "models": rows,
        "declared_count": len(rows),
        "ts": time.time(),
    }


# --- internals --------------------------------------------------------------- #
def _fits(s: "spec.ModelSpec", free: float) -> tuple[bool, str]:
    """Delegate the arithmetic to `model_fit` so there is one implementation.

    Falls back to the same comparison inline when `model_fit` is unavailable, so a
    trimmed fork without the router still gets admission checks.
    """
    try:
        from .. import model_fit
        prof = model_fit.FitProfile(
            backend_id=s.id, weight_gb=s.weight_gb, workloads=s.workloads,
            tier=s.tier or "large", min_free_gb=s.min_free_gb, local=s.local)
        return model_fit.fits_now(prof, free, assume_loaded=False)
    except Exception:  # noqa: BLE001 — model_fit absent; use the same rule inline
        need = s.need_gib or 0.0
        if free >= need:
            return True, f"{free:.0f}GiB free ≥ {need:.0f}GiB needed"
        return False, f"{free:.0f}GiB free < {need:.0f}GiB needed"


def _driver(s: "spec.ModelSpec"):
    ctx = DriverContext(
        model_id=s.id, weight_gb=s.weight_gb,
        config={**s.driver_config, "release": s.release, "restore": s.restore},
        readiness=s.readiness,
        free_gib=capacity.free_gib,
        wait_free=capacity.wait_free,
    )
    return for_spec(s.driver, ctx)


def _r(v):
    return None if v is None else round(float(v), 2)
