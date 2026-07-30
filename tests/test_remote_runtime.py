"""RemoteRuntime tests — drive the adapter against an in-process fake shim.

Proves the bridge<->agent contract (auth header, endpoint shapes, live-CoT
proxying, graceful failure) without any container or network. The fake shim is a
FastAPI app mirroring ava_bridge.agent_runtime_server.
"""
import unittest
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from ava_bridge import config
from ava_bridge.runtime.remote import RemoteRuntime

TOKEN = "shared-agent-token"


def _fake_shim(ready: bool = True) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path != "/healthz":
            if request.headers.get("x-ava-agent-token") != TOKEN:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "ready": ready}

    @app.get("/status")
    def status():
        return {"name": "nemoclaw", "available": ready, "sandbox": "my-assistant"}

    @app.post("/run_turn")
    async def run_turn(request: Request):
        body = await request.json()
        return {"reply": f"echo: {body.get('text')}", "tools": ["get_weather"]}

    @app.post("/exec")
    async def exec_(request: Request):
        body = await request.json()
        return {"out": f"ran: {body.get('inner')}"}

    @app.post("/session_file")
    async def session_file(request: Request):
        body = await request.json()
        return {"path": f"/sandbox/.openclaw/agents/main/sessions/{body.get('session_id')}.jsonl"}

    @app.post("/discard_session")
    async def discard(request: Request):
        return {"ok": True}

    @app.post("/warm")
    def warm():
        return {"ok": True}

    return app


class RemoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.rt = RemoteRuntime()
        self.rt._avail_cache = {"ts": 0.0, "ok": None}
        self.client = TestClient(_fake_shim(ready=True), base_url="http://localhost")
        # Route the adapter's requests.* calls into the TestClient, and pin config.
        self._patches = [
            mock.patch.object(config, "AGENT_URL", "http://agent:9100"),
            mock.patch.object(config, "AGENT_TOKEN", TOKEN),
            mock.patch.object(config, "AGENT_ENABLED", True),
            mock.patch.object(config, "OC_TIMEOUT", 30),
            mock.patch("ava_bridge.runtime.remote.requests.post", self._post),
            mock.patch("ava_bridge.runtime.remote.requests.get", self._get),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    # Translate the adapter's `requests` calls to the TestClient. The adapter
    # expects a requests.Response API (.ok), but TestClient yields httpx
    # responses (.is_success), so wrap them.
    def _rel(self, url):
        return url.replace("http://agent:9100", "")

    @staticmethod
    def _wrap(resp):
        resp.ok = resp.is_success   # requests-style attribute the adapter reads
        return resp

    def _post(self, url, json=None, headers=None, timeout=None):
        return self._wrap(self.client.post(self._rel(url), json=json, headers=headers))

    def _get(self, url, headers=None, timeout=None):
        return self._wrap(self.client.get(self._rel(url), headers=headers))

    def test_available_reads_ready_flag(self):
        self.assertTrue(self.rt.available())

    def test_available_false_when_not_ready(self):
        self.client = TestClient(_fake_shim(ready=False), base_url="http://localhost")
        self.rt._avail_cache = {"ts": 0.0, "ok": None}
        self.assertFalse(self.rt.available())

    def test_run_turn_proxies_and_returns_reply_tools(self):
        reply, tools = self.rt.run_turn("hello", session_id="s1")
        self.assertEqual(reply, "echo: hello")
        self.assertEqual(tools, ["get_weather"])

    def test_exec_proxies_for_live_cot(self):
        self.assertEqual(self.rt.exec("wc -l file"), "ran: wc -l file")

    def test_session_file_proxies(self):
        p = self.rt.session_file("s1")
        self.assertTrue(p.endswith("sessions/s1.jsonl"))

    def test_discard_session(self):
        self.assertTrue(self.rt.discard_session("s1"))
        self.assertFalse(self.rt.discard_session(""))   # empty id short-circuits

    def test_auth_token_sent(self):
        seen = {}

        def spy(url, json=None, headers=None, timeout=None):
            seen["hdr"] = (headers or {}).get("X-Ava-Agent-Token")
            return self._post(url, json=json, headers=headers)

        with mock.patch("ava_bridge.runtime.remote.requests.post", spy):
            self.rt.run_turn("x")
        self.assertEqual(seen["hdr"], TOKEN)

    def test_exec_returns_empty_on_failure(self):
        def boom(*a, **k):
            raise ConnectionError("agent down")
        with mock.patch("ava_bridge.runtime.remote.requests.post", boom):
            self.assertEqual(self.rt.exec("x"), "")
            self.assertIsNone(self.rt.session_file("s1"))

    def test_disabled_when_agent_not_enabled(self):
        with mock.patch.object(config, "AGENT_ENABLED", False):
            self.rt._avail_cache = {"ts": 0.0, "ok": None}
            self.assertFalse(self.rt.available())


if __name__ == "__main__":
    unittest.main()
