"""Owner-facing learning API (/api/learning/*) — cookie-gated, session auth.

Ava's local-first learning cycles analyse her own activity and park improvement
proposals; this is where the owner reviews them. Two proposal streams, each with
the same apply/reject/feedback shape: `code` (edits to Ava's own source, which
go through code_agent and the approval gate) and `chat` (behavioural notes).

NOT to be confused with the /internal/learning/* routes, which live in
ava_bridge/internal.py. Those are the SANDBOX side of the same feature — token-
gated callbacks the agent uses to read its own learning state — and they stay
with the rest of the /internal surface because their auth model, not their
topic, is what governs them. Same subject, different trust boundary.
"""
from datetime import datetime

from fastapi import APIRouter

from . import code_agent, config, learning, state

router = APIRouter()
@router.post("/api/learning/run")
async def learning_run():
    """Manually trigger a learning cycle now (the Learning page 'Run now' button).
    Analyzes recent code + chat activity locally and parks any proposals."""
    summary = await learning.run_all_cycles()
    return {"ok": True, **summary}

@router.get("/api/learning/code/state")
def learning_code_state():
    """Get code-learning cycles and proposals."""
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        return {
            "context": "code",
            "cycles": cycles,
            "last_cycle": state.code_learning_state.get("last_cycle"),
            # Lets the UI say "learning is off" instead of promising proposals
            # that a disabled scheduler will never produce.
            "enabled": config.LEARNING_ENABLED,
        }

@router.get("/api/learning/chat/state")
def learning_chat_state():
    """Get chat-learning cycles and proposals."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        return {
            "context": "chat",
            "cycles": cycles,
            "last_cycle": state.chat_learning_state.get("last_cycle"),
            "enabled": config.LEARNING_ENABLED,
        }

@router.post("/api/learning/code/apply")
def learning_code_apply(proposal_id: str):
    """Approve a code proposal. If it carries staged code changes (from Ava's
    code_change_request / self-fix), actually write + commit them; otherwise just
    mark the improvement suggestion approved."""
    # Does this proposal carry real staged changes to apply?
    has_changes = False
    with state.code_learning_state_lock:
        for cycle in state.code_learning_state.get("cycles", []):
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    has_changes = bool(prop.get("staged_changes"))
                    break
    if has_changes:
        res = code_agent.apply_approved_proposal(proposal_id)
        if not res.get("ok"):
            err = res.get("error", "apply failed")
            if res.get("test_output"):
                err += f"\n\n{res['test_output']}"
            return {"ok": False, "error": err}
        msg = f"Applied {len(res.get('files', []))} file(s)"
        if res.get("branch"):
            msg += f" to branch {res['branch']}"
            if res.get("tests_passed"):
                msg += " (checks passed)"
        if res.get("commit"):
            msg += f" · commit {res['commit']}"
        # Report what HAPPENED. This said " · restarting bridge" whenever a
        # restart was wanted, regardless of whether one occurred — and on any
        # install without an active systemd unit (which is every fork, since no
        # .service file ships) none ever did.
        _rr = res.get("restart_result") or {}
        if _rr.get("ok"):
            msg += f" · {_rr.get('detail', 'restarting bridge')}"
        elif res.get("restart"):
            msg += " · RESTART NEEDED: " + str(_rr.get("detail") or "restart Ava "
                                               "to pick up the change")
        return {"ok": True, "message": msg, "files": res.get("files", []),
                "commit": res.get("commit"), "restart": res.get("restart", False),
                "branch": res.get("branch")}
    # Plain improvement suggestion — just record approval.
    with state.code_learning_state_lock:
        for cycle in state.code_learning_state.get("cycles", []):
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "completed"
                    prop["applied"] = True
                    prop["completed_by"] = prop.get("requested_by") or "Ava"
                    prop["approved_by"] = config.OPERATOR_NAME
                    prop["completed_at"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {"ok": True, "proposal": prop,
                            "message": f"Approved: {prop.get('title')}"}
    return {"ok": False, "error": "Proposal not found"}

@router.post("/api/learning/code/reject")
def learning_code_reject(proposal_id: str):
    """Reject a code-improvement proposal."""
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "rejected"
                    state.save_learning_state()
                    return {"ok": True, "message": "Proposal rejected"}
    return {"ok": False, "error": "Proposal not found"}

@router.post("/api/learning/code/feedback")
def learning_code_feedback(proposal_id: str, rating: int):
    """Record feedback on a code-improvement proposal (1=helpful, 0=not helpful)."""
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["feedback"] = rating
                    prop["feedback_timestamp"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {"ok": True}
    return {"ok": False, "error": "Proposal not found"}

@router.post("/api/learning/chat/apply")
def learning_chat_apply(proposal_id: str):
    """Approve a chat-improvement proposal."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "completed"
                    prop["applied"] = True
                    prop["completed_by"] = prop.get("requested_by") or "Ava"
                    prop["approved_by"] = config.OPERATOR_NAME
                    prop["completed_at"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {
                        "ok": True,
                        "proposal": prop,
                        "message": f"Approved: {prop.get('title')}"
                    }
    return {"ok": False, "error": "Proposal not found"}

@router.post("/api/learning/chat/reject")
def learning_chat_reject(proposal_id: str):
    """Reject a chat-improvement proposal."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["status"] = "rejected"
                    state.save_learning_state()
                    return {"ok": True, "message": "Proposal rejected"}
    return {"ok": False, "error": "Proposal not found"}

@router.post("/api/learning/chat/feedback")
def learning_chat_feedback(proposal_id: str, rating: int):
    """Record feedback on a chat-improvement proposal (1=helpful, 0=not helpful)."""
    with state.chat_learning_state_lock:
        cycles = state.chat_learning_state.get("cycles", [])
        for cycle in cycles:
            for prop in cycle.get("proposals", []):
                if prop.get("id") == proposal_id:
                    prop["feedback"] = rating
                    prop["feedback_timestamp"] = datetime.now().isoformat()
                    state.save_learning_state()
                    return {"ok": True}
    return {"ok": False, "error": "Proposal not found"}

