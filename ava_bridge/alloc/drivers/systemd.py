"""SystemdDriver — a model served by a systemd unit.

The residency number comes from the unit's **cgroup** (`MemoryCurrent`): exact,
unprivileged, no container runtime required, and available on any Linux with systemd.
That is a genuine measurement, not the declared weight.

**`is-active` is deliberately NOT used for readiness.** A unit goes `active` the
instant its exec succeeds, which for a Python model server is long before any weights
load — and if the model's own warm-up catches an out-of-memory error and lets the HTTP
server bind anyway, `is-active` stays green forever while the model serves nothing.
That failure has been observed in the field lasting days, with the service's own
`/health` reporting the truth the entire time because nothing polled it.

So readiness here is: the declared probe returns 2xx **and** every key in
`readiness.require` is truthy in the JSON body. A service that already reports its own
readiness honestly needs no code change — it just needs something to read it.

    readiness:
      url: "http://127.0.0.1:PORT/health"
      require: [ok, model_ready]     # every key must be truthy
      timeout_s: 5

Config: `driver_config: {unit: <name>.service, scope: user|system}`. `scope` defaults
to `user`, matching how Ava's own units are installed.
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time

from ..base import ActionResult, ModelDriver, ReleaseMode, Residency
from ._probe import probe_ready


class SystemdDriver(ModelDriver):
    name = "systemd"
    RELEASE_MODES = (ReleaseMode.STOP,)

    @property
    def _unit(self) -> str:
        return str(self.ctx.config.get("unit") or "").strip()

    @property
    def _scope(self) -> str:
        return str(self.ctx.config.get("scope") or "user").strip().lower()

    def usable(self) -> bool:
        return bool(self._unit) and shutil.which("systemctl") is not None

    def unusable_reason(self) -> str:
        if not self._unit:
            return "systemd driver needs driver_config.unit"
        return "systemctl not found on PATH"

    def residency(self) -> Residency:
        state = self._systemctl("is-active", self._unit).strip()
        if state in ("", "not-found", "inactive-or-missing"):
            # Absent is not stopped: a unit that was never installed must be dropped
            # from the plan, not counted as memory we could reclaim.
            return Residency(resident=None, gib=self.ctx.weight_gb,
                             detail=f"{self._unit or 'unit'} not installed")
        if state != "active":
            return Residency(resident=False, gib=0.0, measured=True, ready=False,
                             detail=f"unit {state}")
        gib = self._cgroup_gib()
        ready = probe_ready(self.ctx.readiness)
        return Residency(
            resident=True, gib=gib if gib is not None else self.ctx.weight_gb,
            measured=gib is not None, ready=ready,
            detail=self._detail(gib, ready),
        )

    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 180.0) -> ActionResult:
        t0 = time.monotonic()
        before = self.ctx.free_gib()
        rc, out = self._run(["stop", self._unit], timeout=min(120.0, timeout))
        if rc != 0:
            return ActionResult(ok=False, acted=False, detail=f"systemctl stop failed: {out.strip()[:200]}")
        # Verify rather than sleep: the reclaim is asynchronous, and only the
        # measured drop licenses starting something else.
        target = (before or 0.0) + (need_gib or self.ctx.weight_gb or 0.0)
        # ok = did the POOL come back, which is the only thing that licenses
        # starting something else. The boolean was discarded here, so a stop
        # whose memory the kernel never returned reported success — the exact
        # failure base.py's release() contract names. acted=True regardless:
        # it IS down, so we owe the restore either way.
        freed = self.ctx.wait_free(
            target, timeout=max(5.0, timeout - (time.monotonic() - t0)), abort=abort)
        after = self.ctx.free_gib()
        return ActionResult(ok=bool(freed), acted=True, freed_gib=_delta(before, after),
                            seconds=round(time.monotonic() - t0, 2),
                            detail=(f"stopped {self._unit}" if freed else
                                    f"stopped {self._unit}, but the pool did not reach "
                                    f"the target in time"))

    def acquire(self, *, abort: threading.Event | None = None,
                timeout: float = 600.0) -> ActionResult:
        t0 = time.monotonic()
        rc, out = self._run(["start", self._unit], timeout=60.0)
        if rc != 0:
            return ActionResult(ok=False, acted=False, detail=f"systemctl start failed: {out.strip()[:200]}")
        ok = self.wait_ready(timeout=timeout, abort=abort)
        return ActionResult(ok=ok, seconds=round(time.monotonic() - t0, 2),
                            detail="ready" if ok else "started but not ready")

    def validate(self) -> list[str]:
        problems = super().validate()
        if self._unit and not self.ctx.readiness.get("url"):
            problems.append(
                f"{self.ctx.model_id}: no readiness.url — 'unit is active' cannot "
                "distinguish a loaded model from one that failed to load")
        return problems

    # --- helpers ------------------------------------------------------------- #
    def _detail(self, gib, ready) -> str:
        bits = ["active"]
        if gib is not None:
            bits.append(f"{gib:.1f} GiB cgroup")
        if ready is False:
            bits.append("probe says NOT ready")
        elif ready is None and self.ctx.readiness.get("url"):
            bits.append("probe unreachable")
        return ", ".join(bits)

    def _cgroup_gib(self) -> float | None:
        """What this unit is actually holding.

        Per-process accelerator accounting first, attributed through the unit's process
        tree; `MemoryCurrent` second. The cgroup figure is exact for host RAM but blind
        to device allocations, so for a unit serving a model on an accelerator it can
        under-report by an order of magnitude — the same trap `docker stats` sets for a
        container. The cgroup reading remains the right answer for a CPU-side model, and
        the only one available where per-process accounting is not.
        """
        from . import _gpu
        gib = _gpu.for_tree(self._main_pid())
        if gib is not None:
            return gib
        raw = self._systemctl("show", self._unit, "-p", "MemoryCurrent", "--value").strip()
        if raw.isdigit():
            v = int(raw)
            # systemd reports the sentinel for "not accounted"; treat as unknown.
            if v > 0 and v < (1 << 62):
                return round(v / 1024 ** 3, 2)
        return None

    def _main_pid(self) -> int | None:
        raw = self._systemctl("show", self._unit, "-p", "MainPID", "--value").strip()
        try:
            pid = int(raw)
        except ValueError:
            return None
        return pid or None      # 0 means "not running"

    def _systemctl(self, *args: str) -> str:
        rc, out = self._run(list(args), timeout=10.0)
        return out if rc == 0 or out else ""

    def _run(self, args: list[str], *, timeout: float) -> tuple[int, str]:
        cmd = ["systemctl"]
        if self._scope == "user":
            cmd.append("--user")
        cmd += args
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception as e:  # noqa: BLE001 — missing binary/timeout is a state, not a crash
            return 1, str(e)


def _delta(before: float | None, after: float | None) -> float | None:
    if before is None or after is None:
        return None
    return round(max(0.0, after - before), 2)
