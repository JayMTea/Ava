"""The /apps/<cid> HTTP proxies: header contract, streaming, and both gates.

The two HTTP halves of the app proxy (phone_bridge.app_api_proxy /
app_ui_proxy) were blocking `requests` calls in a threadpool that forwarded an
allowlist of four cache headers. That shape had two measured failure classes:

  * A long-lived response (SSE, a long poll) pinned an anyio worker thread that
    cancellation could never reclaim, and ~40 of those froze every
    run_in_threadpool route in the bridge until restart.
  * Everything outside the allowlist was eaten — every redirect an app issued,
    every Set-Cookie (so a login inside an embedded app could never stick),
    Content-Disposition, WWW-Authenticate.

The proxies are now native async httpx streams with a forward-by-default
header contract and two rewrites (Location, Set-Cookie) so an app keeps
working from under /apps/<cid>/. These tests pin that contract from the
OUTSIDE: a real upstream HTTP app on loopback, the real bridge app, and
assertions on what each side of the hop actually received. The streaming pair
runs the bridge under a real uvicorn on an ephemeral port, because the whole
point — a browser disconnect tears down the upstream call — only exists on a
real socket; an in-process transport buffers and lies.

House style: stdlib unittest, no network beyond localhost — the same shape as
tests/test_app_ws_proxy.py next door.
"""
from __future__ import annotations

import contextlib
import gzip
import http.server
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-appproxy-test-"))

import httpx
from fastapi.testclient import TestClient

import phone_bridge
from ava_bridge import apps_origin, auth, config, embed_route

CID = "myapp"
_LOCAL = {"host": "localhost"}
ORIGIN = "http://apps.ava.test:8096"
APPS_HOST = {"host": "apps.ava.test:8096"}
SHELL = "https://ava.example"

#: What a framed document looks like: a head, a charset meta that has to keep
#: its place, and something after it that must not be jumped.
HTML_PAGE = (b'<!doctype html><html><head><meta charset="utf-8">'
             b'<title>My App</title></head><body>hi</body></html>')

#: The same page with its charset declaration SECOND — the shape that makes the
#: insertion point a question about the browser's encoding prescan.
LATE_CHARSET = (b'<!doctype html><html><head><title>My App</title>'
                b'<meta charset="utf-8"></head><body>hi</body></html>')

LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"

#: What an `<iframe src>` navigation asks for. `<object data>` and `<embed src>`
#: share its `Sec-Fetch-Dest` and send `*/*`, which is the difference the gate
#: rests on — so a test that leaves this out is testing the other branch.
FRAME_ACCEPT = "text/html,application/xhtml+xml,image/avif,*/*;q=0.8"


class _State:
    def __init__(self):
        self.seen: list[tuple[str, str, dict]] = []   # (method, path, headers)
        self.stream_started = threading.Event()
        self.stream_closed = threading.Event()


