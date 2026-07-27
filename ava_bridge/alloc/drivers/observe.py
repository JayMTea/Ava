"""ObserveDriver — knows, never touches. The graceful floor.

Chosen whenever a declared model cannot be actuated here: an unknown driver name, an
adapter whose tooling is missing, a model hosted inside another one (`via:`), or a
model declared on a different host. It mirrors `runtime.DirectRuntime` — the working
floor that keeps the system honest instead of failing.

The distinction that matters: an unresolvable declaration must degrade to
*visibility*, never to a no-op that returns success. A driver that silently reported
"released, 0 GiB freed" would let the planner count memory it never got and start a
model into a pool that was never freed. So `release()` here always declines.

Residency is best-effort from whatever the platform exposes. Reporting `resident=None`
(unknown) is a perfectly good answer and is deliberately never softened to False:
memory we cannot see is memory the planner must not promise to free.
"""
from __future__ import annotations

import threading

from ..base import ActionResult, DriverContext, ModelDriver, ReleaseMode, Residency


class ObserveDriver(ModelDriver):
    name = "observe"
    observe_only = True
    RELEASE_MODES = ()

    def __init__(self, ctx: DriverContext, reason: str = "") -> None:
        super().__init__(ctx)
        self._reason = reason or "observe-only: no actuation configured"

    def residency(self) -> Residency:
        """Unknown unless something cheap and read-only can say otherwise.

        We do not probe a container runtime or a service manager here — this driver
        exists precisely for the cases where that is unavailable or inappropriate.
        The declared weight is reported as an unmeasured hint so a report can still
        show the operator what the model is expected to cost.
        """
        return Residency(
            resident=None,
            gib=self.ctx.weight_gb,
            measured=False,
            ready=None,
            detail=self._reason,
        )

    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 180.0) -> ActionResult:
        # acted=False: the floor never touches anything, so it must never leave
        # the broker owing a restore for a model it did not stop.
        return ActionResult(ok=False, acted=False, freed_gib=None,
                            detail=f"declined: {self._reason}")

    def validate(self) -> list[str]:
        # Not a problem in itself — being observe-only is often correct (a remote
        # model, a checkpoint inside an engine). The reason travels in the report.
        return []

    def unusable_reason(self) -> str:
        return self._reason
