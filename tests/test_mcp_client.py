"""MCP client tests — the "wrap any MCP server in an egress policy" feature.

Covers: a REAL end-to-end stdio session against a stub MCP server subprocess
(initialize -> tools/list -> tools/call over newline JSON-RPC), the HTTP
transport's JSON/SSE/session-id handling via mocked requests, and the
connectors-layer integration (routing + egress policy auto-allow).
"""
import json
import sys
import textwrap
import unittest
from unittest import mock

from ava_bridge import connectors, mcp_client

# A minimal MCP server speaking newline-delimited JSON-RPC on stdio.
STUB_SERVER = textwrap.dedent("""
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notification
        m = msg.get("method")
        if m == "initialize":
            r = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                 "serverInfo": {"name": "stub", "version": "0"}}
        elif m == "tools/list":
            r = {"tools": [{"name": "echo", "description": "echo args back",
                            "inputSchema": {"type": "object"}}]}
        elif m == "tools/call":
            r = {"content": [{"type": "text",
                              "text": json.dumps(msg["params"]["arguments"])}],
                 "isError": False}
        else:
            r = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": r}) + "\\n")
        sys.stdout.flush()
""")

STDIO_SPEC = {"transport": "stdio", "url": None, "env": None, "token_env": None,
              "command": [sys.executable, "-u", "-c", STUB_SERVER]}


class TestStdioEndToEnd(unittest.TestCase):
    def tearDown(self):
        mcp_client.reset()

    def test_list_and_call(self):
        out = mcp_client.list_tools("t1", STDIO_SPEC)
        self.assertNotIn("error", out, out)
        self.assertEqual(out["tools"][0]["name"], "echo")

        data, status = mcp_client.call_tool("t1", STDIO_SPEC, "echo", {"x": 1})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data["content"][0]["text"]), {"x": 1})

    def test_list_is_cached_across_calls(self):
        mcp_client.list_tools("t2", STDIO_SPEC)
        s1 = mcp_client._sessions["t2"]
        mcp_client.list_tools("t2", STDIO_SPEC)
        self.assertIs(mcp_client._sessions["t2"], s1)  # same live session

    def test_dead_server_returns_error_not_raise(self):
        spec = dict(STDIO_SPEC, command=[sys.executable, "-c", "pass"])  # exits at once
        out = mcp_client.list_tools("t3", spec)
        self.assertIn("error", out)


class _Resp:
    def __init__(self, body, headers=None, status=200, sse=False):
        self._body = body
        self.status_code = status
        self.headers = {"Content-Type": "text/event-stream" if sse else "application/json",
                        **(headers or {})}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class TestHttpTransport(unittest.TestCase):
    def tearDown(self):
        mcp_client.reset()

    def _spec(self):
        return {"transport": "http", "url": "http://mcp.test/mcp",
                "command": None, "env": None, "token_env": None}

    def test_session_id_echoed_and_tools_listed(self):
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append({"url": url, "json": json, "headers": headers})
            method = (json or {}).get("method")
            if method == "initialize":
                return _Resp({"jsonrpc": "2.0", "id": json["id"],
                              "result": {"protocolVersion": "2025-03-26"}},
                             headers={"Mcp-Session-Id": "sess-42"})
            if method == "notifications/initialized":
                return _Resp({}, status=202)
            if method == "tools/list":
                return _Resp({"jsonrpc": "2.0", "id": json["id"],
                              "result": {"tools": [{"name": "web_search"}]}})
            raise AssertionError(f"unexpected method {method}")

        with mock.patch("requests.post", side_effect=fake_post):
            out = mcp_client.list_tools("h1", self._spec())
        self.assertEqual(out["tools"][0]["name"], "web_search")
        # every request after initialize must carry the session id
        self.assertEqual(calls[-1]["headers"].get("Mcp-Session-Id"), "sess-42")

    def test_sse_response_parsed(self):
        def fake_post(url, json=None, headers=None, timeout=None):
            method = (json or {}).get("method")
            if method == "initialize":
                return _Resp({"jsonrpc": "2.0", "id": json["id"], "result": {}})
            if method == "notifications/initialized":
                return _Resp({}, status=202)
            body = ("event: message\n"
                    f"data: {{\"jsonrpc\": \"2.0\", \"id\": {json['id']}, "
                    f"\"result\": {{\"tools\": [{{\"name\": \"sse_tool\"}}]}}}}\n\n")
            return _Resp(body, sse=True)

        with mock.patch("requests.post", side_effect=fake_post):
            out = mcp_client.list_tools("h2", self._spec())
        self.assertEqual(out["tools"][0]["name"], "sse_tool")

    def test_http_error_becomes_error_dict(self):
        with mock.patch("requests.post",
                        return_value=_Resp({"error": "no"}, status=500)):
            out = mcp_client.list_tools("h3", self._spec())
        self.assertIn("error", out)


class TestConnectorsIntegration(unittest.TestCase):
    """The manifest `mcp:` block routes through the seam and polices egress.
    The registry is mocked at connectors.load — no on-disk manifests needed."""

    def tearDown(self):
        mcp_client.reset()

    def _manifest(self):
        return {"id": "mcptest", "mcp": {"url": "http://127.0.0.1:9999/mcp"}}

    def test_mcp_spec_parsed(self):
        spec = connectors._mcp_spec(self._manifest())
        self.assertEqual(spec["transport"], "http")
        self.assertEqual(spec["url"], "http://127.0.0.1:9999/mcp")

    def test_mcp_spec_stdio_inferred(self):
        spec = connectors._mcp_spec({"id": "x", "mcp": {"command": ["npx", "-y", "srv"]}})
        self.assertEqual(spec["transport"], "stdio")

    def test_mcp_spec_string_command_split(self):
        spec = connectors._mcp_spec({"id": "x", "mcp": {"command": "npx -y srv"}})
        self.assertEqual(spec["command"], ["npx", "-y", "srv"])

    def test_discover_routes_to_mcp(self):
        with mock.patch.object(connectors, "load",
                               return_value=[self._manifest()]):
            with mock.patch("ava_bridge.mcp_client.list_tools",
                            return_value={"tools": [{"name": "t"}]}) as m:
                out = connectors.discover_tools("mcptest")
        self.assertEqual(out["tools"][0]["name"], "t")
        m.assert_called_once()

    def test_call_routes_to_mcp(self):
        with mock.patch.object(connectors, "load",
                               return_value=[self._manifest()]):
            with mock.patch("ava_bridge.mcp_client.call_tool",
                            return_value=({"content": []}, 200)) as m:
                data, status = connectors.call_discovered("mcptest", "t", {"a": 1})
        self.assertEqual(status, 200)
        m.assert_called_once()

    def test_egress_policy_allows_exactly_the_bridge_routes(self):
        """The security invariant for MCP: policy exists and allow-lists the
        __tools/__call routes — the agent's ONLY surface for this connector."""
        with mock.patch.object(connectors, "load",
                               return_value=[self._manifest()]):
            pol = connectors.render_egress_policy("mcptest")
        self.assertIsNotNone(pol)
        allowed = {(r["allow"]["method"], r["allow"]["path"])
                   for np in pol["network_policies"].values()
                   for ep in np.get("endpoints", []) for r in ep.get("rules", [])}
        self.assertIn(("GET", "/internal/connector/mcptest/__tools"), allowed)
        self.assertIn(("POST", "/internal/connector/mcptest/__call"), allowed)
        # and NOTHING else — the MCP server itself is not agent-reachable
        self.assertEqual(len(allowed), 2)


if __name__ == "__main__":
    unittest.main()
