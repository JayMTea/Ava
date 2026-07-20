"""The single source of truth for a hermetic Ava instance's environment.

Used by conftest.py (in-process, applied to os.environ BEFORE any ava_bridge
import) and bridge_proc.py (subprocess tiers). Pure stdlib — importing this
must never pull in ava_bridge.
"""
import os
import socket

QA_PASSWORD = "qa-password-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hermetic_env(home: str, llm_url: str, gpusvc_url: str, *,
                 port: int | None = None, router_port: int | None = None) -> dict:
    """Env overrides for a fully isolated, model-free, network-free Ava.

    AVA_PASSWORD is set to "" (empty is falsy in auth.current_password) so a
    password in the developer's real .env can't leak in via settings' setdefault
    dotenv loader — the instance genuinely starts in first-run mode.
    """
    env = {
        "AVA_HOME": home,
        "AVA_PASSWORD": "",
        "AVA_AGENT_ENABLED": "0",        # DirectRuntime floor (no NemoClaw)
        "AVA_ROUTER_EMBEDDED": "false",  # no in-process inference router
        "AVA_LEARNING": "0",             # no self-analysis cycles
        "AVA_COOKIE_SECURE": "0",        # tests speak plain http
        "AVA_GPU_REFINE": "0",         # image jobs stop at the base render
        "AVA_ROUTER_CHAT": llm_url,      # DirectRuntime -> fake LLM
        "AVA_GPU_SERVICE": gpusvc_url,          # media jobs -> fake the GPU service
    }
    if port is not None:
        env["AVA_PORT"] = str(port)
    if router_port is not None:
        env["AVA_ROUTER_PORT"] = str(router_port)
    return env


def shims_pythonpath() -> str:
    """PYTHONPATH prefix that blocks the optional voice extras in subprocesses."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "_shims")
