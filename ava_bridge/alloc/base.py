"""ModelDriver — the seam between the allocator and one kind of thing that holds memory.

A box running Ava plus any second model oversubscribes its memory: two model
servers each want tens of GiB of the same pool, so at most one can be
resident. Freeing that memory means acting on something, and every
*kind* of something is actuated differently — a container is stopped, a service
unit is stopped, an engine with an unload endpoint drops its weights while staying
up, an in-process singleton is dereferenced. This interface isolates each of those
behind one class so:

  * the allocator's planner never branches on model kind,
  * adding support for a new engine is one adapter file plus one config block,
  * and nothing in the core knows the name of any particular model or container.

Two questions every driver must answer, and they map 1:1 onto the two abstract
methods: **`residency()`** — is this model holding memory right now, how much, and
is it actually ready to serve — and **`release(mode)`** — give the memory back.
Everything else has a working default.

**Release is an ordered axis, not a set of verbs.** `UNLOAD` (drop weights, keep
the service up) and `STOP` (stop the process) are the same operation at different
costs, and the planner relies on `UNLOAD` never costing more than `STOP` for the
same model. Encoding that as an ordered enum keeps "try the cheap lever first" in
one place instead of as an `if hasattr(driver, "unload")` at every call site.

**Three contract rules, each learned from a production failure:**

  1. **`release()` returns only once the pool has actually dropped**, and reports
     the *measured* delta in `freed_gib`. Sleeping a few seconds and assuming the
     kernel complied is how an allocator ends up starting a model into memory that
     was never freed. If you cannot measure, say so — do not guess.
  2. **`residency()` prefers a real measurement** from the driver's own control
     plane over the model's declared weight, and sets `measured=False` when it
     falls back. A declared number is a hope; the planner needs to know which it
     has.
  3. **`ready()` is never "the port is open."** An engine binds its socket long
     before its weights are resident, and a service manager reports "active" the
     instant exec succeeds. A model that OOMs during warm-up, catches the
     exception, and serves HTTP anyway is indistinguishable from healthy to a
     liveness probe — and will stay that way for as long as nobody looks.

**Portability.** No driver is required to work everywhere. A driver whose tooling
is missing (no container runtime, no service manager, wrong platform) reports it
from `validate()` and is replaced by the observe-only floor, so the model stays
*visible* rather than becoming a silent no-op the planner mistakes for reclaimable
memory. `resident=None` means UNKNOWN and must never be coerced to False.

**Scope.** A driver actuates one declared model. It does not decide *whether* to
act (that is `alloc.policy`), does not read the memory pool (that is
`alloc.capacity`, which is the only caller of the `hwinfo` HAL), does not download
weights (that is the `models:` manifest), and never touches anything the operator
did not declare.
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum


class ReleaseMode(str, Enum):
    """How to give memory back, cheapest first. Order is meaningful: the planner
    assumes UNLOAD never costs more than STOP for the same model."""

    UNLOAD = "unload"   # drop resident weights, leave the process/service running
    STOP = "stop"       # stop the process/service entirely


@dataclass(frozen=True)
class Residency:
    """What one model is holding right now.

    `resident is None` means UNKNOWN — the probe could not answer. It must never
    be treated as False: memory we cannot see is memory the planner cannot promise
    to free.
    """

    resident: bool | None
    gib: float | None = None        # measured if `measured`, else the declared weight
    measured: bool = False          # True = a real read, not the config number
    ready: bool | None = None       # SERVING-ready, not merely process-up
    detail: str = ""                # one human sentence, for logs and reports

    @property
    def known(self) -> bool:
        return self.resident is not None


@dataclass(frozen=True)
class ReleaseOption:
    """One lever this driver could pull right now, with its costs.

    `costs_known` is load-bearing. Costs are optional in config, so an undeclared
    cost arrives here as 0.0 — and 0.0 would make an expensive action look free,
    which is exactly backwards. The planner will only treat a lever as cheap enough
    to pull speculatively when its cost was actually declared, so a forgotten
    annotation makes the allocator more cautious, never less.
    """

    mode: ReleaseMode
    frees_gib: float | None         # None = unknown yield (still worth trying if cheap)
    release_s: float = 0.0          # wall time until the memory is actually back
    restore_s: float = 0.0          # what undoing this costs
    reversible: bool = True         # can the allocator bring it back unattended?
    costs_known: bool = False       # were release_s/restore_s declared, or defaulted?
    detail: str = ""


@dataclass(frozen=True)
class ReleasePlan:
    """The levers available for one model, cheapest-first.

    `blocked` non-None means do not touch this model, and says why — an
    observe-only model, a missing driver, or a model on another host.
    """

    model_id: str
    options: tuple[ReleaseOption, ...] = ()
    blocked: str | None = None


@dataclass(frozen=True)
class ActionResult:
    """Outcome of one actuation. `freed_gib` is MEASURED, never estimated.

    `ok` and `acted` are different questions, and conflating them strands models.
    `ok` answers "did the pool actually come back" — a release whose memory the
    kernel never returned is a failure, which is the whole point of this layer.
    `acted` answers "did the thing get taken down", which is what decides whether
    we OWE a restore.

    They diverge exactly where it matters: `docker stop` succeeds, the container
    is down, and the pool does not drop within the timeout. That is ok=False,
    acted=True — record the debt, or the model stays stopped forever because
    nothing believes it was us. Set acted=False only when the actuation never
    happened (the command failed, the driver declined).
    """

    ok: bool
    freed_gib: float | None = None
    seconds: float = 0.0
    detail: str = ""
    acted: bool = True


@dataclass
class DriverContext:
    """Everything a driver may use, injected so drivers stay unit-testable.

    Drivers never import `capacity` or `hwinfo` directly: they receive
    `free_gib()` and `wait_free()` here. That keeps the single-hardware-reader
    rule enforceable and lets a test drive a driver with a fake pool.
    """

    model_id: str
    weight_gb: float | None = None
    # EXACTLY the model's `driver_config:` block, verbatim. Core does not write
    # into it. It used to merge the spec's `release`/`restore` blocks in here,
    # which silently clobbered any driver whose own config had keys by those
    # names — while base.py and spec.py both documented it as opaque and
    # author-owned. They are separate fields now.
    config: dict = field(default_factory=dict)      # the model's `driver_config:`
    release: dict = field(default_factory=dict)     # the model's `release:`
    restore: dict = field(default_factory=dict)     # the model's `restore:`
    readiness: dict = field(default_factory=dict)   # the model's `readiness:`
    free_gib: "callable" = lambda: None             # -> float | None
    wait_free: "callable" = lambda target, **kw: False
    log: "callable" = lambda msg: None


class ModelDriver(ABC):
    """One kind of thing that can hold memory on this box."""

    name: str = "base"                              # registry key; matches `driver:`
    RELEASE_MODES: tuple[ReleaseMode, ...] = ()     # what this KIND can do
    observe_only: bool = False                      # True -> reported, never actuated
    # True when the model comes back by itself on next use (an engine that loads
    # weights per request, a lazily-built singleton). Such a driver SHOULD leave
    # acquire() at the no-op default, so validate() must not scold it for that.
    SELF_RESTORING: bool = False

    def __init__(self, ctx: DriverContext) -> None:
        self.ctx = ctx

    # ---- the two questions every driver must answer -------------------------
    @abstractmethod
    def residency(self) -> Residency:
        """Is this model holding memory right now, how much, and is it ready?

        Prefer a measured number from this driver's OWN control plane (container
        stats, a service cgroup, the engine's API) over `ctx.weight_gb`, and set
        `measured=False` when falling back so the planner knows which it has.
        Return `Residency(resident=None, ...)` when the probe cannot answer —
        never False, which would advertise memory as reclaimable that may not be.
        """

    @abstractmethod
    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 180.0) -> ActionResult:
        """Give the memory back, by `mode`.

        Return only once the pool has ACTUALLY dropped (use `ctx.wait_free`) and
        report the measured delta in `freed_gib`. Returning ok=True on a release
        the kernel never honoured is the failure this whole layer exists to
        prevent. Honour `abort` promptly — a higher-priority request is waiting.
        """

    # ---- optional capabilities (sane no-op defaults) ------------------------
    def acquire(self, *, abort: threading.Event | None = None,
                timeout: float = 600.0) -> ActionResult:
        """Bring the model back.

        The no-op default is a correct answer, not a missing implementation: some
        kinds reload themselves on next use (an engine that loads weights per
        request, a lazily-constructed singleton), so there is genuinely nothing to
        start. Drivers that CAN start something override this — and must, if they
        offer a `reversible` release option (`validate()` checks that pairing).
        """
        return ActionResult(ok=True, detail="no start lever; reloads on next use")

    def plan_release(self, need_gib: float | None = None) -> ReleasePlan:
        """The levers available right now, cheapest-first.

        The default derives them from `RELEASE_MODES` and the current residency,
        which is right for almost every driver. Override only for a kind with a
        partial lever (dropping some adapters but not the base weights).
        """
        if self.observe_only:
            return ReleasePlan(self.ctx.model_id, blocked="observe-only")
        if not self.RELEASE_MODES:
            return ReleasePlan(self.ctx.model_id, blocked="driver has no release lever")
        res = self.residency()
        if res.resident is False:
            return ReleasePlan(self.ctx.model_id, blocked="not resident")
        opts = []
        for m in self.RELEASE_MODES:
            rel, rel_known = self._cost(m, "release")
            res_s, res_known = self._cost(m, "restore")
            opts.append(ReleaseOption(
                mode=m, frees_gib=res.gib, release_s=rel, restore_s=res_s,
                costs_known=rel_known and res_known,
                detail="declared costs" if (rel_known and res_known)
                       else "costs not declared — treated as unknown"))
        return ReleasePlan(self.ctx.model_id, options=tuple(opts))

    def ready(self) -> bool | None:
        """SERVING-ready — resident and able to answer, not merely listening.

        The default defers to `residency().ready`, which each driver fills from
        its declared `readiness:` probe. None means unknown.
        """
        return self.residency().ready

    def usable(self) -> bool:
        """Can this adapter actually run on this box right now?

        Checked once at resolution time so an adapter whose tooling is absent is
        swapped for the observe floor *before* anything asks it to act. Default True:
        an adapter with no external dependency (an in-process one) is always usable.
        """
        return True

    def unusable_reason(self) -> str:
        """Why `usable()` is False, for the report. Empty when usable."""
        return ""

    def wait_ready(self, timeout: float = 600.0,
                   abort: threading.Event | None = None) -> bool:
        """Poll `ready()` until true or the deadline. False on timeout/abort."""
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if abort is not None and abort.is_set():
                return False
            if self.ready():
                return True
            time.sleep(min(3.0, max(0.5, timeout / 60.0)))
        return False

    def status(self) -> dict:
        """Read-only view for reports and `ava doctor`. Decides nothing."""
        return {"id": self.ctx.model_id, "driver": self.name,
                "observe_only": self.observe_only,
                "modes": [m.value for m in self.RELEASE_MODES],
                **asdict(self.residency())}

    def validate(self) -> list[str]:
        """Config/tooling problems, for `ava doctor`. Empty list = fine.

        Also self-polices the contract: a driver that advertises a reversible
        release while leaving `acquire()` at the no-op default would strand the
        model down, so that pairing is reported rather than discovered at 3 a.m.
        """
        problems: list[str] = []
        if (self.RELEASE_MODES and not self.SELF_RESTORING
                and type(self).acquire is ModelDriver.acquire
                and any(o.reversible for o in self.plan_release().options)):
            problems.append(
                f"driver {self.name!r} offers a reversible release but neither "
                "implements acquire() nor declares SELF_RESTORING — a released model "
                "could not be brought back")
        return problems

    # ---- helpers ------------------------------------------------------------
    def _cost(self, mode: ReleaseMode, phase: str) -> tuple[float, bool]:
        """Declared cost for a mode -> (seconds, was_declared).

        Costs are declared rather than measured because the planner needs them
        *before* acting, and they are only ever used to ORDER options — so a rough
        number is useful. What is NOT survivable is silently treating an undeclared
        cost as zero: that would make stopping a service look cheaper than an
        engine's unload endpoint and get it pulled speculatively. Hence the flag.
        """
        block = (self.ctx.release if phase == "release" else self.ctx.restore) or {}
        key = f"{mode.value}_s" if phase == "release" else None
        raw = block.get(key) if key else block.get("warm_s", block.get("cold_s"))
        if raw is None:
            return 0.0, False
        try:
            return float(raw), True
        except (TypeError, ValueError):
            return 0.0, False
