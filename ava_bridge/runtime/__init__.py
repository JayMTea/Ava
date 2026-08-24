"""Agent runtime registry + resolver.

`active()` returns the runtime that should serve a turn right now: the configured
runtime (default NemoClaw) when it's available, else the Direct floor. `nemoclaw()`
and `direct()` return the concrete singletons for provisioning / status regardless
of which is active.

Selection order for a turn:
  agent.enabled? -> configured().available()? -> that runtime, else Direct
  agent.enabled == false                      -> Direct (explicit opt-out)

Four adapters ship: `nemoclaw` (the default, drives OpenClaw through its CLI),
`openclaw_gw` (the same OpenClaw over its gateway WebSocket, which is what makes
sessions/cron/devices/approvals reachable), `remote` (nemoclaw in its own
container) and `direct` (the tool-less floor).
"""
from __future__ import annotations

from .base import AgentRuntime
from .nemoclaw import NemoClawRuntime
from .direct import DirectRuntime
from .openclaw_gw import OpenClawGatewayRuntime
from .remote import RemoteRuntime

_nemoclaw = NemoClawRuntime()
_direct = DirectRuntime()
_remote = RemoteRuntime()
_openclaw_gw = OpenClawGatewayRuntime()

# Registry so a runtime can be selected by name via config `agent.runtime`.
#
# `openclaw` STAYS an alias for the CLI adapter. It has meant "nemoclaw runs
# openclaw" since this registry existed, and anyone who set it did so to get
# that. Repointing it at the gateway would change behaviour under people who
# never asked — which is precisely what `name_error()` exists to make loud.
_REGISTRY: dict[str, AgentRuntime] = {
    "nemoclaw": _nemoclaw,
    "openclaw": _nemoclaw,   # alias — nemoclaw runs openclaw (see above)
    "openclaw_gw": _openclaw_gw,  # OpenClaw over its own gateway (WebSocket RPC)
    "remote": _remote,       # nemoclaw in a separate container (Docker full agent)
    "direct": _direct,
    "none": _direct,
}


def nemoclaw() -> NemoClawRuntime:
    return _nemoclaw


def direct() -> DirectRuntime:
    return _direct


def remote() -> RemoteRuntime:
    return _remote


def openclaw_gw() -> OpenClawGatewayRuntime:
    return _openclaw_gw


def configured() -> AgentRuntime:
    """The runtime the user asked for (config agent.runtime), default nemoclaw."""
    from .. import config
    return _REGISTRY.get(str(config.AGENT_RUNTIME).strip().lower(), _nemoclaw)


def name_error() -> str | None:
    """Non-None when `agent.runtime` names something that does not exist.

    The fallback itself is right — a typo must not brick the box — but it was
    SILENT, and selecting a different runtime than the one asked for is not the
    same kind of degradation as running with less. `agent.runtime: nemocalw` ran
    NemoClaw and reported `runtime: nemocalw` back, so the status screen agreed
    with the typo and nothing anywhere disagreed with the owner.
    """
    from .. import config
    want = str(config.AGENT_RUNTIME).strip().lower()
    if want in _REGISTRY:
        return None
    return (f"agent.runtime is {config.AGENT_RUNTIME!r}, which is not a known "
            f"runtime ({', '.join(sorted(_REGISTRY))}). Falling back to nemoclaw.")


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

# The config contradicts itself: `direct` IS the tool-less floor, so asking for it
# and then requiring the full agent cannot both be honoured. Named plainly rather
# than resolved for them — picking a side silently is how this went unnoticed.
_DIRECT_REQUIRED_MSG = (
    "agent.required is true, but agent.runtime is set to the tool-less Direct "
    "floor, which has no sandbox and no tools — those two settings contradict "
    "each other. Set `agent.runtime: nemoclaw` (or `remote`) to get the full "
    "agent, or `agent.required: false` to allow tool-less direct chat."
)


class GateError(str):
    """The gate's message, carrying its machine-readable code.

    A plain `str` subclass so every existing `rt, err = gate()` caller keeps
    working unchanged, while the ones that can use a code read it off the same
    value. The turn dict is returned verbatim by `/api/turn/<id>`, so a code here
    is all it takes to reach `frontend/src/lib/fixes.ts`.

    The agent runtime is the biggest optional capability Ava has, and it was the
    one emitting bare prose: a chat turn that failed because the agent was off or
    unreachable gave the owner an accurate sentence and nowhere to go, while a
    failed web search — a far smaller thing — got a guided fix link.
    """

    code: str

    def __new__(cls, message: str, code: str) -> "GateError":
        obj = super().__new__(cls, message)
        obj.code = code
        return obj


def gate() -> tuple[AgentRuntime, GateError | None]:
    """Resolve the runtime for a turn AND enforce the `agent.required` policy.

    Returns (runtime, error). When `agent.required` is true and the configured
    runtime is unavailable, `error` is set — callers must surface it instead of
    silently serving a degraded (tool-less) reply. Otherwise error is None and
    the runtime is the one to use (full agent, or the Direct floor when allowed).

    Codes follow the feature-registry convention (`ava_bridge/features.py`):
    `agent_off` when the switch is deliberately off, `agent_down` when it is on
    and the runtime will not answer, `agent_conflict` for two settings that
    cannot both be honoured.
    """
    from .. import config
    rt = configured()
    if rt is _direct:
        # The explicit-Direct case. This branch used to fall straight through to
        # `return rt, None`, so `agent.required: true` was silently void for
        # anyone who had also set `agent.runtime: direct` — the runtime docs
        # promise "a hard, actionable error at startup, in `ava doctor`, and on
        # every chat turn", and they got tool-less chat with nothing said.
        return _direct, (GateError(_DIRECT_REQUIRED_MSG, "agent_conflict")
                         if config.AGENT_REQUIRED else None)
    if not rt.available():
        if config.AGENT_REQUIRED:
            from .. import features
            # `agent_off` and `agent_down` are different problems with different
            # fixes — a switch to flip vs a runtime to provision — and the owner
            # gets told which one they have.
            off = features.preflight("agent")
            if off is not None:
                return _direct, GateError(
                    f"{off[1]} It is also marked required (agent.required), so "
                    "chat cannot fall back to the tool-less floor.", off[0])
            return _direct, GateError(_REQUIRED_MSG, "agent_down")
        return _direct, None
    return rt, None

