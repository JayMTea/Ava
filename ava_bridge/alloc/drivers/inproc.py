"""InProcDriver — models loaded inside the bridge's own process.

Every other driver actuates something *outside* the process asking: a container, a
service unit, an engine behind an HTTP port. Ava's voice models are not like that.
`faster_whisper.WhisperModel` and the ECAPA speaker verifier are Python objects on
the bridge's heap, and the only thing that can drop them is the bridge itself.

That single fact decides the whole design:

  * **It works from the bridge and declines everywhere else.** `ava alloc release`
    runs in a different PID and could no more free the bridge's heap than it could
    free another machine's. Rather than pretend, this reports honestly that only the
    running bridge can do it — the same discipline `ObserveDriver` applies to a model
    it cannot reach. The owner's button reaches it because that route *is* the bridge.
  * **`SELF_RESTORING`, with no acquire of its own.** `phone_bridge._ensure_loaded()`
    already runs on every voice turn and rebuilds whatever is None, so the reload half
    exists for free. There is nothing to start, and the ABC's no-op `acquire` is
    exactly right.
  * **Honest about scale.** This is a few hundred MB of HOST RAM — whisper int8 on
    CPU plus one or two ~80 MB ECAPA models — not accelerator memory. On a box whose
    pool reading is VRAM it will not move the number at all, and `release()` says so
    rather than reporting a failure. That is why the broker classifies an
    unmeasurable pool as no-fault: the release worked, the box just cannot see it.

Declared like any other model:

    alloc:
      models:
        ava-voice:
          driver: inproc
          weight_gb: 0.4
          driver_config: { target: voice }
"""
from __future__ import annotations

import threading

from ..base import ActionResult, DriverContext, ModelDriver, ReleaseMode, Residency

# What this driver knows how to drop. Deliberately a closed set with one entry: a
# driver that could dereference an arbitrary name would be a remote-code lever
# wearing a config file, and "declared, never discovered" is meant to bound the
# blast radius to what an operator wrote, not to what they could reach.
_TARGETS = ("voice",)


def _in_bridge() -> bool:
    """Are we inside the process that holds these objects?

    Keyed on the app module being imported, not on a flag someone sets: the bridge
    is `phone_bridge`, and if that module is not in this interpreter then neither is
    the heap the driver would act on.
    """
    import sys
    return "phone_bridge" in sys.modules


class InProcDriver(ModelDriver):
    name = "inproc"
    RELEASE_MODES = (ReleaseMode.UNLOAD,)
    # The engine reloads its own weights on next use — see `_ensure_loaded`.
    SELF_RESTORING = True

    def __init__(self, ctx: DriverContext) -> None:
        super().__init__(ctx)
        self.target = str((ctx.config or {}).get("target") or "voice").strip().lower()

    # --- reporting ----------------------------------------------------------- #
    def residency(self) -> Residency:
        """Whether the objects are loaded. Measured as a boolean, never as bytes.

        `resident` is genuinely knowable — the references are either there or not —
        but `gib` is not: these live inside the PID that also serves the UI, the chat
        stores and the agent runtime, and no reader can sub-attribute a few hundred
        MB within one process. So the declared weight travels as an UNMEASURED hint,
        which is what `measured=False` means, and the planner treats it accordingly.
        """
        if not _in_bridge():
            return Residency(resident=None, gib=self.ctx.weight_gb, measured=False,
                             detail="only the running bridge can see these")
        try:
            from ... import state
        except Exception as e:  # noqa: BLE001 — a probe that raises is "unknown"
            return Residency(resident=None, measured=False, detail=str(e))
        with state.heavy_lock:
            if state.heavy.get("voice_unavailable"):
                # Not "released": the optional deps were never installed, so there is
                # nothing to free and never was. Those are different answers.
                return Residency(resident=False, gib=0.0, measured=True, ready=None,
                                 detail="voice extras are not installed")
            held = [k for k in ("whisper", "verifier")
                    if state.heavy.get(k) is not None]
        return Residency(
            resident=bool(held), gib=self.ctx.weight_gb, measured=False,
            # `ready` is about SERVING, and these serve as soon as they are loaded.
            ready=bool(held) or None,
            detail=(f"holding {', '.join(held)}" if held else "not loaded"))

    # --- actuation ----------------------------------------------------------- #
    def release(self, mode: ReleaseMode, *, need_gib: float | None = None,
                abort: threading.Event | None = None,
                timeout: float = 180.0) -> ActionResult:
        if not _in_bridge():
            # acted=False, so the broker never owes a restore for a release that did
            # not happen. The floor's discipline, for the same reason.
            return ActionResult(
                ok=False, acted=False, freed_gib=None,
                detail="declined: these models live in the bridge's own process, so "
                       "only the running bridge can free them")
        from ... import state
        held = state.release_voice_models()
        dropped = [k for k, v in held.items() if v]
        if not dropped:
            return ActionResult(ok=True, acted=False, freed_gib=None,
                                detail="nothing was loaded")
        # ok=True with freed_gib=None, deliberately. The objects ARE gone — that is
        # observable and true — but how much came back is not measurable from inside
        # one process, and `base.py` is unambiguous that an unmeasurable amount is
        # reported as unknown rather than guessed. Claiming `weight_gb` here would be
        # exactly the estimate that contract forbids.
        return ActionResult(ok=True, acted=True, freed_gib=None,
                            detail=f"dropped {', '.join(sorted(dropped))}")

    # --- config -------------------------------------------------------------- #
    def validate(self) -> list[str]:
        out = []
        if self.target not in _TARGETS:
            out.append(f"alloc.models.{self.ctx.model_id}.driver_config.target "
                       f"is {self.target!r} — known targets: {', '.join(_TARGETS)}")
        return out

    def unusable_reason(self) -> str:
        if not _in_bridge():
            return "only the running bridge can free its own in-process models"
        return ""
