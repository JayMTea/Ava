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

import hashlib
import shlex
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
                  on_line=None, connector: str | None = None) -> dict:
        """Make the runtime ready (install CLI, create sandbox, deploy tools).
        Idempotent. Returns {ok, steps, detail, scope}.

        `scope` is one or more of ava_bridge.provision.SCOPES, comma-separated,
        or "all": changing a persona should not cost a full redeploy of every
        server, skill and policy.

        `connector` narrows a `policies,servers` run to ONE connected app — its
        generated egress policy, and the one server its tools live in. It is
        deliberately NOT a fifth scope: a scope is a domain of the desired
        manifest (`provision.desired()` has exactly four keys) and a connector's
        material is already enumerated as rows inside two of them. A fifth domain
        would double-count it in every pending total.

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

    # ---- sandbox probes ----------------------------------------------------
    # Implemented HERE, over `exec()`, rather than on NemoClawRuntime.
    #
    # Every one is a shell command plus parsing, so any runtime that can exec
    # into its sandbox can answer them — and RemoteRuntime can: its `exec`
    # proxies to a shim that wraps the same NemoClawRuntime. Living on the
    # concrete adapter, they were inherited as no-ops instead, so on
    # `agent.runtime: remote` the drift report had no evidence source for
    # persona, servers or skills and reported `unknown` for all three
    # permanently, with nothing anywhere saying why. A "faithful network mirror"
    # that cannot answer three of four scopes is not one.

    def read_file(self, path: str, timeout: int = 20) -> str | None:
        out = self.exec(f"cat -- {shlex.quote(path)}", timeout=timeout)
        return out if out else None

    def digest(self, paths: list[str], timeout: int = 30) -> dict[str, str]:
        """{path: sha256} for files inside the sandbox, in ONE exec.

        `sha256sum` prints `<sum>␣␣<path>`; a missing file goes to stderr (dropped)
        so it simply does not appear in the result — which the drift ladder reads
        as `undeployed`, correctly.
        """
        if not paths:
            return {}
        quoted = " ".join(shlex.quote(p) for p in paths)
        out = self.exec(f"sha256sum -- {quoted} 2>/dev/null", timeout=timeout)
        found: dict[str, str] = {}
        for line in (out or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 64:
                found[parts[1].strip()] = parts[0]
        return found

    def glob_digest(self, pattern: str, timeout: int = 30) -> dict[str, str]:
        """Like digest(), but for a shell glob — for skills, whose in-sandbox
        directory is the SKILL.md frontmatter `name:`, not the repo directory."""
        out = self.exec(f"sha256sum -- {pattern} 2>/dev/null", timeout=timeout)
        found: dict[str, str] = {}
        for line in (out or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 64:
                found[parts[1].strip()] = parts[0]
        return found

    def tree_digests(self, roots: list[str],
                     timeout: int = 30) -> dict[str, str] | None:
        """Fold whole sandbox directories the same way `provision.tree_digest()`
        folds the repo side: sorted `relpath\\0sha256` lines, hashed.

        This is the sandbox half of a promise `tree_digest`'s own docstring
        already made — install.sh extracts `tar czf - -C "$src" .` into a fresh
        directory and swaps that over the destination, so the sandbox copy is a
        byte-exact mirror of the source tree and the two folds are comparable. Without this half, the
        repo's TREE digest was being compared against the sandbox's ENTRY-POINT
        digest, which can never match: every server read `stale` forever, so the
        pending count never cleared and the post-apply assert vetoed every
        successful run.

        One exec for every root. A root that does not exist contributes no
        lines and simply does not appear in the result, which the caller reads
        as absent. Empty output is indistinguishable from "the exec did not
        run", so it yields `None` (unknown) rather than an empty mapping that
        would read as "the sandbox holds nothing".

        `find -type f` tests the link itself, so a symlink inside a server dir
        would be folded on the repo side and skipped here — permanent, loud
        drift rather than a silent wrong answer. Nothing ships one today.
        """
        roots = [r.rstrip("/") for r in roots if r]
        if not roots:
            return {}
        quoted = " ".join(shlex.quote(r) for r in roots)
        try:
            out = self.exec(
                f"find {quoted} -type f -exec sha256sum -- {{}} + 2>/dev/null",
                timeout=timeout)
        except Exception:  # noqa: BLE001 — a failed probe is `unknown`, never `{}`
            return None
        if not (out or "").strip():
            return None

        # Longest root first so nested roots attribute to the deepest one.
        order = sorted(roots, key=len, reverse=True)
        lines: dict[str, list[str]] = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or len(parts[0]) != 64:
                continue
            sha, path = parts[0], parts[1].strip()
            for root in order:
                if path.startswith(root + "/"):
                    lines.setdefault(root, []).append(
                        f"{path[len(root) + 1:]}\0{sha}")
                    break
        return {root: hashlib.sha256(
            "\n".join(sorted(rows)).encode("utf-8")).hexdigest()
            for root, rows in lines.items()}

    def remove_policy(self, preset: str, timeout: int = 60) -> bool:
        """Withdraw one applied egress policy. False when unsupported.

        The counterpart to the `policy-add` that `agent/install.sh` runs. Ava
        only ever added, so a connector's allowance outlived the connector: the
        manifest went, the generated file went, the audit ledger recorded that
        the security posture had changed — and the sandbox carried on permitting
        the routes until someone rebuilt it.
        """
        return False

    def status(self) -> dict:
        """Rich health for `ava doctor` / the ops dashboard."""
        return {"name": self.name, "available": self.available()}
