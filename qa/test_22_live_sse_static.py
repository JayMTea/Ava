"""Tier 2 — real-socket serving: the SPA shell, the PWA assets and the static
mounts over an actual TCP connection, which TestClient cannot prove.

This module also held `TestLiveSse`, against `/api/stream/ops`. That route left
with `ava_bridge/ops_api.py` when the Vitals, Operations and Data dashboards
were removed; the commit that deleted them updated eight other qa files and
missed this one, so two tests went on asserting 200 from a route that answers
404. Chat streaming is a different endpoint (`/api/chat-stream`) and is covered
by qa/test_03_chat_turns.py, so nothing moved here — the surface is gone.
"""
import http.client
import unittest
import urllib.parse

from qa.bridge_proc import BridgeProc
from qa.conftest import FAKE_LLM
from qa.env_recipe import QA_PASSWORD

PROC: BridgeProc | None = None
COOKIE = ""


def setUpModule():
    global PROC, COOKIE
    PROC = BridgeProc(FAKE_LLM.url).start(timeout=90)
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

    def test_uploads_mount_serves_attachments(self):
        import os
        target = os.path.join(PROC.home, "media", "uploads", "qa_static.txt")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write("qa-bytes")
        r = _get("/uploads/qa_static.txt")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body, b"qa-bytes")
