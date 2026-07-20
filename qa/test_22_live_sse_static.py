"""Tier 2 — real-socket serving: the SPA shell + PWA assets, static mounts,
and the SSE stream over an actual TCP connection (TestClient can't prove
streaming framing; this does).
"""
import http.client
import json
import unittest
import urllib.parse

from qa.bridge_proc import BridgeProc
from qa.conftest import FAKE_gpusvc, FAKE_LLM
from qa.env_recipe import QA_PASSWORD

PROC: BridgeProc | None = None
COOKIE = ""


def setUpModule():
    global PROC, COOKIE
    PROC = BridgeProc(FAKE_LLM.url, FAKE_gpusvc.url).start(timeout=90)
    conn = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=30)
    conn.request("POST", "/setup",
                 urllib.parse.urlencode({"password": QA_PASSWORD,
                                         "confirm": QA_PASSWORD}).encode(),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    r = conn.getresponse()
    r.read()
    COOKIE = (r.getheader("set-cookie") or "").split(";")[0]
    conn.close()


def tearDownModule():
    if PROC:
        PROC.stop()


def _get(path: str, timeout: float = 15):
    conn = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=timeout)
    conn.request("GET", path, headers={"Cookie": COOKIE})
    r = conn.getresponse()
    r.body = r.read()
    conn.close()
    return r


class TestStaticServing(unittest.TestCase):
    def test_spa_shell_and_pwa_assets(self):
        r = _get("/")
        self.assertEqual(r.status, 200)
        html = r.body.decode(errors="replace")
        self.assertIn("<html", html.lower())
        # The bundle referenced by the shell actually serves.
        import re
        m = re.search(r'src="(/assets/[^"]+\.js)"', html)
        self.assertIsNotNone(m, "no bundle referenced in index.html")
        self.assertEqual(_get(m.group(1)).status, 200)
        # PWA endpoints are public.
        self.assertEqual(_get("/manifest.webmanifest").status, 200)
        self.assertEqual(_get("/sw.js").status, 200)

    def test_media_mount_serves_generated_files(self):
        import os
        target = os.path.join(PROC.home, "media", "gen", "qa_static.txt")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("qa-bytes")
        r = _get("/media/qa_static.txt")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, b"qa-bytes")


class TestLiveSse(unittest.TestCase):
    def test_stream_emits_events_over_tcp(self):
        conn = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=20)
        conn.request("GET", "/api/stream/ops", headers={"Cookie": COOKIE})
        r = conn.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/event-stream", r.getheader("content-type", ""))
        # Read a bounded chunk: the primer (alert.state) and/or first ticks.
        buf = b""
        try:
            while b"event:" not in buf and len(buf) < 65536:
                chunk = r.read1(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            conn.close()
        self.assertIn(b"event:", buf, f"no SSE events in first bytes: {buf[:200]}")

    def test_turn_update_reaches_the_stream(self):
        FAKE_LLM.reply = "SSE-observed reply."
        # Open the stream FIRST, then fire a turn, then expect turn.update.
        conn = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=30)
        conn.request("GET", "/api/stream/ops", headers={"Cookie": COOKIE})
        stream = conn.getresponse()
        assert stream.status == 200

        c2 = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=30)
        c2.request("POST", "/api/chat-stream",
                   urllib.parse.urlencode({"text": "sse test"}).encode(),
                   {"Content-Type": "application/x-www-form-urlencoded",
                    "Cookie": COOKIE})
        r2 = c2.getresponse()
        tid = json.loads(r2.read())["turn_id"]
        c2.close()

        buf = b""
        try:
            while b"turn.update" not in buf and len(buf) < 262144:
                chunk = stream.read1(4096)
                if not chunk:
                    break
                buf += chunk
        finally:
            conn.close()
        self.assertIn(b"turn.update", buf)
        self.assertIn(tid.encode(), buf, "our turn id never appeared on the stream")
