"""Capacity — how much of this box is free, and who is holding the rest.

The allocator's arithmetic is only as good as its numbers, and on real hardware
most of the numbers on offer are wrong. This module is the one place capacity is
measured, so the rest of the layer cannot pick a convenient source by accident.

**One free-memory oracle: `hwinfo.fit_memory()`.** The HAL already decides what
"free" means per platform — free VRAM on a discrete GPU, the system pool on
unified memory (where a GPU-memory query returns nothing at all), and None when
nothing is readable. Every other candidate is actively misleading:

  * a process-level "free" that excludes reclaimable page cache reads tens of GiB
    low right after any large model load, because loading weights fills that cache;
  * an engine that caches allocations in a CUDA async pool can report ~0 GiB
    reserved while holding tens of GiB of the pool;
  * a GPU-memory query is simply unavailable on unified-memory hardware.

Deciding on any of those produces confident, wrong answers, so this module reads
the HAL and nothing else. A convention guard test enforces that repo-wide.

**Residency is a different question with a different source.** How much is free is
a pool question; who is holding it is a per-process question, and on unified
hardware the per-process compute-apps query still works where the pool query does
not. So residency is asked of each driver (which knows its own control plane) and,
for attribution of everything else, of the existing `hardware` inventory. Neither
is ever used as the free-memory number.

**The baseline is learned, not declared.** Every box carries a floor of memory that
is simply not available to models — the OS, a desktop session, unrelated services.
Hardcoding a number from one machine would make the planner wrong everywhere else,
so `baseline: measure` keeps a rolling minimum of `total - free - declared` and lets
each box discover its own. Samples where a declared model's residency is unmeasured
are discarded, otherwise an unmeasurable model gets folded into the baseline and the
planner under-promises forever.

**Unknown is a residual, never a target.** Whatever is held but not attributable to
a declared model shrinks what the planner believes it can reach, and is surfaced so
an operator can see it. It is never actuated: the allocator only touches what was
declared.

**Portability.** `free_gib is None` means UNKNOWN, and unknown means *do not
gate* — never zero. A box with no readable memory source, no container runtime and
no GPU still gets a working (if coarse) allocator, and behaves exactly as it did
before this layer existed.

**Scope.** Measurement only. This module decides nothing and actuates nothing.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field

from .. import hwinfo, settings

# A learned baseline is only trustworthy within bounds: too small and we would
# promise memory the OS needs, too large and the planner refuses everything. These
# clamps are deliberately wide — they exist to reject nonsense, not to tune.
_BASELINE_MIN_GIB = 1.0
_BASELINE_MAX_FRACTION = 0.5

_lock = threading.RLock()
_baseline_cache: dict = {"gib": None, "ts": 0.0}
_BASELINE_TTL_S = 60.0


@dataclass(frozen=True)
class Holder:
    """One thing holding memory, declared or not."""

    id: str                       # declared model id, or an observed identifier
    gib: float | None             # None = unknown
    measured: bool = False
    declared: bool = False        # False -> observed only, never actuated
    detail: str = ""


@dataclass(frozen=True)
class Pool:
    """A capacity reading. Every field may be None, which always means unknown."""

    free_gib: float | None = None
    total_gib: float | None = None
    source: str | None = None          # e.g. 'vram-nvml' | 'system-psutil' | None
    platform: str | None = None
    baseline_gib: float | None = None
    declared_gib: float = 0.0          # summed residency of declared models
    unknown_gib: float | None = None   # residual: held, unattributed, untouchable
    holders: tuple[Holder, ...] = field(default_factory=tuple)
    ts: float = 0.0

    @property
    def readable(self) -> bool:
        """False means gating is impossible and every request must be admitted."""
        return self.free_gib is not None

    @property
    def plannable_gib(self) -> float | None:
        """Free memory the planner may count on: what is free now, plus what it
        could recover from declared models. Computed by the planner, which knows
        the release plan; this is the floor it starts from."""
        return self.free_gib


def free_gib() -> float | None:
    """Free fit-limiting memory in GiB, or None when nothing is readable.

    The single hardware read in this layer. Deliberately a thin pass-through so
    the HAL stays the only thing that knows about platforms.
    """
    return hwinfo.fit_memory().free_gb


def source() -> str | None:
    """Where `free_gib()` came from, for honest reporting."""
    return hwinfo.fit_memory().source


def accel_unreadable() -> bool:
    """Would freeing accelerator memory be invisible to `free_gib()`?

    True only when a card exists here and no reader can reach it — `free_gib()` is
    then reporting system RAM, so a model released from that card moves it not at
    all. Every other case (VRAM read directly, unified memory, no accelerator) is
    watching the pool the model actually holds.

    Lives here rather than at the call site because this module is the allocator's
    only HAL caller by convention (`tests/test_alloc_measurement.py`), and this is a
    HAL question: it is about what the box can see, not about what to do about it.
    """
    return hwinfo.accel_unreadable()


def snapshot(holders: "list[Holder] | None" = None) -> Pool:
    """A full capacity reading, optionally with residency already gathered.

    `holders` is passed in rather than fetched here so the caller controls how much
    probing happens: a report wants everything, an admission check on the hot path
    wants nothing but the pool.
    """
    mem = hwinfo.fit_memory()
    hs = tuple(holders or ())
    declared = sum(h.gib or 0.0 for h in hs if h.declared)
    base = baseline_gib(mem, hs)
    unknown = None
    if mem.total_gb is not None and mem.free_gb is not None:
        held = mem.total_gb - mem.free_gb
        unknown = max(0.0, held - declared - (base or 0.0))
    return Pool(
        free_gib=mem.free_gb, total_gib=mem.total_gb, source=mem.source,
        platform=hwinfo.platform_id(), baseline_gib=base,
        declared_gib=round(declared, 2),
        unknown_gib=None if unknown is None else round(unknown, 2),
        holders=hs, ts=time.time(),
    )


def wait_free(target_gib: float, *, timeout: float = 90.0, poll: float = 2.0,
              abort: "threading.Event | None" = None) -> bool:
    """Block until free memory reaches `target_gib`. True if it did.

    This is what makes a release verifiable instead of hopeful: the kernel reclaims
    asynchronously, so "we ran the stop command" and "the memory is back" are
    different facts, and only the second one licenses starting something else.

    Returns True immediately when memory is unreadable — with no number to wait on,
    blocking would be an indefinite stall for no benefit, and the caller's own
    behaviour must not depend on a measurement this box cannot make.
    """
    if target_gib is None:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        cur = free_gib()
        if cur is None:
            return True
        if cur >= target_gib:
            return True
        if abort is not None and abort.is_set():
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.25, poll))


# --- learned baseline -------------------------------------------------------- #
def _baseline_path() -> str:
    return os.path.join(settings.data_dir(), "alloc", "baseline.json")


def baseline_gib(mem: "hwinfo.MemInfo | None" = None,
                 holders: "tuple[Holder, ...] | list[Holder]" = ()) -> float | None:
    """This box's non-model memory floor, learned or configured.

    Configured (`alloc.pool.baseline: <GiB>`) wins. Otherwise a rolling minimum is
    kept on disk and refined opportunistically whenever a caller takes a snapshot
    with fully-measured residency. Returns None until enough is known — and None
    simply means the planner shows one fewer number, not that it misbehaves.
    """
    from .spec import pool_config
    configured = pool_config().get("baseline")
    if isinstance(configured, (int, float)):
        return float(configured)

    with _lock:
        if _baseline_cache["gib"] is not None and \
                time.time() - _baseline_cache["ts"] < _BASELINE_TTL_S:
            cached = _baseline_cache["gib"]
        else:
            cached = _read_baseline()
            _baseline_cache.update(gib=cached, ts=time.time())

    sample = _baseline_sample(mem, holders)
    if sample is None:
        return cached
    if cached is None or sample < cached:
        _write_baseline(sample)
        with _lock:
            _baseline_cache.update(gib=sample, ts=time.time())
        return sample
    return cached


def _baseline_sample(mem, holders) -> float | None:
    """One candidate floor: held memory minus what declared models account for.

    Discarded unless EVERY declared holder reported a measured number. A single
    estimated residency would leak that model's weight into the baseline, and since
    the baseline is a running minimum the error would never wash out.
    """
    if mem is None:
        mem = hwinfo.fit_memory()
    if mem.total_gb is None or mem.free_gb is None:
        return None
    declared = [h for h in holders if h.declared]
    if any(not h.measured for h in declared):
        return None
    held = mem.total_gb - mem.free_gb
    sample = held - sum(h.gib or 0.0 for h in declared)
    ceiling = mem.total_gb * _BASELINE_MAX_FRACTION
    if sample < _BASELINE_MIN_GIB or sample > ceiling:
        return None
    return round(sample, 2)


def _read_baseline() -> float | None:
    try:
        with open(_baseline_path(), encoding="utf-8") as fh:
            v = json.load(fh).get("baseline_gib")
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001 — absent/corrupt is simply "not learned yet"
        return None


def _write_baseline(gib: float) -> None:
    """Persist the floor. Atomic, and silent on failure — a read-only data dir is a
    legitimate deployment, and losing a learned number is not worth an exception."""
    path = _baseline_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"baseline_gib": gib, "ts": time.time()}, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 — never fail a render over telemetry
        pass


def reset_baseline() -> None:
    """Forget the learned floor (for `ava alloc calibrate` and for tests)."""
    with _lock:
        _baseline_cache.update(gib=None, ts=0.0)
    try:
        os.unlink(_baseline_path())
    except OSError:
        pass
