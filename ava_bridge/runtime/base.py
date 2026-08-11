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

    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None) -> dict:
        """Make the runtime ready (install CLI, create sandbox, deploy tools).
        Idempotent. Returns {ok, steps, detail, scope}.

        `scope` is one of ava_bridge.provision.ALL_SCOPES: changing a persona
        should not cost a full redeploy of every server, skill and policy.

        `on_line` is an optional `(str) -> None` called with each output line as
        it arrives. A callback rather than a generator because there are three
        implementations — in-process, subprocess line iteration, and an HTTP POST
        — and a callback is the only shape that works identically for all three
        while leaving the return value unchanged for existing callers.
        """
        return {"ok": True, "steps": [], "detail": "no provisioning needed",
                "scope": scope}

    # ---- observation seams (ava_bridge/provision.py) ------------------------
    # Safe no-op defaults on purpose: a runtime that cannot answer yields
    # `source="none"`, which the drift ladder renders as `unknown` — "we could
    # not look" — rather than inventing a verdict or raising.

    def registry_record(self) -> dict | None:
        """This sandbox's entry in the agent runtime's own registry, if any."""
        return None

    def live(self) -> dict:
        """{live, reason} — is the sandbox actually up and reachable right now?

        Distinct from `available()`, which answers "can I serve a turn": a runtime
        can be configured and installed while its container is stopped, and the
        drift report must say so rather than report an empty sandbox.
        """
        return {"live": self.available(), "reason": ""}

    def read_file(self, path: str, timeout: int = 20) -> str | None:
        """Read a file from inside the sandbox. None when unsupported."""
        return None

    def digest(self, paths: list[str], timeout: int = 30) -> dict[str, str]:
        """{path: sha256} for files inside the sandbox. {} when unsupported.

        Takes a LIST so an implementation can answer every path of every scope in
        one round trip rather than one exec per file.
        """
        return {}

    def remove_policy(self, preset: str, timeout: int = 60) -> bool:
        """Withdraw one applied egress policy. False when unsupported.

        The counterpart to the `policy-add` that `agent/install.sh` runs. Ava
        only ever added, so a connector's allowance outlived the connector: the
        manifest went, the generated file went, the audit ledger recorded that
        the security posture had changed — and the sandbox carried on permitting
        the routes until someone rebuilt it.
        """
        return False

    def tree_digests(self, roots: list[str],
                     timeout: int = 30) -> dict[str, str] | None:
        """{root: fold} for whole directories inside the sandbox, comparably to
        `provision.tree_digest()` on the repo side.

        `None` — not `{}` — when this runtime cannot look inside. The two are a
        different answer to the drift ladder: `{}` means "I looked and the
        sandbox holds nothing", which reads as `undeployed` and puts a to-do list
        in front of the owner; `None` means "no evidence source", which reads as
        `unknown` and counts as nothing. A runtime that cannot fold a tree must
        say so rather than imply an empty one.

        Takes a LIST for the same reason `digest()` does: one round trip for
        every server, not one exec per server.
        """
        return None

    def status(self) -> dict:
        """Rich health for `ava doctor` / the ops dashboard."""
        return {"name": self.name, "available": self.available()}
