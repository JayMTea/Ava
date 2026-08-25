"""sdk/host/ava_mcp — the adapter that turns an ava-tools/1 facade into a REAL
MCP server.

End-to-end, over a live socket, using Ava's own MCP client as the harness: if
`ava_bridge.mcp_client` can `initialize` -> `tools/list` -> `tools/call`
against it, then any MCP client can, which is the whole claim.

The tier assertion is the load-bearing one. Plain MCP has no `access` field, so
a naive facade->MCP port silently demotes every `read` tool to `write` and the
owner starts getting consent prompts for things that used to run silently. The
adapter carries the tier through; this test fails if that ever regresses.
"""
import json
import pathlib
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


import pytest

from ava_bridge import mcp_client

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "sdk/host"))
from ava_mcp import FacadeSource, serve_mcp

# The module-level asserts below write REAL grants/tier-cache entries; without
# isolation they land in the shared conftest AVA_HOME and the five-category
# `connectors.orphans()` (2026-08 audit) then reports them as stale state in
# whichever test runs next. Same isolation pattern as tests/test_grants.py.
import pytest as _pytest
from unittest import mock as _mock

from ava_bridge import grants as _grants_mod
from ava_bridge import tools_cache as _tools_cache_mod


@_pytest.fixture(autouse=True)
def _isolated_consent_stores(tmp_path):
    with _mock.patch.object(_grants_mod, "PATH",
                            str(tmp_path / "connector_grants.yaml")), \
         _mock.patch.object(_tools_cache_mod, "PATH",
                            str(tmp_path / "tools_cache.json")):
        _grants_mod._cache.update(data=None, mtime=0.0)
        _tools_cache_mod._cache.update(data=None, mtime=0.0)
        yield
        _grants_mod._cache.update(data=None, mtime=0.0)
        _tools_cache_mod._cache.update(data=None, mtime=0.0)


TOOLS = [
    {"name": "list_things", "description": "List the things.",
     "inputSchema": {"type": "object", "properties": {}}, "access": "read"},
    {"name": "make_thing", "description": "Make a thing.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
     "access": "write"},
    {"name": "wreck_thing", "description": "Wreck a thing.",
     "inputSchema": {"type": "object", "properties": {}}, "access": "destructive"},
]
UPSTREAM_TOKEN = "upstream-secret"
MCP_TOKEN = "endpoint-secret"


