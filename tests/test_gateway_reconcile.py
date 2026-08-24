"""Reconcile: recovering a reply when the event stream did not deliver it.

This is the reconnect story — a dropped socket loses events but not the
transcript. It had NEVER WORKED. The matcher required `msg["runId"] ==
handle.run_id`, and no message in a transcript carries a `runId` at all; the
whole assistant message is `__openclaw, api, content, model, provider,
responseId, role, stopReason, timestamp, usage`. So it returned None every time
and the failure was invisible: a lost reply looks like a slow one.

Every shape asserted here was captured from a live gateway (OpenClaw 2026.7.1)
by running real turns — including the two details that made the first attempt at
this fix still fail.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-recon-test-"))

from ava_bridge.runtime.base import RunHandle

openclaw_gw = importlib.import_module("ava_bridge.runtime.openclaw_gw")


class FakeClient:
    def __init__(self, answer):
        self._answer = answer
        self.calls: list[tuple] = []

    def methods(self):
        return frozenset({"chat.history", "chat.send"})

    def rpc(self, method, params=None, *, timeout=None, idempotency_key=None):
        self.calls.append((method, params))
        return self._answer

    def status(self):
        return {"phase": "ready"}

    def start(self):
        pass


def _rt(answer):
    return openclaw_gw.OpenClawGatewayRuntime(client=FakeClient(answer))


def _handle(key="turn:abc123"):
    return RunHandle(run_id=key, session_id="ava:chat:1", idempotency_key=key)


# The exact transcript shape a live gateway returns after a completed turn.
def _committed(idem="turn:abc123:user", reply="RECOVERED"):
    return {
        "messages": [
            {"role": "user", "idempotencyKey": idem,
             "content": "Reply with exactly: RECOVERED", "timestamp": 1},
            {"role": "assistant", "content": [{"type": "text", "text": reply}],
             "model": "m", "provider": "p", "responseId": "r",
             "stopReason": "stop", "timestamp": 2},
        ],
        "inFlightRun": None,
        "sessionKey": "ava:chat:1",
    }


class AnchorTests(unittest.TestCase):

    def test_a_reply_is_found_with_no_runid_anywhere(self):
        """The case that matters: this is what every real transcript looks
        like, and the old matcher scored zero on it."""
        got = _rt(_committed())._reconcile(_handle())
        self.assertIsNotNone(got, "a committed reply must be recoverable")
        self.assertEqual(got[0], "RECOVERED")

    def test_the_stored_key_is_suffixed_by_role(self):
        """Sending `turn:abc123` records `turn:abc123:user`. An equality test
        matches NOTHING — the first version of this fix shipped that and still
        recovered nothing at all."""
        got = _rt(_committed(idem="turn:abc123:user"))._reconcile(_handle())
        self.assertIsNotNone(got)

    def test_an_unsuffixed_key_still_matches(self):
        """Belt and braces: a build that stores the key verbatim must not
        regress just because today's stores it with a role suffix."""
        got = _rt(_committed(idem="turn:abc123"))._reconcile(_handle())
        self.assertIsNotNone(got)

    def test_another_turns_reply_is_not_claimed(self):
        """The hazard of anchoring: finishing this turn with somebody else's
        reply is worse than not finishing it."""
        got = _rt(_committed(idem="turn:SOMEONE-ELSE:user"))._reconcile(_handle())
        self.assertIsNone(got)

    def test_an_assistant_message_with_no_user_before_it_is_not_a_match(self):
        answer = {"messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]}
        self.assertIsNone(_rt(answer)._reconcile(_handle()))


class InFlightTests(unittest.TestCase):

    def test_a_run_still_in_flight_is_not_an_answer(self):
        """`inFlightRun` is {runId, text} while the reply accumulates. Reading
        `messages` then returns the PREVIOUS turn — which is how a reconnect
        answers a question with the reply to the one before it."""
        answer = _committed(idem="turn:OLDER:user", reply="the previous reply")
        answer["inFlightRun"] = {"runId": "turn:abc123", "text": ""}
        self.assertIsNone(_rt(answer)._reconcile(_handle()))

    def test_somebody_elses_run_in_flight_does_not_block_us(self):
        """The gateway serves every operator; another live run is not ours."""
        answer = _committed()
        answer["inFlightRun"] = {"runId": "turn:OTHER", "text": ""}
        self.assertIsNotNone(_rt(answer)._reconcile(_handle()))


class RunIdTests(unittest.TestCase):

    def test_a_runid_is_still_preferred_when_present(self):
        """Anchoring is the fallback, not the design. A build that starts
        recording runId must be matched on it directly."""
        answer = _committed(idem="turn:MISMATCH:user")
        answer["messages"][1]["runId"] = "turn:abc123"
        got = _rt(answer)._reconcile(_handle())
        self.assertIsNotNone(got)

    def test_a_mismatched_runid_is_rejected_without_falling_back(self):
        """If a message says which run it belongs to and it is not ours, the
        anchor must not then claim it anyway."""
        answer = _committed()
        answer["messages"][1]["runId"] = "turn:SOMEONE-ELSE"
        self.assertIsNone(_rt(answer)._reconcile(_handle()))


class RestraintTests(unittest.TestCase):

    def test_a_streaming_message_is_not_final(self):
        answer = _committed()
        answer["messages"][1]["streaming"] = True
        self.assertIsNone(_rt(answer)._reconcile(_handle()))

    def test_the_session_key_is_sent_not_the_session_id(self):
        """chat.history REFUSES sessionId. With the wrong name every call
        failed validation and this silently returned None."""
        rt = _rt(_committed())
        rt._reconcile(_handle())
        method, params = rt._client.calls[0]
        self.assertEqual(method, "chat.history")
        self.assertIn("sessionKey", params)
        self.assertNotIn("sessionId", params)


if __name__ == "__main__":
    unittest.main()
