"""Human-in-the-loop approvals for sensitive connector actions.

A connector action can be marked to require the operator's OK before it runs
(manifest: `confirm: true` on an action, or connector-level `confirm: true` /
`confirm: [tool, …]`). When the agent calls such an action, the bridge route
parks a pending request here and BLOCKS until the operator approves or denies
in the UI (or a timeout elapses) — then the call proceeds or is refused.

The block uses a Condition, so approve/deny wakes the waiter immediately (no
polling on the server side). Every request + outcome is written to the audit
ledger, so "what did I approve" is on the record.
"""
from __future__ import annotations

import threading
import time
import uuid

TIMEOUT_S = 120.0            # how long a call waits for the operator
_MAX_PENDING = 50            # backstop against a flood of parked calls

_pending: dict[str, dict] = {}
_cv = threading.Condition()


def _slim_args(args) -> dict:
    """A compact, display-safe view of the call args for the approval prompt."""
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[str(k)[:40]] = s[:200] + ("…" if len(s) > 200 else "")
    return out


def gate(cid: str, action: str, args: dict | None) -> str:
    """Block until the operator decides. Returns 'approved' | 'denied' |
    'timeout' | 'skip' (skip = no confirmation required)."""
    from . import connectors, audit
    if not connectors.needs_confirm(cid, action):
        return "skip"

    aid = uuid.uuid4().hex[:10]
    with _cv:
        if len(_pending) >= _MAX_PENDING:
            return "denied"                 # too many parked — fail safe
        _pending[aid] = {"id": aid, "connector": cid, "action": action,
                         "args": _slim_args(args), "status": "pending",
                         "ts": round(time.time(), 3)}
        audit.record("approval", connector=cid, action=action, state="requested")
        deadline = time.time() + TIMEOUT_S
        while _pending[aid]["status"] == "pending":
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            _cv.wait(timeout=min(remaining, 5))
        status = _pending[aid]["status"]
        if status == "pending":
            status = "timeout"
        _pending.pop(aid, None)
    audit.record("approval", connector=cid, action=action, state=status)
    return status


def pending() -> list[dict]:
    with _cv:
        return [dict(p) for p in _pending.values() if p["status"] == "pending"]


def decide(aid: str, approve: bool) -> bool:
    with _cv:
        p = _pending.get(aid)
        if not p or p["status"] != "pending":
            return False
        p["status"] = "approved" if approve else "denied"
        _cv.notify_all()
        return True
