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


class RemoteObservabilityTests(unittest.TestCase):
    """The remote runtime can answer the drift probes, and used to answer none.

    `read_file`, `digest`, `glob_digest` and `tree_digests` are each a shell
    command plus parsing, and they lived on NemoClawRuntime — so RemoteRuntime
    inherited four no-ops. On `agent.runtime: remote` the drift report then had
    no evidence source for persona, servers or skills and reported `unknown` for
    all three permanently, with nothing saying why. They live on the base class
    now, over `exec()`, which RemoteRuntime proxies to a shim wrapping the same
    NemoClawRuntime.
    """

    def _rt(self, out: str):
        from ava_bridge.runtime.remote import RemoteRuntime

        rt = RemoteRuntime()
        return rt, mock.patch.object(rt, "exec", return_value=out)

    def test_it_can_read_a_file(self):
        rt, patched = self._rt("hello")
        with patched:
            self.assertEqual(rt.read_file("/sandbox/x"), "hello")

    def test_it_can_digest_paths(self):
        rt, patched = self._rt(f"{'a' * 64}  /sandbox/x\n")
        with patched:
            self.assertEqual(rt.digest(["/sandbox/x"]), {"/sandbox/x": "a" * 64})

    def test_it_can_fold_a_tree(self):
        rt, patched = self._rt(f"{'b' * 64}  /sandbox/s/_server.mjs\n")
        with patched:
            got = rt.tree_digests(["/sandbox/s"])
        self.assertIsInstance(got, dict)
        self.assertIn("/sandbox/s", got)

    def test_an_exec_that_answers_nothing_is_unknown_not_empty(self):
        """The honesty rule survives the move: `{}` would read as "the sandbox
        holds nothing", which is a to-do list."""
        rt, patched = self._rt("")
        with patched:
            self.assertIsNone(rt.tree_digests(["/sandbox/s"]))

    def test_the_direct_floor_still_answers_nothing(self):
        """It has no sandbox; `exec` returns '' and every probe follows."""
        from ava_bridge.runtime.direct import DirectRuntime

        d = DirectRuntime()
        self.assertIsNone(d.read_file("/x"))
        self.assertEqual(d.digest(["/x"]), {})
        self.assertIsNone(d.tree_digests(["/x"]))
