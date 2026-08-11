"""RemoteRuntime — the full agent runtime, across the network.

For the Docker "full agent" path: the bridge container stays clean (no Docker
socket, no nemoclaw CLI) and talks over HTTP to a separate **agent-runtime**
container that owns the nemoclaw CLI + the Docker socket. That container runs
`ava_bridge/agent_runtime_server.py`, a thin shim that wraps the SAME
`NemoClawRuntime` — so this adapter is a faithful network mirror of the local
one, and every existing behaviour (tools, memory, live chain-of-thought via
`exec`/`session_file`) works unchanged, just proxied.

Bare metal is unaffected: this runtime is selected only when
`agent.runtime: remote` (env AVA_AGENT_RUNTIME=remote); the default stays the
in-process NemoClawRuntime.
"""
from __future__ import annotations

import time

import requests

from .base import AgentRuntime
from .. import config


class RemoteRuntime(AgentRuntime):
    name = "remote"
    supports_tools = True
    supports_cot = True

    def __init__(self):
        self._avail_cache: dict = {"ts": 0.0, "ok": None, "caps": [], "why": ""}

    # ---- plumbing -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return config.AGENT_URL.rstrip("/") + path

    def _headers(self) -> dict:
        return {"X-Ava-Agent-Token": config.AGENT_TOKEN} if config.AGENT_TOKEN else {}

    def _post(self, path: str, body: dict, timeout: int):
        r = requests.post(self._url(path), json=body, headers=self._headers(),
                          timeout=timeout)
        r.raise_for_status()
        return r.json()

    # ---- availability -------------------------------------------------------
    def available(self) -> bool:
        """Cached ~15s health probe against the agent shim's /healthz."""
        if not config.AGENT_ENABLED:
            return False
        now = time.time()
        c = self._avail_cache
        if c["ok"] is not None and now - c["ts"] < 15:
            return bool(c["ok"])
        ok = False
        caps: list = []
        # WHY it is not available, kept beside the verdict. This used to be a bare
        # `ok = False`, so a token that does not match between the two containers
        # — the one setup mistake the Docker full-agent profile makes easy — was
        # indistinguishable from a container that had not booted yet, forever, in
        # every surface. The two have nothing in common except the symptom.
        why = ""
        try:
            r = requests.get(self._url("/healthz"), headers=self._headers(), timeout=3)
            body = (r.json() or {}) if r.ok else {}
            ok = r.ok and bool(body.get("ready"))
            # Rides along on the health probe so capabilities() is free. A shim
            # that predates scoped provisioning has no such key, which is exactly
            # the fail-closed default provision() wants.
            got = body.get("capabilities")
            caps = [str(c) for c in got] if isinstance(got, list) else []
            if body.get("authed") is False:
                # /healthz is the one route the shim leaves unauthenticated, so
                # a wrong token used to read as a perfectly healthy agent whose
                # every actual call 401'd. Available means usable.
                ok = False
                why = ("the agent service rejected our token — AVA_AGENT_TOKEN "
                       "must be the same in the bridge and agent containers")
            elif r.status_code in (401, 403):
                why = (f"the agent service rejected our token ({r.status_code}) — "
                       "AVA_AGENT_TOKEN must be the same in the bridge and agent "
                       "containers")
            elif not r.ok:
                why = f"the agent service answered {r.status_code}"
            elif not ok:
                why = "the agent service is up but reports it is not ready yet"
        except Exception as e:  # noqa: BLE001
            ok = False
            why = f"could not reach {config.AGENT_URL}: {type(e).__name__}"
        c.update(ts=now, ok=ok, caps=caps, why=why)
        return ok

    # ---- one turn -----------------------------------------------------------
    def run_turn(self, text: str, session_id: str | None = None,
                 history: list[dict] | None = None) -> tuple[str, list[str]]:
        data = self._post("/run_turn",
                          {"text": text, "session_id": session_id},
                          timeout=config.OC_TIMEOUT)
        return (data.get("reply") or "").strip(), (data.get("tools") or [])

    def warm(self) -> None:
        try:
            self._post("/warm", {}, timeout=config.OC_TIMEOUT)
        except Exception:  # noqa: BLE001
            pass

    # ---- sandbox primitives (live chain-of-thought) -------------------------
    def exec(self, inner: str, timeout: int = 20) -> str:
        try:
            data = self._post("/exec", {"inner": inner, "timeout": timeout},
                              timeout=timeout + 5)
            return data.get("out", "")
        except Exception:  # noqa: BLE001
            return ""

    def session_file(self, session_id: str) -> str | None:
        try:
            data = self._post("/session_file", {"session_id": session_id}, timeout=10)
            return data.get("path")
        except Exception:  # noqa: BLE001
            return None

    def discard_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        try:
            data = self._post("/discard_session", {"session_id": session_id}, timeout=20)
            return bool(data.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    # ---- provisioning / status ---------------------------------------------
    def capabilities(self) -> list[str]:
        """What the agent-runtime container on the other end supports.

        Carried on /healthz, which available() already polls every 15s — so this
        costs no extra round trip — and which is the one route the shim's auth
        middleware skips, so it works independently of token setup.
        """
        self.available()  # refreshes the shared cache
        return list(self._avail_cache.get("caps") or [])

    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None) -> dict:
        """Provision the remote agent, refusing to silently widen the scope.

        The shim reads its body with `body.get(...)` and ignores unknown keys, so
        a container built before scoped provisioning existed would take
        `{"scope": "persona"}`, run a FULL provision — re-pushing every MCP
        server and reinstalling every skill — and report success. The UI would
        then tell the owner their persona was applied, which is true, alongside
        ten minutes of work they did not ask for.

        An old container returns no `capabilities` key, so this fails closed by
        construction, with no version parsing (ava_bridge.version is `0.0.0+dev`
        on a checkout, which makes any semver comparison meaningless).
        """
        if scope and scope != "all" and "provision.scope" not in self.capabilities():
            return {
                "ok": False, "steps": [], "scope": scope,
                "error_code": "remote_scope_unsupported",
                "detail": ("this agent-runtime container predates scoped "
                           "provisioning, so it cannot apply just "
                           f"'{scope}'. Rebuild it, or apply everything."),
            }
        try:
            return self._post("/provision",
                              {"auto_install": auto_install, "scope": scope},
                              timeout=900)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "steps": [], "scope": scope,
                    "detail": f"agent service unreachable: {e}"}

    def registry_record(self) -> dict | None:
        """The remote sandbox's NemoClaw registry entry, when the shim can serve it."""
        if "provision.assert" not in self.capabilities():
            return None
        try:
            return (self._post("/registry_record", {}, timeout=10) or {}).get("record")
        except Exception:  # noqa: BLE001
            return None

    def live(self) -> dict:
        ok = self.available()
        return {"live": ok,
                "reason": "" if ok else (
                    self._avail_cache.get("why")
                    or f"the agent service at {config.AGENT_URL} is not ready")}

    def status(self) -> dict:
        out = {"name": self.name, "available": False, "url": config.AGENT_URL,
               "remote": None}
        try:
            r = requests.get(self._url("/status"), headers=self._headers(), timeout=5)
            if r.ok:
                out["remote"] = r.json()
                out["available"] = bool(out["remote"].get("available"))
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
        return out
