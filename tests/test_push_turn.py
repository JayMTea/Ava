"""The streaming turn path: a run that announces itself and reports as it goes.

The CLI path blocks on one call and tails a file beside it to guess at progress.
The gateway path starts a run, gets an id back immediately, and receives every
step as an ordered event. That is a genuinely different shape, and the things
that can go wrong are different too — lost events, a run that never ends, a
broadcast meant for somebody else's session.

What must NOT change is the turn's contract with the rest of the app: a terminal
status always, a persisted reply even on failure, `steps` safe to read from
another thread, and the same audit line either way. Those are what this file
pins.

House style (tests/test_runtime_gate.py): stdlib unittest, no bridge, no
sandbox, no network.
"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-push-test-"))

from ava_bridge import runtime, state, turns
from ava_bridge.runtime.base import AgentRuntime, RunHandle


class FakeStreamRuntime(AgentRuntime):
    """A runtime that streams, scripted with Ava's own event vocabulary.

    Deliberately speaks the POST-translation shape: if this fake had to emit
    OpenClaw's wire format, the test would be asserting the adapter's parser
    rather than the turn path, and `turns.py` is not supposed to know that
    format at all.
    """

    name = "fake-stream"
    supports_tools = True
    supports_cot = True

    def __init__(self, script, *, fail_start=None):
        self.script = script
        self.fail_start = fail_start
        self.started: list[dict] = []
        self.subs = 0
        self.closed = 0

    def available(self):
        return True

    def supports_push_turns(self):
        return True

    def subscribe(self, topics=None, *, maxlen=1000):
        outer = self

        class _Sub:
            def get(self, timeout=None):
                return None

            def close(self):
                outer.closed += 1
        outer.subs += 1
        return _Sub()

    def run_turn(self, text, session_id=None, history=None):
        # Still required: /api/talk, /api/talk-text and warm() all want a
        # finished answer, so a streaming runtime owes a blocking shape too.
        # The real adapter builds this on iter_run for the same reason.
        raise AssertionError("the streaming path must not call run_turn")

    def start_run(self, text, *, session_id, idempotency_key, thinking=None):
        if self.fail_start:
            raise self.fail_start
        self.started.append({"text": text, "session_id": session_id,
                             "idempotency_key": idempotency_key})
        return RunHandle(run_id="run-9", session_id=session_id,
                         idempotency_key=idempotency_key)

    def iter_run(self, sub, handle, timeout=None):
        yield from self.script


def _turn(rt, *, chat_id="") -> dict:
    """Run one turn to completion through the real dispatcher, return its record."""
    tid = "t-push"
    state.turns.pop(tid, None)
    turns.start_turn  # (documentation: this is the same record start_turn makes)
    with state.turns_lock:
        state.turns[tid] = {"id": tid, "status": "running", "steps": [],
                            "reply": None, "previews": [], "artifact": None,
                            "model": None, "ctx_tokens": None, "tools_used": [],
                            "degraded": False, "error": None, "created": 0.0,
                            "run_id": None, "session_id": None}
    with mock.patch.object(runtime, "gate", return_value=(rt, None)), \
         mock.patch.object(turns, "which_model", return_value=None), \
         mock.patch.object(turns, "_tooling_note", return_value=""), \
         mock.patch.object(turns, "_pickup_previews_since", return_value=[]), \
         mock.patch.object(turns, "build_turn_artifact", return_value=None), \
         mock.patch.object(turns, "chat_append"), \
         mock.patch.object(turns.audit, "record"):
        turns._run_turn(tid, "hi", "sess-1", chat_id)
    return dict(state.turns.pop(tid))


class HappyPathTests(unittest.TestCase):

    def test_a_streamed_run_finishes_with_its_steps_and_tools(self):
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "thinking", "text": "hmm"}},
            {"kind": "step", "step": {"kind": "tool", "name": "get_weather",
                                      "args": {"location": "Austin"}}},
            {"kind": "final", "text": "It is warm.", "tools": ["get_weather"]},
        ])
        rec = _turn(rt)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["reply"], "It is warm.")
        self.assertEqual(rec["tools_used"], ["get_weather"])
        self.assertEqual([s["kind"] for s in rec["steps"]], ["thinking", "tool"])
        self.assertFalse(rec["degraded"])

    def test_the_run_is_identified_on_the_record(self):
        """`/api/turn/<id>` returns this dict verbatim, so a reconnecting client
        can say WHICH run it is waiting on rather than only that it is."""
        rt = FakeStreamRuntime([{"kind": "final", "text": "ok", "tools": []}])
        rec = _turn(rt)
        self.assertEqual(rec["run_id"], "run-9")
        self.assertEqual(rec["session_id"], "sess-1")

    def test_the_idempotency_key_is_the_turn(self):
        """A send retried across a reconnect must not start two runs."""
        rt = FakeStreamRuntime([{"kind": "final", "text": "ok", "tools": []}])
        _turn(rt)
        self.assertEqual(rt.started[0]["idempotency_key"], "turn:t-push")

    def test_tools_are_recovered_from_steps_when_final_omits_them(self):
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "tool", "name": "read_file"}},
            {"kind": "final", "text": "done", "tools": []},
        ])
        self.assertEqual(_turn(rt)["tools_used"], ["read_file"])

    def test_the_subscription_is_always_closed(self):
        """The fan-out holds a strong reference until close(); a turn that
        forgets leaks a queue per message, forever."""
        rt = FakeStreamRuntime([{"kind": "final", "text": "ok", "tools": []}])
        _turn(rt)
        self.assertEqual((rt.subs, rt.closed), (1, 1))


class LiveProgressTests(unittest.TestCase):

    def test_steps_are_published_as_they_arrive_not_only_at_the_end(self):
        """The whole point of the path. If steps only landed at the end, the
        chain-of-thought would be a replay rather than a live record."""
        seen: list[int] = []
        tid = "t-live"

        def _script():
            for i in range(3):
                yield {"kind": "step", "step": {"kind": "text", "text": f"s{i}"}}
                with state.turns_lock:
                    seen.append(len(state.turns[tid]["steps"]))
            yield {"kind": "final", "text": "end", "tools": []}

        rt = FakeStreamRuntime(_script())
        with state.turns_lock:
            state.turns[tid] = {"id": tid, "status": "running", "steps": [],
                                "reply": None, "previews": [], "artifact": None,
                                "model": None, "ctx_tokens": None,
                                "tools_used": [], "degraded": False,
                                "error": None, "created": 0.0,
                                "run_id": None, "session_id": None}
        try:
            with mock.patch.object(runtime, "gate", return_value=(rt, None)), \
                 mock.patch.object(turns, "which_model", return_value=None), \
                 mock.patch.object(turns, "_tooling_note", return_value=""), \
                 mock.patch.object(turns, "_pickup_previews_since", return_value=[]), \
                 mock.patch.object(turns, "build_turn_artifact", return_value=None), \
                 mock.patch.object(turns, "chat_append"), \
                 mock.patch.object(turns.audit, "record"):
                turns._run_turn(tid, "hi", "sess-1", "")
        finally:
            state.turns.pop(tid, None)
        self.assertEqual(seen, [1, 2, 3], "each step must be visible immediately")

    def test_the_published_list_is_a_copy(self):
        """The worker keeps appending while /api/turn/<id> serialises. Handing
        out the live list is a mutation racing a read; the symptom is an
        occasional truncated or duplicated step, not an exception."""
        captured = {}
        tid = "t-copy"

        def _script():
            yield {"kind": "step", "step": {"kind": "text", "text": "a"}}
            with state.turns_lock:
                captured["ref"] = state.turns[tid]["steps"]
            yield {"kind": "step", "step": {"kind": "text", "text": "b"}}
            yield {"kind": "final", "text": "end", "tools": []}

        rt = FakeStreamRuntime(_script())
        with state.turns_lock:
            state.turns[tid] = {"id": tid, "status": "running", "steps": [],
                                "reply": None, "previews": [], "artifact": None,
                                "model": None, "ctx_tokens": None,
                                "tools_used": [], "degraded": False,
                                "error": None, "created": 0.0,
                                "run_id": None, "session_id": None}
        try:
            with mock.patch.object(runtime, "gate", return_value=(rt, None)), \
                 mock.patch.object(turns, "which_model", return_value=None), \
                 mock.patch.object(turns, "_tooling_note", return_value=""), \
                 mock.patch.object(turns, "_pickup_previews_since", return_value=[]), \
                 mock.patch.object(turns, "build_turn_artifact", return_value=None), \
                 mock.patch.object(turns, "chat_append"), \
                 mock.patch.object(turns.audit, "record"):
                turns._run_turn(tid, "hi", "sess-1", "")
        finally:
            state.turns.pop(tid, None)
        self.assertEqual(len(captured["ref"]), 1,
                         "the snapshot handed out earlier must not have grown")

    def test_a_gap_is_recorded_in_the_trajectory_rather_than_hidden(self):
        """Silently rendering a chain with a hole in it tells the owner the
        record is complete when it is not."""
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "text", "text": "a"}},
            {"kind": "gap"},
            {"kind": "final", "text": "end", "tools": []},
        ])
        rec = _turn(rt)
        texts = [s.get("text") for s in rec["steps"]]
        self.assertIn("(some steps were not received)", texts)


class FailurePathTests(unittest.TestCase):

    def test_a_failed_run_still_persists_a_reply(self):
        """Never a dangling question and an endless spinner."""
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "tool", "name": "read_file"}},
            {"kind": "error", "message": "the model refused", "code": "X"},
        ])
        rec = _turn(rt, chat_id="c1")
        self.assertEqual(rec["status"], "done")
        self.assertTrue(rec["degraded"])
        self.assertIn("couldn't finish", rec["reply"])
        self.assertEqual(rec["tools_used"], ["read_file"],
                         "the flight recorder must show the tools that DID run")
        self.assertIn("refused", rec["error"])

    def test_a_run_that_never_ends_becomes_a_degraded_turn_not_a_hang(self):
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "text", "text": "thinking"}},
        ])  # iterator exhausts with no final — that IS the timeout
        rec = _turn(rt)
        self.assertEqual(rec["status"], "done")
        self.assertTrue(rec["degraded"])
        self.assertIn("did not finish", rec["error"])

    def test_a_runtime_that_cannot_start_the_run_fails_cleanly(self):
        rt = FakeStreamRuntime([], fail_start=RuntimeError("gateway said no"))
        rec = _turn(rt, chat_id="c1")
        self.assertEqual(rec["status"], "done")
        self.assertTrue(rec["degraded"])
        self.assertIn("gateway said no", rec["error"])
        self.assertEqual(rt.closed, 1, "the subscription must still be closed")

    def test_the_terminal_status_guarantee_holds_on_this_path_too(self):
        """`_run_turn_guarded` is the backstop, and it must never be the thing
        that saves this path — a turn left `running` is polled until the tab
        closes."""
        class Exploding(FakeStreamRuntime):
            def iter_run(self, sub, handle, timeout=None):
                raise MemoryError("boom")

        rt = Exploding([])
        rec = _turn(rt)
        self.assertNotEqual(rec["status"], "running")
        self.assertTrue(rec["degraded"])


class DispatchTests(unittest.TestCase):

    def test_a_non_streaming_runtime_still_takes_the_polled_path(self):
        """The choice is a capability question, never an isinstance one."""
        calls = []

        class Polled(AgentRuntime):
            name = "polled"
            supports_tools = True
            supports_cot = True

            def available(self):
                return True

            def run_turn(self, text, session_id=None, history=None):
                calls.append(session_id)
                return "from the cli", []

        tid = "t-dispatch"
        with state.turns_lock:
            state.turns[tid] = {"id": tid, "status": "running", "steps": [],
                                "reply": None, "previews": [], "artifact": None,
                                "model": None, "ctx_tokens": None,
                                "tools_used": [], "degraded": False,
                                "error": None, "created": 0.0,
                                "run_id": None, "session_id": None}
        try:
            with mock.patch.object(runtime, "gate", return_value=(Polled(), None)), \
                 mock.patch.object(turns, "_session_line_count", return_value=0), \
                 mock.patch.object(turns, "_poll_turn_steps", lambda *a, **k: None), \
                 mock.patch.object(turns, "_tools_from_session", return_value=[]), \
                 mock.patch.object(turns, "_read_session_steps", return_value=[]), \
                 mock.patch.object(turns, "which_model", return_value=None), \
                 mock.patch.object(turns, "_tooling_note", return_value=""), \
                 mock.patch.object(turns, "_pickup_previews_since", return_value=[]), \
                 mock.patch.object(turns, "build_turn_artifact", return_value=None), \
                 mock.patch.object(turns, "chat_append"), \
                 mock.patch.object(turns.audit, "record"):
                turns._run_turn(tid, "hi", "sess-1", "")
            rec = dict(state.turns[tid])
        finally:
            state.turns.pop(tid, None)
        self.assertEqual(rec["reply"], "from the cli")
        self.assertEqual(calls, ["sess-1"])
        self.assertIsNone(rec["run_id"], "the polled path has no run id to give")

    def test_both_paths_write_the_same_record_shape(self):
        """The turn dict is the contract with the UI. A chat that renders
        differently depending on which runtime answered is the exact bug the
        seam exists to prevent."""
        rt = FakeStreamRuntime([{"kind": "final", "text": "ok", "tools": []}])
        pushed = _turn(rt)
        for key in ("id", "status", "steps", "reply", "previews", "artifact",
                    "model", "ctx_tokens", "tools_used", "degraded", "error",
                    "created", "run_id", "session_id"):
            self.assertIn(key, pushed, f"the streamed record is missing {key}")


class ThreadSafetyTests(unittest.TestCase):

    def test_reading_steps_while_they_are_written_never_tears(self):
        tid = "t-race"
        stop = threading.Event()
        seen: list[list] = []

        def _reader():
            while not stop.is_set():
                with state.turns_lock:
                    rec = state.turns.get(tid)
                    if rec:
                        seen.append(list(rec["steps"]))

        def _script():
            for i in range(50):
                yield {"kind": "step", "step": {"kind": "text", "text": f"s{i}"}}
            yield {"kind": "final", "text": "end", "tools": []}

        with state.turns_lock:
            state.turns[tid] = {"id": tid, "status": "running", "steps": [],
                                "reply": None, "previews": [], "artifact": None,
                                "model": None, "ctx_tokens": None,
                                "tools_used": [], "degraded": False,
                                "error": None, "created": 0.0,
                                "run_id": None, "session_id": None}
        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        try:
            rt = FakeStreamRuntime(_script())
            with mock.patch.object(runtime, "gate", return_value=(rt, None)), \
                 mock.patch.object(turns, "which_model", return_value=None), \
                 mock.patch.object(turns, "_tooling_note", return_value=""), \
                 mock.patch.object(turns, "_pickup_previews_since", return_value=[]), \
                 mock.patch.object(turns, "build_turn_artifact", return_value=None), \
                 mock.patch.object(turns, "chat_append"), \
                 mock.patch.object(turns.audit, "record"):
                turns._run_turn(tid, "hi", "sess-1", "")
        finally:
            stop.set()
            t.join(timeout=5)
            state.turns.pop(tid, None)
        for snap in seen:
            self.assertEqual([s["text"] for s in snap],
                             [f"s{i}" for i in range(len(snap))],
                             "a snapshot was torn or reordered")


class ToolResultAndMediaTests(unittest.TestCase):
    """The rich-chat contract: a tool call and its result fold into one card,
    and reply media plus the gateway's own token count reach the record.

    These use same-origin media URLs so resolution is a pure passthrough — no
    network, no gateway, matching the house 'no bridge, no sandbox, no network'
    rule. `agent_media` resolution of sandbox paths is covered separately.
    """

    def test_a_tool_result_folds_into_its_start_by_id(self):
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "tool", "name": "exec",
                                      "id": "c1", "args": {"cmd": "render"}}},
            {"kind": "step", "step": {"kind": "tool_result", "name": "exec",
                                      "id": "c1", "output": "601 frames",
                                      "attachments": [{"url": "/apps/r/out.mp4",
                                                       "kind": "video"}]}},
            {"kind": "final", "text": "Rendered.", "tools": ["exec"]},
        ])
        rec = _turn(rt)
        tool_steps = [s for s in rec["steps"] if s["kind"] == "tool"]
        self.assertEqual(len(tool_steps), 1, "start+result must be ONE card")
        self.assertEqual(tool_steps[0]["output"], "601 frames")
        self.assertEqual(tool_steps[0]["args"], {"cmd": "render"})
        self.assertEqual(tool_steps[0]["attachments"][0]["url"], "/apps/r/out.mp4")
        self.assertNotIn("tool_result", [s["kind"] for s in rec["steps"]])
        self.assertEqual(rec["tools_used"], ["exec"])

    def test_a_result_without_an_id_folds_by_name(self):
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "tool", "name": "exec"}},
            {"kind": "step", "step": {"kind": "tool_result", "name": "exec",
                                      "output": "done"}},
            {"kind": "final", "text": "ok", "tools": []},
        ])
        rec = _turn(rt)
        tool_steps = [s for s in rec["steps"] if s["kind"] == "tool"]
        self.assertEqual(len(tool_steps), 1)
        self.assertEqual(tool_steps[0]["output"], "done")

    def test_an_orphan_result_becomes_a_standalone_card(self):
        rt = FakeStreamRuntime([
            {"kind": "step", "step": {"kind": "tool_result", "name": "exec",
                                      "output": "surprise", "is_error": True}},
            {"kind": "final", "text": "ok", "tools": []},
        ])
        rec = _turn(rt)
        self.assertEqual([s["kind"] for s in rec["steps"]], ["tool"])
        self.assertEqual(rec["steps"][0]["output"], "surprise")
        self.assertTrue(rec["steps"][0]["is_error"])
        self.assertEqual(rec["tools_used"], ["exec"])

    def test_reply_media_and_usage_reach_the_record(self):
        rt = FakeStreamRuntime([
            {"kind": "final", "text": "Here it is.", "tools": [],
             "attachments": [{"url": "/apps/r/clip.mp4", "kind": "video",
                              "filename": "clip.mp4"},
                             {"url": "/apps/r/clip.mp4"}],  # dup, must collapse
             "usage_tokens": 18900},
        ])
        rec = _turn(rt)
        self.assertEqual(len(rec["attachments"]), 1, "duplicate media collapses")
        self.assertEqual(rec["attachments"][0]["url"], "/apps/r/clip.mp4")
        self.assertEqual(rec["attachments"][0]["kind"], "video")
        self.assertEqual(rec["ctx_tokens"], 18900,
                         "the gateway's own usage count is the numerator")

    def test_no_media_no_usage_leaves_the_record_clean(self):
        rt = FakeStreamRuntime([{"kind": "final", "text": "hi", "tools": []}])
        rec = _turn(rt)
        self.assertEqual(rec["attachments"], [])
        self.assertIsNone(rec["ctx_tokens"])  # which_model is mocked to None


if __name__ == "__main__":
    unittest.main()
