"""Auth posture of the ENTIRE route table, generated from app.routes.

Instead of hand-picking endpoints (and silently missing new ones), walk every
registered route and assert its unauthenticated behavior class:
  * the public allowlist answers without auth,
  * every /api|/apps|/media|/uploads route answers 401 JSON,
  * /internal/* rejects missing and wrong bearer tokens,
  * anything else redirects (303) to the login/setup flow.
Plus cookie hygiene and scoped internal tokens.
"""
import unittest

import pytest

from qa import helpers

# Both imported, never re-listed. A local copy of API_PREFIXES drifted from the
# app's own list and went stale on /thumb: the app correctly answered 401 for
# /thumb/audit while this test demanded a 303 page redirect, so the suite failed
# on correct behaviour.
#
# PUBLIC used to sit right above that comment as exactly the same hand-maintained
# copy the comment argues against — and it bit the same way the first time the
# app's allowlist grew: adding the five branding asset slots to
# auth._PUBLIC_PATHS failed this file for the app being right. Importing the
# source of truth makes that drift impossible for both.
from ava_bridge.auth import API_PREFIXES as _APP_API_PREFIXES
from ava_bridge.auth import _PUBLIC_PATHS as PUBLIC

API_PREFIXES = tuple(f"{p}/" for p in _APP_API_PREFIXES)


def _sample_path(route) -> str:
    """A concrete URL for a (possibly templated) route path."""
    p = route.path
    for name, val in (("{cid}", "qa-x"), ("{aid}", "qa-x"), ("{mid}", "qa-x"),
                      ("{tid}", "qa-x"), ("{job_id}", "qa-x"), ("{bid}", "qa-x"),
                      ("{name}", "audit"), ("{action}", "ping"),
                      ("{path:path}", "x"), ("{path}", "x")):
        p = p.replace(name, val)
    return p


@pytest.fixture(scope="module", autouse=True)
def _clients(bridge):
    helpers.ensure_authed(bridge)      # password exists from here on
    globals()["AUTHED"] = bridge
    globals()["ANON"] = helpers.anon_client()
    yield


