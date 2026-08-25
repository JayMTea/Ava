"""One turn, all the way through, against a gateway that actually speaks.

Everything else in this feature is tested in pieces: the client against scripted
frames, the runtime against a fake client, the turn path against a fake runtime.
Each of those can pass while the seams between them are wrong — a payload key
spelled one way on one side and another way on the other would satisfy all three
and still produce a chat that never finishes.

So this file wires the REAL client to the REAL adapter to the REAL turn path,
and puts a websocket server on the other end that behaves the way the gateway
documents: it speaks first, answers `chat.send` with an id, and streams the run
afterwards in `seq` order.

Slow-ish by this suite's standards (a socket and a handshake per class), which
is why it is one file and not a habit.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-e2e-test-"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "qa"))

from fakes.fake_gateway import FakeGateway

from ava_bridge import runtime, state, turns
from ava_bridge.runtime.base import RunHandle
from ava_bridge.runtime.openclaw_gw import OpenClawGatewayRuntime
from ava_bridge.runtime.openclaw_gw_client import (
    OpenClawGatewayClient)


def _connected(gw: FakeGateway, token: str | None = None):
    """A live client + adapter pointed at `gw`, or a skip if it will not come up."""
    client = OpenClawGatewayClient(url_resolver=lambda: gw.url,
                                   token_resolver=lambda: token)
    client.start()
    for _ in range(100):
        if client.status()["phase"] == "ready":
            return OpenClawGatewayRuntime(client=client), client
        time.sleep(0.05)
    client.stop()
    raise AssertionError(f"handshake never completed: {client.status()['why']}")


def _run_a_turn(rt, *, tid="t-e2e", sid="s-1", chat_id="") -> dict:
    with state.turns_lock:
        state.turns[tid] = {"id": tid, "status": "running", "steps": [],
                            "reply": None, "previews": [], "artifact": None,
                            "model": None, "ctx_tokens": None, "tools_used": [],
                            "degraded": False, "error": None,
                            "created": time.time(),
                            "run_id": None, "session_id": None}
    try:
        with mock.patch.object(runtime, "gate", return_value=(rt, None)), \
             mock.patch.object(turns, "which_model", return_value=None), \
             mock.patch.object(turns, "_tooling_note", return_value=""), \
             mock.patch.object(turns, "_pickup_previews_since", return_value=[]), \
             mock.patch.object(turns, "chat_append"), \
             mock.patch.object(turns.audit, "record"):
            turns._run_turn(tid, "hello", sid, chat_id)
        return dict(state.turns[tid])
    finally:
        state.turns.pop(tid, None)


class HandshakeTests(unittest.TestCase):

    def test_the_client_completes_the_documented_three_phases(self):
        gw = FakeGateway(token="tok").start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw, token="tok")
        self.addCleanup(client.stop)
        st = client.status()
        self.assertEqual(st["phase"], "ready")
        self.assertEqual(st["protocol"], 4)
        self.assertIn("chat.send", st["methods"])
        self.assertEqual(gw.calls[0]["method"], "connect")

    def test_a_rejected_token_is_reported_as_a_token_problem(self):
        """Not as "the gateway is down". The fix is a token, and the two have
        completely different remedies."""
        gw = FakeGateway(token="right").start()
        self.addCleanup(gw.stop)
        client = OpenClawGatewayClient(url_resolver=lambda: gw.url,
                                       token_resolver=lambda: "wrong")
        client.start()
        self.addCleanup(client.stop)
        for _ in range(60):
            if client.status()["why_code"]:
                break
            time.sleep(0.05)
        self.assertEqual(client.status()["why_code"], "agent_token_rejected")
        self.assertNotEqual(client.status()["phase"], "ready")

    def test_a_protocol_mismatch_refuses_rather_than_half_working(self):
        gw = FakeGateway(protocol=9).start()
        self.addCleanup(gw.stop)
        client = OpenClawGatewayClient(url_resolver=lambda: gw.url,
                                       token_resolver=lambda: None)
        client.start()
        self.addCleanup(client.stop)
        for _ in range(60):
            if client.status()["why_code"]:
                break
            time.sleep(0.05)
        self.assertEqual(client.status()["why_code"], "agent_protocol_mismatch")

    def test_the_device_token_the_gateway_issues_is_reused_next_time(self):
        """What stops a reconnect consuming a fresh pairing on every blip.

        The stored token is a single file under `secrets/`, shared by the whole
        process — so this clears it first. Another fixture's leftover token
        would otherwise be the thing offered, and the test would fail for a
        reason that has nothing to do with the behaviour under test.
        """
        from ava_bridge import settings
        settings.clear_secret("openclaw_device_token")
        self.addCleanup(settings.clear_secret, "openclaw_device_token")

        gw = FakeGateway().start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw)
        self.addCleanup(client.stop)
        first_issued = list(gw.issued_device_tokens)
        self.assertTrue(first_issued, "the gateway issued no device token")

        client.reconnect()
        for _ in range(80):
            if gw.connections >= 2:
                break
            time.sleep(0.05)
        connects = [c for c in gw.calls if c["method"] == "connect"]
        self.assertGreaterEqual(len(connects), 2, "no reconnect happened")
        offered = connects[-1]["params"]["auth"].get("deviceToken")
        self.assertIn(offered, first_issued,
                      "the reconnect did not re-offer the token THIS gateway "
                      "issued, so every blip consumes a fresh pairing")


class TurnTests(unittest.TestCase):

    def setUp(self):
        self.gw = FakeGateway().start()
        self.addCleanup(self.gw.stop)
        self.rt, self.client = _connected(self.gw)
        self.addCleanup(self.client.stop)

    def test_a_streamed_turn_completes_with_its_reply_steps_and_tools(self):
        """The whole feature, in one assertion block."""
        rec = _run_a_turn(self.rt)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["reply"], "Done.")
        self.assertEqual(rec["tools_used"], ["read_file"])
        # The assistant's streamed text IS a step — that is what the live path
        # renders as it arrives (a real turn against OpenClaw 2026.7.1 produced
        # exactly these three kinds). The old expectation omitted it because the
        # fake never scripted a `chat`/delta.
        self.assertEqual([s["kind"] for s in rec["steps"]],
                         ["thinking", "tool", "text"])
        self.assertFalse(rec["degraded"])

    def test_the_run_id_on_the_record_is_the_one_the_gateway_issued(self):
        rec = _run_a_turn(self.rt)
        sent = [c for c in self.gw.calls if c["method"] == "chat.send"]
        self.assertEqual(len(sent), 1)
        # The gateway ADOPTS the caller's idempotency key as the run id rather
        # than minting one, so the record must carry exactly that. `turns.py`
        # passes `turn:<tid>`, which is what makes a send retried across a
        # reconnect unable to start a second run.
        self.assertEqual(rec["run_id"], sent[0]["idempotencyKey"])
        self.assertEqual(rec["session_id"], "s-1")

    def test_the_turn_carries_its_own_idempotency_key(self):
        """A send retried across a reconnect must not start two runs."""
        _run_a_turn(self.rt, tid="t-idem")
        sent = next(c for c in self.gw.calls if c["method"] == "chat.send")
        self.assertEqual(sent["idempotencyKey"], "turn:t-idem")

    def test_tool_arguments_survive_the_trip(self):
        """The streaming path's advantage over the CLI one: the artifact builder
        no longer has to re-parse a session file to learn what a tool was
        called with."""
        rec = _run_a_turn(self.rt)
        tool = next(s for s in rec["steps"] if s["kind"] == "tool")
        self.assertEqual(tool["args"], {"path": "/etc/hosts"})

    def test_a_tool_result_and_reply_media_survive_the_real_wire_format(self):
        """The rich-chat seam end to end: the REAL gateway shapes — a tool
        `phase:start`/`phase:result` pair and a `MEDIA:` reply line with a usage
        count — must arrive as one folded tool card, a reply attachment, the
        gateway's own token count, and prose with the MEDIA line stripped.

        Same-origin media (`/apps/...`) so resolution is a pure passthrough; the
        point here is the parse+fold+thread, not the download path."""
        self.gw.script = [
            {"event": "agent", "payload": {"stream": "lifecycle",
                                           "data": {"phase": "start"}}},
            {"event": "agent", "payload": {"stream": "tool",
                "data": {"phase": "start", "name": "exec", "toolCallId": "c1",
                         "args": {"cmd": "render"}}}},
            {"event": "agent", "payload": {"stream": "tool",
                "data": {"phase": "update", "name": "exec", "toolCallId": "c1",
                         "partialResult": "…"}}},
            {"event": "agent", "payload": {"stream": "tool",
                "data": {"phase": "result", "name": "exec", "toolCallId": "c1",
                         "isError": False,
                         "result": {"content": [{"type": "text",
                                                 "text": "601 frames"}]}}}},
            {"event": "chat", "payload": {"state": "final", "stopReason": "stop",
                "message": {"role": "assistant",
                            "content": [{"type": "text",
                                         "text": "Rendered.\nMEDIA: /apps/r/out.mp4"}],
                            "usage": {"promptTokens": 18900}}}},
        ]
        rec = _run_a_turn(self.rt, tid="t-rich")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["reply"], "Rendered.")  # MEDIA line stripped
        self.assertEqual(rec["tools_used"], ["exec"])
        tool_steps = [s for s in rec["steps"] if s["kind"] == "tool"]
        self.assertEqual(len(tool_steps), 1, "start+update+result → one card")
        self.assertEqual(tool_steps[0]["output"], "601 frames")
        self.assertEqual(tool_steps[0]["args"], {"cmd": "render"})
        self.assertNotIn("tool_result", [s["kind"] for s in rec["steps"]])
        self.assertEqual(len(rec["attachments"]), 1)
        self.assertEqual(rec["attachments"][0]["url"], "/apps/r/out.mp4")
        self.assertEqual(rec["attachments"][0]["kind"], "video")
        self.assertEqual(rec["ctx_tokens"], 18900)

    def test_a_gap_in_the_event_sequence_is_reported_not_smoothed(self):
        """Silently rendering a chain with a hole in it tells the owner the
        record is complete when it is not."""
        self.gw.script = [
            {"event": "agent", "payload": {"stream": "reasoning",
                                           "data": {"reasoning": "one"}}},
            {"event": "__skip__", "payload": {}},
            {"event": "chat", "payload": {"state": "final", "tools": [],
                                          "message": {"role": "assistant",
                                                      "content": [
                                                          {"type": "text",
                                                           "text": "ok"}]}}},
        ]
        # Turn the marker into a real hole in `seq`.
        orig = self.gw._play

        async def _play(conn, run_id, session_id):
            for step in list(self.gw.script):
                if step["event"] == "__skip__":
                    self.gw.skip_seq(3)
                    continue
                self.gw.script = [step]
                await orig(conn, run_id, session_id)
            self.gw.script = []
        self.gw._play = _play

        rec = _run_a_turn(self.rt, tid="t-gap")
        self.assertIn("(some steps were not received)",
                      [s.get("text") for s in rec["steps"]])

    def test_an_unadvertised_chat_send_makes_the_runtime_decline_the_turn(self):
        """Fail closed: a gateway that cannot serve a turn must not be selected
        for one."""
        gw = FakeGateway(methods=["system.info"]).start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw)
        self.addCleanup(client.stop)
        self.assertFalse(rt.supports_tools)
        self.assertFalse(rt.supports_push_turns())


class RecoveryTests(unittest.TestCase):
    """The two RPC paths that had never actually worked.

    Both sent an invented `sessionId` param, both got INVALID_REQUEST from the
    live gateway, and both swallowed it — reconcile as a silent None, ghost
    mode as a False the UI reported without saying what failed. The fake now
    validates params the way the live gateway does, so these tests could not
    have passed before the param names were fixed.
    """

    def setUp(self):
        self.gw = FakeGateway(answers={"chat.history": {"messages": [
            {"role": "user", "runId": "turn:t-rec", "text": "hello"},
            {"role": "assistant", "runId": "turn:t-rec",
             "text": "Recovered.", "toolsUsed": ["read_file"]},
        ]}}).start()
        self.addCleanup(self.gw.stop)
        self.rt, self.client = _connected(self.gw)
        self.addCleanup(self.client.stop)

    def test_reconcile_round_trips_through_chat_history(self):
        """The fallback for a renamed event or a dropped socket returns the
        landed reply — the promise the module header makes."""
        handle = RunHandle(run_id="turn:t-rec", session_id="ava-phone-c9",
                           idempotency_key="turn:t-rec")
        got = self.rt._reconcile(handle)
        # (reply, tools, media, usage_tokens) — the fallback now also recovers
        # any reply media and the gateway's usage count; this history message
        # carries neither.
        self.assertEqual(got, ("Recovered.", ["read_file"], [], None))
        sent = next(c for c in self.gw.calls
                    if c["method"] == "chat.history")
        self.assertIn("sessionKey", sent["params"])
        self.assertNotIn("sessionId", sent["params"])

    def test_ghost_discard_actually_deletes_the_gateway_session(self):
        """/api/ghost/discard promises "leaves no trace". That is only true if
        sessions.delete really runs — with `{key}`, the one param the method
        accepts (it refuses sessionKey AND sessionId, verified live)."""
        self.assertTrue(self.rt.discard_session("ava-phone-ghost"))
        sent = next(c for c in self.gw.calls
                    if c["method"] == "sessions.delete")
        self.assertEqual(sent["params"], {"key": "ava-phone-ghost"})


class RelayTests(unittest.TestCase):
    """/ws/gateway's `ava.run` frames, through the real bridge route.

    The chat client maps a frame back to a chat via the session key, and
    gateway chat events carry `sessionKey` (full 'agent:main:...' form), not
    `sessionId` — so a relay that only attached sessionId shipped None for the
    one field the mapping needs.
    """

    def test_ava_run_frames_carry_the_session_key(self):
        from fastapi.testclient import TestClient

        import phone_bridge
        from ava_bridge import auth, config

        gw = FakeGateway().start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw)
        self.addCleanup(client.stop)

        c = TestClient(phone_bridge.app, base_url="http://localhost")
        c.cookies.set(config.COOKIE_NAME, auth._make_token())
        frames = []
        with mock.patch.object(runtime, "configured", return_value=rt),                 c.websocket_connect("/ws/gateway",
                                    headers={"host": "localhost"}) as ws:
            self.assertEqual(ws.receive_json().get("op"), "state")
            rt.start_run("hi", session_id="ava-phone-c1",
                         idempotency_key="turn:t-relay")
            # Raw gateway frames and their translations interleave; stop at
            # the translated final so a broken relay fails fast instead of
            # blocking forever on a frame that never comes.
            for _ in range(30):
                frame = ws.receive_json()
                if frame.get("topic") != "ava.run":
                    continue
                frames.append(frame["payload"])
                if frame["payload"].get("kind") == "final":
                    break
        self.assertTrue(frames, "no ava.run frame was relayed")
        self.assertEqual(frames[-1]["kind"], "final")
        for payload in frames:
            self.assertEqual(payload.get("sessionKey"),
                             "agent:main:ava-phone-c1")
            self.assertEqual(payload.get("runId"), "turn:t-relay")


class ObservationTests(unittest.TestCase):

    def test_drift_reads_the_persona_through_the_gateway(self):
        gw = FakeGateway(answers={
            "agents.files.get": {"file": {"content": "PERSONA"}}}).start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw)
        self.addCleanup(client.stop)
        want = {"persona": [{"id": "IDENTITY.md", "sha256": "x"}],
                "policies": [], "servers": [], "skills": []}
        got = rt.observe(want)
        self.assertEqual(got["sources"]["persona"], "gateway")
        self.assertIn("IDENTITY.md", got["maps"]["persona"])

    def test_extras_report_what_the_gateway_is_running(self):
        gw = FakeGateway(answers={
            "plugins.uiDescriptors": {"descriptors": [{"id": "github"}]},
            "cron.list": {"jobs": [{"name": "nightly"}]}}).start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw)
        self.addCleanup(client.stop)
        got = rt.observe({"persona": [], "policies": [], "servers": [],
                          "skills": []})
        self.assertEqual(got["extras"]["plugins"], ["github"])
        self.assertEqual(got["extras"]["cron"], ["nightly"])

    def test_a_gateway_without_the_read_method_claims_nothing(self):
        gw = FakeGateway(methods=["system.info", "chat.send"]).start()
        self.addCleanup(gw.stop)
        rt, client = _connected(gw)
        self.addCleanup(client.stop)
        got = rt.observe({"persona": [{"id": "IDENTITY.md", "sha256": "x"}],
                          "policies": [], "servers": [], "skills": []})
        self.assertNotIn("persona", got["maps"])


if __name__ == "__main__":
    unittest.main()
