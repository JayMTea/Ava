"""Tier 2 — restart persistence: everything a continuing user accumulates must
survive a real process restart on the same AVA_HOME, and the session cookie
(stateless HMAC) must still be valid afterwards.
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


def _request(method: str, path: str, body: bytes | None = None,
             headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=30)
    h = dict(headers or {})
    if COOKIE:
        h.setdefault("Cookie", COOKIE)
    conn.request(method, path, body=body, headers=h)
    r = conn.getresponse()
    data = r.read()
    r.body = data
    conn.close()
    return r


def _form(data: dict) -> tuple[bytes, dict]:
    return (urllib.parse.urlencode(data).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})


def setUpModule():
    global PROC, COOKIE
    PROC = BridgeProc(FAKE_LLM.url, FAKE_gpusvc.url).start(timeout=90)
    body, headers = _form({"password": QA_PASSWORD, "confirm": QA_PASSWORD})
    r = _request("POST", "/setup", body, headers)
    assert r.status == 303, f"setup: {r.status}"
    COOKIE = (r.getheader("set-cookie") or "").split(";")[0]
    assert COOKIE.startswith("ava_session=")


def tearDownModule():
    if PROC:
        PROC.stop()


class TestRestartPersistence(unittest.TestCase):
    def test_everything_survives_a_restart(self):
        # -- accumulate state ------------------------------------------------
        FAKE_LLM.reply = "Persisted reply."
        r = _request("POST", "/api/chats")
        cid = json.loads(r.body)["id"]
        body, headers = _form({"text": "please remember the number 7183",
                               "chat_id": cid})
        r = _request("POST", "/api/talk-text", body, headers)
        self.assertEqual(r.status, 200, r.body[:300])
        r = _request("POST", "/api/hub/memory",
                     json.dumps({"text": "QA persistence fact 7183",
                                 "kind": "fact"}).encode(),
                     {"Content-Type": "application/json"})
        self.assertEqual(r.status, 200, r.body[:300])
        r = _request("POST", "/api/hub/system/retention?days=365")
        self.assertEqual(r.status, 200, r.body[:300])

        # -- the real thing: kill the process, boot it again -----------------
        PROC.restart(timeout=90)

        # Old session cookie still valid (stateless HMAC, secret on disk).
        r = _request("GET", "/api/brand")
        self.assertEqual(r.status, 200, "session died across restart")
        # Chat + its messages survived.
        r = _request("GET", f"/api/chats/{cid}")
        self.assertEqual(r.status, 200)
        msgs = json.loads(r.body)["messages"]
        self.assertIn("7183", json.dumps(msgs))
        # Memory survived.
        r = _request("GET", "/api/hub/memory")
        self.assertIn("7183", r.body.decode())
        # Settings survived.
        r = _request("GET", "/api/hub/system")
        self.assertEqual(json.loads(r.body)["retention_days"], 365)
        # And it is still first-run-complete: no /setup redirect.
        r = _request("GET", "/login")
        self.assertIn(r.status, (200, 303))
