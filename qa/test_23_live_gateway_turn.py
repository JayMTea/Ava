"""Tier 2 — a whole TURN through a real bridge on the gateway runtime.

Everything else verifies the pieces. `tests/test_gateway_end_to_end.py` drives
the runtime against a fake gateway in-process; `qa/test_20_live_boot.py` boots
the real bridge on the default runtime. Neither puts BOTH in the loop, so the
streamed turn path — HTTP /api/chat-stream -> turns.py -> the gateway adapter ->
events -> the turn record -> /api/turn/<id> — was only ever exercised by hand,
in a browser, on one machine.

That is the path every reply the owner sees actually takes. This runs it: a
REAL uvicorn process, on a fresh AVA_HOME, configured for `openclaw_gw`, talking
to a fake gateway that now validates its params against a live capture (see
tests/test_gateway_capture_consistency.py) — so a wrong param shape fails here
rather than in the browser.
"""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "qa", "fakes"))

from qa.bridge_proc import BridgeProc
from qa.conftest import FAKE_LLM
from qa.env_recipe import QA_PASSWORD

from fake_gateway import FakeGateway

PROC: BridgeProc | None = None
GW: FakeGateway | None = None
COOKIE = ""
TOKEN = "qa-operator-token"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects.

    `urlopen` follows a 303 silently and turns the POST into a GET, so setup's
    redirect-on-success was observed as the redirect TARGET's 200 — and the
    Set-Cookie that rides on the 303 was lost with it. Seeing the real status
    is the point of asserting on it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, path: str, body=None, headers=None, timeout=30):
    h = dict(headers or {})
    if COOKIE:
        h.setdefault("Cookie", COOKIE)
    req = urllib.request.Request(PROC.base_url + path, data=body,
                                 headers=h, method=method)
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e


def _json(method: str, path: str, body=None, headers=None):
    r = _request(method, path, body, headers)
    raw = r.read().decode()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, {"_raw": raw[:200]}


def _form(data: dict):
    return (urllib.parse.urlencode(data).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"})


def setUpModule():
    global PROC, GW, COOKIE
    GW = FakeGateway(token=TOKEN).start()
    PROC = BridgeProc(FAKE_LLM.url, env_extra={
        # hermetic_env sets AVA_AGENT_ENABLED=0 to pin the Direct floor. Without
        # turning it back on, this whole file would boot on the floor and pass
        # while proving nothing about the gateway — which is why
        # TestGatewayIsActuallySelected exists below.
        "AVA_AGENT_ENABLED": "1",
        "AVA_AGENT_RUNTIME": "openclaw_gw",
        "AVA_OC_GATEWAY_URL": GW.url,
        "AVA_OC_GATEWAY_TOKEN": TOKEN,
        # Loopback already, but be explicit: this must not be the setting that
        # makes the test pass.
        "AVA_OC_GATEWAY_ALLOW_REMOTE": "0",
    }).start(timeout=90)
    body, headers = _form({"password": QA_PASSWORD, "confirm": QA_PASSWORD})
    r = _request("POST", "/setup", body, headers)
    assert r.status == 303, f"setup: {r.status}"
    COOKIE = (r.getheader("set-cookie") or "").split(";")[0]
    assert COOKIE.startswith("ava_session=")


def tearDownModule():
    if PROC:
        PROC.stop()
    if GW:
        GW.stop()


def _poll_turn(tid: str, timeout: float = 60.0) -> dict:
    """Wait for a terminal status, the way the browser does."""
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        status, last = _json("GET", f"/api/turn/{tid}")
        if status == 200 and last.get("status") not in ("running", None):
            return last
        time.sleep(0.5)
    return last


class TestGatewayIsActuallySelected(unittest.TestCase):
    """If the bridge quietly fell back to the Direct floor, every turn test
    below would pass while proving nothing about the gateway."""

    def test_the_gateway_runtime_is_the_one_serving(self):
        status, body = _json("GET", "/api/gateway/status")
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("configured"), body)
        self.assertEqual(body.get("runtime"), "openclaw_gw", body)

    def test_the_connection_actually_reached_the_gateway(self):
        end = time.time() + 30
        while time.time() < end:
            _, body = _json("GET", "/api/gateway/status")
            if body.get("phase") == "ready":
                return
            time.sleep(0.5)
        self.fail(f"never reached the gateway: {body}")


