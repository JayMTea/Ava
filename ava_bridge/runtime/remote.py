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
        self._avail_cache = {"ts": 0.0, "ok": None}

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
        try:
            r = requests.get(self._url("/healthz"), headers=self._headers(), timeout=3)
            ok = r.ok and bool((r.json() or {}).get("ready"))
        except Exception:  # noqa: BLE001
            ok = False
        c.update(ts=now, ok=ok)
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
    def provision(self, auto_install: bool = False) -> dict:
        try:
            return self._post("/provision", {"auto_install": auto_install}, timeout=900)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "steps": [], "detail": f"agent service unreachable: {e}"}

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
