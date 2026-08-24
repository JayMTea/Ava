"""Gateway exec approvals, merged into Ava's one approvals banner.

An exec approval is the same kind of thing as one of Ava's own: a call blocked
with a person waiting on it. So it belongs in the one banner rather than a
second place to look — but merging two sources into one endpoint has hazards
this file exists to pin:

  * a decision must reach the RIGHT resolver (ids are namespaced, not guessed),
  * a gateway that is down must not take the Hub's banner down with it,
  * the buttons offered must be the ones the ROW accepts.

Every shape here was captured from a live gateway by creating, listing and
resolving real approvals (2026-08-24) — including that `exec.approval.list`
answers with a bare LIST, and that no event fires while one is pending, so this
is polled by design rather than by omission.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-gwappr-test-"))

import importlib

from ava_bridge.hub import governance as g

openclaw_gw = importlib.import_module("ava_bridge.runtime.openclaw_gw")


class FakeClient:
    def __init__(self, rows=None, methods=None, raise_on=()):
        self._rows = rows if rows is not None else []
        self._methods = frozenset(
            methods if methods is not None
            else {"exec.approval.list", "exec.approval.resolve"})
        self._raise_on = set(raise_on)
        self.calls: list[tuple] = []

    def methods(self):
        return self._methods

    def rpc(self, method, params=None, *, timeout=None, idempotency_key=None):
        self.calls.append((method, params))
        if method in self._raise_on:
            raise RuntimeError("gateway is down")
        if method == "exec.approval.list":
            return self._rows
        return {}


def FakeRt(client):
    """The REAL adapter over a fake client: the row shaping and the decision
    vocabulary are the adapter's job, so a stub of it would test nothing."""
    return openclaw_gw.OpenClawGatewayRuntime(client=client)


def _with(client, fn):
    from ava_bridge import runtime
    old = runtime.configured
    runtime.configured = lambda: FakeRt(client)
    try:
        return fn()
    finally:
        runtime.configured = old


# The row shape a live gateway returns.
def _row(aid="abc", command="echo hi",
         allowed=("allow-once", "allow-always", "deny")):
    return {"id": aid, "createdAtMs": 1700000000000,
            "request": {"command": command, "allowedDecisions": list(allowed),
                        "warningText": None, "commandAnalysis": {}}}


class ListTests(unittest.TestCase):

    def _pending(self, client):
        return _with(client, g._gateway_pending)

    def test_a_bare_list_is_what_the_gateway_answers(self):
        """Captured live: it is NOT {approvals: [...]}. A reader expecting a
        dict finds nothing and shows an empty banner forever."""
        got = _with(FakeClient([_row()]), g._gateway_pending)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["action"], "echo hi")

    def test_an_empty_object_means_nothing_pending(self):
        """The empty case comes back as {} rather than []."""
        self.assertEqual(_with(FakeClient([]), g._gateway_pending), [])

    def test_ids_are_namespaced_so_a_decision_can_be_routed(self):
        got = _with(FakeClient([_row(aid="xyz")]), g._gateway_pending)
        self.assertTrue(got[0]["id"].startswith(g._GW_PREFIX))
        self.assertIn("xyz", got[0]["id"])

    def test_rows_are_marked_as_the_agents(self):
        """The banner renders a COMMAND differently from a connector action —
        'wants to run X on <connector>' would name a connector not involved."""
        got = _with(FakeClient([_row()]), g._gateway_pending)
        self.assertEqual(got[0]["source"], "agent")

    def test_only_decisions_the_row_accepts_are_offered(self):
        got = _with(FakeClient([_row(allowed=("allow-once", "deny"))]),
                    g._gateway_pending)
        self.assertEqual(got[0]["decisions"], ["approve", "deny"])
        self.assertFalse(got[0]["grantable"],
                         "offering Always on a row that does not accept it is "
                         "a button that fails when pressed")

    def test_a_malformed_row_is_skipped_not_crashed_on(self):
        got = _with(FakeClient([{"no": "id"}, _row()]), g._gateway_pending)
        self.assertEqual(len(got), 1)


class RestraintTests(unittest.TestCase):

    def test_a_gateway_that_is_down_does_not_break_the_banner(self):
        """This runs on EVERY Hub tab poll. An exception here would take out
        Ava's own approvals too — the ones this bridge is itself blocking on."""
        client = FakeClient(raise_on=["exec.approval.list"])
        self.assertEqual(_with(client, g._gateway_pending), [])

    def test_a_runtime_without_approvals_answers_empty(self):
        """The ABC default: a runtime with no approval mechanism has none
        pending, which is the truthful answer rather than an error."""
        from ava_bridge.runtime.base import AgentRuntime
        self.assertEqual(AgentRuntime.pending_approvals(object()), [])

    def test_a_gateway_without_the_method_is_not_asked(self):
        client = FakeClient([_row()], methods={"chat.send"})
        self.assertEqual(_with(client, g._gateway_pending), [])
        self.assertEqual(client.calls, [], "must not call what is not offered")


class DecisionTests(unittest.TestCase):

    def test_decisions_are_translated_to_the_gateways_vocabulary(self):
        for ours, theirs in (("approve", "allow-once"),
                             ("always", "allow-always"),
                             ("deny", "deny")):
            client = FakeClient()
            _with(client, lambda d=ours: g._decide_gateway("abc", d))
            self.assertEqual(client.calls[-1],
                             ("exec.approval.resolve",
                              {"id": "abc", "decision": theirs}))

    def test_an_unknown_decision_is_refused_rather_than_forwarded(self):
        client = FakeClient()
        ok = _with(client, lambda: g._decide_gateway("abc", "maybe"))
        self.assertFalse(ok)
        self.assertEqual(client.calls, [])

    def test_an_expired_approval_reports_failure_instead_of_raising(self):
        """It may have timed out while sitting on screen — ordinary, not a 500."""
        client = FakeClient(raise_on=["exec.approval.resolve"])
        self.assertFalse(_with(client, lambda: g._decide_gateway("abc", "deny")))


if __name__ == "__main__":
    unittest.main()
