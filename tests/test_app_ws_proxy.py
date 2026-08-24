"""The websocket half of the /apps/<cid> proxy.

Its two HTTP twins are `requests` calls in a threadpool, and neither `requests`
nor `httpx` can speak WebSocket — so before this route existed, a `GET` carrying
`Upgrade: websocket` matched the HTTP catch-all and was executed as an ordinary
`requests.get`. It never performed the handshake, and the failure looked like the
app being broken.

What is worth pinning here is not "bytes move". It is the four things that are
easy to get subtly wrong and impossible to notice afterwards: the gate runs
before accept, the connector's credential stays on the bridge, the subprotocol
is negotiated rather than guessed, and an unreachable app fails as a refused
handshake rather than as an accepted socket that immediately dies.

House style: stdlib unittest, a real app via TestClient, a real upstream socket
on loopback. No network beyond localhost.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-wsproxy-test-"))

import websockets
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ava_bridge import auth, config

_LOCAL = {"host": "localhost"}


class EchoServer:
    """A real upstream websocket app, on its own loop in its own thread."""

    def __init__(self, *, subprotocol=None):
        self.port = 0
        self.headers: dict = {}
        self.subprotocol = subprotocol
        self._loop = None
        self._ready = threading.Event()
        self._stop = None

    def __enter__(self):
        threading.Thread(target=self._serve, daemon=True).start()
        assert self._ready.wait(timeout=10), "upstream never started"
        return self

    def __exit__(self, *exc):
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)

    def _serve(self):
        async def _main():
            self._stop = asyncio.Event()

            async def handler(conn):
                self.headers = dict(conn.request.headers)
                async for msg in conn:
                    if isinstance(msg, (bytes, bytearray)):
                        await conn.send(b"echo:" + bytes(msg))
                    else:
                        await conn.send("echo:" + msg)

            kwargs = {}
            if self.subprotocol:
                kwargs["subprotocols"] = [self.subprotocol]
            async with websockets.serve(handler, "127.0.0.1", 0, **kwargs) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self._ready.set()
                await self._stop.wait()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(_main())
        finally:
            self._loop.close()


def _app():
    import phone_bridge
    return phone_bridge.app


def _authed() -> TestClient:
    c = TestClient(_app(), base_url="http://localhost")
    c.cookies.set(config.COOKIE_NAME, auth._make_token())
    return c


def _connector(port: int, cid: str = "demoapp") -> dict:
    return {"id": cid, "label": "Demo", "embed": "iframe",
            "url": f"http://127.0.0.1:{port}"}


class UrlTests(unittest.TestCase):

    def test_the_scheme_is_upgraded_and_the_path_preserved(self):
        from phone_bridge import _ws_url
        self.assertEqual(_ws_url("http://127.0.0.1:9000", "live", ""),
                         "ws://127.0.0.1:9000/live")
        self.assertEqual(_ws_url("https://app.local/", "a/b", "x=1"),
                         "wss://app.local/a/b?x=1")
        self.assertEqual(_ws_url("http://127.0.0.1:9000", "", ""),
                         "ws://127.0.0.1:9000/")

    def test_a_query_string_survives(self):
        """A terminal or a live feed routinely carries a session id in the
        query; dropping it turns a working panel into an auth failure."""
        from phone_bridge import _ws_url
        self.assertEqual(_ws_url("http://h:1", "ws", "sid=abc&v=2"),
                         "ws://h:1/ws?sid=abc&v=2")


class GateTests(unittest.TestCase):

    def test_an_unauthenticated_socket_never_reaches_the_app(self):
        with EchoServer() as up:
            with mock.patch("ava_bridge.connectors.app",
                            return_value=_connector(up.port)):
                anon = TestClient(_app(), base_url="http://localhost")
                with self.assertRaises((WebSocketDisconnect, Exception)):
                    with anon.websocket_connect("/apps/demoapp/live",
                                                headers=_LOCAL):
                        raise AssertionError("accepted without a session")
            self.assertEqual(up.headers, {},
                             "the upstream app was dialled for an "
                             "unauthenticated caller")

    def test_a_connector_that_is_not_an_iframe_app_is_refused(self):
        with mock.patch("ava_bridge.connectors.app",
                        return_value={"id": "x", "embed": "none"}):
            c = _authed()
            with self.assertRaises((WebSocketDisconnect, Exception)):
                with c.websocket_connect("/apps/x/live", headers=_LOCAL):
                    raise AssertionError("a non-iframe connector was proxied")

    def test_an_unreachable_app_is_a_refused_handshake(self):
        """Not an accepted socket that dies a moment later — the browser can
        retry a failed handshake and can only guess at an immediate close."""
        with mock.patch("ava_bridge.connectors.app",
                        return_value=_connector(1)):   # nothing listens on :1
            c = _authed()
            with self.assertRaises((WebSocketDisconnect, Exception)):
                with c.websocket_connect("/apps/demoapp/live", headers=_LOCAL):
                    raise AssertionError("accepted a socket to a dead app")


class RelayTests(unittest.TestCase):

    def test_text_and_binary_both_relay_in_both_directions(self):
        with EchoServer() as up:
            with mock.patch("ava_bridge.connectors.app",
                            return_value=_connector(up.port)), \
                 mock.patch("ava_bridge.connectors.app_token", return_value=None):
                c = _authed()
                with c.websocket_connect("/apps/demoapp/live",
                                         headers=_LOCAL) as ws:
                    ws.send_text("hello")
                    self.assertEqual(ws.receive_text(), "echo:hello")
                    ws.send_bytes(b"\x00\x01")
                    self.assertEqual(ws.receive_bytes(), b"echo:\x00\x01")

    def test_the_apps_own_credential_is_added_by_the_bridge(self):
        """Ava-never-has-passwords: the browser never sees the app's token, the
        same rule both HTTP proxies follow."""
        with EchoServer() as up:
            with mock.patch("ava_bridge.connectors.app",
                            return_value=_connector(up.port)), \
                 mock.patch("ava_bridge.connectors.app_token",
                            return_value="s3cret"):
                c = _authed()
                with c.websocket_connect("/apps/demoapp/live",
                                         headers=_LOCAL) as ws:
                    ws.send_text("x")
                    ws.receive_text()
            self.assertEqual(up.headers.get("authorization"), "Bearer s3cret")

    def test_avas_session_cookie_is_not_forwarded_to_the_app(self):
        """The app is a separate trust domain. Handing it Ava's session would
        make every embedded app able to act as the owner."""
        with EchoServer() as up:
            with mock.patch("ava_bridge.connectors.app",
                            return_value=_connector(up.port)), \
                 mock.patch("ava_bridge.connectors.app_token", return_value=None):
                c = _authed()
                with c.websocket_connect("/apps/demoapp/live",
                                         headers=_LOCAL) as ws:
                    ws.send_text("x")
                    ws.receive_text()
            self.assertNotIn(config.COOKIE_NAME,
                             up.headers.get("cookie", ""),
                             "Ava's session cookie reached the embedded app")


class SubprotocolTests(unittest.TestCase):

    def test_the_upstreams_choice_is_what_the_browser_is_given(self):
        """`accept()` with no subprotocol against a browser that offered one is
        a silent immediate close, which reads as "the app is broken"."""
        with EchoServer(subprotocol="chat.v1") as up:
            with mock.patch("ava_bridge.connectors.app",
                            return_value=_connector(up.port)), \
                 mock.patch("ava_bridge.connectors.app_token", return_value=None):
                c = _authed()
                with c.websocket_connect("/apps/demoapp/live", headers=_LOCAL,
                                         subprotocols=["chat.v1"]) as ws:
                    ws.send_text("hi")
                    self.assertEqual(ws.receive_text(), "echo:hi")


class HazardNoteTests(unittest.TestCase):

    def test_the_token_lifetime_gap_is_written_down(self):
        """`apps_origin.TOKEN_TTL_S` is 300s, but a socket is authorized once at
        the handshake and never re-checked. That is ordinary websocket
        behaviour; leaving it undocumented is how it becomes a surprise."""
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1]
               / "phone_bridge.py").read_text(encoding="utf-8")
        body = src[src.index("async def app_ws_proxy("):]
        head = body[:body.index('"""', body.index('"""') + 3)]
        self.assertIn("ws_max_lifetime_s", head)
        self.assertIn("TOKEN_TTL_S", head)


if __name__ == "__main__":
    unittest.main()
