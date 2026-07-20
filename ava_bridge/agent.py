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
        raise RuntimeError(err)
    return rt.run_turn(text, session_id=session_id, history=history)


def ask_openclaw(text: str, session_id: str | None = None) -> tuple[str, list[str]]:
    """Run a turn specifically through the NemoClaw agent (used on the agent path,
    where the caller has already checked runtime_available())."""
    return runtime.nemoclaw().run_turn(text, session_id=session_id)


def chat_direct(text: str, history: list[dict] | None = None) -> tuple[str, list[str]]:
    """Tool-less direct chat (the degraded floor)."""
    return runtime.direct().run_turn(text, history=history)


def _warm_openclaw():
    """Prime the runtime's cold-start cost (no-op for the direct floor)."""
    runtime.active().warm()


def _sbx_read(inner: str, timeout: int = 20) -> str:
    """Run a command inside the agent sandbox (for live chain-of-thought).

    Uses the CONFIGURED runtime (nemoclaw in-process by default, or the remote
    agent container when `agent.runtime: remote`) so live CoT works on both."""
    return runtime.configured().exec(inner, timeout=timeout)


def _session_file(sid: str) -> str | None:
    return runtime.configured().session_file(sid)


def discard_session(sid: str) -> bool:
    """Erase a conversation's runtime-side memory (ghost mode)."""
    return runtime.active().discard_session(sid)


# ---- Inference router control (independent of the agent runtime) ------------
def _router_headers() -> dict:
    """Auth header for the inference router's control endpoints (/which, /route)."""
    return {"X-Ava-Router-Token": config.ROUTER_TOKEN} if config.ROUTER_TOKEN else {}


def which_model() -> dict | None:
    """Which brain served the most recent completion (for the UI pill).

    The router only knows about completions it proxied. When the NemoClaw agent
    is active, turns run inside the sandbox and never touch the router — so on
    a router miss we fall back to the sandbox's own model (cached in the
    runtime), otherwise the pill goes blank while Ava is plainly answering.
    Best-effort — None only when neither source knows."""
    try:
        r = requests.get(config.ROUTER_WHICH_URL, timeout=3, headers=_router_headers())
        d = r.json() or {}
    except Exception:  # noqa: BLE001 — the pill is purely informational
        d = {}
    if d.get("id"):
        return {"id": d.get("id"), "label": d.get("label"), "model": d.get("model"),
                "prompt_tokens": d.get("prompt_tokens"),
                "total_tokens": d.get("total_tokens")}
    rt = runtime.active()
    if rt.name == "nemoclaw":
        try:
            info = rt.sandbox_info() or {}
        except Exception:  # noqa: BLE001
            info = {}
        model = info.get("model")
        if model:
            return {"id": "agent-sandbox", "label": str(model).split("/")[-1],
                    "model": model, "prompt_tokens": None, "total_tokens": None}
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
