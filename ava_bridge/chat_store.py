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


def _chat_append(cid: str, role: str, content: str,
                 atts: list | None = None, image: str | None = None,
                 model: dict | None = None,
                 img_models: list | None = None,
                 tools_used: list[str] | None = None,
                 steps: list | None = None) -> None:
    with state.chats_lock:
        c = state.chats.get(cid)
        if not c:
            return
        msg = {"role": role, "content": content or "", "ts": time.time()}
        if atts:
            msg["atts"] = atts
        if image:
            msg["image"] = image
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
