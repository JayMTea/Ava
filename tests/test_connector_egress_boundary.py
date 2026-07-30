"""What may leave the bridge on a connector's behalf, and to whom.

Two boundaries, both of which were crossable and are now closed.

1. CREDENTIAL BOUNDARY. `auth_env` answers "which credential slot does this
   connector own?" — for an `mcp:` connector, the MCP server's token. `app_token`
   answers a different question: "which credential is this APP's own, such that
   Ava may present it to the app's UI?" They shared one implementation, so a
   manifest carrying both `mcp.token_env` and `ui.embed: iframe` had the MCP
   server's bearer attached to every proxied request to `ui.url` — a token issued
   by one host handed to another. The Hub's own Connect-an-app form emits exactly
   that manifest.

2. REDIRECT BOUNDARY. Every outbound connector request followed redirects. A
   connector's own API could answer `302 Location: http://127.0.0.1:8010/...` and
   the bridge would fetch loopback and return the body — an SSRF primitive with no
   guard, in contrast to `web_fetch`, which re-validates every hop
   (ava_bridge/web.py:108). For MCP, 307/308 preserve the POST body, so tool
   ARGUMENTS would be resent to the redirect target.

These are unit-level assertions about the resolver and about the kwargs actually
passed to `requests` — the transport is mocked, so no socket is opened.
"""
import os
import tempfile
import unittest
from unittest import mock

from ava_bridge import connectors, mcp_client, settings

MCP_WITH_TILE = {
    "id": "mcptile", "label": "MCP + tile", "kind": "app",
    "mcp": {"url": "https://example.invalid/mcp", "token_env": "MCPTILE_TOKEN"},
    "ui": {"label": "MCP + tile", "embed": "iframe", "url": "https://app.invalid"},
}
REST_APP = {
    "id": "restapp", "label": "REST app", "kind": "app",
    "base_url": "http://127.0.0.1:9000",
    "auth": {"token_env": "RESTAPP_TOKEN"},
    "ui": {"label": "REST app", "embed": "iframe", "url": "http://127.0.0.1:9000"},
}
DATA_PROXY = {
    "id": "dproxy", "label": "Data proxy", "kind": "app",
    "base_url": "http://127.0.0.1:9001",
    "ui": {"label": "Data proxy", "embed": "iframe", "url": "http://127.0.0.1:9001",
           "api": {"prefix": "/api", "token_env": "DPROXY_TOKEN"}},
}


class CredentialBoundaryTests(unittest.TestCase):
    """`auth_env` stays broad (the Hub needs it); `proxy_token_env` is narrow."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"AVA_SECRETS_DIR": self.tmp},
                                    clear=False)
        self._env.start()
        for k in ("MCPTILE_TOKEN", "RESTAPP_TOKEN", "DPROXY_TOKEN"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()

    def test_hub_still_sees_the_mcp_credential_slot(self):
        """The Hub keys its credential field off auth_env — that must not change,
        or the owner's MCP token gets filed under a slot nothing reads."""
        self.assertEqual(connectors.auth_env(MCP_WITH_TILE), "MCPTILE_TOKEN")

    def test_an_mcp_token_is_never_proxyable(self):
        self.assertIsNone(connectors.proxy_token_env(MCP_WITH_TILE))

    def test_the_mcp_bearer_is_not_presented_to_the_app(self):
        """The regression this file exists for."""
        settings.set_env_secret("MCPTILE_TOKEN", "mcp-secret-value")
        with mock.patch.object(connectors, "load", return_value=[MCP_WITH_TILE]):
            self.assertEqual(connectors.app_token("mcptile"), "")

    def test_a_rest_app_token_is_still_presented(self):
        """The SSO contract in docs/CONNECTOR_SDK.md §3 must keep working."""
        settings.set_env_secret("RESTAPP_TOKEN", "rest-secret-value")
        with mock.patch.object(connectors, "load", return_value=[REST_APP]):
            self.assertEqual(connectors.app_token("restapp"), "rest-secret-value")

    def test_a_ui_api_token_is_still_presented(self):
        settings.set_env_secret("DPROXY_TOKEN", "dproxy-secret-value")
        with mock.patch.object(connectors, "load", return_value=[DATA_PROXY]):
            self.assertEqual(connectors.app_token("dproxy"), "dproxy-secret-value")

    def test_discover_tokens_are_server_side_only(self):
        m = {"id": "d", "actions": {"discover": {"base": "http://127.0.0.1:9002",
                                                 "token_env": "D_TOKEN"}},
             "ui": {"embed": "iframe", "url": "http://127.0.0.1:9002"}}
        self.assertEqual(connectors.auth_env(m), "D_TOKEN")
        self.assertIsNone(connectors.proxy_token_env(m))


