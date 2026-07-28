"""Per-chat conversation persistence (data/chats.json).

Each chat is an independent conversation with its own OpenClaw session-id, so Ava
keeps separate memory per chat. Persisted to JSON so chats survive page reloads
and service restarts.
"""
import json
import os
import time
import uuid

from . import config, state


def _secure_opener(path: str, flags: int) -> int:
    """open() opener that creates chats.json 0600 (chat history is sensitive)."""
    return os.open(path, flags, 0o600)


def _chats_read() -> dict:
    try:
        with open(config.CHATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# Load persisted chats into the shared store (mutate in place — never rebind).
state.chats.update(_chats_read())


def _chats_persist() -> None:
    with state.chats_lock:
        tmp = config.CHATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", opener=_secure_opener) as f:
            json.dump(state.chats, f, ensure_ascii=False)
        os.replace(tmp, config.CHATS_FILE)
        os.chmod(config.CHATS_FILE, 0o600)


def _chat_session(cid: str) -> str:
    """Per-chat OpenClaw session-id so Ava's memory is isolated per conversation."""
    return f"{config.OC_SESSION}-{cid}"


def _chat_new(title: str = "New chat") -> dict:
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    with state.chats_lock:
        state.chats[cid] = {"id": cid, "title": title, "created": now,
                            "updated": now, "messages": []}
        _chats_persist()
        return dict(state.chats[cid])


def _trim_steps(steps: list | None) -> list | None:
    """Keep the reasoning trajectory durable but bounded — chats.json is loaded
    whole into memory, so cap the count and truncate long thinking blocks."""
    if not steps:
        return None
    out = []
    for s in steps[-60:]:
        if not isinstance(s, dict):
            continue
        st = {"kind": s.get("kind", "text")}
        if s.get("name"):
            st["name"] = str(s["name"])[:120]
        tx = s.get("text")
        if tx:
            tx = str(tx)
            st["text"] = tx[:4000] + (" …[truncated]" if len(tx) > 4000 else "")
        out.append(st)
    return out or None


def chat_append(cid: str, role: str, content: str,
                 atts: list | None = None, image: str | None = None,
                 model: dict | None = None,
                 img_models: list | None = None,
                 tools_used: list[str] | None = None,
                 steps: list | None = None,
                 error_code: str | None = None) -> None:
    with state.chats_lock:
        c = state.chats.get(cid)
        if not c:
            return
        msg = {"role": role, "content": content or "", "ts": time.time()}
        if atts:
            msg["atts"] = atts
        if image:
            msg["image"] = image
        if error_code:
            # machine-readable ("image_off", "gpusvc_down") — the chat UI derives
            # the guided-fix link from the code pattern (frontend/src/lib/fixes.ts)
            msg["error_code"] = error_code
        if img_models:
            msg["img_models"] = img_models
        if model:
            msg["model"] = model
        if tools_used:
            msg["tools_used"] = [str(t) for t in tools_used if str(t).strip()]
        trimmed = _trim_steps(steps)
        if trimmed:
            msg["steps"] = trimmed  # durable chain-of-thought (survives reload)
        c["messages"].append(msg)
        c["updated"] = msg["ts"]
        # Auto-title an untitled chat from its first user message.
        if (role == "user" and content.strip()
                and c.get("title") in (None, "", "New chat")):
            c["title"] = content.strip()[:48]
        _chats_persist()


def last_render_context(cid: str) -> tuple[str | None, str | None]:
    """(last_route, last_image_prompt) for the intent gate: what did the
    assistant last produce in this chat? An image message → ("image", its
    prompt/caption); any other assistant reply → ("chat", None); an empty or
    unknown chat → (None, None)."""
    if not cid:
        return None, None
    with state.chats_lock:
        c = state.chats.get(cid)
        msgs = list(c.get("messages", [])) if c else []
    for m in reversed(msgs):
        if m.get("role") != "assistant":
            continue
        if m.get("image"):
            return "image", (m.get("content") or "").strip() or None
        if m.get("error_code"):
            continue  # a failed render verdict isn't a conversational turn
        return "chat", None
    return None, None


def recent_user_text(cid: str, limit: int = 240) -> str:
    """The user's PREVIOUS message in this chat — conversation context for the
    intent gate. The gate runs before the current message is stored, so the last
    user message here is the prior one; it lets the gate classify context-
    dependent follow-ups (a pasted prompt after "help me write a prompt", "keep
    it general", "that same scene") that are ambiguous in isolation. Empty if
    none."""
    if not cid:
        return ""
    with state.chats_lock:
        c = state.chats.get(cid)
        msgs = list(c.get("messages", [])) if c else []
    for m in reversed(msgs):
        if m.get("role") == "user":
            return (m.get("content") or "").strip()[:limit]
    return ""


def history_for(cid: str, limit: int = 20) -> list[dict]:
    """Prior {role, content} messages for the degraded (runtime-less) chat path.
    Excludes the trailing user message (that's the current turn) and caps length."""
    if not cid:
        return []
    with state.chats_lock:
        c = state.chats.get(cid)
        msgs = list(c.get("messages", [])) if c else []
    if msgs and msgs[-1].get("role") == "user":
        msgs = msgs[:-1]
    return [{"role": m.get("role"), "content": m.get("content")} for m in msgs[-limit:]]


def recent_messages(since_ts: float, limit: int = 200) -> list[dict]:
    """User/assistant messages newer than since_ts across ALL chats, oldest
    first — the memory distiller's feed (ava_bridge/learning.py)."""
    out = []
    with state.chats_lock:
        for c in state.chats.values():
            for m in c.get("messages", []):
                ts = m.get("ts", 0)
                if ts > since_ts and m.get("role") in ("user", "assistant") \
                        and (m.get("content") or "").strip():
                    out.append({"role": m["role"], "content": m["content"],
                                "ts": ts, "chat_id": c.get("id", "")})
    out.sort(key=lambda m: m["ts"])
    return out[-limit:]


def _chat_summary(c: dict) -> dict:
    return {"id": c["id"], "title": c.get("title") or "New chat",
            "updated": c.get("updated", 0), "count": len(c.get("messages", []))}


def _atts_meta(ids: list) -> list:
    """Lightweight attachment metadata to persist alongside a user message."""
    out = []
    with state.attachments_lock:
        for i in ids:
            r = state.attachments.get(i)
            if r:
                out.append({"filename": r["filename"], "kind": r["kind"], "url": r["url"]})
    return out
