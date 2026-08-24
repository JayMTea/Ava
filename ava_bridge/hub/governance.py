"""Setup -> Approvals and the audit ledger.

Two small surfaces that belong together: the queue of code changes waiting on
the owner's approval, and the append-only record of what was actually done.
Kept in one module because an approval decision and its audit entry are two
halves of the same governance story — reviewing one without the other is how a
gate becomes decorative.
"""
from fastapi import APIRouter

from .. import audit
from .. import approvals
from .. import policy_inventory

router = APIRouter()


# --------------------------------------------------------------------------- #
# Egress policies — what the sandbox is actually allowed to reach
# --------------------------------------------------------------------------- #
@router.get("/policies/inventory")
def policies_inventory():
    """Every egress policy on the box: declared, generated and overlay.

    Facts only — file digests, endpoint/rule/binary counts, and any wildcard
    paths with the host each applies to. Which of those are *acceptable* is
    `ava_security_check`'s judgement, not this route's, because `/**` on a
    third-party API and `/**` on Ava's own loopback bridge are not the same claim.

    The generated policies are the point: they are written from connector
    manifests rather than reviewed by hand, and until this existed nothing showed
    them — including the wildcard check.
    """
    return policy_inventory.snapshot()


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
#: Gateway approval ids are namespaced on the way out so one endpoint can route
#: a decision back to the right resolver without a second lookup.
_GW_PREFIX = "gw:"


def _gateway_pending() -> list[dict]:
    """Approvals the AGENT parked, as banner rows.

    These are the same kind of thing as Ava's own connector approvals — a call
    blocked with a human waiting on it — so they share the one banner rather
    than giving the operator a second place to look.

    The gateway method names and their vocabulary live in the runtime adapter,
    not here: `tests/test_gateway_method_names.py` keeps every RPC inside the
    seam so an upstream rename lands in one file instead of across the app.
    """
    from .. import runtime
    try:
        rows = runtime.configured().pending_approvals()
    except Exception:  # noqa: BLE001 — a banner must never take the Hub down
        return []
    return [{**r, "id": _GW_PREFIX + r["id"]} for r in rows if r.get("id")]


@router.get("/approvals")
def approvals_list():
    # Ava's own first: they are the ones this bridge is itself blocking on.
    return {"pending": [{**p, "source": "connector"} for p in approvals.pending()]
                       + _gateway_pending()}

@router.post("/approvals/{aid}")
def approvals_decide(aid: str, decision: str = "approve"):
    """decision: approve (once) | always (approve + durable grant) | deny."""
    if aid.startswith(_GW_PREFIX):
        return {"ok": _decide_gateway(aid[len(_GW_PREFIX):], decision)}
    return {"ok": approvals.decide(aid, decision != "deny",
                                   remember=decision == "always")}


def _decide_gateway(aid: str, decision: str) -> bool:
    from .. import audit, runtime
    try:
        ok = runtime.configured().resolve_approval(aid, decision)
    except Exception:  # noqa: BLE001 — no gateway, or it refused
        ok = False
    if not ok:
        # A FAILURE, not a refusal: the approval most likely expired while it
        # sat on screen. Nothing refused it.
        audit.record("gateway_failed", method="exec.approval.resolve",
                     reason="resolve_failed")
        return False
    # The DECISION is the record; the command is already on the row the operator
    # saw, and re-recording it here would duplicate it in the ledger.
    audit.record("approval", connector="agent", action="exec", state=decision)
    return True
