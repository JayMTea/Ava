"""Human-in-the-loop approval gate for sensitive connector actions."""
import threading
import time
import unittest
from unittest import mock

from ava_bridge import approvals, connectors


class TestNeedsConfirm(unittest.TestCase):
    def test_connector_level_and_per_action(self):
        with mock.patch.object(connectors, "load", return_value=[{"id": "a", "confirm": True}]):
            self.assertTrue(connectors.needs_confirm("a", "anything"))
        with mock.patch.object(connectors, "load",
                               return_value=[{"id": "b", "confirm": ["transfer"]}]):
            self.assertTrue(connectors.needs_confirm("b", "transfer"))
            self.assertFalse(connectors.needs_confirm("b", "balance"))
        with mock.patch.object(connectors, "load", return_value=[
                {"id": "c", "actions": [{"id": "wipe", "path": "/x", "confirm": True},
                                        {"id": "read", "path": "/y"}]}]):
            self.assertTrue(connectors.needs_confirm("c", "wipe"))
            self.assertFalse(connectors.needs_confirm("c", "read"))
        with mock.patch.object(connectors, "load", return_value=[{"id": "d"}]):
            self.assertFalse(connectors.needs_confirm("d", "anything"))


class TestGate(unittest.TestCase):
    def _gate_async(self, cid, action, args):
        out = {}
        t = threading.Thread(target=lambda: out.__setitem__("r", approvals.gate(cid, action, args)))
        t.start()
        return out, t

    def test_skip_when_not_required(self):
        with mock.patch.object(connectors, "load", return_value=[{"id": "free"}]):
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