class _Facade(BaseHTTPRequestHandler):
    """A minimal conforming ava-tools/1 app (CONNECTOR_SDK.md §5)."""

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        return (self.headers.get("Authorization") or "") == "Bearer " + UPSTREAM_TOKEN

    def do_GET(self):
        if self.path == "/tools":
            if not self._authed():
                return self._send(401, {"error": "Not authenticated"})
            return self._send(200, {"facade": "ava-tools/1", "tools": TOOLS})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/call":
            return self._send(404, {"error": "not found"})
        if not self._authed():
            return self._send(401, {"error": "Not authenticated"})
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        name = req.get("name")
        if name == "list_things":
            return self._send(200, {"things": ["a", "b"]})
        if name == "make_thing":
            return self._send(200, {"made": (req.get("arguments") or {}).get("name")})
        self._send(404, {"error": f"unknown tool '{name}'"})

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def mcp_url():
    """A fake facade, fronted by the adapter. Yields the MCP endpoint URL."""
    facade = ThreadingHTTPServer(("127.0.0.1", 0), _Facade)
    threading.Thread(target=facade.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{facade.server_address[1]}"

    httpd = serve_mcp(FacadeSource(base, token=UPSTREAM_TOKEN),
                      host="127.0.0.1", port=0, auth_token=MCP_TOKEN, block=False)
    yield f"http://127.0.0.1:{httpd.server_address[1]}/mcp"
    httpd.shutdown()
    facade.shutdown()


@pytest.fixture
def spec(mcp_url, monkeypatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", MCP_TOKEN)
    mcp_client.reset()
    return {"transport": "http", "url": mcp_url, "token_env": "TEST_MCP_TOKEN"}


def test_speaks_real_mcp(spec) -> None:
    """Ava's MCP client completes the handshake and lists the facade's tools."""
    r = mcp_client.list_tools("adapter-test", spec)
    assert "error" not in r, r.get("error")
    assert [t["name"] for t in r["tools"]] == [t["name"] for t in TOOLS]
    for t in r["tools"]:
        assert t["description"] and t["inputSchema"]["type"] == "object"


def test_access_tiers_survive_the_conversion(spec) -> None:
    """`access` rides through on every tool — top-level AND mirrored in _meta.

    Top-level is where ava_bridge/tools_cache.py reads it; `_meta` is the
    spec-sanctioned home for vendor fields, for stricter MCP clients."""
    tools = {t["name"]: t for t in mcp_client.list_tools("adapter-test", spec)["tools"]}
    for want in TOOLS:
        got = tools[want["name"]]
        assert got.get("access") == want["access"], (
            f"{want['name']}: tier lost in conversion — every read tool would "
            "start prompting for consent")
        assert (got.get("_meta") or {}).get("ava/access") == want["access"]


def test_tools_cache_reads_the_tiers(spec) -> None:
    """The tiers land in Ava's gate exactly as they did over the facade."""
    from ava_bridge import tools_cache
    tools_cache.update("adapter-test", mcp_client.list_tools("adapter-test", spec)["tools"])
    assert tools_cache.access("adapter-test", "list_things") == "read"
    assert tools_cache.access("adapter-test", "make_thing") == "write"
    assert tools_cache.access("adapter-test", "wreck_thing") == "destructive"


def test_call_round_trips(spec) -> None:
    """tools/call reaches the upstream app and returns its result as content."""
    res, status = mcp_client.call_tool("adapter-test", spec, "make_thing", {"name": "widget"})
    assert status == 200 and not res.get("isError")
    assert json.loads(res["content"][0]["text"]) == {"made": "widget"}


def test_tool_failure_is_data_not_a_transport_error(spec) -> None:
    """An unknown tool comes back as isError content — the model reads it and
    tries something else — never as a JSON-RPC/transport failure."""
    res, status = mcp_client.call_tool("adapter-test", spec, "nope", {})
    assert status == 200, "a tool-level failure must not surface as a transport error"
    assert res.get("isError") is True
    assert "nope" in res["content"][0]["text"]


def test_endpoint_requires_its_bearer(mcp_url) -> None:
    """The adapter's own token guards it, independently of the app's."""
    mcp_client.reset()
    r = mcp_client.list_tools("adapter-unauth",
                              {"transport": "http", "url": mcp_url, "token_env": "UNSET_VAR"})
    assert "error" in r and "401" in r["error"]


def test_stale_session_reconnects_silently(spec, monkeypatch) -> None:
    """A restarted MCP server forgets its sessions; the next call must recover.

    Without the retry, the first call after any app restart reported the
    connector unreachable — a false negative on the one signal Setup's
    transport chip is meant to make trustworthy."""
    mcp_client.list_tools("adapter-stale", spec)          # establish a session

    calls = {"n": 0}
    real = mcp_client._HttpSession._rpc

    def flaky(self, payload, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise mcp_client.McpError(
                'MCP server returned 404: {"error":{"message":"Session not found"}}')
        return real(self, payload, timeout)

    monkeypatch.setattr(mcp_client._HttpSession, "_rpc", flaky)
    r = mcp_client.list_tools("adapter-stale", spec)
    assert "error" not in r, r.get("error")
    assert len(r["tools"]) == len(TOOLS)


def test_other_failures_are_not_retried(spec, monkeypatch) -> None:
    """The retry is scoped to session loss — a real outage still reports."""
    attempts = {"n": 0}

    def dead(self, payload, timeout):
        attempts["n"] += 1
        raise mcp_client.McpError("connection refused")

    monkeypatch.setattr(mcp_client._HttpSession, "_rpc", dead)
    mcp_client.reset()
    r = mcp_client.list_tools("adapter-dead", spec)
    assert "error" in r and "connection refused" in r["error"]
    assert attempts["n"] == 1, "a non-session error must not be retried"
