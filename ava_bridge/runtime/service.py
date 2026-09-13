"""Vendor-neutral HTTP runtime contract; the operator owns the agent and sandbox."""
from __future__ import annotations

import time
import json
from urllib.parse import urlsplit

import requests

from .base import AgentRuntime
from .errors import GatewayUnsupported, GatewayError
from .. import settings

PROTOCOL = "ava-runtime/1"


class ServiceRuntime(AgentRuntime):
    name = "service"
    display_name = "External agent service"

    def __init__(self):
        self._info: dict = {}
        self._checked = 0.0
        self._fingerprint = ""

    def _base(self) -> str:
        raw = str(settings.get("agent.service.url", "", env="AVA_RUNTIME_URL") or "").rstrip("/")
        if not raw:
            return ""
        url = urlsplit(raw)
        if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("agent.service.url must be an HTTP(S) URL without credentials or query")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            if not settings.get_bool("agent.service.allow_http", False, env="AVA_RUNTIME_ALLOW_HTTP"):
                raise ValueError("Use HTTPS, or explicitly enable agent.service.allow_http on a trusted network")
        return raw

    def _request(self, method: str, path: str, body: dict | None = None):
        base = self._base()
        if not base:
            raise RuntimeError("Configure agent.service.url to connect the runtime")
        token = settings.secret("runtime_service_token", env="AVA_RUNTIME_TOKEN")
        headers = {"Accept": "application/json", "X-Ava-Runtime-Version": PROTOCOL}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = min(600, max(1, settings.get_int("agent.service.timeout_s", 120)))
        with requests.request(method, base + path, json=body, headers=headers,
                              timeout=(5, timeout if method == "POST" else 5),
                              allow_redirects=False, stream=True) as response:
            if response.status_code >= 300:
                raise GatewayError(f"Runtime request failed (HTTP {response.status_code})",
                                   code="gateway_rpc_failed")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 8 * 1024 * 1024:
                    raise ValueError("Runtime response exceeds 8 MiB")
                chunks.append(chunk)
            data = json.loads(b"".join(chunks))
        if not isinstance(data, dict):
            raise ValueError("Runtime response must be an object")
        return data

    def available(self) -> bool:
        try:
            base = self._base()
            if not base:
                return False
            if base != self._fingerprint or time.monotonic() - self._checked > 10:
                self._info = {}
                self._checked = time.monotonic()
                self._fingerprint = base
                info = self._request("GET", "/v1/runtime")
                caps = info.get("capabilities")
                if info.get("protocol") != PROTOCOL or not isinstance(caps, list) or not all(isinstance(c, str) for c in caps):
                    raise ValueError("Runtime does not implement ava-runtime/1")
                self._info = info
            return self._info.get("ready") is True and "turns" in self.capabilities()
        except (requests.RequestException, ValueError, RuntimeError):
            self._info = {}
            return False

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities()

    def capabilities(self) -> list[str]:
        return list(self._info.get("capabilities") or [])

    def _require(self, capability: str) -> None:
        if not self.available():
            raise GatewayError("The configured runtime service is unavailable", code="gateway_unavailable")
        if capability not in self.capabilities():
            raise GatewayUnsupported(self.name)

    def run_turn(self, text: str, session_id=None, history=None):
        self._require("turns")
        result = self._request("POST", "/v1/turns", {"protocol": PROTOCOL,
                               "text": text, "session_id": session_id, "history": history or []})
        reply, used = result.get("reply"), result.get("tools_used", [])
        if not isinstance(reply, str) or not isinstance(used, list) or not all(isinstance(t, str) for t in used):
            raise ValueError("Runtime returned an invalid turn")
        return reply, used

    def discard_session(self, session_id: str) -> bool:
        self._require("sessions.discard")
        return self._request("POST", "/v1/sessions/discard", {"session_id": session_id}).get("ok") is True

    def exec(self, inner: str, timeout: int = 20) -> str:
        self._require("sandbox.exec")
        value = self._request("POST", "/v1/exec", {"command": inner, "timeout_s": timeout}).get("output")
        if not isinstance(value, str):
            raise ValueError("Runtime returned invalid command output")
        return value

    def desired_state(self) -> dict:
        # Operators declare vendor-neutral resource identities and digests.
        raw = settings.get("agent.service.desired", {}) or {}
        if not isinstance(raw, dict):
            raise ValueError("agent.service.desired must be a mapping")
        result = {key: raw.get(key, []) for key in ("persona", "policies", "servers", "skills")}
        if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
               for rows in result.values()):
            raise ValueError("Each desired scope must contain a list of resource objects")
        return result

    def observe(self, want: dict) -> dict | None:
        if "provision.observe" not in self.capabilities():
            return None
        return self._request("POST", "/v1/observe", {"desired": want})

    def provision(self, auto_install=False, scope="all", on_line=None, connector=None):
        self._require("provision")
        result = self._request("POST", "/v1/provision", {"protocol": PROTOCOL,
                               "scope": scope, "connector": connector,
                               "desired": self.desired_state()})
        if type(result.get("ok")) is not bool:
            raise ValueError("Runtime must report whether provisioning succeeded")
        return result

    def is_local(self) -> bool:
        return False

    def status(self) -> dict:
        available = self.available()
        info = self.sandbox_info()
        try:
            url, error = self._base(), ""
        except ValueError:
            url, error = "", "Invalid runtime URL; check agent.service.url"
        return {"name": self.name, "available": available, "protocol": PROTOCOL,
                "url": url, "error": error,
                "capabilities": self.capabilities(), "display_name": self.display_name,
                "credential": "runtime_service_token", "sandbox_model": info["model"],
                "sandbox_provider": info["provider"], "sandbox": info["sandbox"]}

    def sandbox_info(self, wait: bool = False) -> dict:
        """Cached model facts supplied by the service, never a vendor CLI probe."""
        if wait:
            self.available()
        info = self._info.get("model") or {}
        if not isinstance(info, dict):
            info = {}
        return {"model": str(info.get("id") or ""),
                "provider": str(info.get("provider") or ""),
                "sandbox": str(self._info.get("name") or "")}

    def blurb(self) -> str:
        return "Your configured agent service owns inference, tools and sandbox execution."

    def install_hint(self) -> str:
        return "Configure agent.service.url and its runtime_service_token; see docs/RUNTIME_SERVICE.md."
