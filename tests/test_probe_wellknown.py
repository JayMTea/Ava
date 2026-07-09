"""Detect via /.well-known/ava.json: the self-description wins and prefills."""
import unittest
from unittest import mock

from ava_bridge.hub_api import _probe


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