class TestAuthSurface(unittest.TestCase):
    def _routes(self):
        import phone_bridge
        for r in phone_bridge.app.routes:
            methods = getattr(r, "methods", None)
            path = getattr(r, "path", "")
            if not methods or not path:
                continue  # mounts (static) tested separately
            yield r, sorted(m for m in methods if m != "HEAD")

    def test_every_route_has_correct_unauth_behavior(self):
        anon = ANON
        failures = []
        for route, methods in self._routes():
            path = _sample_path(route)
            method = methods[0]
            r = anon.request(method, path)
            if route.path in PUBLIC or route.path.startswith("/setup"):
                # Public routes must not demand a session. POST /login IS the
                # auth endpoint: an empty password correctly answers 401.
                if r.status_code == 401 and not (method == "POST"
                                                 and route.path == "/login"):
                    failures.append(f"{method} {path}: public route returned 401")
            elif path.startswith("/internal"):
                if r.status_code != 401:
                    failures.append(f"{method} {path}: internal route without token "
                                    f"-> {r.status_code}, want 401")
            elif (path.startswith(API_PREFIXES) or path in ("/api", "/apps")
                  or path.startswith("/studio")):  # overlay API surface
                # Device ingest is the one deliberate bearer-authed exception.
                if "connectors/qa-x/events" in path:
                    continue
                if r.status_code != 401:
                    failures.append(f"{method} {path}: unauthed API -> "
                                    f"{r.status_code}, want 401")
            else:
                if r.status_code not in (303, 307):
                    failures.append(f"{method} {path}: unauthed page -> "
                                    f"{r.status_code}, want 303 redirect")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_static_mounts_gated(self):
        anon = ANON
        for path in ("/media/x.png", "/uploads/x.png", "/apps/foo/"):
            self.assertEqual(anon.get(path).status_code, 401, path)
        # /assets is intentionally public-ish? No — only the allowlist is public.
        # The SPA index redirects unauthenticated users:
        self.assertEqual(anon.get("/").status_code, 303)

    def test_internal_token_scopes(self):
        c = AUTHED
        # No token and a wrong token are both rejected.
        self.assertEqual(c.get("/internal/documents").status_code, 401)
        self.assertEqual(c.get("/internal/documents",
                               headers={"X-Ava-Internal-Token": "nope"}).status_code, 401)
        # The root token passes.
        self.assertEqual(c.get("/internal/documents",
                               headers=helpers.internal_headers()).status_code, 200)
        # A derived group token also passes an unscoped route.
        self.assertEqual(c.get("/internal/documents",
                               headers=helpers.internal_headers("content")).status_code, 200)

    def test_session_cookie_hygiene(self):
        anon = helpers.anon_client()
        from qa.env_recipe import QA_PASSWORD
        r = anon.post("/login", data={"password": QA_PASSWORD})
        raw = r.headers.get("set-cookie", "")
        self.assertIn("ava_session=", raw)
        self.assertIn("HttpOnly", raw)
        self.assertIn("SameSite=lax", raw)
        # A forged cookie is rejected.
        forged = helpers.anon_client()
        forged.cookies.set("ava_session", "9999999999.deadbeef")
        self.assertEqual(forged.get("/api/brand").status_code, 401)

    def test_the_cookie_is_not_secure_over_plain_http(self):
        """A browser DISCARDS a Secure cookie sent over http, so forcing the flag
        on for a LAN address logs the user out on every request with no error
        anywhere — indistinguishable from a wrong password, and exactly the
        phone/PWA flow docs/MOBILE.md markets.

        This assertion is only meaningful because qa/env_recipe.py no longer pins
        AVA_COOKIE_SECURE=0: the suite now runs the same `auto` resolution the
        product ships, so it can actually see a regression here.
        """
        anon = helpers.anon_client()
        from qa.env_recipe import QA_PASSWORD
        r = anon.post("/login", data={"password": QA_PASSWORD})
        raw = r.headers.get("set-cookie", "")
        self.assertIn("ava_session=", raw)
        self.assertNotIn("Secure", raw)

    def test_the_cookie_is_secure_behind_a_trusted_https_proxy(self):
        from qa.env_recipe import QA_PASSWORD
        anon = helpers.anon_client()
        r = anon.post("/login", data={"password": QA_PASSWORD},
                      headers={"X-Forwarded-Proto": "https"})
        raw = r.headers.get("set-cookie", "")
        self.assertIn("ava_session=", raw)
        self.assertIn("Secure", raw)

    def test_an_unclaimed_ava_refuses_remote_setup(self):
        """`/setup` has to be public — there is no password yet — so on a
        non-loopback bind the first device on the network to find the box got to
        set the admin password and lock the owner out of their own install.

        Loopback still claims freely (the common case: a browser on the same
        machine). Anyone else must present the token printed to the server's
        console, which is a proof that you can read the server's disk.
        """
        from unittest import mock

        from fastapi.testclient import TestClient

        import phone_bridge
        from ava_bridge import auth

        # One patch point, not two. The handler used to live in phone_bridge.py,
        # which held its own `from ava_bridge.auth import needs_setup` binding, so
        # this had to patch both modules. It now lives in ava_bridge/pages.py and
        # calls auth.needs_setup() through the module, so patching auth is enough
        # — and this test no longer breaks when the handler moves file again.
        with mock.patch.object(auth, "needs_setup", return_value=True):
            auth.clear_claim()
            try:
                remote = TestClient(phone_bridge.app, follow_redirects=False,
                                    client=("192.168.1.50", 40000), base_url="http://localhost")
                self.assertEqual(remote.get("/setup").status_code, 403)
                self.assertEqual(
                    remote.post("/setup", data={"password": "hunter2hunter2",
                                                "confirm": "hunter2hunter2"}).status_code,
                    403)

                local = TestClient(phone_bridge.app, follow_redirects=False,
                                   client=("127.0.0.1", 40001), base_url="http://localhost")
                self.assertEqual(local.get("/setup").status_code, 200)

                token = auth.claim_token()
                self.assertTrue(token)
                self.assertEqual(
                    remote.get(f"/setup?claim={token}").status_code, 200)
                # A near-miss is still refused.
                self.assertEqual(
                    remote.get(f"/setup?claim={token}x").status_code, 403)
            finally:
                auth.clear_claim()

    def test_logout_clears_cookie(self):
        anon = helpers.anon_client()
        from qa.env_recipe import QA_PASSWORD
        anon.post("/login", data={"password": QA_PASSWORD})
        self.assertEqual(anon.get("/api/brand").status_code, 200)
        r = anon.post("/logout")
        self.assertEqual(r.status_code, 303)
        self.assertIn("ava_session=", r.headers.get("set-cookie", ""))
        anon.cookies.clear()
        self.assertEqual(anon.get("/api/brand").status_code, 401)
