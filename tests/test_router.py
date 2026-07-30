"""Router proxy tests — app factory + mocked backends (no network, no GPU).

Covers the failover proxy's contract: model rewrite, backend ordering/failover,
engine adapters (vLLM reasoning kwargs, stream_options usage), control-endpoint
auth, /v1 bearer auth, tools-capability skipping, env-declared backends and
api-key injection. Backend ordering by hardware fit is covered separately in
test_model_fit.py.
"""
import json
import unittest
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from ava_bridge import router_app


def _backend(bid="b1", url="http://backend-a/v1", engine="vllm",
             model="test-model", tools="native", api_key=None):
    return {"id": bid, "url": url, "model": model, "label": bid,
            "engine": engine, "api_key": api_key, "tools": tools, "fit": None}


def _mock_client(app_kwargs, handler):
    """Build a router app whose upstream calls hit `handler` (MockTransport)."""
    transport = httpx.MockTransport(handler)
    app = router_app.create_app(transport=transport, **app_kwargs)
    return TestClient(app, base_url="http://localhost")


def _chat_body(**extra):
    return {"model": "whatever", "messages": [{"role": "user", "content": "hi"}],
            **extra}


_OK_COMPLETION = {"choices": [{"message": {"content": "hello"}}],
                  "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                            "total_tokens": 5}}


def _resp(status: int, obj: dict | None = None) -> httpx.Response:
    """MockTransport response whose body is still streamable — the proxy relays
    with aiter_raw(), and a Response built with json=/content= arrives already
    consumed (httpx.StreamConsumed)."""
    body = json.dumps(obj if obj is not None else _OK_COMPLETION).encode()
    return httpx.Response(status, stream=httpx._content.ByteStream(body),
                          headers={"content-type": "application/json"})


class ProxyTests(unittest.TestCase):
    def test_proxy_rewrites_model_to_backend(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _resp(200)

        client = _mock_client({"backends": [_backend()], "token": "tok"}, handler)
        r = client.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen["body"]["model"], "test-model")
        self.assertEqual(r.headers["x-ava-backend"], "b1")

    def test_failover_on_503_then_success(self):
        def handler(request):
            if request.url.host == "backend-a":
                return _resp(503, {"error": "loading"})
            return _resp(200)

        backends = [_backend("a", "http://backend-a/v1"),
                    _backend("b", "http://backend-b/v1")]
        client = _mock_client({"backends": backends, "token": "tok"}, handler)
        r = client.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-ava-backend"], "b")
        which = client.get("/which", headers={"X-Ava-Router-Token": "tok"})
        self.assertEqual(which.json()["id"], "b")

    def test_all_backends_down_returns_503_router_unavailable(self):
        def handler(request):
            raise httpx.ConnectError("down", request=request)

        backends = [_backend("a"), _backend("b", "http://backend-b/v1")]
        client = _mock_client({"backends": backends, "token": "tok"}, handler)
        r = client.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.json()["error"]["type"], "router_unavailable")

    def test_reasoning_kwargs_injected_only_for_vllm(self):
        seen = {}

        def handler(request):
            seen[request.url.host] = json.loads(request.content)
            return _resp(200)

        with mock.patch.dict("os.environ", {"AVA_REASONING": "budget:128"}):
            client = _mock_client(
                {"backends": [_backend("v", "http://vllm-host/v1", "vllm")],
                 "token": "tok"}, handler)
            client.post("/v1/chat/completions", json=_chat_body())
            client2 = _mock_client(
                {"backends": [_backend("o", "http://ollama-host/v1", "ollama")],
                 "token": "tok"}, handler)
            client2.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(seen["vllm-host"].get("chat_template_kwargs"),
                         {"reasoning_budget": 128})
        self.assertNotIn("chat_template_kwargs", seen["ollama-host"])

    def test_caller_reasoning_kwargs_not_overridden(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _resp(200)

        client = _mock_client({"backends": [_backend()], "token": "tok"}, handler)
        client.post("/v1/chat/completions",
                    json=_chat_body(chat_template_kwargs={"enable_thinking": True}))
        self.assertEqual(seen["body"]["chat_template_kwargs"],
                         {"enable_thinking": True})

    def test_stream_options_usage_injected_per_engine(self):
        seen = {}

        def handler(request):
            seen[request.url.host] = json.loads(request.content)
            return _resp(200)

        for engine, host in (("ollama", "ollama-host"), ("llamacpp", "lcp-host")):
            client = _mock_client(
                {"backends": [_backend("x", f"http://{host}/v1", engine)],
                 "token": "tok"}, handler)
            client.post("/v1/chat/completions", json=_chat_body(stream=True))
        self.assertEqual(seen["ollama-host"].get("stream_options"),
                         {"include_usage": True})
        self.assertNotIn("stream_options", seen["lcp-host"])

    def test_workload_hint_stripped_from_upstream_body(self):
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return _resp(200)

        client = _mock_client({"backends": [_backend()], "token": "tok"}, handler)
        client.post("/v1/chat/completions", json=_chat_body(workload="fast"))
        self.assertNotIn("workload", seen["body"])

    def test_tools_request_skips_tools_none_backend(self):
        def handler(request):
            return _resp(200)

        backends = [_backend("plain", "http://backend-a/v1", "llamacpp",
                             tools="none"),
                    _backend("toolful", "http://backend-b/v1", "vllm")]
        client = _mock_client({"backends": backends, "token": "tok"}, handler)
        r = client.post("/v1/chat/completions",
                        json=_chat_body(tools=[{"type": "function",
                                                "function": {"name": "f"}}]))
        self.assertEqual(r.headers["x-ava-backend"], "toolful")
        # Without tools, the primary (first) backend serves as usual.
        r2 = client.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(r2.headers["x-ava-backend"], "plain")

    def test_api_key_env_injected_as_bearer_and_router_cred_stripped(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return _resp(200)

        client = _mock_client(
            {"backends": [_backend(api_key="cloud-key")], "token": "tok"},
            handler)
        # The caller authenticates to the ROUTER with its token; upstream must
        # see the backend's own key, never the router credential.
        client.post("/v1/chat/completions", json=_chat_body(),
                    headers={"Authorization": "Bearer tok"})
        self.assertEqual(seen["auth"], "Bearer cloud-key")


class ControlAuthTests(unittest.TestCase):
    def _client(self, **kw):
        def handler(request):
            return _resp(200)
        return _mock_client({"backends": [_backend()], "token": "tok", **kw},
                            handler)

    def test_control_endpoints_require_token(self):
        client = self._client()
        for path in ("/which", "/route", "/fit"):
            self.assertEqual(client.get(path).status_code, 401)
            self.assertNotEqual(
                client.get(path, headers={"X-Ava-Router-Token": "tok"})
                .status_code, 401)

    def test_control_accepts_bearer_form(self):
        client = self._client()
        r = client.get("/which", headers={"Authorization": "Bearer tok"})
        self.assertNotEqual(r.status_code, 401)

    def test_v1_open_on_loopback_default(self):
        client = self._client()   # require_v1_auth resolves False (loopback)
        r = client.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(r.status_code, 200)

    def test_v1_requires_bearer_when_auth_enabled(self):
        client = self._client(require_v1_auth=True)
        r = client.post("/v1/chat/completions", json=_chat_body())
        self.assertEqual(r.status_code, 401)
        for hdr in ({"Authorization": "Bearer tok"},
                    {"X-Ava-Router-Token": "tok"}):
            ok = client.post("/v1/chat/completions", json=_chat_body(),
                             headers=hdr)
            self.assertEqual(ok.status_code, 200)

    def test_healthz_always_open(self):
        client = self._client(require_v1_auth=True)
        self.assertEqual(client.get("/healthz").status_code, 200)

    def test_route_switch(self):
        def handler(request):
            return _resp(200)
        backends = [_backend("a"), _backend("b", "http://backend-b/v1")]
        client = _mock_client({"backends": backends, "token": "tok"}, handler)
        hdr = {"X-Ava-Router-Token": "tok"}
        r = client.post("/route", json={"mode": "b"}, headers=hdr)
        # persisted:false is the honesty flag — a route pick lives in router
        # memory only and reverts on restart.
        self.assertEqual(r.json(), {"ok": True, "mode": "b", "persisted": False})
        r = client.post("/route", json={"mode": "nope"}, headers=hdr)
        self.assertEqual(r.status_code, 400)


class LoadBackendsTests(unittest.TestCase):
    def test_env_backend_fallback(self):
        env = {"AVA_BACKEND_URL": "http://ollama:11434/v1",
               "AVA_BACKEND_ENGINE": "ollama", "AVA_BACKEND_MODEL": "llama3.2"}
        with mock.patch.dict("os.environ", env), \
                mock.patch("ava_bridge.settings._CFG", {}):
            out = router_app.load_backends()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["engine"], "ollama")
        self.assertEqual(out[0]["url"], "http://ollama:11434/v1")
        self.assertEqual(out[0]["model"], "llama3.2")

    def test_legacy_omni_fallback_without_config_or_env(self):
        with mock.patch.dict("os.environ", {}, clear=False), \
                mock.patch("ava_bridge.settings._CFG", {}):
            # Ensure no env backend bleeds in from the developer's shell.
            import os
            os.environ.pop("AVA_BACKEND_URL", None)
            out = router_app.load_backends()
        self.assertEqual(out[0]["id"], "omni")
        self.assertEqual(out[0]["engine"], "vllm")

    def test_config_backends_ordered_primary_first(self):
        cfg = {"inference": {
            "primary": "second",
            "backends": {
                "first": {"base_url": "http://a/v1", "model": "m1"},
                "second": {"base_url": "http://b/v1", "model": "m2",
                           "engine": "ollama", "tools": "none"},
            }}}
        with mock.patch("ava_bridge.settings._CFG", cfg):
            out = router_app.load_backends()
        self.assertEqual([b["id"] for b in out], ["second", "first"])
        self.assertEqual(out[0]["tools"], "none")
        self.assertEqual(out[1]["engine"], "openai")  # default engine


class RewriteBodyModelTests(unittest.TestCase):
    """The router must FILL IN the model, not only overwrite one that is there.

    The regression: `_rewrite_body` guarded the assignment with
    `if "model" in obj`, so a caller that omitted the field had its body
    forwarded verbatim and the upstream engine answered
    400 {"error": {"message": "model is required"}}.

    DirectRuntime — the tool-less floor every install runs until the agent
    sandbox is provisioned — posts exactly {"messages": [...], "stream": false}.
    So on a fresh clone the first chat message returned
    `400 Client Error: Bad Request for url: .../v1/chat/completions`, on every
    profile. Found by booting the cpu profile from a clean checkout and actually
    sending a message; no unit or QA test covered it, because both mock the
    router rather than proxy through it.
    """

    BACKEND = {"id": "backend", "url": "http://ollama:11434/v1",
               "model": "llama3.2", "engine": "ollama", "label": "llama3.2"}

    def _rewrite(self, body, backend=None, is_comp=True):
        raw = json.dumps(body).encode()
        out = router_app._rewrite_body(raw, True, backend or self.BACKEND, "", is_comp)
        return json.loads(out)

    def test_missing_model_is_filled_in_for_completions(self):
        out = self._rewrite({"messages": [{"role": "user", "content": "hi"}],
                             "stream": False})
        self.assertEqual(out["model"], "llama3.2")

    def test_present_model_is_still_forced_to_the_backend(self):
        out = self._rewrite({"model": "something-else", "messages": []})
        self.assertEqual(out["model"], "llama3.2")

    def test_non_completion_routes_get_no_invented_model(self):
        out = self._rewrite({"input": "x"}, is_comp=False)
        self.assertNotIn("model", out)

    def test_backend_without_a_model_leaves_the_caller_alone(self):
        """An empty backend model means 'serves whatever is asked'.

        The old code rewrote a valid model id to "" for any yaml backend
        declared without a `model:` key, which fails upstream the same way.
        """
        b = dict(self.BACKEND, model="")
        out = self._rewrite({"model": "caller-choice", "messages": []}, backend=b)
        self.assertEqual(out["model"], "caller-choice")


if __name__ == "__main__":
    unittest.main()
