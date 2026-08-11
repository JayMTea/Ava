"""Ava-the-agent — facade over the pluggable agent runtime.

The runtime specifics now live in `ava_bridge/runtime/` (NemoClawRuntime is the
default; DirectRuntime is the tool-less floor). This module keeps the stable
function names the rest of the bridge imports and delegates them to the active
runtime, plus the inference-router control calls (which brain answered, model
picker) which are independent of the agent runtime.
"""
import requests

from . import config, runtime


# ---- Agent runtime seam (delegates to ava_bridge/runtime) -------------------
def runtime_available() -> bool:
    """True when the active runtime is the full agent (tools + live CoT), i.e.
    NOT the direct floor. Callers use this to choose the agent vs. direct path."""
    return runtime.active().supports_tools


def run_turn(text: str, session_id: str | None = None,
             history: list[dict] | None = None) -> tuple[str, list[str]]:
    """Run one turn through the active runtime (agent if available, else direct).
    Honors `agent.required`: raises with a clear message rather than silently
    serving tool-less chat when the required runtime is missing."""
    rt, err = runtime.gate()
    if err:
        # `.code` rides along: turns.py reads it off the exception the same way
        # it reads one off a router failure, and it becomes the chat's fix link.
        exc = RuntimeError(str(err))
        exc.code = getattr(err, "code", "")  # type: ignore[attr-defined]
        raise exc
    return rt.run_turn(text, session_id=session_id, history=history)


def ask_openclaw(text: str, session_id: str | None = None) -> tuple[str, list[str]]:
    """Run a turn specifically through the NemoClaw agent (used on the agent path,
    where the caller has already checked runtime_available())."""
    return runtime.nemoclaw().run_turn(text, session_id=session_id)


def chat_direct(text: str, history: list[dict] | None = None) -> tuple[str, list[str]]:
    """Tool-less direct chat (the degraded floor)."""
    return runtime.direct().run_turn(text, history=history)


def warm_openclaw():
    """Prime the runtime's cold-start cost (no-op for the direct floor)."""
    runtime.active().warm()


def sbx_read(inner: str, timeout: int = 20) -> str:
    """Run a command inside the agent sandbox (for live chain-of-thought).

    Uses the CONFIGURED runtime (nemoclaw in-process by default, or the remote
    agent container when `agent.runtime: remote`) so live CoT works on both."""
    return runtime.configured().exec(inner, timeout=timeout)


def session_file(sid: str) -> str | None:
    return runtime.configured().session_file(sid)


def discard_session(sid: str) -> bool:
    """Erase a conversation's runtime-side memory (ghost mode)."""
    return runtime.active().discard_session(sid)


# ---- Inference router control (independent of the agent runtime) ------------
def _router_headers() -> dict:
    """Auth header for the inference router's control endpoints (/which, /route)."""
    return {"X-Ava-Router-Token": config.ROUTER_TOKEN} if config.ROUTER_TOKEN else {}


def which_model() -> dict | None:
    """What ACTUALLY served the most recent completion (for the UI pill).

    An observation, not a derivation, and the distinction is the point: the
    router reports the model it really proxied, which is the only source that can
    disagree with configuration and be right.

    The router only knows about completions IT proxied, though. When the agent
    runtime is active, turns run inside the sandbox and never reach it — so a
    miss means either "nothing has been asked yet" or "the sandbox is answering".
    That second case is answered by `models.effective_brain()`, the ONE resolver,
    rather than by re-reading `sandbox_info` here: this used to derive the
    sandbox's model independently, which made it a second answer to the same
    question, free to drift from every other surface.

    Best-effort — `None` only when neither source knows, which is a real state
    (a fresh box that has not been asked anything) and not an error.
    """
    try:
        r = requests.get(config.ROUTER_WHICH_URL, timeout=3, headers=_router_headers())
        d = r.json() or {}
    except Exception:  # noqa: BLE001 — the pill is purely informational
        d = {}
    if d.get("id"):
        return {"id": d.get("id"), "label": d.get("label"), "model": d.get("model"),
                "prompt_tokens": d.get("prompt_tokens"),
                "total_tokens": d.get("total_tokens"),
                # This half was OBSERVED: the router proxied it.
                "observed": True}
    from . import models
    try:
        brain = models.effective_brain()
    except Exception:  # noqa: BLE001
        return None
    if brain.get("source") == "agent" and brain.get("model_id"):
        return {"id": "agent-sandbox", "label": brain.get("label") or "",
                "model": brain.get("model_id"),
                "prompt_tokens": None, "total_tokens": None,
                # Nothing measured this turn — it is what the sandbox is
                # CONFIGURED with. Callers that care must not print it as a
                # measurement.
                "observed": False}
    return None


def get_route() -> dict | None:
    """Current model choice + selectable backends (for the chat model dropdown)."""
    try:
        return requests.get(config.ROUTER_ROUTE_URL, timeout=3,
                            headers=_router_headers()).json()
    except Exception:  # noqa: BLE001
        return None


def set_route(mode: str) -> dict | None:
    """Pick which backend the router prefers as primary (a backend id)."""
    try:
        r = requests.post(config.ROUTER_ROUTE_URL, json={"mode": mode}, timeout=3,
                          headers=_router_headers())
        return r.json()
    except Exception:  # noqa: BLE001
        return None
