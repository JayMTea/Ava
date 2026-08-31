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
from .errors import GatewayUnsupported
from .. import config


class RemoteRuntime(AgentRuntime):
    name = "remote"
    display_name = "Remote agent"

    def blurb(self) -> str:
        return ("The full agent, running on another host. The CLI and sandbox "
                "rows describe that machine, not this one.")
    supports_tools = True
    supports_cot = True

    def __init__(self):
        self._avail_cache: dict = {"ts": 0.0, "ok": None, "caps": [], "why": "",
                                   "model": "", "provider": ""}
        # Memoised, never constructed at import: `runtime.configured()` hands back
        # a module-level singleton, and the RPC path and /ws/gateway's fan-out
        # must share ONE client or a subscriber registered on one would never see
        # events dispatched into the other.
        self._cp = None

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
    def is_local(self) -> bool:
        """The whole point of this adapter: the agent is on another host."""
        return False

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
        model = provider = ""
        try:
            r = requests.get(self._url("/healthz"), headers=self._headers(), timeout=3)
            body = (r.json() or {}) if r.ok else {}
            ok = r.ok and bool(body.get("ready"))
            # Rides along on the health probe so capabilities() is free. A shim
            # that predates scoped provisioning has no such key, which is exactly
            # the fail-closed default provision() wants.
            got = body.get("capabilities")
            caps = [str(c) for c in got] if isinstance(got, list) else []
            # Rides along for the same reason capabilities does: this is the one
            # call that already talks to the shim, and `sandbox_info` is read
            # from the public /api/health, which must not add a round trip.
            # A shim that does not report a model leaves these empty, and
            # `sandbox_info` then answers None — "I do not know", which is the
            # truth, rather than a model name we invented.
            model = str(body.get("model") or "").strip()
            provider = str(body.get("provider") or "").strip()
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
        c.update(ts=now, ok=ok, caps=caps, why=why,
                 model=model, provider=provider)
        return ok

    def sandbox_info(self, wait: bool = True) -> dict | None:
        """{model, provider} of the remote agent, or None when it does not say.

        `models.effective_brain()` asks the ACTIVE runtime what it thinks with,
        via `getattr(rt, "sandbox_info", None)`. Without this method that lookup
        returned None, the model id resolved EMPTY, and every surface reading it
        reported "No model is configured, so there is nothing to answer with"
        while the remote agent answered turns normally.

        Served from the /healthz cache `available()` already fills, so the
        public /api/health pays no extra round trip and `wait` is unnecessary.
        """
        c = self._avail_cache
        if not c.get("ts"):
            self.available()          # nothing cached yet; one probe, ~3s bound
            c = self._avail_cache
        model = str(c.get("model") or "").strip()
        if not model:
            return None
        return {"model": model, "provider": str(c.get("provider") or "").strip()}

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

        The shim answers in the CONTAINER's vocabulary (`gateway.proxy`);
        `AgentRuntime.capabilities()` speaks the ADAPTER's (`gateway.rpc`).
        Translate rather than conflate — two names for two questions — so that
        adding a route to the shim can never silently re-partition
        `tests/test_runtime_capability_contract.py` on a network probe.
        """
        self.available()  # refreshes the shared cache
        caps = list(self._avail_cache.get("caps") or [])
        if "gateway.proxy" in caps:
            caps.append("gateway.rpc")
        if "gateway.proxy.events" in caps:
            caps.append("gateway.events")
        return caps

    # ---- control plane ------------------------------------------------------
    def control_plane(self):
        """The gateway client, or None when the agent is switched off.

        MUST NOT PROBE. Four `async def` routes call this, and
        `capabilities()` is a live 3s `requests.get` on a cold cache — calling it
        here would put that stall on the event loop inside `/api/gateway/status`.
        Hand back the client unconditionally and let IT report `phase: "down"`
        with `why_code: "gateway_proxy_unsupported"` once its own poller learns
        the truth.

        Returning a client rather than None on a shim that cannot proxy is also
        what keeps the browser retrying: `gatewayClient.ts` stops redialling
        forever once it sees `unconfigured`, so an owner who then fixes the agent
        container would have to reload the page. `configured: true, phase: down`
        keeps it on its backoff and lets the fix land by itself.
        """
        if not config.AGENT_ENABLED:
            return None
        if self._cp is None:
            from . import remote_gateway
            self._cp = remote_gateway.RemoteGatewayClient()
        return self._cp

    def rpc_methods(self) -> frozenset[str]:
        cp = self.control_plane()
        return cp.methods() if cp is not None else frozenset()

    def rpc(self, method: str, params: dict | None = None, *,
            timeout: float = 30.0, idempotency_key: str | None = None) -> dict:
        cp = self.control_plane()
        if cp is None:
            raise GatewayUnsupported(self.name)
        return cp.rpc(method, params, timeout=timeout,
                      idempotency_key=idempotency_key)

    def subscribe(self, topics=None, *, maxlen: int = 1000):
        cp = self.control_plane()
        if cp is None:
            raise GatewayUnsupported(self.name)
        return cp.subscribe(topics, maxlen=maxlen)

    def translate_event(self, topic: str, payload):
        """Delegate to the gateway runtime's table — never a second copy.

        `openclaw_gw` is the single home for gateway vocabulary (a guard and the
        runtime reference both say so), and its implementation is pure: it reads
        no client state. A copy here would be a table that drifts on the next
        upstream rename, and the symptom would be chat losing its live
        chain-of-thought with nothing erroring.
        """
        from .openclaw_gw import OpenClawGatewayRuntime
        return OpenClawGatewayRuntime.translate_event(self, topic, payload)

    def provision(self, auto_install: bool = False, scope: str = "all",
                  on_line=None, connector: str | None = None) -> dict:
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
        # Its own capability string, for the reason the docstring above gives:
        # the shim reads its body with `.get()` and ignores keys it does not
        # know, so an older container handed `connector` would deploy EVERYTHING
        # and report success — the same silent widening, one narrowing later.
        if connector and "provision.connector" not in self.capabilities():
            return {
                "ok": False, "steps": [], "scope": scope,
                "error_code": "remote_connector_unsupported",
                "detail": ("this agent-runtime container predates per-connector "
                           f"deploys, so it cannot apply just '{connector}'. "
                           "Rebuild it, or apply everything."),
            }
        try:
            body = {"auto_install": auto_install, "scope": scope}
            if connector:
                body["connector"] = connector
            return self._post("/provision", body, timeout=900)
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
