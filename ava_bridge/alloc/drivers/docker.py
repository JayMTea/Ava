"""DockerDriver — a model that lives in a container.

A container has no unload-without-stop lever, so `STOP` is the only mode and its
restore cost is a full weight reload. That is exactly why the planner prefers any
other option first, and why a container-hosted model is usually the *last* thing an
allocator should reach for.

**Absent is not stopped.** `docker inspect` failing means the container was never
created here, which must drop the model from the plan — reporting it as "stopped"
would advertise memory as reclaimable that does not exist. Reporting it as *unknown*
is the honest answer. (A container we stopped ourselves, by contrast, still exists and
must stay governed — which is why the probe distinguishes the two.)

**Restart policy is surfaced, not assumed.** A container supervised with an unbounded
restart policy will fight an allocator: it retries a start the allocator has decided
should not happen, with no backoff cap. A rising restart count is therefore reported as
a first-class signal — an uncapped retry loop against an impossible start is exactly
how a box accumulates thousands of restarts unnoticed.

Config: `driver_config: {container: <name>, stop_timeout_s: 120}`
"""
from __future__ import annotations

import shutil
import subprocess
import threading
import time

from ..base import ActionResult, ModelDriver, ReleaseMode, Residency
from ._probe import probe_ready


class DockerDriver(ModelDriver):
    name = "docker"
    RELEASE_MODES = (ReleaseMode.STOP,)

    @property
    def _name(self) -> str:
        return str(self.ctx.config.get("container") or "").strip()

    def usable(self) -> bool:
        return bool(self._name) and shutil.which("docker") is not None

    def unusable_reason(self) -> str:
        if not self._name:
            return "docker driver needs driver_config.container"
        return "docker not found on PATH"

    def residency(self) -> Residency:
        rc, out = self._docker("inspect", "-f",
                              "{{.State.Running}}|{{.RestartCount}}", self._name,
                              timeout=15.0)
        if rc != 0:
            return Residency(resident=None, gib=self.ctx.weight_gb,
                             detail=f"container {self._name!r} absent")
        running, _, restarts = out.strip().partition("|")
        if running.strip() != "true":
            return Residency(resident=False, gib=0.0, measured=True, ready=False,
                             detail="container exists, stopped")
        gib = self._stats_gib()
        ready = probe_ready(self.ctx.readiness)
        return Residency(
            resident=True, gib=gib if gib is not None else self.ctx.weight_gb,
            measured=gib is not None, ready=ready,
            detail=self._detail(gib, ready, restarts.strip()),
        )

    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 180.0) -> ActionResult:
        t0 = time.monotonic()
        before = self.ctx.free_gib()
        grace = int(_f(self.ctx.config.get("stop_timeout_s"), 120.0))
        rc, out = self._docker("stop", "-t", str(grace), self._name,
                              timeout=grace + 30.0)
        if rc != 0:
            return ActionResult(ok=False, acted=False, detail=f"docker stop failed: {out.strip()[:200]}")
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
                            detail=(f"stopped {self._name}" if freed else
                                    f"stopped {self._name}, but the pool did not reach "
                                    f"the target in time"))

    def acquire(self, *, abort: threading.Event | None = None,
                timeout: float = 600.0) -> ActionResult:
        t0 = time.monotonic()
        rc, out = self._docker("start", self._name, timeout=60.0)
        if rc != 0:
            return ActionResult(ok=False, acted=False, detail=f"docker start failed: {out.strip()[:200]}")
        ok = self.wait_ready(timeout=timeout, abort=abort)
        return ActionResult(ok=ok, seconds=round(time.monotonic() - t0, 2),
                            detail="ready" if ok else "started but not ready")

    def validate(self) -> list[str]:
        problems = super().validate()
        if not self._name:
            return problems
        policy = self._restart_policy()
        if policy in ("always", "unless-stopped"):
            problems.append(
                f"{self.ctx.model_id}: container {self._name!r} has restart policy "
                f"{policy!r} — it will retry starts the allocator declines, with no "
                "backoff cap. Recreate it with `--restart no` so the allocator is the "
                "sole supervisor.")
        if not self.ctx.readiness.get("url"):
            problems.append(
                f"{self.ctx.model_id}: no readiness.url — 'container running' cannot "
                "distinguish a loaded model from one whose warm-up failed")
        return problems

    # --- helpers ------------------------------------------------------------- #
    def _detail(self, gib, ready, restarts: str) -> str:
        bits = ["running"]
        if gib is not None:
            bits.append(f"{gib:.1f} GiB")
        if restarts and restarts.isdigit() and int(restarts) > 0:
            bits.append(f"restarts={restarts}")
        if ready is False:
            bits.append("probe says NOT ready")
        elif ready is None and self.ctx.readiness.get("url"):
            bits.append("probe unreachable")
        return ", ".join(bits)

    def _restart_policy(self) -> str:
        rc, out = self._docker("inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}",
                               self._name, timeout=15.0)
        return out.strip().lower() if rc == 0 else ""

    def _stats_gib(self) -> float | None:
        """What this container is actually holding.

        Asks the accelerator's per-process accounting first, attributed through the
        container's process tree. `docker stats` reports what the *cgroup* is charged
        for, and an inference engine's weights are device allocations that are not
        charged there — measured on the development box, one engine read 3.6 GiB by
        `docker stats` against 43.7 GiB by per-process accounting, a 12x
        under-report. Acting on that number would make the planner refuse work that
        fits and over-release trying to reach a target it had already passed.

        Falls back to `docker stats` where per-process accounting is unavailable (a
        CPU-only box, a non-NVIDIA accelerator), because a cgroup figure is still
        better than nothing for a container whose memory *is* host RAM.
        """
        from . import _gpu
        gib = _gpu.for_tree(self._main_pid())
        if gib is not None:
            return gib
        rc, out = self._docker("stats", "--no-stream", "--format", "{{.MemUsage}}",
                               self._name, timeout=20.0)
        if rc != 0 or not out.strip():
            return None
        return _parse_mem(out.strip().split("/")[0].strip())

    def _main_pid(self) -> int | None:
        """The container's host-visible root pid, for tree attribution."""
        rc, out = self._docker("inspect", "-f", "{{.State.Pid}}", self._name,
                               timeout=15.0)
        try:
            return int(out.strip()) if rc == 0 else None
        except ValueError:
            return None

    def _docker(self, *args: str, timeout: float) -> tuple[int, str]:
        try:
            p = subprocess.run(["docker", *args], capture_output=True, text=True,
                               timeout=timeout)
            return p.returncode, (p.stdout or "") + (p.stderr or "")
        except Exception as e:  # noqa: BLE001 — absent binary/timeout is a state
            return 1, str(e)


_UNITS = {"b": 1 / 1024 ** 3, "kib": 1 / 1024 ** 2, "kb": 1 / 1024 ** 2,
          "mib": 1 / 1024, "mb": 1 / 1024, "gib": 1.0, "gb": 1.0,
          "tib": 1024.0, "tb": 1024.0}


def _parse_mem(s: str) -> float | None:
    """'46.5GiB' / '512MiB' -> GiB. None when unparseable."""
    t = s.strip().lower()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if t.endswith(unit):
            try:
                return round(float(t[: -len(unit)].strip()) * _UNITS[unit], 2)
            except ValueError:
                return None
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
