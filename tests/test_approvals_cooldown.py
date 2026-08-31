"""One unanswered prompt per gated call, not a stack of them.

A local model that reads a gate timeout as a flaky service retried one gated
write four times in a row, and every retry parked a fresh prompt for the owner.
After a timeout, an identical call is refused for COOLDOWN_S without parking;
a decision by the owner clears the cooldown at once.
"""
import threading
import time

from ava_bridge import approvals


def _gated(monkeypatch, timeout=0.05, cooldown=300.0):
    from ava_bridge import connectors
    monkeypatch.setattr(connectors, "needs_confirm", lambda cid, action: True)
    monkeypatch.setattr(connectors, "grantable", lambda cid, action: True, raising=False)
    monkeypatch.setattr(connectors, "action_access", lambda cid, action: "write", raising=False)
    monkeypatch.setattr(approvals, "TIMEOUT_S", timeout)
    monkeypatch.setattr(approvals, "COOLDOWN_S", cooldown)
    approvals._cooldown.clear()


def test_a_repeat_after_timeout_is_refused_without_parking(monkeypatch):
    _gated(monkeypatch)
    assert approvals.gate("healthapp", "log_weight", {"weight_kg": 80}) == "timeout"
    t0 = time.time()
    assert approvals.gate("healthapp", "log_weight", {"weight_kg": 80}) == "cooldown"
    assert time.time() - t0 < 0.05, "a cooled-down call must not wait on the operator"
    assert approvals.pending() == []


def test_different_arguments_are_a_different_ask(monkeypatch):
    _gated(monkeypatch)
    assert approvals.gate("healthapp", "log_weight", {"weight_kg": 80}) == "timeout"
    assert approvals.gate("healthapp", "log_weight", {"weight_kg": 81}) == "timeout"


def test_the_cooldown_expires(monkeypatch):
    _gated(monkeypatch, cooldown=0.05)
    assert approvals.gate("senses", "record_clip", {}) == "timeout"
    time.sleep(0.08)
    assert approvals.gate("senses", "record_clip", {}) == "timeout"


def test_a_decision_clears_the_cooldown(monkeypatch):
    _gated(monkeypatch, timeout=2.0)
    approvals.gate("healthapp", "log_weight", {"weight_kg": 80})          # times out -> cooldown armed
    # The owner now answers a fresh prompt for the same call: it must be parked,
    # and once decided the cooldown must be gone.
    approvals._cooldown.clear()
    result = {}
    def run():
        result["gate"] = approvals.gate("healthapp", "log_weight", {"weight_kg": 80})
    t = threading.Thread(target=run)
    t.start()
    for _ in range(100):
        if approvals.pending():
            break
        time.sleep(0.01)
    aid = approvals.pending()[0]["id"]
    assert approvals.decide(aid, True)
    t.join(timeout=3)
    assert result["gate"] == "approved"
    assert approvals._cooldown == {}
