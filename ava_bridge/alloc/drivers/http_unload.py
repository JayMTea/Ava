"""HttpUnloadDriver — one HTTP call that drops resident weights without stopping.

The cheapest lever an allocator can have. An engine that exposes "free your memory"
gives back tens of GiB in seconds and, if it reloads weights per request anyway, costs
nothing to have released — which is why the planner tries levers like this
speculatively, before it considers stopping anything.

**The engine's own memory numbers are not trusted here.** An engine that caches
allocations in a CUDA async pool can truthfully report ~0 GiB *reserved by its
allocator* while holding tens of GiB of the pool, and a "free memory" figure that
excludes reclaimable page cache reads far too low right after any large model load.
Both have been observed misleading by an order of magnitude. So this driver measures
residency from the process that hosts the engine — via a declared `rss_unit` (a systemd
unit) or `rss_container` — and treats the unload endpoint strictly as an **actuator,
never a sensor**.

Config:
    driver_config:
      base: "http://127.0.0.1:PORT"     # ${ENV} expansion is done by the config loader
      path: /free
      json: {unload_models: true, free_memory: true}
      rss_unit: <name>.service          # optional: where to MEASURE residency
      rss_container: <name>             # optional alternative
"""
from __future__ import annotations

import json as _json
import threading
import time
import urllib.error
import urllib.request

from ..base import ActionResult, ModelDriver, ReleaseMode, Residency
from ._probe import probe_ready


class HttpUnloadDriver(ModelDriver):
    name = "http-unload"
    RELEASE_MODES = (ReleaseMode.UNLOAD,)
    # The engine reloads its weights on the next request, so the inherited no-op
    # acquire() is the correct implementation rather than a missing one.
    SELF_RESTORING = True

    @property
    def _url(self) -> str:
        base = str(self.ctx.config.get("base") or "").strip().rstrip("/")
        path = str(self.ctx.config.get("path") or "/free").strip()
        if not base:
            return ""
        return base + (path if path.startswith("/") else "/" + path)

    def usable(self) -> bool:
        return bool(self._url)

    def unusable_reason(self) -> str:
        return "http-unload driver needs driver_config.base"

    def residency(self) -> Residency:
        """Measured from the hosting process when declared; otherwise unknown.

        We cannot see inside the engine, and its own counters are untrustworthy for
        this purpose. Without an `rss_unit`/`rss_container` to measure, the honest
        answer is unknown — and the planner then treats this lever's yield as
        unknown too, which is fine because it is cheap enough to try anyway.
        """
        gib = self._host_gib()
        ready = probe_ready(self.ctx.readiness)
        if gib is None:
            return Residency(resident=None, gib=self.ctx.weight_gb, measured=False,
                             ready=ready,
                             detail="engine up; resident weights not introspectable")
        return Residency(resident=gib > 0.5, gib=gib, measured=True, ready=ready,
                         detail=f"host process holds {gib:.1f} GiB")

    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 60.0) -> ActionResult:
        t0 = time.monotonic()
        before = self.ctx.free_gib()
        payload = self.ctx.config.get("json")
        body = _json.dumps(payload if payload is not None else {}).encode()
        req = urllib.request.Request(
            self._url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 — operator-declared URL
                code = getattr(resp, "status", 200)
        except (urllib.error.URLError, OSError) as e:
            # Unreachable is survivable: the caller proceeds, and the memory wait
            # below simply will not be satisfied by us.
            return ActionResult(ok=False, detail=f"unload endpoint unreachable: {e}")
        if not (200 <= code < 300):
            return ActionResult(ok=False, detail=f"unload returned HTTP {code}")

        # An unload frees an unknown amount, so wait for any measurable improvement
        # rather than a computed target — and cap the wait by the declared cost.
        declared = _f((self.ctx.config.get("release") or {}).get("unload_s"), 10.0)
        self.ctx.wait_free((before or 0.0) + 0.5,
                           timeout=min(timeout, max(5.0, declared * 3)), abort=abort)
        after = self.ctx.free_gib()
        return ActionResult(ok=True, freed_gib=_delta(before, after),
                            seconds=round(time.monotonic() - t0, 2),
                            detail="unloaded")

    # `acquire()` is inherited: an engine with an unload endpoint reloads on next
    # use, so there is genuinely nothing to start.

    def validate(self) -> list[str]:
        problems = super().validate()
        if not (self.ctx.config.get("rss_unit") or self.ctx.config.get("rss_container")):
            problems.append(
                f"{self.ctx.model_id}: no driver_config.rss_unit/rss_container — "
                "residency cannot be measured, so this model's memory is reported as "
                "unknown (the engine's own counters are not trustworthy here)")
        return problems

    # --- helpers ------------------------------------------------------------- #
    def _host_gib(self) -> float | None:
        """Measure the hosting process, not the engine's self-report."""
        unit = str(self.ctx.config.get("rss_unit") or "").strip()
        if unit:
            from .systemd import SystemdDriver
            from ..base import DriverContext
            ctx = DriverContext(model_id=self.ctx.model_id,
                                weight_gb=self.ctx.weight_gb,
                                config={"unit": unit,
                                        "scope": self.ctx.config.get("scope", "user")},
                                free_gib=self.ctx.free_gib,
                                wait_free=self.ctx.wait_free)
            return SystemdDriver(ctx)._cgroup_gib()
        container = str(self.ctx.config.get("rss_container") or "").strip()
        if container:
            from .docker import DockerDriver
            from ..base import DriverContext
            ctx = DriverContext(model_id=self.ctx.model_id,
                                weight_gb=self.ctx.weight_gb,
                                config={"container": container},
                                free_gib=self.ctx.free_gib,
                                wait_free=self.ctx.wait_free)
            return DockerDriver(ctx)._stats_gib()
        return None


def _f(v, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return round(max(0.0, after - before), 2)
