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
import re
import shlex
import subprocess
import time

from .base import AgentRuntime
from .. import config

try:  # perf_log lives at the repo root; best-effort only (same as router_app).
    import perf_log
except Exception:  # noqa: BLE001
    perf_log = None


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
        self._info_cache = {"ts": 0.0, "info": None}
        self._registry_cache: dict = {"ts": 0.0, "record": None}

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
        """Enabled in config AND the CLI resolves on disk AND the sandbox exists.
        Cached ~30s so we don't probe every turn; an install/removal or an
        `onboard` is picked up within 30s.

        The sandbox check is not optional. `ava agent provision --install` puts the
        CLI on disk without onboarding a sandbox, and with only the CLI checked
        here `runtime.active()` selected this runtime anyway — then every turn ran
        the full ~120s tool timeout and returned the canned "my tools timed out"
        reply. The documented behaviour (degrade to tool-less Direct chat) never
        fired, because the thing gating it reported healthy.

        An indeterminate probe (timeout, CLI error) counts as UNAVAILABLE. That
        looks like the opposite of this codebase's usual "degrade, never brick"
        default, but here the graceful degradation IS Direct: a working tool-less
        assistant beats a runtime that burns two minutes per turn to fail.
        """
        if not config.AGENT_ENABLED:
            return False
        now = time.time()
        c = self._avail_cache
        if c["ok"] is not None and now - c["ts"] < 30:
            return bool(c["ok"])
        ok = bool(self.cli) and os.path.exists(self.cli) and self._sandbox_exists(timeout=10)
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
        _t0 = time.time()
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
        self._log_turn_perf(time.time() - _t0, data)
        return reply.strip(), (tools or [])

    def _log_turn_perf(self, seconds: float, data) -> None:
        """One honest `llm` perf record per agent turn. Agent turns bypass the
        inference router (the historical sole writer of llm records), so
        without this the Vitals throughput/routing panels stay empty forever
        on a default install while the user chats all day. Only facts we have
        are recorded: duration, the sandbox model, and token usage when the
        runtime reports it — nothing fabricated. Never raises into a turn."""
        if perf_log is None:
            return
        try:
            info = self.sandbox_info(wait=False) or {}
            model = info.get("model")
            usage = _find_key(data, "usage")
            usage = usage if isinstance(usage, dict) else {}
            ct = usage.get("completion_tokens") or usage.get("output_tokens")
            pt = usage.get("prompt_tokens") or usage.get("input_tokens")
            perf_log.log_perf(
                "llm",
                served_model=model,
                served_label=(str(model).split("/")[-1] if model else "agent sandbox"),
                gen_seconds=round(seconds, 3),
                source="agent",
                prompt_tokens=pt if isinstance(pt, int) else None,
                completion_tokens=ct if isinstance(ct, int) else None,
                tokens_per_sec=(perf_log.tok_per_sec(ct, seconds)
                                if isinstance(ct, int) else None),
            )
        except Exception:  # noqa: BLE001 — telemetry must never break a turn
            pass

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

    def _sandbox_exists(self, timeout: int = 30) -> bool:
        """Does `self.sandbox` exist? `timeout` is bounded lower by available(),
        which runs this on the turn path and must not stall a reply."""
        if not (self.cli and os.path.exists(self.cli)):
            return False
        rc, out = self._run([self.cli, "list", "--json"], timeout=timeout)
        if rc != 0:
            return False
        try:
            data = json.loads(out[out.find("["):] or "[]")
            names = [s.get("name") for s in data] if isinstance(data, list) else []
            return self.sandbox in names
        except Exception:  # noqa: BLE001
            return self.sandbox in out

    def sandbox_info(self, wait: bool = True) -> dict | None:
        """{model, provider} of THIS sandbox from `nemoclaw list --json` — the
        model the agent actually thinks with. That is decided by `nemoclaw
        onboard`, NOT by ava.yaml's inference block, so the Hub must read it
        from here or "Ava's brain" lies to the user. Cached ~120s; None when
        the CLI can't answer (result cached too, so a dead CLI isn't re-polled
        on every status call).

        ``wait=False`` (hot paths like the public /api/health) never blocks:
        it serves the cache and, when stale, refreshes in a daemon thread."""
        now = time.time()
        c = self._info_cache
        if now - c["ts"] < 120:
            return c["info"]
        if not wait:
            if not c.get("refreshing"):
                c["refreshing"] = True

                def _bg():
                    try:
                        self.sandbox_info(wait=True)
                    finally:
                        c["refreshing"] = False
                import threading
                threading.Thread(target=_bg, daemon=True,
                                 name="nemoclaw-info-refresh").start()
            return c["info"]
        info = None
        if self.cli and os.path.exists(self.cli):
            rc, out = self._run([self.cli, "list", "--json"], timeout=15)
            if rc == 0:
                try:
                    data = json.loads(out[out.find("{"):] or "{}")
                    for s in (data.get("sandboxes") or []):
                        if isinstance(s, dict) and s.get("name") == self.sandbox:
                            # `connected` is the liveness signal live() reads. It
                            # rides along here because this call already parses it
                            # and costs nothing extra.
                            info = {"model": s.get("model"),
                                    "provider": s.get("provider"),
                                    "connected": s.get("connected")}
                            break
                except Exception:  # noqa: BLE001 — a CLI format change must not break status
                    info = None
        c.update(ts=now, info=info)
        return info

    # ---- observation seams (ava_bridge/provision.py) ------------------------
    def registry_record(self) -> dict | None:
        """This sandbox's entry in `~/.nemoclaw/sandboxes.json`.

        NemoClaw's own registry, and the single best provisioning-assert source
        on the box: it carries `customPolicies[]` (each with the full applied
        content, so a policy can be diffed byte-for-byte with no CLI call), plus
        `imageTag`, `model`, `provider` and the nemoclaw/openshell/agent versions.
        Crucially it is readable with the container STOPPED — which is exactly
        when you most want to know what is live.

        30s cache. Never raises: a renamed file or a NemoClaw format change must
        degrade to `unknown`, not break the panel.
        """
        now = time.time()
        c = self._registry_cache
        if now - c["ts"] < 30:
            return c["record"]
        record = None
        home = os.environ.get("NEMOCLAW_HOME") or os.path.expanduser("~/.nemoclaw")
        try:
            with open(os.path.join(home, "sandboxes.json"), encoding="utf-8") as f:
                data = json.load(f)
            entry = (data.get("sandboxes") or {}).get(self.sandbox)
            record = entry if isinstance(entry, dict) else None
        except Exception:  # noqa: BLE001
            record = None
        c.update(ts=now, record=record)
        return record

    def live(self) -> dict:
        """{live, reason} from `nemoclaw list --json`'s `connected` flag.

        DELIBERATELY NOT `nemoclaw <sandbox> status --json`: that command is not
        read-only. Running it restarted the OpenShell Docker gateway and killed a
        host process ("Existing OpenShell Docker-driver gateway is stale;
        restarting… Stopped host openshell-gateway process"). `list --json` is
        cheap, side-effect free, and sandbox_info() already makes the call.
        """
        if not (self.cli and os.path.exists(self.cli)):
            return {"live": False, "reason": "the nemoclaw CLI is not installed"}
        info = self.sandbox_info()
        if info is None:
            return {"live": False, "reason": f"sandbox '{self.sandbox}' not found"}
        connected = info.get("connected")
        if connected is None:
            # Older CLI with no `connected` field — do not guess either way.
            return {"live": bool(self.available()), "reason": ""}
        if connected:
            return {"live": True, "reason": ""}
        return {"live": False,
                "reason": f"the sandbox container for '{self.sandbox}' is not running"}

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

    def _stream(self, argv: list[str], timeout: int, on_line) -> tuple[int, str]:
        """Like `_run`, but hands each output line to `on_line` as it arrives.

        Same line-iteration shape as hub/models.py's model pull: merge stderr,
        text mode, line-buffered, strip ANSI. Used by the provision job so the
        Hub can render progress instead of a spinner.
        """
        ansi = re.compile(r"\x1b\[[0-9;]*m")
        buf: list[str] = []
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
        except Exception as e:  # noqa: BLE001
            return 1, str(e)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = ansi.sub("", line).rstrip()
                buf.append(line)
                if line:
                    try:
                        on_line(line)
                    except Exception:  # noqa: BLE001 — a bad sink must not kill the deploy
                        pass
            rc = proc.wait(timeout=timeout)
        except Exception as e:  # noqa: BLE001
            proc.kill()
            return 1, "\n".join(buf) + f"\n{e}"
        return rc, "\n".join(buf)

    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None) -> dict:
        """Make NemoClaw ready, idempotently:
          1. ensure the `nemoclaw` CLI (via NVIDIA's official installer if asked;
             the npm package is a stub — needs Node >=22.16 + a Docker daemon),
          2. ensure the sandbox exists (guides you to `nemoclaw onboard`),
          3. deploy Ava's tools/policies/skills via agent/install.sh.

        `scope` narrows step 3 to `agent/install.sh --only <scope>`. There is no
        version-skew problem here the way there is for the remote runtime:
        install.sh lives at config.ROOT/agent/install.sh, the same checkout as
        this file, so the flag it is handed is always one it understands.

        Returns {ok, steps, detail, scope}."""
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
            return {"ok": False, "steps": steps, "detail": "nemoclaw CLI missing",
                    "scope": scope}

        # 2. sandbox
        if self._sandbox_exists():
            step("sandbox", True, f"'{self.sandbox}' exists")
        else:
            step("sandbox", False,
                 f"sandbox '{self.sandbox}' not found. Run `nemoclaw onboard` "
                 "(interactive: configures inference endpoint + credentials, then "
                 "creates the sandbox), then re-run provisioning.")
            return {"ok": False, "steps": steps, "detail": "run `nemoclaw onboard`",
                    "scope": scope}

        # 3. deploy tools/policies/skills
        install_sh = os.path.join(config.ROOT, "agent", "install.sh")
        if os.path.exists(install_sh):
            argv = ["bash", install_sh]
            if scope and scope != "all":
                argv += ["--only", scope]
            if on_line is not None:
                rc, out = self._stream(argv, timeout=600, on_line=on_line)
            else:
                rc, out = self._run(argv, timeout=600)
            step("deploy", rc == 0, "agent/install.sh" + ("" if rc == 0
                                                          else f" rc={rc}: {out[-200:]}"))
        else:
            step("deploy", False, "agent/install.sh not found")

        ok = all(s["ok"] for s in steps)
        return {"ok": ok, "steps": steps, "scope": scope,
                "detail": "provisioned" if ok else "provisioning incomplete — see steps"}

    def status(self) -> dict:
        cli_ok = bool(self.cli) and os.path.exists(self.cli)
        info = self.sandbox_info() or {}
        out = {"name": self.name, "available": self.available(),
               "cli": self.cli if cli_ok else None, "sandbox": self.sandbox,
               # What the agent actually thinks with (set by `nemoclaw onboard`)
               # — surfaced so the Hub's brain panel reflects reality.
               "sandbox_model": info.get("model"),
               "sandbox_provider": info.get("provider"),
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
