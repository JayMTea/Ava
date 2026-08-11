"""Detect via /.well-known/ava.json: the self-description wins and prefills."""
import unittest
from unittest import mock

# _probe moved with the Connectors panel when hub_api.py was split; it is
# the connector-detection helper and belongs beside the routes that call it.
from ava_bridge.hub.connectors import _probe


def _resp(status, payload):
    r = mock.Mock()
    r.status_code = status
    r.json.return_value = payload
    return r


class WellKnownProbeTests(unittest.TestCase):
    def test_well_known_prefills_and_respects_custom_paths(self):
        meta = {"facade": "ava-tools/1", "label": "My App",
                "tools": "/api/ava/tools", "call": "/api/ava/call",
                "health": "/api/health", "ui": True}
        listing = {"facade": "ava-tools/1",
                   "tools": [{"name": "ping", "description": "d",
                              "inputSchema": {"type": "object"}, "access": "read"}]}

        def fake_get(url, **kw):
            if url.endswith("/.well-known/ava.json"):
                return _resp(200, meta)
            if url.endswith("/api/ava/tools"):
                return _resp(200, listing)
            raise AssertionError(f"unexpected GET {url}")

        with mock.patch("requests.get", side_effect=fake_get):
            r = _probe("http://127.0.0.1:9999", "", None)
        self.assertEqual(r["kind"], "discover")
        self.assertEqual(r["label"], "My App")
        self.assertEqual(r["health"], "http://127.0.0.1:9999/api/health")
        self.assertEqual(r["discover"], {"list": "/api/ava/tools", "call": "/api/ava/call"})
        self.assertTrue(r["has_ui"])
        self.assertEqual(r["tools"][0]["name"], "ping")

    def test_no_well_known_falls_through(self):
        # A 404 on the well-known path must not short-circuit the ladder — the
        # plain /tools facade (step 3) still detects.
        listing = {"tools": [{"name": "ping", "description": "d"}]}

        def fake_get(url, **kw):
            if url.endswith("/.well-known/ava.json"):
                return _resp(404, {})
            if url.endswith("/tools"):
                return _resp(200, listing)
            return _resp(404, {})   # openapi probes, html sniff, etc.

        with mock.patch("requests.get", side_effect=fake_get), \
             mock.patch("ava_bridge.mcp_client.list_tools",
                        return_value={"error": "not mcp"}), \
             mock.patch("ava_bridge.mcp_client.reset"):
            r = _probe("http://127.0.0.1:9999", "", None)
        self.assertEqual(r["kind"], "discover")
        self.assertIsNone(r.get("label"))


if __name__ == "__main__":
    unittest.main()


class ProbeSaysWhatWentWrongTests(unittest.TestCase):
    """Six discovery attempts, six bare `except: pass`. A TLS failure, a wrong
    port and an expired token all ended at the same sentence — "No tools to
    auto-discover — tell Ava what this app can do" — and the owner was asked to
    hand-write actions against a host that had never answered."""

    def test_an_address_nothing_answers_at_is_not_a_regular_web_app(self):
        import requests

        def boom(url, **kw):
            raise requests.exceptions.ConnectionError("refused")

        with mock.patch("requests.get", side_effect=boom), \
             mock.patch("ava_bridge.mcp_client.list_tools",
                        return_value={"error": "connection refused"}), \
             mock.patch("ava_bridge.mcp_client.reset"):
            r = _probe("http://127.0.0.1:9/", "", None)
        self.assertFalse(r["ok"], "an address with nothing behind it read as a "
                                  "connectable app the owner should describe")
        self.assertTrue(r["tried"], "no trace of what was attempted")
        self.assertTrue(any("could not connect" in t for t in r["tried"]), r["tried"])
        # No error_code: every guided fix resolves to a connector's own row in
        # Setup, and this connector does not exist yet.
        self.assertNotIn("error_code", r)

    def test_tls_failure_is_named_rather_than_reported_as_no_tools(self):
        import requests

        def boom(url, **kw):
            raise requests.exceptions.SSLError("self signed certificate")

        with mock.patch("requests.get", side_effect=boom), \
             mock.patch("ava_bridge.mcp_client.list_tools", return_value={}), \
             mock.patch("ava_bridge.mcp_client.reset"):
            r = _probe("https://box.local", "", None)
        self.assertFalse(r["ok"])
        self.assertTrue(any("TLS failed" in t for t in r["tried"]), r["tried"])

    def test_a_401_says_it_wants_a_token(self):
        """It IS there. Telling the owner it has no tools is a statement about
        the app, when the truth is about the empty token field above it."""
        r401 = mock.Mock()
        r401.status_code = 401
        r401.headers = {"content-type": "application/json"}
        r401.text = "{}"
        r401.json.return_value = {}

        with mock.patch("requests.get", return_value=r401), \
             mock.patch("ava_bridge.mcp_client.list_tools", return_value={}), \
             mock.patch("ava_bridge.mcp_client.reset"):
            r = _probe("http://127.0.0.1:9999", "", None)
        self.assertTrue(r["ok"], "a 401 means reachable — the connect must stay "
                                 "possible once a token is pasted")
        self.assertTrue(r.get("needs_auth"), r)
        self.assertIn("401", r["detail"])

    def test_a_reachable_app_with_no_tool_list_still_reads_as_a_web_app(self):
        """The honest case must keep working: it answered, it just has nothing
        to discover, so the owner declares actions."""
        ok = mock.Mock()
        ok.status_code = 200
        ok.headers = {"content-type": "text/html"}
        ok.text = "<!doctype html><html></html>"
        ok.json.side_effect = ValueError("not json")

        with mock.patch("requests.get", return_value=ok), \
             mock.patch("ava_bridge.mcp_client.list_tools", return_value={}), \
             mock.patch("ava_bridge.mcp_client.reset"):
            r = _probe("http://127.0.0.1:9999", "", None)
        self.assertTrue(r["ok"])
        self.assertEqual(r["kind"], "rest")
        self.assertFalse(r.get("needs_auth"))
        self.assertTrue(r["has_ui"])
