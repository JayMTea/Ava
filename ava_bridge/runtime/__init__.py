"""Agent runtime registry + resolver.

`active()` returns the runtime that should serve a turn right now: the configured
runtime (default NemoClaw) when it's available, else the Direct floor. `nemoclaw()`
and `direct()` return the concrete singletons for provisioning / status regardless
of which is active.

Selection order for a turn:
  agent.enabled? -> nemoclaw.available()? -> NemoClaw, else Direct
  agent.enabled == false                   -> Direct (explicit opt-out)
"""
from __future__ import annotations

from .base import AgentRuntime
from .nemoclaw import NemoClawRuntime
from .direct import DirectRuntime

_nemoclaw = NemoClawRuntime()
_direct = DirectRuntime()

# Registry so a future runtime can be selected by name via config `agent.runtime`.
_REGISTRY: dict[str, AgentRuntime] = {
    "nemoclaw": _nemoclaw,
    "openclaw": _nemoclaw,   # alias — nemoclaw runs openclaw
    "direct": _direct,
    "none": _direct,
}


def nemoclaw() -> NemoClawRuntime:
    return _nemoclaw


def direct() -> DirectRuntime:
    return _direct


def configured() -> AgentRuntime:
    """The runtime the user asked for (config agent.runtime), default nemoclaw."""
    from .. import config
    return _REGISTRY.get(str(config.AGENT_RUNTIME).lower(), _nemoclaw)


def active() -> AgentRuntime:
    """The runtime to actually use for a turn: the configured one if available,
    otherwise the Direct floor."""
    rt = configured()
    if rt is not _direct and not rt.available():
        return _direct
    return rt


_REQUIRED_MSG = (
    "The agent runtime (NemoClaw) is required but isn't available. "
    "Provision it with `ava agent provision --install`, or set `agent.required: "
    "false` in ava.yaml to allow tool-less direct chat."
)


def gate() -> tuple[AgentRuntime, str | None]:
    """Resolve the runtime for a turn AND enforce the `agent.required` policy.

    Returns (runtime, error). When `agent.required` is true and the configured
    runtime is unavailable, `error` is set — callers must surface it instead of
    silently serving a degraded (tool-less) reply. Otherwise error is None and
    the runtime is the one to use (full agent, or the Direct floor when allowed).
    """
    from .. import config
    rt = configured()
    if rt is not _direct and not rt.available():
        if config.AGENT_REQUIRED:
            return _direct, _REQUIRED_MSG
        return _direct, None
    return rt, None