class _Upstream:
    """A real HTTP app on loopback, dumb on purpose.

    http.server rather than an ASGI app: the contract under test is bytes on a
    socket, and a hand-rolled handler can hold a response open forever and
    OBSERVE its own connection dying — which is the one assertion an in-process
    fake cannot make.
    """

    def __init__(self):
        self.state = _State()
        self.port = 0
        self._srv: http.server.ThreadingHTTPServer | None = None

    def __enter__(self):
        state = self.state

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _record(self):
                state.seen.append(
                    (self.command, self.path.split("?")[0],
                     {k.lower(): v for k, v in self.headers.items()}))

            def _text(self, body: bytes = b"ok", code: int = 200, hdrs=()):
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Served", self.command)
                for k, v in hdrs:
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                self._record()
                p = self.path.split("?")[0]
                port = self.server.server_address[1]
                if p in ("/private-html", "/unversioned-html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    if p == "/private-html":
                        self.send_header("Cache-Control", "private, no-store")
                    self.send_header("Content-Length", "15")
                    self.end_headers()
                    self.wfile.write(b"<p>analysis</p>")
                    return
                if p == "/redir-abs":
                    self.send_response(302)
                    self.send_header("Location",
                                     f"http://127.0.0.1:{port}/dash?x=1")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p == "/redir-rel":
                    self.send_response(302)
                    self.send_header("Location", "/dash")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p == "/redir-ext":
                    self.send_response(302)
                    self.send_header("Location", "https://example.com/away")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if p == "/cookies":
                    # A perfectly ordinary app login: session cookie at its own
                    # root, a second cookie deeper, a Domain for its own host.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header(
                        "Set-Cookie",
                        "sess=abc; Path=/; HttpOnly; SameSite=Lax; Domain=127.0.0.1")
                    self.send_header("Set-Cookie",
                                     "pref=1; Path=/deep; Secure; Max-Age=60")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    return
                if p in ("/page", "/csp-page", "/blocked-page"):
                    # A document the shell would FRAME: a head with a charset
                    # meta in it, an ETag (so the revalidation case is real),
                    # and for two of them an app's own CSP to be honoured.
                    if '"v1"' in (self.headers.get("If-None-Match") or ""):
                        self.send_response(304)
                        self.send_header("ETag", '"v1"')
                        self.end_headers()
                        return
                    body = HTML_PAGE
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("ETag", '"v1"')
                    if p == "/csp-page":
                        self.send_header("Content-Security-Policy",
                                         "script-src 'nonce-abcd1234' 'self'")
                    if p == "/blocked-page":
                        self.send_header("Content-Security-Policy",
                                         "default-src 'none'")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if p == "/missing-page":
                    body = b"<!doctype html><html><head></head><body>gone</body></html>"
                    self.send_response(404)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if p == "/vary-page":
                    # An app that already negotiates. Injection adds an axis to
                    # what it named; replacing it would have a cache serve a
                    # representation negotiated for somebody else.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Vary", "Accept-Encoding")
                    self.send_header("Content-Length", str(len(HTML_PAGE)))
                    self.end_headers()
                    self.wfile.write(HTML_PAGE)
                    return
                if p == "/late-charset":
                    # The head opens with a <title>, so the charset declaration
                    # is NOT first — and the Content-Type names no charset, so
                    # that declaration is the only thing deciding how the app's
                    # own text decodes.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(LATE_CHARSET)))
                    self.end_headers()
                    self.wfile.write(LATE_CHARSET)
                    return
                if p == "/gzip-page":
                    # Ignores `Accept-Encoding: identity` and compresses anyway.
                    # Nothing can be injected into it, so nothing about it may
                    # be marked as carrying the shim.
                    body = gzip.compress(HTML_PAGE)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("ETag", '"v1"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if p in ("/lastmod-page", "/weak-etag-page"):
                    # The two ordinary shapes with no validator the bridge can
                    # mark: `Last-Modified` and no ETag at all (python's own
                    # http.server, and most small static servers), and an ETag
                    # that is not a quoted entity-tag. Both revalidate by DATE,
                    # which is the door `mint_etag` exists to keep open.
                    if self.headers.get("If-Modified-Since") == LAST_MODIFIED:
                        self.send_response(304)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Last-Modified", LAST_MODIFIED)
                    if p == "/weak-etag-page":
                        self.send_header("ETag", "not-an-entity-tag")
                    self.send_header("Content-Length", str(len(HTML_PAGE)))
                    self.end_headers()
                    self.wfile.write(HTML_PAGE)
                    return
                if p == "/poster.pdf":
                    # What an <object data> or <embed src> asks for: the same
                    # framed destination, but `Accept: */*` and a body with no
                    # head in it, ever.
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("ETag", '"strong-v1"')
                    self.send_header("Content-Length", "5")
                    self.end_headers()
                    self.wfile.write(b"%PDF-")
                    return
                if p == "/echo":
                    body = json.dumps(
                        {"cookie": self.headers.get("Cookie"),
                         "authorization": self.headers.get("Authorization")},
                    ).encode()
                    self._text(body)
                    return
                if p == "/events":
                    # An SSE feed that never ends: first event immediately,
                    # then heartbeats until the CONNECTION dies. The closed
                    # event is the observation the disconnect test rests on.
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        self.wfile.write(b"data: first\n\n")
                        self.wfile.flush()
                        state.stream_started.set()
                        while True:
                            time.sleep(0.1)
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                    except OSError:
                        state.stream_closed.set()
                    return
                self._text()

            def do_HEAD(self):
                self._record()
                self._text(b"")

            def do_OPTIONS(self):
                self._record()
                self._text(b"", hdrs=[("Allow", "GET, POST")])

            def do_POST(self):
                self._record()
                n = int(self.headers.get("Content-Length") or 0)
                self._text(b"got:" + self.rfile.read(n))

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()

    @property
    def meta(self) -> dict:
        # `route` is what `connectors.app` resolves from `ui.route`; stated here
        # rather than left out so the default this fixture exercises is visible.
        return {"id": CID, "label": "My App", "embed": "iframe",
                "url": f"http://127.0.0.1:{self.port}", "route": "auto"}


def _authed() -> TestClient:
    c = TestClient(phone_bridge.app, base_url="http://localhost")
    c.cookies.set(config.COOKIE_NAME, auth._make_token())
    return c


def _closed_port() -> int:
    with socket.socket() as s:   # bind :0 and release it — nothing listens
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HeaderContractTests(unittest.TestCase):
    """Single-origin: what crosses the hop, in both directions."""

    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.up = self._stack.enter_context(_Upstream())
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app", return_value=self.up.meta))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_token", return_value=""))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_api", return_value=None))
        self.c = _authed()

    # --- redirects ------------------------------------------------------------

    def test_an_app_internal_absolute_redirect_is_repointed_at_the_proxy(self):
        r = self.c.get(f"/apps/{CID}/redir-abs", headers=_LOCAL,
                       follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], f"/apps/{CID}/dash?x=1",
                         "an absolute Location on the app's own host:port must "
                         "come back mapped under the proxy prefix")

    def test_a_root_relative_redirect_is_repointed_at_the_proxy(self):
        r = self.c.get(f"/apps/{CID}/redir-rel", headers=_LOCAL,
                       follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], f"/apps/{CID}/dash",
                         "forwarded verbatim, /dash walks the browser out of "
                         "the proxy onto Ava's own routes")

    def test_a_redirect_to_another_host_passes_through_untouched(self):
        r = self.c.get(f"/apps/{CID}/redir-ext", headers=_LOCAL,
                       follow_redirects=False)
        self.assertEqual(r.headers["location"], "https://example.com/away",
                         "an OAuth hop or docs link is the app's business")

    # --- cookies --------------------------------------------------------------

    def test_set_cookie_is_rescoped_and_every_cookie_survives(self):
        r = self.c.get(f"/apps/{CID}/cookies", headers=_LOCAL)
        cookies = r.headers.get_list("set-cookie")
        self.assertEqual(len(cookies), 2,
                         "both upstream Set-Cookie headers must survive as "
                         "separate headers — folding corrupts them")
        sess = next(c for c in cookies if c.startswith("sess="))
        pref = next(c for c in cookies if c.startswith("pref="))
        self.assertIn(f"Path=/apps/{CID}/", sess)
        self.assertNotIn("Domain", sess,
                         "a Domain naming the app's host makes the browser "
                         "reject the whole cookie")
        self.assertIn("HttpOnly", sess)
        self.assertIn("SameSite=Lax", sess)
        self.assertIn(f"Path=/apps/{CID}/deep", pref)
        self.assertIn("Secure", pref)
        self.assertIn("Max-Age=60", pref)

    def test_private_html_retains_no_store_and_unversioned_html_revalidates(self):
        private = self.c.get(f"/apps/{CID}/private-html", headers=_LOCAL)
        self.assertEqual(private.headers["cache-control"], "private, no-store")
        ordinary = self.c.get(f"/apps/{CID}/unversioned-html", headers=_LOCAL)
        self.assertEqual(ordinary.headers["cache-control"], "no-cache")

    def test_browser_cookies_forward_except_avas_own(self):
        self.c.cookies.set("myapp_session", "zzz")
        self.c.cookies.set("other", "1")
        self.c.cookies.set(apps_origin.cookie_name(CID), "embedtok")
        r = self.c.get(f"/apps/{CID}/echo", headers=_LOCAL)
        got = r.json()["cookie"] or ""
        self.assertIn("myapp_session=zzz", got,
                      "the app's own session must survive the hop")
        self.assertIn("other=1", got)
        self.assertNotIn(config.COOKIE_NAME, got,
                         "Ava's session cookie reached the embedded app")
        self.assertNotIn(apps_origin.cookie_name(CID), got,
                         "the embed cookie is Ava's infrastructure, not the "
                         "app's to see")

    # --- bearer injection -----------------------------------------------------

    def test_the_saved_credential_is_injected_and_wins(self):
        with mock.patch("ava_bridge.connectors.app_token",
                        return_value="s3cret"):
            r = self.c.get(f"/apps/{CID}/echo", headers={
                **_LOCAL, "authorization": "Bearer stale-from-app-storage"})
        self.assertEqual(r.json()["authorization"], "Bearer s3cret",
                         "the connector's saved credential is authoritative")

    def test_the_browsers_own_bearer_rides_through_when_none_is_saved(self):
        r = self.c.get(f"/apps/{CID}/echo",
                       headers={**_LOCAL, "authorization": "Bearer mine"})
        self.assertEqual(r.json()["authorization"], "Bearer mine")

    def test_the_data_proxy_injects_the_declared_api_token(self):
        cfg = {"base": f"http://127.0.0.1:{self.up.port}", "prefix": "",
               "token": "cfg-tok"}
        with mock.patch("ava_bridge.connectors.app_api", return_value=cfg):
            r = self.c.get(f"/apps/{CID}/api/echo", headers=_LOCAL)
        self.assertEqual(r.json()["authorization"], "Bearer cfg-tok")
        self.assertEqual(self.up.state.seen[-1][1], "/echo",
                         "ui.api routes to the API base, not the UI")

    def test_the_api_route_still_falls_through_to_the_ui_proxy(self):
        # No declared ui.api, single origin: /api/* is the app's own same-origin
        # API and must reach it through the UI proxy rather than 404.
        r = self.c.get(f"/apps/{CID}/api/echo", headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.up.state.seen[-1][1], "/api/echo")

    # --- methods --------------------------------------------------------------

    def test_head_and_options_are_proxied_not_405d(self):
        r = self.c.head(f"/apps/{CID}/thing", headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-served"), "HEAD")
        r = self.c.request("OPTIONS", f"/apps/{CID}/thing", headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-served"), "OPTIONS")
        self.assertEqual(r.headers.get("allow"), "GET, POST",
                         "the app's own Allow must reach the caller")
        cfg = {"base": f"http://127.0.0.1:{self.up.port}", "prefix": "",
               "token": ""}
        with mock.patch("ava_bridge.connectors.app_api", return_value=cfg):
            r = self.c.head(f"/apps/{CID}/api/thing", headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.up.state.seen[-1][:2], ("HEAD", "/thing"))

    def test_a_request_body_reaches_the_app(self):
        r = self.c.post(f"/apps/{CID}/submit", headers=_LOCAL,
                        content=b"hello")
        self.assertEqual(r.text, "got:hello")

    # --- failure & gate -------------------------------------------------------

    def test_a_down_app_still_classifies_as_down(self):
        # httpx buries the ConnectionRefusedError inside an ExceptionGroup
        # where connectors._errno_cause's linear walk cannot see it; the
        # `<cid>_down` spelling is what the frontend's fix-it link keys on, so
        # losing it in the transport swap would be a silent regression.
        meta = dict(self.up.meta, url=f"http://127.0.0.1:{_closed_port()}")
        with mock.patch("ava_bridge.connectors.app", return_value=meta):
            r = self.c.get(f"/apps/{CID}/x",
                           headers={**_LOCAL, "accept": "application/json"})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["error_code"], f"{CID}_down")

    def test_an_unauthenticated_request_is_refused_before_the_app(self):
        anon = TestClient(phone_bridge.app, base_url="http://localhost")
        r = anon.get(f"/apps/{CID}/echo", headers=_LOCAL)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.up.state.seen, [],
                         "the upstream app was dialled for an unauthenticated "
                         "caller")


