"""NemoClawRuntime — the default agent runtime.

Wraps NVIDIA NemoClaw (Apache-2.0; runs OpenClaw inside an OpenShell sandbox).
NemoClaw is hardware-portable: OpenShell creates the sandbox from a container
image via the local Docker daemon (`nemoclaw onboard` / a community `openclaw`
image), so it runs wherever Docker + the runtime run — not tied to any specific
GPU/box. All CLI specifics (flags, sandbox paths, JSON keys) live here.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import time

from .base import AgentRuntime
from .. import config


def _find_key(obj, key):
    """Depth-first search for the first value of `key` in a nested dict/list."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_key(v, key)
            if found is not None:
                return found
    return None


class NemoClawRuntime(AgentRuntime):
    name = "nemoclaw"
    supports_tools = True
    supports_cot = True

    def __init__(self):
        self._avail_cache = {"ts": 0.0, "ok": None}

    # ---- identity / plumbing ------------------------------------------------
    @property
    def cli(self) -> str:
        return config.OC_NEMOCLAW

    @property
    def sandbox(self) -> str:
        return config.OC_SANDBOX

    def _base(self, *args: str) -> list[str]:
        return [self.cli, self.sandbox, *args]

    # ---- availability -------------------------------------------------------
    def available(self) -> bool:
        """Enabled in config AND the CLI resolves on disk. Cached ~30s so we
        don't stat every turn; an install/removal is picked up within 30s."""
        if not config.AGENT_ENABLED:
            return False
        now = time.time()
        c = self._avail_cache
        if c["ok"] is not None and now - c["ts"] < 30:
            return bool(c["ok"])
        ok = bool(self.cli) and os.path.exists(self.cli)
        c.update(ts=now, ok=ok)
        return ok

    # ---- one turn -----------------------------------------------------------
    def run_turn(self, text: str, session_id: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, list[str]]:
        """Route a turn through `openclaw agent` (NemoClaw owns memory per
        session-id, so we send only the new message). Returns (reply, tools)."""
        sid = session_id or config.OC_SESSION
        # nemoclaw exec mangles newlines in argv, so base64 the message and decode
        # it inside the sandbox — keeping `inner` a single physical line.
        msg_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        inner = (
            f"__AVA_MSG=$(printf %s {shlex.quote(msg_b64)} | base64 -d); "
            f"openclaw agent --agent {shlex.quote(config.OC_AGENT)} "
            f"--session-id {shlex.quote(sid)} "
            f'--message "$__AVA_MSG" '
            f"--thinking {shlex.quote(config.OC_THINKING)} --json 2>/dev/null"
        )
        cp = subprocess.run(
            self._base("exec", "--no-tty", "--", "bash", "-lc", inner),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=config.OC_TIMEOUT,
        )
        out = cp.stdout.decode(errors="ignore")
        start = out.find("{")
        if start < 0:
            raise RuntimeError(f"no JSON from openclaw agent (rc={cp.returncode})")
        data, _ = json.JSONDecoder().raw_decode(out[start:])
        reply = _find_key(data, "finalAssistantVisibleText")
        if not reply:
            reply = _find_key(data, "finalAssistantRawText") or ""
        ts = _find_key(data, "toolSummary")
        tools = ts.get("tools") if isinstance(ts, dict) else None
        return reply.strip(), (tools or [])

    def warm(self) -> None:
        if not self.available():
            return
        try:
            self.run_turn("hi", session_id="ava-phone-warmup")
        except Exception:  # noqa: BLE001
            pass

    # ---- sandbox primitives (used for live chain-of-thought) ---------------
    def exec(self, inner: str, timeout: int = 20) -> str:
        cp = subprocess.run(
            self._base("exec", "--no-tty", "--", "bash", "-lc", inner),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
        )
        return cp.stdout.decode(errors="ignore")

    def session_file(self, session_id: str) -> str:
        return f"/sandbox/.openclaw/agents/{config.OC_AGENT}/sessions/{session_id}.jsonl"

    def discard_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        try:
            self.exec(f"rm -f {shlex.quote(self.session_file(session_id))}", timeout=15)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---- provisioning -------------------------------------------------------
    def _run(self, argv: list[str], timeout: int = 120) -> tuple[int, str]:
        try:
            cp = subprocess.run(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, timeout=timeout)
            return cp.returncode, cp.stdout.decode(errors="ignore")
        except Exception as e:  # noqa: BLE001
            return 1, str(e)

    def _sandbox_exists(self) -> bool:
        if not (self.cli and os.path.exists(self.cli)):
            return False
        rc, out = self._run([self.cli, "list", "--json"], timeout=30)
        if rc != 0:
            return False
        try:
            data = json.loads(out[out.find("["):] or "[]")
            names = [s.get("name") for s in data] if isinstance(data, list) else []
            return self.sandbox in names
        except Exception:  # noqa: BLE001
            return self.sandbox in out

    def provision(self, auto_install: bool = False) -> dict:
        """Make NemoClaw ready, idempotently:
          1. ensure the `nemoclaw` CLI (via NVIDIA's official installer if asked;
             the npm package is a stub — needs Node >=22.16 + a Docker daemon),
          2. ensure the sandbox exists (guides you to `nemoclaw onboard`),
          3. deploy Ava's tools/policies/skills via agent/install.sh.
        Returns {ok, steps, detail}."""
        steps: list[dict] = []

        def step(name, ok, detail):
            steps.append({"step": name, "ok": ok, "detail": detail})
            return ok

        # 1. CLI. NOTE: the npm `nemoclaw` package is an empty stub — the real
        # CLI is NVIDIA's official installer (needs Node >=22.16 + a reachable
        # Docker daemon). The installer also attempts an onboard at the end, so
        # tolerate a non-zero exit and verify by the CLI being present.
        _INSTALL = ("curl -fsSL https://raw.githubusercontent.com/NVIDIA/"
                    "NemoClaw/main/install.sh | bash")
        have_cli = bool(self.cli) and os.path.exists(self.cli)
        if not have_cli and auto_install:
            if _which("curl") and _which("bash"):
                env = {**os.environ, "NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE": "1"}
                cp = subprocess.run(["bash", "-lc", _INSTALL + " || true"],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=900, env=env)
                have_cli = bool(_which("nemoclaw")) or (
                    bool(self.cli) and os.path.exists(self.cli))
                step("install-cli", have_cli,
                     "NemoClaw official installer"
                     + ("" if have_cli else f" — CLI not found after install: "
                        f"{cp.stdout.decode(errors='ignore')[-200:]}"))
            else:
                step("install-cli", False,
                     "curl/bash not found — install them + Node >=22, then run: " + _INSTALL)
        elif not have_cli:
            step("install-cli", False,
                 "nemoclaw not installed. Run the official installer (or pass "
                 "--install), then re-run:\n  " + _INSTALL
                 + "\n(needs Node >=22.16. See github.com/NVIDIA/NemoClaw.)")
        else:
            step("install-cli", True, self.cli)
        if not (bool(self.cli) and os.path.exists(self.cli)) and not _which("nemoclaw"):
            return {"ok": False, "steps": steps, "detail": "nemoclaw CLI missing"}

        # 2. sandbox
        if self._sandbox_exists():
            step("sandbox", True, f"'{self.sandbox}' exists")
        else:
            step("sandbox", False,
                 f"sandbox '{self.sandbox}' not found. Run `nemoclaw onboard` "
                 "(interactive: configures inference endpoint + credentials, then "
                 "creates the sandbox), then re-run provisioning.")
            return {"ok": False, "steps": steps, "detail": "run `nemoclaw onboard`"}

        # 3. deploy tools/policies/skills
        install_sh = os.path.join(config.ROOT, "agent", "install.sh")
        if os.path.exists(install_sh):
            rc, out = self._run(["bash", install_sh], timeout=600)
            step("deploy", rc == 0, "agent/install.sh" + ("" if rc == 0 else f" rc={rc}: {out[-200:]}"))
        else:
            step("deploy", False, "agent/install.sh not found")

        ok = all(s["ok"] for s in steps)
        return {"ok": ok, "steps": steps,
                "detail": "provisioned" if ok else "provisioning incomplete — see steps"}

    def status(self) -> dict:
        cli_ok = bool(self.cli) and os.path.exists(self.cli)
        out = {"name": self.name, "available": self.available(),
               "cli": self.cli if cli_ok else None, "sandbox": self.sandbox,
               "sandbox_exists": None, "health": None}
        if cli_ok:
            out["sandbox_exists"] = self._sandbox_exists()
            rc, txt = self._run([self.cli, self.sandbox, "doctor", "--json"], timeout=30)
            if rc == 0:
                try:
                    out["health"] = json.loads(txt[txt.find("{"):] or "{}")
                except Exception:  # noqa: BLE001
                    out["health"] = {"raw": txt[:400]}
        return out


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)
