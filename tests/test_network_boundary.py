"""Who is asking, over what, and may they claim an unowned Ava.

Three defects sat behind one missing idea — that "the caller's address" and "was
this request encrypted" are questions with one right answer, resolved in one place:

  * `/setup` is necessarily public (there is no password yet) and POST /setup only
    checked `needs_setup()`. Bound off-loopback, the first device on the network
    to find the box set the admin password and the owner was locked out of their
    own install.
  * The session cookie was marked Secure unconditionally. Browsers DISCARD a
    Secure cookie sent over plain http, so on a LAN address the session vanished
    on every request and the user bounced back to /login with no message — which
    looks exactly like a wrong password. That is the flow docs/MOBILE.md markets.
  * The login throttle keyed on `request.client.host`, so behind any reverse
    proxy every device shared one lockout bucket: eight wrong guesses from one
    phone locked out the house.

The trap worth naming: X-Forwarded-For must be believed ONLY from a trusted peer.
Behind a loopback proxy every client otherwise appears to be 127.0.0.1, which
would hand the claim gate to anyone who could reach the proxy.
"""
import os
import tempfile
import unittest
from unittest import mock

# An isolated AVA_HOME before anything imports config (which resolves paths and
# creates directories at import time).
os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-netbound-test-"))

from starlette.requests import Request  # noqa: E402

from ava_bridge import auth, config  # noqa: E402


def _req(*, client=("192.168.1.50", 40000), scheme="http", headers=None, query=""):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": scheme, "path": "/setup", "raw_path": b"/setup",
        "query_string": query.encode(), "headers": raw,
        "client": client, "server": ("testserver", 80),
    })


class ClientIpTests(unittest.TestCase):
    def test_the_immediate_peer_is_used_by_default(self):
        self.assertEqual(auth.client_ip(_req(client=("203.0.113.9", 1234))),
                         "203.0.113.9")

    def test_forwarded_for_is_honoured_from_a_trusted_proxy(self):
        with mock.patch.object(config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            r = _req(client=("127.0.0.1", 1234),
                     headers={"X-Forwarded-For": "203.0.113.9"})
            self.assertEqual(auth.client_ip(r), "203.0.113.9")

    def test_forwarded_for_from_an_untrusted_peer_is_ignored(self):
        """Otherwise anyone can claim any address just by sending a header."""
        with mock.patch.object(config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            r = _req(client=("192.168.1.50", 1234),
                     headers={"X-Forwarded-For": "127.0.0.1"})
            self.assertEqual(auth.client_ip(r), "192.168.1.50")

    def test_only_the_last_hop_is_believed(self):
        """Earlier entries are client-supplied and forgeable."""
        with mock.patch.object(config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            r = _req(client=("127.0.0.1", 1234),
                     headers={"X-Forwarded-For": "1.1.1.1, 203.0.113.9"})
            self.assertEqual(auth.client_ip(r), "203.0.113.9")


class LoopbackTests(unittest.TestCase):
    def test_loopback_addresses_are_recognised(self):
        for host in ("127.0.0.1", "127.0.0.5", "::1"):
            self.assertTrue(auth._is_loopback(host), host)

    def test_non_loopback_is_not(self):
        for host in ("192.168.1.50", "10.0.0.4", "203.0.113.9", ""):
            self.assertFalse(auth._is_loopback(host), host)

    def test_an_unparseable_host_fails_closed(self):
        """Starlette's TestClient reports the literal string "testclient". A gate
        that read "cannot parse" as "local" would open to anything that confused
        it, so qa/conftest.py passes a real address instead."""
        self.assertFalse(auth._is_loopback("testclient"))
        self.assertFalse(auth._is_loopback("localhost"))


class CookieSecureTests(unittest.TestCase):
    def test_auto_is_off_over_plain_http(self):
        with mock.patch.object(config, "COOKIE_SECURE", None):
            self.assertFalse(auth.cookie_secure_for(_req(scheme="http")))

    def test_auto_is_on_over_https(self):
        with mock.patch.object(config, "COOKIE_SECURE", None):
            self.assertTrue(auth.cookie_secure_for(_req(scheme="https")))

    def test_auto_honours_forwarded_proto_from_a_trusted_proxy(self):
        with mock.patch.object(config, "COOKIE_SECURE", None), \
             mock.patch.object(config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            r = _req(client=("127.0.0.1", 1), headers={"X-Forwarded-Proto": "https"})
            self.assertTrue(auth.cookie_secure_for(r))

    def test_auto_ignores_forwarded_proto_from_an_untrusted_peer(self):
        with mock.patch.object(config, "COOKIE_SECURE", None), \
             mock.patch.object(config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            r = _req(client=("192.168.1.50", 1), headers={"X-Forwarded-Proto": "https"})
            self.assertFalse(auth.cookie_secure_for(r))

    def test_explicit_true_and_false_still_win(self):
        with mock.patch.object(config, "COOKIE_SECURE", True):
            self.assertTrue(auth.cookie_secure_for(_req(scheme="http")))
        with mock.patch.object(config, "COOKIE_SECURE", False):
            self.assertFalse(auth.cookie_secure_for(_req(scheme="https")))

    def test_the_string_false_is_not_read_as_true(self):
        """The old code was `os.environ.get(...) != "0"`, so AVA_COOKIE_SECURE=false
        evaluated True — the opposite of what the operator wrote."""
        for raw, want in (("false", False), ("0", False), ("no", False),
                          ("off", False), ("true", True), ("1", True),
                          ("auto", None), ("", None)):
            with mock.patch.dict(os.environ, {"AVA_COOKIE_SECURE": raw}):
                self.assertIs(config._resolve_cookie_secure(), want, raw)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        auth.clear_claim()
        self._pw = mock.patch.object(auth, "needs_setup", return_value=True)
        self._pw.start()
        self.addCleanup(self._pw.stop)
        self.addCleanup(auth.clear_claim)

    def test_loopback_may_always_claim(self):
        self.assertTrue(auth.may_claim(_req(client=("127.0.0.1", 1))))

    def test_a_remote_caller_without_a_token_may_not(self):
        self.assertFalse(auth.may_claim(_req(client=("192.168.1.50", 1))))

    def test_a_remote_caller_with_the_right_token_may(self):
        tok = auth.claim_token()
        self.assertTrue(tok)
        r = _req(client=("192.168.1.50", 1), query=f"claim={tok}")
        self.assertTrue(auth.may_claim(r))

    def test_a_wrong_token_is_refused(self):
        auth.claim_token()
        r = _req(client=("192.168.1.50", 1), query="claim=not-the-token")
        self.assertFalse(auth.may_claim(r))

    def test_the_token_is_stable_across_calls(self):
        """Regenerating per request would make the printed one useless."""
        self.assertEqual(auth.claim_token(), auth.claim_token())

    def test_a_claimed_instance_issues_no_token(self):
        with mock.patch.object(auth, "needs_setup", return_value=False):
            self.assertEqual(auth.claim_token(), "")

    def test_a_remote_caller_cannot_claim_by_forging_forwarded_for(self):
        """The whole gate turns on this: a header must not make you local."""
        with mock.patch.object(config, "TRUSTED_PROXIES", ("127.0.0.1",)):
            auth.claim_token()
            r = _req(client=("192.168.1.50", 1),
                     headers={"X-Forwarded-For": "127.0.0.1"})
            self.assertFalse(auth.may_claim(r))


if __name__ == "__main__":
    unittest.main()
