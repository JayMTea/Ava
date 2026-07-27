"""Changing the admin password from inside the product, and revoking sessions.

Before this existed the only ways to change the password were editing
data/auth_password by hand or setting AVA_PASSWORD and restarting — neither
available to someone running Ava as an app rather than as their own checkout. And
because a session cookie was a signed expiry stamp with no session id and no
server-side store, there was no way to invalidate one: /logout only deleted the
client's copy, so a cookie captured from a shared machine stayed valid for its
full 30-day TTL even after a password change.

Revocation here IS key rotation — with no session store to evict from, changing
the key that signs cookies is what makes outstanding ones stop validating.
"""
import os
import tempfile
import unittest
from unittest import mock

# An isolated AVA_HOME before anything imports config (which resolves paths and
# creates directories at import time).
os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-pwchange-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from ava_bridge import auth, config  # noqa: E402

# Everything below is patched per-test rather than set through the environment at
# import time. `config` resolves its paths ONCE at first import, so whichever test
# module imports it first wins the process — this file passed alone and failed in
# the suite until it stopped depending on that race. Two other env facts also bite:
#   * `settings` auto-loads the repo .env and setdefault()s AVA_PASSWORD/AVA_SECRET,
#     so a pop() before the import is undone by it.
#   * COOKIE_SECURE defaults on, and a Secure cookie is not returned over the
#     TestClient's plain http, so the session looks lost on the next request.


def _client():
    import phone_bridge
    return TestClient(phone_bridge.app)


class TokenShapeTests(unittest.TestCase):
    def test_two_logins_produce_different_cookies(self):
        """The old `exp.sig` format made two logins in the same second identical."""
        a, b = auth._make_token(), auth._make_token()
        self.assertNotEqual(a, b)
        self.assertEqual(len(a.split(".")), 3, "expected exp.sid.sig")

    def test_legacy_two_part_token_is_rejected(self):
        exp = str(2 ** 31)
        legacy = f"{exp}.{auth._sign(exp)}"
        self.assertFalse(auth._valid_token(legacy))

    def test_tampered_session_id_is_rejected(self):
        exp, _sid, sig = auth._make_token().split(".")
        self.assertFalse(auth._valid_token(f"{exp}.forged.{sig}"))

    def test_rotation_invalidates_an_outstanding_token(self):
        tok = auth._make_token()
        self.assertTrue(auth._valid_token(tok))
        self.assertTrue(auth.rotate_secret())
        self.assertFalse(auth._valid_token(tok),
                         "rotating the signing key must invalidate old cookies")

    def test_rotation_refuses_when_the_key_is_pinned_in_the_env(self):
        """AVA_SECRET wins over the file, so writing the file would be a lie."""
        with mock.patch.dict(os.environ, {"AVA_SECRET": "x" * 32}):
            auth._AUTH_SECRET = None            # force re-read under the pin
            self.assertFalse(auth.rotate_secret())
        auth._AUTH_SECRET = None


class ChangePasswordEndpointTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="ava-pw-case-")
        for p in (
            mock.patch.object(config, "COOKIE_SECURE", False),
            mock.patch.object(auth, "_PASSWORD_FILE", os.path.join(tmp, "auth_password")),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            p.start()
            self.addCleanup(p.stop)
        for pinned in ("AVA_PASSWORD", "AVA_SECRET"):
            os.environ.pop(pinned, None)
        auth._AUTH_SECRET = None
        self.addCleanup(setattr, auth, "_AUTH_SECRET", None)
        auth.set_password("original-password")
        self.c = _client()
        r = self.c.post("/login", data={"password": "original-password"},
                        follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), "login should redirect on success")

    def test_requires_authentication(self):
        anon = _client()
        r = anon.post("/api/auth/password",
                      json={"current": "original-password", "new": "brand-new-pass"})
        self.assertEqual(r.status_code, 401)

    def test_wrong_current_password_is_refused(self):
        r = self.c.post("/api/auth/password",
                        json={"current": "wrong", "new": "brand-new-pass"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(auth.current_password(), "original-password")

    def test_short_new_password_is_refused(self):
        r = self.c.post("/api/auth/password",
                        json={"current": "original-password", "new": "short"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(auth.current_password(), "original-password")

    def test_change_succeeds_and_revokes_other_sessions(self):
        # A second device, logged in before the change.
        other = _client()
        other.post("/login", data={"password": "original-password"},
                   follow_redirects=False)
        self.assertEqual(other.get("/api/health").status_code, 200)

        r = self.c.post("/api/auth/password",
                        json={"current": "original-password", "new": "a-new-password"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["revoked_other_sessions"])
        self.assertEqual(auth.current_password(), "a-new-password")

        # The other device's cookie no longer validates on a gated route.
        self.assertEqual(other.get("/api/chats", follow_redirects=False).status_code, 401)
        # ...while the caller keeps working, on the cookie it was re-issued.
        self.assertEqual(self.c.get("/api/chats").status_code, 200)

    def test_env_pinned_password_is_reported_not_silently_ignored(self):
        with mock.patch.dict(os.environ, {"AVA_PASSWORD": "from-env"}):
            r = self.c.post("/api/auth/password",
                            json={"current": "from-env", "new": "a-new-password"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("AVA_PASSWORD", r.json()["error"])


if __name__ == "__main__":
    unittest.main()
