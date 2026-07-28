"""Chat API (/api/chats/*, /api/chat-stream, /api/ghost/discard) — cookie-gated.

/api/chat-stream is the ONE ingress for typed messages: it runs the server-side
intent gate, which picks the pipeline (agent turn vs image render), so the
frontend carries no routing knowledge. It lives here rather than with the media
routes even though it can start a render, because what it returns is a chat
turn — the render is one outcome of a chat message, not a separate entry point.

Persistence goes through ava_bridge/chat_store.py and nothing here touches
state.chats. That is enforced, not merely intended — tests/test_chat_store_
boundary.py fails any module but chat_store reaching for it.

It matters because the chat corpus is moving off a whole-file chats.json rewrite
onto a real storage engine, and a store that callers reach past cannot have its
engine swapped underneath it: you migrate the data and the bypassers keep
reading an in-memory dict that is no longer authoritative. These handlers used
to do exactly that in eleven places.
"""
import json

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import audit, turn_router, memory_store
from .agent import discard_session
from .chat_store import (atts_meta, chat_append, chat_new, chat_session,
                         chat_summary, delete, last_render_context,
                         recent_image_urls, recent_user_text, rename, snapshot,
                         summaries)
from .config import OC_SESSION
from .documents import augment, parse_ids
from .gpu_jobs import start_image_job
from .turns import start_turn

router = APIRouter()
@router.post("/api/chat-stream")
async def chat_stream(text: str = Form(...), history: str = Form("[]"),
                      attachments: str = Form("[]"), chat_id: str = Form("")):
    """The ONE ingress for typed messages. The server-side intent gate
    (ava_bridge/turn_router.py) picks the pipeline — the frontend carries zero
    routing knowledge. Returns {"turn_id"} for an agent turn or {"job"} for a
    render; either way the outcome is persisted server-side (Phase 1)."""
    text = text.strip()
    ids = parse_ids(attachments)
    if not text and not ids:
        return JSONResponse({"error": "empty text"}, status_code=400)
    gate_route = None
    # Tier 1: attachments ride the agent turn (documents/images need tools).
    if text and not ids:
        last_route, last_prompt = last_render_context(chat_id)
        recent_user = recent_user_text(chat_id)
        d = await run_in_threadpool(turn_router.classify, text,
                                    last_route, last_prompt, recent_user, chat_id)
        gate_route = d["route"]
        if turn_router.PIPELINES.get(d["route"]) == "image":
            blocked = turn_router.policy_check(d["route"], text)  # Phase-4 seam
            if blocked:
                code, msg = blocked
                if chat_id:
                    chat_append(chat_id, "user", text)
                    chat_append(chat_id, "assistant", msg, error_code=code)
                return {"error": msg, "error_code": code}
            if chat_id:
                chat_append(chat_id, "user", text)
            job_id = start_image_job(d["image_prompt"], chat_id=chat_id or None)
            return {"job": {"id": job_id, "kind": "image",
                            "prompt": d["image_prompt"]},
                    "route": d["route"]}
    agent_text = augment(text, ids)
    agent_text = memory_store.augment_with_recall(agent_text, text, chat_id)
    # prompt_help shares the agent pipeline with chat, but gets the edit-don't-
    # execute hint so Ava refines the prompt instead of running it (July-11 fix).
    if gate_route == "prompt_help":
        agent_text = turn_router.PROMPT_HELP_HINT + agent_text
    sid = chat_session(chat_id) if chat_id else OC_SESSION
    if chat_id:
        chat_append(chat_id, "user", text, atts_meta(ids))
    tid = start_turn(agent_text, sid, chat_id)
    return {"turn_id": tid}

@router.post("/api/ghost/discard")
async def ghost_discard(chat_id: str = Form(...)):
    """Wipe a ghost conversation's agent-side session transcript.

    Ghost chats use an unregistered chat id, so they were never written to
    chats.json (host-side persistence is already a no-op). This also deletes the
    OpenClaw session file so the ephemeral conversation leaves no trace when the user
    exits ghost mode or starts a new chat.
    """
    cid = (chat_id or "").strip()
    if not cid:
        return {"ok": False}
    ok = await run_in_threadpool(discard_session, chat_session(cid))
    return {"ok": bool(ok)}

@router.get("/api/chats")
def chats_list():
    return {"chats": summaries()}

@router.post("/api/chats")
def chats_create():
    return chat_summary(chat_new())

@router.get("/api/chats/{cid}")
def chats_get(cid: str):
    c = snapshot(cid)
    if not c:
        return JSONResponse({"error": "unknown chat"}, status_code=404)
    return {"id": c["id"], "title": c.get("title") or "New chat",
            "messages": c.get("messages", [])}

@router.delete("/api/chats/{cid}")
def chats_delete(cid: str):
    gone = delete(cid)
    if gone is not None:
        # Same ledger treatment as memory edits: deletions leave a trace.
        audit.record("chat_delete", id=cid, title=gone.get("title") or "New chat",
                     messages=len(gone.get("messages") or []))
    return {"ok": gone is not None}

@router.patch("/api/chats/{cid}")
def chats_rename(cid: str, title: str = Form(...)):
    summary = rename(cid, title)
    if summary is None:
        return JSONResponse({"error": "unknown chat"}, status_code=404)
    return summary

@router.post("/api/chats/{cid}/image")
def chats_image(cid: str, url: str = Form(...), caption: str = Form(""),
                models: str = Form("")):
    """DEPRECATED — the bridge persists render outcomes itself now
    (gpu_jobs._finalize_job). Kept for stale cached clients (PWA service
    worker) and deduped so a client that still posts can't double-append."""
    recent = recent_image_urls(cid)
    if recent is None:
        return JSONResponse({"error": "unknown chat"}, status_code=404)
    if url in recent:
        return {"ok": True, "deduped": True}
    img_models = None
    if models:
        try:
            parsed = json.loads(models)
            if isinstance(parsed, list) and parsed:
                img_models = parsed
        except (ValueError, TypeError):
            img_models = None
    chat_append(cid, "assistant", caption, image=url, img_models=img_models)
    return {"ok": True}

