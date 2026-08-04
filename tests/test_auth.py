"""Auth middleware tests — cookie HMAC, path gating, login throttle.

Drives ava_bridge.auth.auth_gate on a minimal FastAPI app (not phone_bridge,
whose startup pulls in heavy deps), with the module's secret/password/state
monkeypatched for isolation.
"""
import time
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from ava_bridge import auth, config, state


def _app():
    app = FastAPI()
    app.middleware("http")(auth.auth_gate)

    @app.get("/{full_path:path}")
    def catch_all(full_path: str):
        return PlainTextResponse("ok")

    return app


class TokenTests(unittest.TestCase):
    def setUp(self):
        self._sec = mock.patch.object(auth, "_AUTH_SECRET", b"unit-test-secret")
        self._sec.start()

    def tearDown(self):
        self._sec.stop()

    def test_roundtrip_valid(self):
        self.assertTrue(auth._valid_token(auth._make_token()))

    def test_tampered_signature_rejected(self):
        tok = auth._make_token()
        exp, _, sig = tok.partition(".")
        # Flip (not just set) the last hex char so the token always differs.
        tampered = "0" if sig[-1] != "0" else "1"
        self.assertFalse(auth._valid_token(f"{exp}.{sig[:-1]}{tampered}"))

    def test_expired_rejected(self):
        exp = str(int(time.time()) - 10)
        import hashlib
        import hmac
        sig = hmac.new(b"unit-test-secret", exp.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(auth._valid_token(f"{exp}.{sig}"))

    def test_wrong_secret_rejected(self):
        tok = auth._make_token()
        with mock.patch.object(auth, "_AUTH_SECRET", b"a-different-secret"):
            self.assertFalse(auth._valid_token(tok))

    def test_garbage_rejected(self):
        for bad in ("", "nodot", "123.", ".abc", "abc.def"):
            self.assertFalse(auth._valid_token(bad))


class GateTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            mock.patch.object(auth, "_AUTH_SECRET", b"unit-test-secret"),
            mock.patch.object(auth, "current_password", lambda: "pw"),
            mock.patch.object(auth, "needs_setup", lambda: False),
        ]
        for p in self._patchers:
            p.start()
        self.client = TestClient(_app(), base_url="http://localhost")

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _cookie(self):
        return {config.COOKIE_NAME: auth._make_token()}

    def test_public_paths_pass_unauthed(self):
        for path in ("/login", "/api/health", "/favicon.ico"):
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_internal_requires_a_valid_token_at_the_gate(self):
        # /internal/* bypasses the COOKIE gate but the middleware validates the
        # internal token before routing — otherwise FastAPI's 422 validation
        # errors would answer unauthenticated callers and leak route schemas.
        from ava_bridge import internal
        self.assertEqual(self.client.get("/internal/model").status_code, 401)
        self.assertEqual(self.client.get(
            "/internal/model",
            headers={"X-Ava-Internal-Token": "wrong"}).status_code, 401)
        # A valid root token reaches the handler (cookie still not needed).
        r = self.client.get(
            "/internal/model",
            headers={"X-Ava-Internal-Token": config.INTERNAL_TOKEN})
        self.assertEqual(r.status_code, 200)
        # A derived capability-group token also passes the gate.
        r = self.client.get(
            "/internal/model",
            headers={"X-Ava-Internal-Token": internal._derived_token("system")})
        self.assertEqual(r.status_code, 200)

    def test_internal_prefix_not_overmatched(self):
        # A path merely STARTING with the string "/internal" must NOT bypass.
        r = self.client.get("/internallywrong", follow_redirects=False)
        self.assertIn(r.status_code, (303, 401))

    def test_api_unauthed_is_401_json(self):
        r = self.client.get("/api/chats", follow_redirects=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["error"], "auth required")

    def test_apps_and_uploads_unauthed_401(self):
        for path in ("/apps/x/", "/uploads/z"):
            r = self.client.get(path, follow_redirects=False)
            self.assertEqual(r.status_code, 401, path)

    def test_page_unauthed_redirects_login(self):
        r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_page_redirects_setup_when_no_password(self):
        with mock.patch.object(auth, "needs_setup", lambda: True):
            r = self.client.get("/", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/setup")

    def test_authed_cookie_reaches_protected(self):
        r = self.client.get("/api/chats", cookies=self._cookie())
        self.assertEqual(r.status_code, 200)

    def test_overlay_can_extend_api_prefixes(self):
        # The overlay appends "/studio" at register() time; simulate it.
        with mock.patch.object(auth, "API_PREFIXES", auth.API_PREFIXES + ["/studio"]):
            r = self.client.get("/studio/api/x", follow_redirects=False)
        self.assertEqual(r.status_code, 401)


class ThrottleTests(unittest.TestCase):
    def setUp(self):
        with state.login_lock:
            state.login_fails.clear()

    def tearDown(self):
        with state.login_lock:
            state.login_fails.clear()

    def test_locks_after_max_failures(self):
        ip = "1.2.3.4"
        for _ in range(config.LOGIN_MAX):
            auth.login_record(ip, ok=False)
        self.assertTrue(auth.login_locked(ip))

    def test_success_clears(self):
        ip = "5.6.7.8"
        for _ in range(config.LOGIN_MAX):
            auth.login_record(ip, ok=False)
        auth.login_record(ip, ok=True)
        self.assertFalse(auth.login_locked(ip))

    def test_window_expiry_unlocks(self):
        ip = "9.9.9.9"
        for _ in range(config.LOGIN_MAX):
            auth.login_record(ip, ok=False)
        # Age the first-failure timestamp beyond the window.
        with state.login_lock:
            state.login_fails[ip][1] = time.time() - config.LOGIN_WINDOW - 1
        self.assertFalse(auth.login_locked(ip))


if __name__ == "__main__":
    unittest.main()
