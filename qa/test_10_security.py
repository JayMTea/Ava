"""Security posture beyond the auth sweep: throttles, secret file modes,
path traversal on the proxies and static mounts, SSRF guard, and scoped
internal tokens on sensitive routes.
"""
import os
import stat
import unittest

import pytest

from qa import helpers
from qa.conftest import QA_HOME


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestLoginThrottle(unittest.TestCase):
    def test_eighth_bad_attempt_locks_the_ip(self):
        anon = helpers.anon_client()
        codes = []
        for _ in range(10):
            r = anon.post("/login", data={"password": "wrong-every-time"})
            codes.append(r.status_code)
        self.assertIn(429, codes, f"login never throttled: {codes}")


class TestSecretsOnDisk(unittest.TestCase):
    def test_secret_files_are_0600(self):
        candidates = [
            os.path.join(QA_HOME, "data", ".secret"),
            os.path.join(QA_HOME, "data", ".internal_token"),
            os.path.join(QA_HOME, "data", "auth_password"),
        ]
        checked = 0
        for p in candidates:
            if not os.path.isfile(p):
                continue
            mode = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(mode, 0o600, f"{p} is {oct(mode)}, want 0600")
            checked += 1
        self.assertGreater(checked, 0, "no secret files found to check")


class TestPathTraversal(unittest.TestCase):
    def test_static_mounts_cannot_escape(self):
        c = CLIENT
        for path in ("/media/../data/.secret",
                     "/uploads/../data/auth_password",
                     "/media/%2e%2e/data/.secret"):
            r = c.get(path)
            self.assertIn(r.status_code, (400, 403, 404), path)
            self.assertNotIn("BEGIN", r.text[:200])

    def test_apps_proxy_unknown_connector_404s(self):
        r = CLIENT.get("/apps/not-a-connector/api/x")
        self.assertIn(r.status_code, (404, 502))


class TestInternalScopes(unittest.TestCase):
    def test_internal_gate_precedes_validation(self):
        """An unauthenticated /internal request must get 401 — never a 422
        that leaks the route's parameter schema (middleware gate)."""
        c = CLIENT
        for path in ("/internal/config", "/internal/policies",
                     "/internal/learning/chats"):
            r = c.get(path)
            self.assertEqual(r.status_code, 401, f"{path}: {r.status_code}")
            r = c.get(path, headers={"X-Ava-Internal-Token": "f" * 64})
            self.assertEqual(r.status_code, 401, f"{path} bogus token")

    def test_valid_tokens_reach_the_handlers(self):
        c = CLIENT
        # Root token passes the gate; the handler itself then answers
        # (200 for a good request, 4xx for bad params — but never 401).
        root = c.get("/internal/config", params={"component": "settings"},
                     headers=helpers.internal_headers())
        self.assertNotEqual(root.status_code, 401)
        self.assertLess(root.status_code, 500)
        # A derived group token passes for a route ITS OWN group owns.
        grp = c.get("/internal/architecture",
                    headers=helpers.internal_headers("system"))
        self.assertNotIn(grp.status_code, (401, 403))

    def test_a_group_token_is_refused_outside_its_capabilities(self):
        """This assertion used to say the opposite — that a `system` token was
        NOT rejected by /internal/policies — which locked in the very gap it
        looked like it was testing. Least privilege was documented in
        SECURITY.md, agent/install.sh and ava_bridge/auth.py, implemented in
        ava_bridge/security.py, and enforced nowhere: 24 of 25 handlers passed no
        scope, so any valid group token reached every route.

        The one that matters is `content`. Its MCP server runs web_fetch, so it
        is the surface prompt injection arrives on; reaching /internal/code-change
        from there is injection -> arbitrary governed edits to Ava's own source.
        """
        c = CLIENT
        for group, path in (
            ("system", "/internal/policies"),
            ("system", "/internal/code-change"),
            ("content", "/internal/code-change"),
            ("content", "/internal/config"),
            ("content", "/internal/policies"),
            ("productivity", "/internal/code-change"),
            ("wellness", "/internal/logs"),
        ):
            r = c.get(path, headers=helpers.internal_headers(group))
            self.assertEqual(r.status_code, 403,
                             f"{group} token reached {path}: {r.status_code}")

    def test_each_group_still_reaches_what_its_own_server_calls(self):
        """The gate is only correct if it does not break the live tools. These
        pairs are taken from what each agent/mcp_server_<group>/ actually calls."""
        c = CLIENT
        for group, path in (
            ("content", "/internal/documents"),
            ("admin", "/internal/logs"),
            ("productivity", "/internal/learning/chats"),
            ("system", "/internal/architecture"),
        ):
            r = c.get(path, headers=helpers.internal_headers(group))
            self.assertNotIn(r.status_code, (401, 403),
                             f"{group} token was refused its own route {path}")


class TestSsrfGuard(unittest.TestCase):
    def test_web_fetch_blocks_private_addresses(self):
        c = CLIENT
        for target in ("http://127.0.0.1:8096/api/health",
                       "http://169.254.169.254/latest/meta-data/",
                       "http://10.0.0.1/"):
            r = c.post("/internal/web/fetch", headers=helpers.internal_headers(),
                       json={"url": target})
            body = str(r.json()).lower()
            self.assertTrue(r.status_code >= 400 or "error" in body
                            or "blocked" in body or "not allowed" in body,
                            f"{target} was not refused: {r.status_code} {body[:200]}")
