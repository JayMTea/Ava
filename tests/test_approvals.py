"""Human-in-the-loop approval gate for sensitive connector actions."""
import threading
import time
import unittest
from unittest import mock

from ava_bridge import approvals, connectors


class TestNeedsConfirm(unittest.TestCase):
    """JIT consent semantics: author confirm always asks; read is silent;
    ungranted writes ask; unknown (dynamic) actions count as writes."""

    def setUp(self):
        # No durable grants in play — point the store at an empty tmp file.
        import tempfile, os
        from ava_bridge import grants
        self._g = mock.patch.object(
            grants, "PATH", os.path.join(tempfile.mkdtemp(), "g.yaml"))
        self._g.start()
        grants._cache.update(data=None, mtime=0.0)

    def tearDown(self):
        self._g.stop()

    def test_connector_level_and_per_action(self):
        with mock.patch.object(connectors, "load", return_value=[{"id": "a", "confirm": True}]):
            self.assertTrue(connectors.needs_confirm("a", "anything"))
        with mock.patch.object(connectors, "load", return_value=[
                {"id": "b", "confirm": ["transfer"],
                 "actions": [{"id": "balance", "method": "GET", "path": "/b"}]}]):
            self.assertTrue(connectors.needs_confirm("b", "transfer"))
            self.assertFalse(connectors.needs_confirm("b", "balance"))  # read: silent
        with mock.patch.object(connectors, "load", return_value=[
                {"id": "c", "actions": [{"id": "wipe", "path": "/x", "confirm": True},
                                        {"id": "read", "method": "GET", "path": "/y"}]}]):
            self.assertTrue(connectors.needs_confirm("c", "wipe"))
            self.assertFalse(connectors.needs_confirm("c", "read"))

    def test_unmarked_writes_and_dynamic_tools_ask(self):
        # The JIT change of heart: an unmarked non-GET action is a write and
        # asks on first use; so does an action with no manifest entry at all.
        with mock.patch.object(connectors, "load", return_value=[
                {"id": "c", "actions": [{"id": "make", "path": "/y"}]}]):
            self.assertTrue(connectors.needs_confirm("c", "make"))
        with mock.patch.object(connectors, "load", return_value=[{"id": "d"}]):
            self.assertTrue(connectors.needs_confirm("d", "anything"))


class TestGate(unittest.TestCase):
    def _gate_async(self, cid, action, args):
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", approvals.gate(cid, action, args)))
        t.start()
        return out, t

    def test_skip_when_not_required(self):
        # Reads are the silent tier under JIT consent.
        with mock.patch.object(connectors, "load", return_value=[
                {"id": "free", "actions": [{"id": "go", "method": "GET", "path": "/g"}]}]):
            self.assertEqual(approvals.gate("free", "go", {}), "skip")

    def test_approve_unblocks(self):
        with mock.patch.object(connectors, "load", return_value=[{"id": "bank", "confirm": True}]):
            out, t = self._gate_async("bank", "transfer", {"amt": 5})
            time.sleep(0.3)
            p = approvals.pending()
            self.assertEqual(len(p), 1)
            self.assertEqual(p[0]["args"], {"amt": "5"})
            self.assertTrue(approvals.decide(p[0]["id"], True))
            t.join(timeout=5)
            self.assertEqual(out["r"], "approved")
            self.assertEqual(approvals.pending(), [])   # cleared

    def test_deny_unblocks(self):
        with mock.patch.object(connectors, "load", return_value=[{"id": "bank", "confirm": True}]):
            out, t = self._gate_async("bank", "transfer", {})
            time.sleep(0.3)
            approvals.decide(approvals.pending()[0]["id"], False)
            t.join(timeout=5)
            self.assertEqual(out["r"], "denied")

    def test_decide_unknown_id_is_false(self):
        self.assertFalse(approvals.decide("nope", True))


if __name__ == "__main__":
    unittest.main()
