"""The approvals gate's tool result must read as a decision, not an outage.

The agent sees exactly this text as the tool's return value, so it decides what
the model does next. "not run — awaiting-approval timeout" read like a flaky
service: the model retried a gated write four times (each retry parking a fresh
prompt for the owner) and then went looking for something to fix.
"""
from ava_bridge import internal


def test_timeout_names_the_owner_and_forbids_retry():
    msg = internal._gate_message("timeout")
    assert msg.startswith("NOT RUN")
    assert "approval" in msg and "owner" in msg
    assert "not retry" in msg or "Do not retry" in msg
    assert "not a service error" in msg


def test_denied_is_final():
    msg = internal._gate_message("denied")
    assert msg.startswith("NOT RUN")
    assert "declined" in msg and ("not retry" in msg or "Do not retry" in msg)


def test_any_other_outcome_still_points_at_approval():
    msg = internal._gate_message("pending")
    assert msg.startswith("NOT RUN") and "approval" in msg and "pending" in msg