class _Resp:
    status_code = 200
    headers = {"Content-Type": "application/json"}
    text = "{}"

    def json(self):
        return {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}


class RedirectBoundaryTests(unittest.TestCase):
    """Assert on the kwargs handed to `requests`, not on network behaviour."""

    def test_connector_action_proxy_refuses_redirects(self):
        m = {"id": "acme", "base_url": "http://127.0.0.1:9000",
             "actions": [{"id": "list_x", "method": "GET", "path": "/api/x"}]}
        seen = {}

        def fake_request(method, url, **kw):
            seen.update(kw)
            return _Resp()

        with mock.patch.object(connectors, "load", return_value=[m]), \
                mock.patch("requests.request", side_effect=fake_request):
            connectors.call_action("acme", "list_x", {})
        self.assertIs(seen.get("allow_redirects"), False)

    def test_discover_list_refuses_redirects(self):
        m = {"id": "disc", "actions": {"discover": {"base": "http://127.0.0.1:9002",
                                                    "list": "/tools", "call": "/call"}}}
        seen = {}

        def fake_get(url, **kw):
            seen.update(kw)
            r = _Resp()
            r.json = lambda: {"tools": []}
            return r

        with mock.patch.object(connectors, "load", return_value=[m]), \
                mock.patch("requests.get", side_effect=fake_get):
            connectors.discover_tools("disc")
        self.assertIs(seen.get("allow_redirects"), False)

    def test_mcp_streamable_http_refuses_redirects(self):
        seen = {}

        def fake_post(url, **kw):
            seen.update(kw)
            return _Resp()

        spec = {"transport": "http", "url": "https://example.invalid/mcp"}
        with mock.patch("requests.post", side_effect=fake_post):
            s = mcp_client._HttpSession(spec)
            s._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, 30)
        self.assertIs(seen.get("allow_redirects"), False)

    def test_a_redirected_mcp_response_raises_instead_of_parsing(self):
        """A 3xx is not >= 400, so before this it fell through to the JSON parse
        and surfaced as a misleading 'non-JSON' error."""
        class Redirect(_Resp):
            status_code = 302
            headers = {"location": "http://127.0.0.1:8010/healthz"}

        spec = {"transport": "http", "url": "https://example.invalid/mcp"}
        with mock.patch("requests.post", return_value=Redirect()):
            s = mcp_client._HttpSession(spec)
            with self.assertRaises(mcp_client.McpError) as ctx:
                s._rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, 30)
        self.assertIn("redirected", str(ctx.exception))
        self.assertIn("127.0.0.1:8010", str(ctx.exception))

    def test_a_redirected_notification_is_not_silently_accepted(self):
        """`>= 400` is never reached for a notification (it returns early), so a
        redirected notifications/initialized used to return None and the session
        was marked initialized against a server that never saw it."""
        class Redirect(_Resp):
            status_code = 307
            headers = {"location": "http://evil.invalid/"}

        spec = {"transport": "http", "url": "https://example.invalid/mcp"}
        with mock.patch("requests.post", return_value=Redirect()):
            s = mcp_client._HttpSession(spec)
            with self.assertRaises(mcp_client.McpError):
                s._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, 30)


class ProxySourceGuardTests(unittest.TestCase):
    """A static guard: the bridge's app proxies must not regain redirect-following.

    Cheaper and more durable than driving Starlette — the failure mode is someone
    adding a new method branch and forgetting the kwarg.
    """

    def test_every_proxy_request_call_disables_redirects(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent.parent
        src = (root / "phone_bridge.py").read_text(encoding="utf-8")
        # Each `requests.<verb>(` inside the two proxy helpers, with its args.
        calls = re.findall(r"requests\.(?:request|get|post|delete|put|patch)\("
                           r"(?:[^()]|\([^()]*\))*\)", src)
        self.assertTrue(calls, "no requests calls found — did the proxies move?")
        missing = [c for c in calls if "allow_redirects" not in c]
        self.assertEqual(missing, [], "these bridge egress calls follow redirects:\n"
                                      + "\n".join(missing))


if __name__ == "__main__":
    unittest.main()
