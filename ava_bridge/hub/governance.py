"""Setup -> Approvals and the audit ledger.

Two small surfaces that belong together: the queue of code changes waiting on
the owner's approval, and the append-only record of what was actually done.
Kept in one module because an approval decision and its audit entry are two
halves of the same governance story — reviewing one without the other is how a
gate becomes decorative.
"""
from fastapi import APIRouter

from .. import audit

router = APIRouter()


# --------------------------------------------------------------------------- #
# Flight recorder — the durable append-only audit ledger
# --------------------------------------------------------------------------- #
@router.get("/audit")
def audit_log(limit: int = 200, kind: str = ""):
    """Recent audit events (newest first): agent turns + self-edit outcomes,
    from $AVA_HOME/logs/audit.jsonl — survives restarts, unlike the ops views."""
    limit = max(1, min(int(limit), 1000))
    return {"events": audit.tail(limit, kind=kind or None)}

# --------------------------------------------------------------------------- #
# Approvals — the agent parked a sensitive connector action; the operator OKs it
# --------------------------------------------------------------------------- #
@router.get("/approvals")
def approvals_list():
    from .. import approvals
    return {"pending": approvals.pending()}

@router.post("/approvals/{aid}")
def approvals_decide(aid: str, decision: str = "approve"):
    """decision: approve (once) | always (approve + durable grant) | deny."""
    from .. import approvals
    return {"ok": approvals.decide(aid, decision != "deny",
                                   remember=decision == "always")}

