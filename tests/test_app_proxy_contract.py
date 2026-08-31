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
from ava_bridge import apps_origin, auth, config

CID = "myapp"
_LOCAL = {"host": "localhost"}
ORIGIN = "http://apps.ava.test:8096"
APPS_HOST = {"host": "apps.ava.test:8096"}


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
        return {"id": CID, "label": "My App", "embed": "iframe",
                "url": f"http://127.0.0.1:{self.port}"}


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
        self.c.cookies.set(apps_origin.cookie_name(CID), apps_origin.mint(CID))
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
