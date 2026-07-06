"""AgentRuntime — the seam between Ava and whatever runs her agent.

Ava's agent (tools, memory, skills, sandboxed execution, egress policies) is
provided by a runtime. The default and recommended runtime is **NemoClaw**
(NVIDIA's Apache-2.0 reference stack that runs OpenClaw inside an OpenShell
sandbox — github.com/NVIDIA/NemoClaw). This interface isolates every
runtime-specific detail behind one class so:

  * the ~15 nemoclaw call sites live in ONE place (ava_bridge/runtime/nemoclaw.py),
  * a version bump or a different backend touches only its adapter,
  * and Ava's core talks to `run_turn` / `exec` / `provision`, never a CLI.

`DirectRuntime` is the graceful floor: when no agent runtime is present it talks
to the inference endpoint directly (a working, tool-less assistant). It is the
explicit opt-out, not a silent default — see config `agent.required`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class AgentRuntime(ABC):
    name: str = "base"
    # Capabilities the shell uses to decide what to render (e.g. live CoT).
    supports_tools: bool = False
    supports_cot: bool = False

    @abstractmethod
    def available(self) -> bool:
        """True if this runtime can serve a turn right now."""

    @abstractmethod
    def run_turn(self, text: str, session_id: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, list[str]]:
        """Run one Ava turn. Returns (reply_text, tools_used).

        A runtime with its own memory (NemoClaw) uses `session_id` and ignores
        `history`; a stateless runtime (Direct) uses `history` and ignores
        `session_id`.
        """

    # ---- optional capabilities (sane no-op defaults) ------------------------
    def warm(self) -> None:
        """Prime any cold-start cost. No-op by default."""

    def discard_session(self, session_id: str) -> bool:
        """Erase a conversation's runtime-side memory (ghost mode). No-op ok."""
        return True

    def exec(self, inner: str, timeout: int = 20) -> str:
        """Run a shell command inside the runtime sandbox; '' if unsupported."""
        return ""

    def session_file(self, session_id: str) -> str | None:
        """Path to the session transcript inside the sandbox (for live CoT)."""
        return None

    def provision(self, auto_install: bool = False) -> dict:
        """Make the runtime ready (install CLI, create sandbox, deploy tools).
        Idempotent. Returns {ok, steps, detail}."""
        return {"ok": True, "steps": [], "detail": "no provisioning needed"}

    def status(self) -> dict:
        """Rich health for `ava doctor` / the ops dashboard."""
        return {"name": self.name, "available": self.available()}