class RouteInjectionTests(unittest.TestCase):
    """Route memory, added to the app's document by the proxy.

    An embedded app is reopened where the owner left it only if something tells
    the shell where that was, and only code inside the app's document can see an
    SPA navigation. Rather than that being a feature the one app that opted in
    has, the proxy adds the reporter (ava_bridge/embed_route.py) as the document
    streams past. The decision half is unit-tested in tests/test_embed_route.py;
    these run it through the REAL proxy and assert on what each side of the hop
    received, because the gate is the request's Fetch Metadata and the app's own
    headers — neither of which exists in a pure test.
    """

    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.up = self._stack.enter_context(_Upstream())
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app", return_value=self.up.meta))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_token", return_value=""))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_api", return_value=None))
        self._stack.enter_context(mock.patch.object(config, "PUBLIC_URL", SHELL))
        self.c = _authed()

    def _framed(self, path="/page", **kw):
        headers = {**_LOCAL, "sec-fetch-dest": "iframe",
                   "accept": FRAME_ACCEPT, **kw.pop("headers", {})}
        return self.c.get(f"/apps/{CID}{path}", headers=headers, **kw)

    def _mode(self, mode: str):
        return mock.patch("ava_bridge.connectors.app",
                          return_value=dict(self.up.meta, route=mode))

    # --- what gets the shim ---------------------------------------------------

    def test_a_framed_document_gets_the_shim_first_in_head(self):
        r = self._framed()
        self.assertEqual(r.status_code, 200)
        self.assertIn("ava:navigation", r.text,
                      "the injected reporter speaks the protocol the shell "
                      "already validates")
        self.assertIn(f'var CID = "{CID}"', r.text)
        self.assertIn(f'var SHELL = "{SHELL}"', r.text)
        head = r.text.index("<script")
        self.assertLess(r.text.index('charset="utf-8"'), head,
                        "the charset declaration has to stay inside the first "
                        "1024 bytes the sniffer reads")
        self.assertLess(head, r.text.index("<title>"),
                        "Next.js's router captures history.pushState at "
                        "startup, so the wrapper has to be installed first")
        self.assertTrue(r.text.startswith("<!doctype html>"),
                        "a tag before the doctype is quirks mode")

    def test_an_apps_own_fetch_is_never_edited(self):
        r = self.c.get(f"/apps/{CID}/page",
                       headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertEqual(r.content, HTML_PAGE,
                         "an app fetching an HTML fragment is about to hand it "
                         "to innerHTML; our script has no business in it")

    def test_framed_media_is_left_entirely_alone(self):
        # `<object data>` and `<embed src>` carry a FRAMED destination and ask
        # for `*/*`. Reading that as a document navigation would cost every
        # framed PDF, video and image its compression and its strong ETag —
        # which is also what `If-Range` byte-range resumption needs.
        r = self.c.get(f"/apps/{CID}/poster.pdf",
                       headers={**_LOCAL, "sec-fetch-dest": "object",
                                "accept": "*/*",
                                "accept-encoding": "gzip, br"})
        self.assertEqual(r.headers["etag"], '"strong-v1"',
                         "a weakened validator is an If-Range that never "
                         "resumes")
        self.assertNotIn("vary", r.headers)
        self.assertEqual(self.up.state.seen[-1][2].get("accept-encoding"),
                         "gzip, br")

    def test_a_request_with_no_fetch_metadata_is_never_edited(self):
        r = self.c.get(f"/apps/{CID}/page",
                       headers={**_LOCAL, "accept": "text/html"})
        self.assertEqual(r.content, HTML_PAGE,
                         "guessing from Accept picks an error page's media "
                         "type; guessing here edits somebody's document")

    def test_a_non_200_and_a_non_html_body_are_left_alone(self):
        missing = self._framed("/missing-page")
        self.assertEqual(missing.status_code, 404)
        self.assertNotIn("<script", missing.text,
                         "a 404 body is the app's own error page, not the page "
                         "route memory is for")
        plain = self._framed("/thing")
        self.assertEqual(plain.text, "ok")

    def test_route_self_and_off_inject_nothing(self):
        for mode in ("self", "off"):
            with self.subTest(route=mode), self._mode(mode):
                r = self._framed()
                self.assertEqual(r.content, HTML_PAGE,
                                 "`self` already reports for itself and a "
                                 "second reporter doubles every message; `off` "
                                 "means hands off")

    # --- the app's own policy -------------------------------------------------

    def test_a_nonce_is_adopted_and_the_policy_travels_unchanged(self):
        r = self._framed("/csp-page")
        self.assertIn('<script nonce="abcd1234">', r.text)
        self.assertEqual(r.headers["content-security-policy"],
                         "script-src 'nonce-abcd1234' 'self'",
                         "an app's policy is read, never edited or widened")

    def test_a_policy_that_blocks_everything_gets_nothing(self):
        r = self._framed("/blocked-page")
        self.assertEqual(r.content, HTML_PAGE)
        self.assertEqual(r.headers["content-security-policy"], "default-src 'none'")

    def test_the_external_tag_is_served_by_ava_and_never_by_the_app(self):
        r = self.c.get(f"/apps/{CID}/.ava/route.js", headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/javascript", r.headers["content-type"])
        self.assertIn("ava:navigation", r.text)
        self.assertNotIn("/.ava/route.js", [p for _, p, _ in self.up.state.seen],
                         "`.ava` is Ava's reserved namespace on an app's mount; "
                         "the upstream must never be asked for it")
        again = self.c.get(f"/apps/{CID}/.ava/route.js",
                           headers={**_LOCAL, "if-none-match": r.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        with self._mode("off"):
            self.assertEqual(
                self.c.get(f"/apps/{CID}/.ava/route.js", headers=_LOCAL).status_code,
                404, "`off` says the bridge is not reporting this app's route; "
                     "a fetchable reporter would contradict it")

    # --- caching --------------------------------------------------------------

    def test_a_copy_cached_before_this_shipped_is_refetched_once(self):
        # The case that decides whether route memory ever reaches an owner whose
        # app already sits in their browser cache: its entry document carries an
        # ETag, so the browser revalidates rather than refetching, and an
        # untouched 304 would keep a shim-less document alive forever.
        fresh = self._framed()
        served = fresh.headers["etag"]
        self.assertNotEqual(served, '"v1"')
        self.assertEqual(embed_route.unmark_etag(served), '"v1"')

        stale = self._framed(headers={"if-none-match": '"v1"'})
        self.assertEqual(stale.status_code, 200)
        self.assertIn("ava:navigation", stale.text)
        self.assertIsNone(self.up.state.seen[-1][2].get("if-none-match"),
                          "an unmarked tag must not reach the app, or it "
                          "answers 304 and the browser keeps the old document")

        revalidated = self._framed(headers={"if-none-match": served})
        self.assertEqual(revalidated.status_code, 304)
        self.assertEqual(revalidated.headers["etag"], served)
        self.assertEqual(self.up.state.seen[-1][2].get("if-none-match"), '"v1"',
                         "our own marker is stripped so the app can answer the "
                         "conditional it issued")

    def test_an_uninjected_copy_is_never_marked_as_carrying_the_shim(self):
        # The marker's whole contract is "a copy with this tag has the script in
        # it". An app that gzips despite `Accept-Encoding: identity` cannot be
        # injected into — and a marked ETag over that copy would have the
        # browser 304 against it forever, so route memory would be silently
        # dead for that app: the exact failure the namespace exists to prevent.
        r = self._framed("/gzip-page")
        self.assertEqual(r.headers["content-encoding"], "gzip")
        self.assertEqual(r.headers["etag"], '"v1"')
        self.assertFalse(embed_route.marked(r.headers["etag"]))
        self._framed("/gzip-page", headers={"if-none-match": '"v1"'})
        self.assertIsNone(self.up.state.seen[-1][2].get("if-none-match"),
                          "an unmarked tag is still dropped, so the app is "
                          "asked for a body we could inject into")

    def test_an_app_with_only_a_last_modified_still_revalidates(self):
        # python's http.server and most small static servers: a date and no
        # ETag. The conditional headers are dropped on every injecting hop, so
        # without a validator of our own this app would refetch its whole
        # document, uncompressed, on every single frame load, forever.
        fresh = self._framed("/lastmod-page")
        self.assertIn("ava:navigation", fresh.text)
        served = fresh.headers["etag"]
        self.assertTrue(embed_route.marked(served))
        self.assertIsNone(embed_route.unmark_etag(served),
                          "minted: there is no app tag inside it")

        back = self._framed("/lastmod-page",
                            headers={"if-none-match": served,
                                     "if-modified-since": LAST_MODIFIED})
        self.assertEqual(back.status_code, 304)
        self.assertEqual(self.up.state.seen[-1][2].get("if-modified-since"),
                         LAST_MODIFIED,
                         "seeing a tag of ours is what lets the app's own "
                         "validator through")
        self.assertIsNone(self.up.state.seen[-1][2].get("if-none-match"),
                          "a minted tag stands for no app tag at all and must "
                          "never be offered upstream")

    def test_an_app_whose_etag_cannot_be_round_tripped_still_revalidates(self):
        # `mark_etag` hands an unquoted tag back verbatim, so serving it would
        # have the browser offer a conditional on every frame load that
        # `upstream_if_none_match` then drops — a full refetch every time.
        fresh = self._framed("/weak-etag-page")
        self.assertIn("ava:navigation", fresh.text)
        served = fresh.headers["etag"]
        self.assertNotEqual(served, "not-an-entity-tag")
        self.assertTrue(embed_route.marked(served))
        back = self._framed("/weak-etag-page",
                            headers={"if-none-match": served,
                                     "if-modified-since": LAST_MODIFIED})
        self.assertEqual(back.status_code, 304)

    def test_the_varying_axis_is_declared_and_the_apps_own_is_kept(self):
        # The body genuinely differs by Sec-Fetch-Dest now. A cache keyed on the
        # URL alone would hand the app's own fetch a copy with Ava's script in
        # it, or hand a frame an uninjected one and switch route memory off.
        r = self._framed("/vary-page")
        self.assertEqual(r.headers["vary"], "Accept-Encoding, Sec-Fetch-Dest",
                         "merged with what the app named, never replacing it")
        plain = self._framed()
        self.assertEqual(plain.headers["vary"], "Sec-Fetch-Dest")

    def test_the_uninjected_copy_declares_the_axis_too(self):
        # Only one of the two representations carries the shim, so BOTH have to
        # say the axis exists. A cache that stored the app's own fetch of its
        # HTML — never injected, by design — under a key that does not mention
        # Sec-Fetch-Dest would answer the next frame navigation with it, and
        # route memory would be off with nothing on screen to show it.
        own = self.c.get(f"/apps/{CID}/page",
                         headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertEqual(own.content, HTML_PAGE)
        self.assertEqual(own.headers["vary"], "Sec-Fetch-Dest")
        asset = self.c.get(f"/apps/{CID}/poster.pdf",
                           headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertNotIn("vary", asset.headers,
                         "an asset is the same bytes either way; splitting its "
                         "cache entry buys nothing")
        with self._mode("off"):
            plain = self.c.get(f"/apps/{CID}/page",
                               headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertNotIn("vary", plain.headers,
                         "a connector that is never injected has no axis")

    def test_a_charset_declaration_that_is_not_first_keeps_its_window(self):
        # Served with a bare `text/html`, so the document's own <meta charset>
        # is the only thing deciding how the app's text decodes. A ~9 KB tag in
        # front of it pushes it past the 1024 bytes the browser's encoding
        # prescan reads — and the app renders as mojibake in Ava's frame and
        # nowhere else.
        body = self._framed("/late-charset").content
        self.assertIn(b"ava:navigation", body)
        self.assertLess(body.index(b'<meta charset="utf-8">'),
                        embed_route.PRESCAN)
        self.assertLess(body.index(b'<meta charset="utf-8">'),
                        body.index(b"<script"))

    def test_the_apps_cache_policy_is_otherwise_untouched(self):
        # Pinned next door for the un-injected path; re-asserted here because
        # injection rewrites a header in the same response.
        r = self._framed()
        self.assertEqual(r.headers["cache-control"], "no-cache")
        private = self._framed("/private-html")
        self.assertEqual(private.headers["cache-control"], "private, no-store")

    # --- transport ------------------------------------------------------------

    def test_identity_is_asked_for_on_framed_documents_only(self):
        self._framed(headers={"accept-encoding": "gzip, deflate, br"})
        self.assertEqual(self.up.state.seen[-1][2].get("accept-encoding"),
                         "identity",
                         "a gzipped body cannot be scanned for the insertion "
                         "point, and the proxy forwards encodings verbatim")
        self.c.get(f"/apps/{CID}/asset.js",
                   headers={**_LOCAL, "sec-fetch-dest": "script",
                            "accept-encoding": "gzip, deflate, br"})
        self.assertEqual(self.up.state.seen[-1][2].get("accept-encoding"),
                         "gzip, deflate, br",
                         "every subresource keeps the browser's own "
                         "negotiation byte for byte")
        with self._mode("off"):
            self._framed(headers={"accept-encoding": "gzip"})
        self.assertEqual(self.up.state.seen[-1][2].get("accept-encoding"), "gzip")

    def test_the_document_still_arrives_in_pieces(self):
        # The injector holds bytes back only until it knows where the tag goes.
        # Content-Length is dropped for every proxied response, so the longer
        # body needs no fix-up — and claiming the app's length would truncate it.
        r = self._framed()
        self.assertNotIn("content-length", r.headers)
        self.assertGreater(len(r.content), len(HTML_PAGE))
        self.assertTrue(r.content.endswith(b"</body></html>"))


def _origin_settings():
    """apps.origin configured, plus the trusted hosts the test hosts need."""
    from ava_bridge import settings as _settings

    def fake(key, default=None, env=None):
        if key == "apps.origin":
            return ORIGIN
        if key == "server.trusted_hosts":
            return ["apps.ava.test", "ava.test", "localhost"]
        return default

    return mock.patch.object(_settings, "get", side_effect=fake)


class OriginSplitTokenTests(unittest.TestCase):
    """The embed-token gate with `apps.origin` on: scope, renewal, expiry."""

    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.up = self._stack.enter_context(_Upstream())
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app", return_value=self.up.meta))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_token", return_value=""))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_api", return_value=None))
        self._stack.enter_context(_origin_settings())
        self.c = TestClient(phone_bridge.app, base_url=ORIGIN)

    def test_app_a_token_is_refused_for_app_b(self):
        self.assertFalse(apps_origin.verify("other", apps_origin.mint(CID)),
                         "a token is HMAC-bound to its cid")
        r = self.c.get(f"/apps/other/?t={apps_origin.mint(CID)}",
                       headers={**APPS_HOST, "sec-fetch-dest": "iframe"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.up.state.seen, [])

    def test_a_valid_token_loads_and_is_exchanged_for_the_cookie(self):
        r = self.c.get(f"/apps/{CID}/?t={apps_origin.mint(CID)}",
                       headers={**APPS_HOST, "sec-fetch-dest": "iframe"})
        self.assertEqual(r.status_code, 200)
        set_cookie = "; ".join(r.headers.get_list("set-cookie"))
        self.assertIn(apps_origin.cookie_name(CID), set_cookie)
        self.assertIn(f"Max-Age={apps_origin.COOKIE_TTL_S}", set_cookie,
                      "the cookie lives its own, longer life — not the URL token's")

    def test_authorized_deep_link_returns_to_the_shell_origin(self):
        with mock.patch.object(config, "PUBLIC_URL", "http://ava.test:8096"):
            response = self.c.get(
                f"/apps/{CID}/reports?model=approved&t={apps_origin.mint(CID)}",
                headers={**APPS_HOST, "sec-fetch-dest": "document"},
                follow_redirects=False,
            )
        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(response.headers["location"],
                         f"http://ava.test:8096/#{CID}/reports?model=approved")
        self.assertEqual(self.up.state.seen, [])

    def test_an_aging_cookie_is_renewed_so_an_active_panel_never_expires(self):
        old = apps_origin.mint(CID, ttl_s=100)      # past the 150s half-life
        self.c.cookies.set(apps_origin.cookie_name(CID), old)
        r = self.c.get(f"/apps/{CID}/echo",
                       headers={**APPS_HOST, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 200)
        renewed = [v for v in r.headers.get_list("set-cookie")
                   if v.startswith(apps_origin.cookie_name(CID) + "=")]
        self.assertTrue(renewed, "no replacement token was set — the panel "
                                 "dies at TOKEN_TTL_S exactly as before")
        tok = renewed[0].split("=", 1)[1].split(";", 1)[0]
        self.assertTrue(apps_origin.verify(CID, tok))
        self.assertGreater(int(tok.split(".", 1)[0]),
                           int(old.split(".", 1)[0]),
                           "the replacement must expire later than what it "
                           "replaces")

    def test_a_fresh_cookie_is_not_churned(self):
        # A cookie is minted for COOKIE_TTL_S at the exchange; a five-minute one
        # would be an aging cookie now, and renewed on sight.
        self.c.cookies.set(apps_origin.cookie_name(CID),
                           apps_origin.mint(CID, ttl_s=apps_origin.COOKIE_TTL_S))
        r = self.c.get(f"/apps/{CID}/echo",
                       headers={**APPS_HOST, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 200)
        renewed = [v for v in r.headers.get_list("set-cookie")
                   if v.startswith(apps_origin.cookie_name(CID) + "=")]
        self.assertEqual(renewed, [],
                         "a token in its first half-life needs no Set-Cookie "
                         "per asset load")

    def test_a_genuinely_stale_token_reenters_through_the_shell(self):
        # Subresources keep the hard 403 — the refusal IS the boundary — but a
        # top-level return to a dead panel gets the shell, which mints afresh.
        stale = apps_origin.mint(CID, ttl_s=-1)
        r = self.c.get(f"/apps/{CID}/?t={stale}",
                       headers={**APPS_HOST, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 403)
        r = self.c.get(f"/apps/{CID}/?t={stale}",
                       headers={**APPS_HOST, "sec-fetch-dest": "document"},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["location"].endswith(f"/#{CID}"))

    def test_a_dead_frame_navigation_gets_the_reconnect_page_not_json(self):
        # The case between the two above: the FRAME itself navigating on a token
        # the bridge no longer accepts. JSON there was rendered as the app's whole
        # UI; the page asks the shell for a fresh URL instead. Still a 403, still
        # nothing proxied — the refusal is unchanged, only its body can recover.
        stale = apps_origin.mint(CID, ttl_s=-1)
        r = self.c.get(f"/apps/{CID}/reports/7?x=1&t={stale}",
                       headers={**APPS_HOST, "sec-fetch-dest": "iframe"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertIn('"type": "ava:embed-expired"', r.text)
        self.assertIn(f'"cid": "{CID}"', r.text)
        self.assertIn('"path": "/reports/7?x=1"', r.text)
        self.assertEqual(self.up.state.seen, [])

    def test_the_keepalive_renews_the_cookie_without_touching_the_app(self):
        # The shell's heartbeat for a frame it keeps mounted: past half-life the
        # gate re-sets the cookie, and the app never learns a request was made.
        self.c.cookies.set(apps_origin.cookie_name(CID), apps_origin.mint(CID, ttl_s=100))
        r = self.c.get(f"/apps/{CID}/.ava/keepalive",
                       headers={**APPS_HOST, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.up.state.seen, [],
                         "a heartbeat is the bridge's business, never the app's")
        renewed = [v for v in r.headers.get_list("set-cookie")
                   if v.startswith(apps_origin.cookie_name(CID) + "=")]
        self.assertTrue(renewed, "an aging cookie must come back renewed")
        self.assertTrue(apps_origin.verify(CID, renewed[0].split("=", 1)[1].split(";", 1)[0]))

    def test_the_keepalive_is_gated_like_everything_else_on_the_apps_host(self):
        r = self.c.get(f"/apps/{CID}/.ava/keepalive",
                       headers={**APPS_HOST, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "forbidden")
        self.assertEqual(self.up.state.seen, [])


class NativeUiApiTests(unittest.TestCase):
    """With the split ON, a native view's ui.api calls stay on Ava's origin.

    A native app view is part of Ava's own bundle — no iframe, no embed token,
    no way to be handed one. Before the carve-out in apps_origin.refuses(),
    turning apps.origin on broke every native panel's data calls with a 404.
    The session cookie is the gate on this host; the iframe boundary is
    untouched (asserted below by refusing the app's UI documents here).
    """

    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.addCleanup(self._stack.close)
        self.up = self._stack.enter_context(_Upstream())
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app", return_value=self.up.meta))
        self._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_token", return_value=""))
        self.cfg = {"base": f"http://127.0.0.1:{self.up.port}", "prefix": "",
                    "token": "apitok"}
        self._stack.enter_context(_origin_settings())
        self.c = _authed()

    def test_the_data_proxy_works_from_avas_own_origin(self):
        with mock.patch("ava_bridge.connectors.app_api", return_value=self.cfg):
            r = self.c.get(f"/apps/{CID}/api/echo",
                           headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["authorization"], "Bearer apitok")

    def test_but_needs_avas_session(self):
        anon = TestClient(phone_bridge.app, base_url="http://localhost")
        with mock.patch("ava_bridge.connectors.app_api", return_value=self.cfg):
            r = anon.get(f"/apps/{CID}/api/echo", headers=_LOCAL)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.up.state.seen, [])

    def test_the_apps_ui_documents_stay_off_avas_origin(self):
        with mock.patch("ava_bridge.connectors.app_api", return_value=self.cfg):
            r = self.c.get(f"/apps/{CID}/index.html",
                           headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 404,
                         "the carve-out must admit /apps/<cid>/api/*, never "
                         "the UI proxy")

    def test_no_declared_api_means_no_fall_through_across_the_split(self):
        with mock.patch("ava_bridge.connectors.app_api", return_value=None):
            r = self.c.get(f"/apps/{CID}/api/echo",
                           headers={**_LOCAL, "sec-fetch-dest": "empty"})
        self.assertEqual(r.status_code, 404,
                         "the api->ui fall-through would serve the app's "
                         "documents on Ava's origin — the hole the split "
                         "exists to close")
        self.assertEqual(self.up.state.seen, [])

    def test_path_classifier(self):
        self.assertTrue(apps_origin.is_app_api_path("/apps/crm/api"))
        self.assertTrue(apps_origin.is_app_api_path("/apps/crm/api/x/y"))
        for p in ("/apps/crm/", "/apps/crm/apiary", "/apps", "/apps//api",
                  "/api/hub/x"):
            self.assertFalse(apps_origin.is_app_api_path(p), p)


class StreamingTests(unittest.TestCase):
    """The headline defect: streams must flow incrementally and DIE on
    disconnect. Run against a real uvicorn — an in-process transport buffers
    the response and never surfaces the browser going away, which is exactly
    the behaviour under test.
    """

    @classmethod
    def setUpClass(cls):
        import uvicorn
        cls._stack = contextlib.ExitStack()
        cls.up = cls._stack.enter_context(_Upstream())
        cls._stack.enter_context(
            mock.patch("ava_bridge.connectors.app", return_value=cls.up.meta))
        cls._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_token", return_value=""))
        cls._stack.enter_context(
            mock.patch("ava_bridge.connectors.app_api", return_value=None))
        cls._server = uvicorn.Server(uvicorn.Config(
            phone_bridge.app, host="127.0.0.1", port=0, log_level="error"))
        threading.Thread(target=cls._server.run, daemon=True).start()
        deadline = time.time() + 15
        while not cls._server.started:
            assert time.time() < deadline, "bridge server never started"
            time.sleep(0.05)
        cls.bport = cls._server.servers[0].sockets[0].getsockname()[1]

    @classmethod
    def tearDownClass(cls):
        cls._server.should_exit = True
        cls._stack.close()

    def test_a_stream_arrives_incrementally_and_disconnect_closes_upstream(self):
        cookie = f"{config.COOKIE_NAME}={auth._make_token()}"
        buf = b""
        t0 = time.time()
        with httpx.Client(timeout=10) as hc:
            with hc.stream(
                    "GET",
                    f"http://127.0.0.1:{self.bport}/apps/{CID}/events",
                    headers={"cookie": cookie}) as r:
                self.assertEqual(r.status_code, 200)
                for chunk in r.iter_raw():
                    buf += chunk
                    if b"data: first" in buf:
                        break
        # The upstream NEVER finishes its response, so receiving the first
        # event at all proves incremental delivery rather than buffering.
        self.assertLess(time.time() - t0, 8,
                        "first SSE event took too long — the proxy is "
                        "buffering the stream")
        self.assertTrue(self.up.state.stream_started.is_set())
        # Leaving the `stream` context hung up the browser side mid-stream.
        # That must propagate: the response task is cancelled and its
        # background task closes the upstream call. Before the rewrite this
        # is where a worker thread was pinned forever instead.
        self.assertTrue(
            self.up.state.stream_closed.wait(20),
            "the upstream app never observed the close — the proxy holds "
            "the connection after the browser is gone")


if __name__ == "__main__":
    unittest.main()


class BasePathRewriteTests(unittest.TestCase):
    """An app served under its own basePath (Next.js `basePath`) redirects and
    scopes cookies WITH that prefix; the proxy must strip it before adding the
    mount, or the mount doubles (`/apps/healthapp/apps/healthapp`) and the first iframe
    load 404s."""

    BASE = "http://127.0.0.1:3000/apps/healthapp"

    def test_root_relative_location_strips_the_upstream_base_path(self):
        import phone_bridge as pb
        self.assertEqual(pb._rewrite_location("/apps/healthapp/login", "healthapp", self.BASE),
                         "/apps/healthapp/login")
        self.assertEqual(pb._rewrite_location("/apps/healthapp", "healthapp", self.BASE),
                         "/apps/healthapp/")
        # a path OUTSIDE the base is still mounted (the app really walked out)
        self.assertEqual(pb._rewrite_location("/other", "healthapp", self.BASE),
                         "/apps/healthapp/other")
        # no base path -> unchanged behaviour
        self.assertEqual(pb._rewrite_location("/login", "senses", "http://127.0.0.1:8081"),
                         "/apps/senses/login")

    def test_set_cookie_path_strips_the_upstream_base_path(self):
        import phone_bridge as pb
        self.assertIn("Path=/apps/healthapp/",
                      pb._rewrite_set_cookie("s=1; Path=/apps/healthapp; HttpOnly", "healthapp", self.BASE))
        self.assertIn("Path=/apps/healthapp/x",
                      pb._rewrite_set_cookie("s=1; Path=/apps/healthapp/x", "healthapp", self.BASE))
        self.assertIn("Path=/apps/healthapp/",
                      pb._rewrite_set_cookie("s=1; Path=/", "healthapp", self.BASE))
