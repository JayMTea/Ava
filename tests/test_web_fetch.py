"""SSRF guard tests for ava_bridge.web — the host-mediated web layer.

Pure IP/URL validation plus redirect-hop revalidation driven through the
_make_client seam with an httpx.MockTransport (no real network).
"""
import unittest
from unittest import mock

import httpx

from ava_bridge import web


class IpPublicTests(unittest.TestCase):
    def test_private_and_special_addresses_are_not_public(self):
        for ip in ("127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1",
                   "169.254.169.254", "0.0.0.0", "224.0.0.1", "240.0.0.1",
                   "::1", "fc00::1", "fe80::1"):
            self.assertFalse(web._ip_is_public(ip), ip)

    def test_public_addresses_are_public(self):
        for ip in ("93.184.216.34", "2606:4700::1111", "8.8.8.8"):
            self.assertTrue(web._ip_is_public(ip), ip)

    def test_literal_ip_detection_incl_bracketed_v6(self):
        self.assertEqual(web._literal_ip("127.0.0.1"), "127.0.0.1")
        self.assertEqual(web._literal_ip("[::1]"), "::1")
        self.assertIsNone(web._literal_ip("example.com"))


class ValidateUrlTests(unittest.TestCase):
    def setUp(self):
        # Deterministic config: Tor off so DNS resolution is exercised.
        self._patchers = [
            mock.patch.object(web.config, "WEB_TOR", False),
            mock.patch.object(web.config, "WEB_DOMAIN_DENYLIST", ["doubleclick.net"]),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_scheme_allowlist(self):
        for url in ("ftp://example.com", "file:///etc/passwd", "example.com/x"):
            with self.assertRaises(web.WebAccessError):
                web._validate_fetch_url(url)

    def test_denylist_substring(self):
        with self.assertRaises(web.WebAccessError):
            web._validate_fetch_url("http://ads.doubleclick.net/x")

    def test_internal_hostnames_blocked(self):
        for host in ("localhost", "metadata.google.internal", "foo.internal",
                     "printer.local", "svc.home.arpa"):
            with self.assertRaises(web.WebAccessError):
                web._validate_fetch_url(f"http://{host}/x")

    def test_literal_private_ip_blocked_both_tor_modes(self):
        for tor in (True, False):
            with mock.patch.object(web.config, "WEB_TOR", tor):
                with self.assertRaises(web.WebAccessError):
                    web._validate_fetch_url("http://127.0.0.1/x")

    def test_dns_all_records_must_be_public(self):
        def fake_gai_public(host, *a, **k):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        def fake_gai_mixed(host, *a, **k):
            return [(2, 1, 6, "", ("93.184.216.34", 0)),
                    (2, 1, 6, "", ("10.0.0.1", 0))]  # one private -> reject

        with mock.patch.object(web.socket, "getaddrinfo", fake_gai_public):
            web._validate_fetch_url("http://example.com/x")  # no raise
        with mock.patch.object(web.socket, "getaddrinfo", fake_gai_mixed):
            with self.assertRaises(web.WebAccessError):
                web._validate_fetch_url("http://example.com/x")

    def test_dns_failure_is_error(self):
        import socket as _socket

        def fake_gai(host, *a, **k):
            raise _socket.gaierror("nope")

        with mock.patch.object(web.socket, "getaddrinfo", fake_gai):
            with self.assertRaises(web.WebAccessError):
                web._validate_fetch_url("http://example.com/x")

    def test_tor_on_skips_local_resolution(self):
        called = {"n": 0}

        def fake_gai(host, *a, **k):
            called["n"] += 1
            return [(2, 1, 6, "", ("10.0.0.1", 0))]

        with mock.patch.object(web.config, "WEB_TOR", True), \
                mock.patch.object(web.socket, "getaddrinfo", fake_gai):
            web._validate_fetch_url("http://example.com/x")  # public name, Tor on
        self.assertEqual(called["n"], 0)   # no DNS leak when Tor resolves remotely


def _html(body=b"<html><body>hi there</body></html>"):
    return httpx.Response(200, content=body,
                          headers={"content-type": "text/html"})


class FetchRedirectTests(unittest.TestCase):
    def setUp(self):
        self._patchers = [
            mock.patch.object(web.config, "WEB_TOR", False),
            mock.patch.object(web.config, "WEB_FETCH_RETRIES", 1),
            mock.patch.object(web.config, "WEB_FETCH_MAX_REDIRECTS", 3),
            mock.patch.object(web.config, "WEB_FETCH_MAX_BYTES", 1_000_000),
            mock.patch.object(web.config, "WEB_FETCH_MAX_CHARS", 10_000),
            mock.patch.object(web.config, "WEB_DOMAIN_DENYLIST", []),
        ]
        for p in self._patchers:
            p.start()
        # Skip DNS entirely: treat every named host as public.
        self._gai = mock.patch.object(
            web.config, "WEB_TOR", False)  # placeholder; overridden per-test

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def _client_factory(self, handler):
        def factory(**kwargs):
            kwargs.pop("proxy", None)
            return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
        return factory

    def test_happy_path_returns_cleaned_text(self):
        def handler(request):
            return _html()

        with mock.patch.object(web.socket, "getaddrinfo",
                               lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]), \
                mock.patch.object(web, "_make_client", self._client_factory(handler)):
            out = web.fetch("http://example.com/")
        self.assertIn("hi there", out["text"])

    def test_redirect_to_private_ip_blocked(self):
        def handler(request):
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

        with mock.patch.object(web.socket, "getaddrinfo",
                               lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]), \
                mock.patch.object(web, "_make_client", self._client_factory(handler)):
            with self.assertRaises(web.WebAccessError):
                web.fetch("http://example.com/")

    def test_redirect_to_metadata_host_blocked(self):
        def handler(request):
            return httpx.Response(
                302, headers={"location": "http://metadata.google.internal/"})

        with mock.patch.object(web.socket, "getaddrinfo",
                               lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]), \
                mock.patch.object(web, "_make_client", self._client_factory(handler)):
            with self.assertRaises(web.WebAccessError):
                web.fetch("http://example.com/")

    def test_too_many_redirects(self):
        def handler(request):
            # Always redirect to another public host -> exceeds the hop cap.
            return httpx.Response(302, headers={"location": "http://example.org/next"})

        with mock.patch.object(web.socket, "getaddrinfo",
                               lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]), \
                mock.patch.object(web, "_make_client", self._client_factory(handler)):
            with self.assertRaises(web.WebAccessError):
                web.fetch("http://example.com/")

    def test_disallowed_content_type(self):
        def handler(request):
            return httpx.Response(200, content=b"\x89PNG",
                                  headers={"content-type": "image/png"})

        with mock.patch.object(web.socket, "getaddrinfo",
                               lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]), \
                mock.patch.object(web, "_make_client", self._client_factory(handler)):
            with self.assertRaises(web.WebAccessError):
                web.fetch("http://example.com/")


if __name__ == "__main__":
    unittest.main()
