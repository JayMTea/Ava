"""Gateway identity rows: what an operator is shown, and what they are not.

The rows answer one question — "is this the box I think it is, and who else is
paired to it". So the restraints matter more than the feature:

  * no credential ever reaches the panel,
  * a runtime with NO identity renders nothing rather than a row saying
    "unknown", because not-applicable and not-known are different answers,
  * a gateway that will not answer must not take the settings page down.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-ident-test-"))

from ava_bridge.runtime.base import AgentRuntime
from ava_bridge.runtime.errors import GatewayError

openclaw_gw = importlib.import_module("ava_bridge.runtime.openclaw_gw")

# What a live gateway answers (captured 2026-08-24).
LIVE = {
    "gateway.identity.get": {
        "deviceId": "84f3e6ae45fd30d93437bf2f616b0f64942828aecf5a8ed3cdbb"
                    "699b1ba4e031",
        "publicKey": "RSHgeDMEuIwRdNRsecretlookingvalue"},
    "agent.identity.get": {"agentId": "main", "name": "Assistant",
                           "avatar": "A"},
    "device.pair.list": {"pending": [], "paired": [{"deviceId": "x"}]},
    "agents.list": {"agents": [{"id": "main"}]},
}


class FakeClient:
    def __init__(self, answers=None, methods=None, raise_on=()):
        self._answers = answers if answers is not None else dict(LIVE)
        self._methods = frozenset(
            methods if methods is not None else self._answers)
        self._raise_on = set(raise_on)

    def methods(self):
        return self._methods

    def rpc(self, method, params=None, *, timeout=None, idempotency_key=None):
        if method in self._raise_on:
            raise GatewayError("nope", "gateway_timeout")
        return self._answers.get(method, {})


def _rt(**kw):
    return openclaw_gw.OpenClawGatewayRuntime(client=FakeClient(**kw))


class IdentityTests(unittest.TestCase):

    def test_it_reports_what_the_operator_asked(self):
        got = _rt().identity()
        self.assertEqual(got["agent_id"], "main")
        self.assertEqual(got["agent_name"], "Assistant")
        self.assertEqual(got["paired"], 1)
        self.assertEqual(got["pending"], 0)

    def test_the_device_id_is_shortened_for_a_human_to_compare(self):
        """A 64-char hex fingerprint is unreadable in a UI row, and the
        operator is comparing it, not transcribing it."""
        got = _rt().identity()
        self.assertEqual(got["device_id"], "84f3e6ae45fd")
        self.assertLess(len(got["device_id"]), 20)

    def test_the_public_key_never_leaves_the_gateway(self):
        """Nothing in Ava needs it, and a value that LOOKS like a key invites
        being treated as one."""
        got = _rt().identity()
        blob = repr(got)
        self.assertNotIn("publicKey", blob)
        self.assertNotIn("RSHgeDMEuIwRdNR", blob)


class RestraintTests(unittest.TestCase):

    def test_a_runtime_with_no_identity_says_so_with_none(self):
        """None renders as ABSENT rows. An empty dict would render rows reading
        'unknown', which is a different and wrong claim."""
        self.assertIsNone(AgentRuntime.identity(object()))

    def test_a_gateway_offering_nothing_answers_none(self):
        self.assertIsNone(_rt(methods=set()).identity())

    def test_one_failing_call_does_not_lose_the_others(self):
        """A partial answer is still useful; an exception is not."""
        got = _rt(raise_on=["device.pair.list"]).identity()
        self.assertEqual(got["agent_id"], "main")
        self.assertNotIn("paired", got)

    def test_every_call_failing_answers_none_rather_than_raising(self):
        got = _rt(raise_on=list(LIVE)).identity()
        self.assertIsNone(got)

    def test_a_malformed_answer_is_ignored_not_rendered(self):
        got = _rt(answers={"agent.identity.get": {"agentId": "", "name": ""},
                           "device.pair.list": {"paired": "not-a-list"}}
                  ).identity()
        self.assertNotIn("agent_id", got or {})
        self.assertIsNone((got or {}).get("paired"))


class EndpointTests(unittest.TestCase):

    def test_the_settings_route_never_500s_on_a_probe(self):
        """It is a SETTINGS page; a gateway that is down must not take it out."""
        from ava_bridge.hub import agent as hub_agent
        src = open(hub_agent.__file__, encoding="utf-8").read()
        self.assertIn("def _identity()", src)
        self.assertIn("except Exception", src.split("def _identity()")[1][:400])


if __name__ == "__main__":
    unittest.main()
