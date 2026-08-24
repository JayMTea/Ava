"""Stopping a turn: who may write its ending, and what happens when nobody can.

The whole hazard in an abort is that it invites a SECOND writer of the turn's
terminal status. The run's ending already arrives as an event and is recorded
by one path; an abort that also wrote "stopped" would race it and could relabel
a turn that in fact completed a moment earlier. So `abort_turn` asks, and never
records.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-abort-test-"))

from ava_bridge import state, turns
from ava_bridge.runtime.base import AgentRuntime
from ava_bridge.runtime.errors import GatewayUnsupported


class _Rt:
    def __init__(self, supports=True, raises=None):
        self._supports = supports
        self._raises = raises
        self.calls: list[tuple] = []

    def supports_abort(self):
        return self._supports

    def abort_run(self, session_id, run_id=""):
        self.calls.append((session_id, run_id))
        if self._raises:
            raise self._raises
        return True


class AbortTests(unittest.TestCase):

    def setUp(self):
        with state.turns_lock:
            state.turns.clear()

    def _turn(self, **over):
        rec = {"id": "t1", "status": "running", "run_id": "turn:t1",
               "session_id": "ava:chat:1"}
        rec.update(over)
        with state.turns_lock:
            state.turns["t1"] = rec
        return rec

    def _with(self, rt, fn=None):
        from ava_bridge import runtime
        old = runtime.configured
        runtime.configured = lambda: rt
        try:
            return (fn or (lambda: turns.abort_turn("t1")))()
        finally:
            runtime.configured = old

    def test_an_unknown_turn_is_reported_not_raised(self):
        got = self._with(_Rt())
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "turn_unknown")

    def test_a_running_turn_is_asked_with_the_captured_params(self):
        self._turn()
        rt = _Rt()
        got = self._with(rt)
        self.assertTrue(got["ok"])
        self.assertEqual(got["code"], "asked")
        self.assertEqual(rt.calls, [("ava:chat:1", "turn:t1")],
                         "abort must carry the session key it sent, and the "
                         "run id when there is one")

    def test_a_turn_that_just_finished_is_not_an_error(self):
        """The owner cannot avoid this race — they clicked while it ended."""
        self._turn(status="done")
        rt = _Rt()
        got = self._with(rt)
        self.assertTrue(got["ok"])
        self.assertEqual(got["code"], "already_finished")
        self.assertEqual(rt.calls, [], "nothing to stop, so nothing was asked")

    def test_a_turn_with_no_run_cannot_be_stopped(self):
        """The polled and direct floors never announce a run, so there is no
        id to abort. Saying so is honest; pretending is not."""
        self._turn(run_id=None, session_id=None)
        got = self._with(_Rt())
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "abort_unsupported")

    def test_a_runtime_that_cannot_abort_says_so(self):
        self._turn()
        got = self._with(_Rt(supports=False))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "abort_unsupported")

    def test_a_refused_abort_is_reported_rather_than_raised(self):
        self._turn()
        got = self._with(_Rt(raises=GatewayUnsupported("gw")))
        self.assertFalse(got["ok"])
        self.assertEqual(got["code"], "abort_failed")
        self.assertTrue(got["error"])

    def test_abort_never_writes_the_turns_status(self):
        """The point of the whole design. Whatever the verdict, the record is
        untouched — the run's own ending is the only thing that ends a turn."""
        for rt in (_Rt(), _Rt(supports=False), _Rt(raises=RuntimeError("x"))):
            with self.subTest(rt=rt):
                self._turn()
                self._with(rt)
                with state.turns_lock:
                    self.assertEqual(state.turns["t1"]["status"], "running")


class AbortedEndingTests(unittest.TestCase):
    """A stopped run must not be reported as a failed one.

    The lifecycle-end frame carries NO `message` field at all (captured live:
    stopReason, aborted, livenessState, phase, endedAt), so the generic error
    branch fell through to "the run failed" — telling the owner their own Stop
    button was an error.
    """

    def _iter(self, payload):
        import importlib
        gw = importlib.import_module("ava_bridge.runtime.openclaw_gw")
        rt = gw.OpenClawGatewayRuntime(client=_StubClient())
        handle = _Handle()
        sub = _Sub([{"topic": "agent", "payload": payload}])
        return list(rt.iter_run(sub, handle, timeout=2.0))

    def test_an_aborted_ending_reads_as_stopped(self):
        got = self._iter({"runId": "r1", "sessionKey": "s1",
                          "stream": "lifecycle",
                          "data": {"phase": "end", "aborted": True,
                                   "stopReason": "abort"}})
        self.assertEqual([e["kind"] for e in got], ["error"])
        self.assertEqual(got[0]["message"], "stopped")
        self.assertEqual(got[0]["code"], "run_aborted")

    def test_a_genuinely_failed_ending_still_reads_as_a_failure(self):
        got = self._iter({"runId": "r1", "sessionKey": "s1",
                          "stream": "lifecycle",
                          "data": {"phase": "end", "aborted": False,
                                   "stopReason": "error"}})
        self.assertEqual([e["kind"] for e in got], ["error"])
        self.assertNotEqual(got[0]["code"], "run_aborted")
        self.assertIn("fail", got[0]["message"])


class _Handle:
    run_id = "r1"
    session_id = "s1"
    idempotency_key = "r1"
    extra: dict = {}


class _Sub:
    def __init__(self, evs):
        self._evs = list(evs)

    def get(self, timeout=None):
        return self._evs.pop(0) if self._evs else None

    def close(self):
        pass


class _StubClient:
    def methods(self):
        return frozenset({"chat.send", "chat.abort", "sessions.subscribe"})

    def rpc(self, *a, **k):
        return {}

    def status(self):
        return {"phase": "ready"}

    def start(self):
        pass


class ContractTests(unittest.TestCase):

    def test_the_base_runtime_refuses_by_default(self):
        """House rule: a new ABC member defaults to refusal, so a runtime that
        has not implemented it cannot silently appear to support it."""
        self.assertFalse(AgentRuntime.supports_abort(object()))
        with self.assertRaises(GatewayUnsupported):
            AgentRuntime.abort_run(_Named(), "s")


class _Named:
    name = "stub"


if __name__ == "__main__":
    unittest.main()
