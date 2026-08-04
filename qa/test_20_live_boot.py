"""Tier 2 — the REAL process, exactly as a user runs it: `serve.py` under
uvicorn on a fresh empty AVA_HOME. Cold boot speed, dir auto-creation, and the
first-run gate over a real socket.
"""
import os
import unittest
import urllib.request

from qa.bridge_proc import BridgeProc
from qa.conftest import FAKE_LLM

PROC: BridgeProc | None = None


def setUpModule():
    global PROC
    PROC = BridgeProc(FAKE_LLM.url).start(timeout=90)


def tearDownModule():
    if PROC:
        PROC.stop()


def _get(path: str, timeout: float = 10):
    req = urllib.request.Request(PROC.base_url + path, method="GET")
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e


class TestLiveBoot(unittest.TestCase):
    def test_health_is_public_over_real_socket(self):
        r = _get("/api/health")
        self.assertEqual(r.status, 200)

    def test_fresh_home_dirs_created(self):
        for sub in ("data", "logs"):
            self.assertTrue(os.path.isdir(os.path.join(PROC.home, sub)),
                            f"{sub}/ missing under fresh AVA_HOME")

    def test_first_run_redirects_to_setup(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", PROC.port, timeout=10)
        conn.request("GET", "/")
        r = conn.getresponse()
        self.assertEqual(r.status, 303)
        self.assertEqual(r.getheader("location"), "/setup")
        conn.close()

    def test_api_is_401_over_real_socket(self):
        r = _get("/api/brand")
        self.assertEqual(r.status, 401)

    def test_two_instances_coexist(self):
        second = BridgeProc(FAKE_LLM.url).start(timeout=90)
        try:
            self.assertNotEqual(second.port, PROC.port)
            self.assertEqual(_get("/api/health").status, 200)
            r = urllib.request.urlopen(second.base_url + "/api/health", timeout=10)
            self.assertEqual(r.status, 200)
        finally:
            second.stop()
