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
# After a gated call times out, an IDENTICAL call (same connector, action and
# arguments) is refused for this long without parking a fresh prompt. The agent
# side is not to be trusted with restraint here: a local model that reads a
# timeout as a flaky service retried one gated write four times in a row, and
# every retry put a new prompt in front of the owner for the same thing. One
# unanswered prompt is a decision the owner has not made yet; a stack of them
# is noise that trains the owner to click through. A decision (approve or deny)
# clears the cooldown, so the owner acting on the prompt is never blocked by it.
COOLDOWN_S = 300.0

_pending: dict[str, dict] = {}
_cv = threading.Condition()
_cooldown: dict[tuple, float] = {}   # (cid, action, args-hash) -> when it timed out


def _slim_args(args) -> dict:
    """A compact, display-safe view of the call args for the approval prompt."""
    out = {}
    for k, v in (args or {}).items():
        s = str(v)
        out[str(k)[:40]] = s[:200] + ("…" if len(s) > 200 else "")
    return out


def _cooldown_key(cid: str, action: str, args: dict | None) -> tuple:
    import hashlib
    import json
    try:
        blob = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — unhashable oddities still get a key
        blob = repr(args)
    return (cid, action, hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16])


def gate(cid: str, action: str, args: dict | None) -> str:
    """Block until the operator decides. Returns 'approved' | 'denied' |
    'timeout' | 'skip' (skip = no confirmation required)."""
    from . import connectors, audit
    if not connectors.needs_confirm(cid, action):
        return "skip"

    key = _cooldown_key(cid, action, args)
    aid = uuid.uuid4().hex[:10]
    with _cv:
        since = time.time() - _cooldown.get(key, 0.0)
        if since < COOLDOWN_S:
            # Same call, same arguments, and the owner has not answered the
            # last prompt for it: do not stack another. Recorded so the ledger
            # shows the repeat, not just the original.
            audit.record("approval", connector=cid, action=action, state="cooldown")
            return "cooldown"
        if len(_pending) >= _MAX_PENDING:
            return "denied"                 # too many parked — fail safe
        _pending[aid] = {"id": aid, "connector": cid, "action": action,
                         "args": _slim_args(args), "status": "pending",
                         "ts": round(time.time(), 3),
                         # JIT consent: write-tier prompts may offer "Always
                         # allow"; destructive/author-confirm ones may not.
                         "grantable": connectors.grantable(cid, action),
                         "access": connectors.action_access(cid, action)}
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
            _cooldown[key] = time.time()
        else:
            _cooldown.pop(key, None)        # the owner decided: no cooldown to serve
        _pending.pop(aid, None)
        # Keep the table small; entries older than the window are dead weight.
        for k in [k for k, t in _cooldown.items() if time.time() - t > COOLDOWN_S]:
            _cooldown.pop(k, None)
    audit.record("approval", connector=cid, action=action, state=status)
    return status


def pending() -> list[dict]:
    with _cv:
        return [dict(p) for p in _pending.values() if p["status"] == "pending"]


def decide(aid: str, approve: bool, remember: bool = False) -> bool:
    """Resolve one pending request. ``remember=True`` on an approval writes a
    durable grant ("Always allow") so this (connector, action) never asks again
    — only honored where the prompt was grantable (write tier, no author gate)."""
    with _cv:
        p = _pending.get(aid)
        if not p or p["status"] != "pending":
            return False
        p["status"] = "approved" if approve else "denied"
        to_grant = (p["connector"], p["action"]) if (
            approve and remember and p.get("grantable")) else None
        _cv.notify_all()
    if to_grant:                     # file IO outside the lock; future calls only
        from . import grants
        grants.grant(*to_grant)
    return True