class TestStreamedTurn(unittest.TestCase):

    def test_a_turn_goes_out_and_a_reply_comes_back(self):
        body, headers = _form({"text": "hello", "history": "[]",
                               "attachments": "[]", "chat_id": "qa-gw-1"})
        status, started = _json("POST", "/api/chat-stream", body, headers)
        self.assertEqual(status, 200, started)
        tid = started.get("turn_id")
        self.assertTrue(tid, f"no turn id: {started}")

        rec = _poll_turn(tid)
        self.assertEqual(rec.get("status"), "done",
                         f"the turn did not complete: {rec}")
        self.assertIn("Done.", str(rec.get("reply") or ""),
                      "the gateway's reply did not reach the turn record")

    def test_the_record_names_the_run_the_gateway_issued(self):
        """The streamed path records run_id/session_id so a reconnecting
        client can say WHICH run a turn is waiting on. Both are None on the
        polled floor, so their presence is also proof the streamed path ran."""
        body, headers = _form({"text": "hello", "history": "[]",
                               "attachments": "[]", "chat_id": "qa-gw-2"})
        _, started = _json("POST", "/api/chat-stream", body, headers)
        rec = _poll_turn(started["turn_id"])
        self.assertTrue(rec.get("run_id"), f"no run id on the record: {rec}")
        self.assertTrue(rec.get("session_id"), f"no session key: {rec}")

    def test_the_steps_survive_the_trip(self):
        """Chain-of-thought is the difference between this path and the polled
        one — it is a RECORD of what happened rather than a guess from a file."""
        body, headers = _form({"text": "hello", "history": "[]",
                               "attachments": "[]", "chat_id": "qa-gw-3"})
        _, started = _json("POST", "/api/chat-stream", body, headers)
        rec = _poll_turn(started["turn_id"])
        self.assertIsInstance(rec.get("steps"), list)


class TestStop(unittest.TestCase):

    def test_stopping_an_unknown_turn_reports_rather_than_500s(self):
        status, body = _json("POST", "/api/turn/nosuchturn/abort")
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get("ok"))
        self.assertEqual(body.get("code"), "turn_unknown")

    def test_stopping_a_finished_turn_is_not_an_error(self):
        """The owner clicked while it ended — a race they cannot avoid."""
        body, headers = _form({"text": "hello", "history": "[]",
                               "attachments": "[]", "chat_id": "qa-gw-4"})
        _, started = _json("POST", "/api/chat-stream", body, headers)
        tid = started["turn_id"]
        _poll_turn(tid)
        status, verdict = _json("POST", f"/api/turn/{tid}/abort")
        self.assertEqual(status, 200, verdict)
        self.assertTrue(verdict.get("ok"), verdict)
        self.assertEqual(verdict.get("code"), "already_finished")


class TestRelay(unittest.TestCase):

    def test_the_relay_forwards_a_read_call(self):
        payload = json.dumps({"method": "sessions.list", "params": {}}).encode()
        status, body = _json("POST", "/api/gateway/rpc", payload,
                             {"Content-Type": "application/json"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)

    def test_the_relay_refuses_a_protected_config_write(self):
        """`dangerouslyDisableDeviceAuth` governs whether the gateway
        authenticates browsers at all. A UI bug that flipped it would turn a
        transient mistake into a permanent posture change."""
        raw = json.dumps({"gateway": {"controlUi": {
            "dangerouslyDisableDeviceAuth": True}}})
        payload = json.dumps({"method": "config.set",
                              "params": {"raw": raw}}).encode()
        status, body = _json("POST", "/api/gateway/rpc", payload,
                             {"Content-Type": "application/json"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get("ok"), body)
        self.assertEqual(body.get("error_code"), "gateway_key_refused", body)

    def test_the_relay_is_behind_the_session_cookie(self):
        global COOKIE
        saved, COOKIE = COOKIE, ""
        try:
            payload = json.dumps({"method": "sessions.list"}).encode()
            r = _request("POST", "/api/gateway/rpc", payload,
                         {"Content-Type": "application/json"})
            self.assertIn(r.status, (401, 403),
                          "the relay answered an unauthenticated caller")
        finally:
            COOKIE = saved


if __name__ == "__main__":
    unittest.main()
