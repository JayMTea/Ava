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


def chats_persist() -> None:
    with state.chats_lock:
        tmp = config.CHATS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", opener=_secure_opener) as f:
            json.dump(state.chats, f, ensure_ascii=False)
        os.replace(tmp, config.CHATS_FILE)
        os.chmod(config.CHATS_FILE, 0o600)


def chat_session(cid: str) -> str:
    """Per-chat OpenClaw session-id so Ava's memory is isolated per conversation."""
    return f"{config.OC_SESSION}-{cid}"


def chat_new(title: str = "New chat") -> dict:
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    with state.chats_lock:
        state.chats[cid] = {"id": cid, "title": title, "created": now,
                            "updated": now, "messages": []}
        chats_persist()
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
                 error_code: str | None = None,
                 ts: float | None = None) -> None:
    """Append one message. `ts` defaults to now.

    An explicit `ts` is not a testing affordance — it is what lets history be
    written faithfully. Importing an existing corpus, or replaying one after a
    storage-engine change, has to preserve when things were actually said; a
    store that stamps every message with the moment of the write turns a
    migration into a corpus that all happened at once.
    """
    with state.chats_lock:
        c = state.chats.get(cid)
        if not c:
            return
        msg = {"role": role, "content": content or "", "ts": time.time() if ts is None else float(ts)}
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
        chats_persist()


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


def chat_summary(c: dict) -> dict:
    return {"id": c["id"], "title": c.get("title") or "New chat",
            "updated": c.get("updated", 0), "count": len(c.get("messages", []))}


def atts_meta(ids: list) -> list:
    """Lightweight attachment metadata to persist alongside a user message."""
    out = []
    with state.attachments_lock:
        for i in ids:
            r = state.attachments.get(i)
            if r:
                out.append({"filename": r["filename"], "kind": r["kind"], "url": r["url"]})
    return out


# --------------------------------------------------------------------------- #
# The store's read/mutate surface.
#
# These exist so nothing outside this module touches state.chats. Three modules
# used to — chats_api, data_api and a test — each holding state.chats_lock and
# walking the dict itself. That is fine while the backing store IS that dict and
# fatal the moment it is not: you migrate the data, and those callers keep
# reading an in-memory dict that is no longer authoritative, silently.
#
# Every operation below is deliberately expressible as a query, not as "hand me
# the dict". That is what makes the engine swappable — see docs on each.
# --------------------------------------------------------------------------- #
def get(cid: str) -> dict | None:
    """One conversation, or None. The live object — do not mutate outside a lock."""
    with state.chats_lock:
        return state.chats.get(cid)


def snapshot(cid: str) -> dict | None:
    """A detached deep copy of one conversation, safe to render or serialise.

    Taken inside the lock so a long render (Markdown export) works from a stable
    picture instead of a dict another request may be appending to.
    """
    with state.chats_lock:
        c = state.chats.get(cid)
        return json.loads(json.dumps(c, ensure_ascii=False, default=str)) if c else None


def delete(cid: str) -> dict | None:
    """Remove a conversation and persist. Returns the removed chat, or None."""
    with state.chats_lock:
        gone = state.chats.pop(cid, None)
    if gone is not None:
        chats_persist()
    return gone


def summaries() -> list[dict]:
    """Every conversation as a summary, newest first."""
    with state.chats_lock:
        items = [chat_summary(c) for c in state.chats.values()]
    items.sort(key=lambda x: x["updated"], reverse=True)
    return items


def counts() -> tuple[int, int]:
    """(conversations, total messages) — for the Data page's store inventory."""
    with state.chats_lock:
        return (len(state.chats),
                sum(len(c.get("messages") or []) for c in state.chats.values()))


def usage() -> list[dict]:
    """Per-conversation size accounting for the Data page, newest first.

    `bytes` is the conversation's own JSON weight rather than a share of the
    file, so it stays meaningful when the backing store is no longer one file.
    """
    with state.chats_lock:
        out = [{"id": c["id"], "title": c.get("title") or "New chat",
                "created": c.get("created", 0), "updated": c.get("updated", 0),
                "messages": len(c.get("messages") or []),
                "bytes": len(json.dumps(c, ensure_ascii=False).encode("utf-8"))}
               for c in state.chats.values()]
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def export_json(indent: int | None = 2) -> str:
    """The whole corpus as JSON text, for the Data page's download bundle."""
    with state.chats_lock:
        return json.dumps(state.chats, ensure_ascii=False, indent=indent, default=str)


def rename(cid: str, title: str) -> dict | None:
    """Retitle a conversation and persist. Returns its summary, or None.

    An operation rather than "fetch the dict and set a key", because the read,
    the write and the persist have to be one atomic step. A caller doing it by
    hand holds the lock across all three or races; over a database it is one
    UPDATE either way.
    """
    title = (title or "").strip()[:80]
    with state.chats_lock:
        c = state.chats.get(cid)
        if c is None:
            return None
        if title:
            c["title"] = title
            changed = True
        else:
            changed = False
        summary = chat_summary(c)
    if changed:
        chats_persist()
    return summary


def recent_image_urls(cid: str, limit: int = 8) -> list[str] | None:
    """Image URLs from a conversation's last `limit` messages; None if unknown.

    Narrow on purpose. The one caller only needs to know whether a URL is
    already there, so handing it the whole conversation to scan would make a
    dedup check depend on the storage shape. None vs [] is load-bearing: the
    caller answers 404 for the first and "nothing to dedup against" for the
    second.
    """
    with state.chats_lock:
        c = state.chats.get(cid)
        if c is None:
            return None
        return [m.get("image") for m in (c.get("messages") or [])[-limit:]
                if m.get("image")]


def reset() -> None:
    """Drop every conversation. Used by tests to start from a known corpus.

    Exposed here rather than letting callers clear state.chats themselves —
    the guard forbids that, and this is the one operation a test genuinely
    needs that no production path wants.
    """
    with state.chats_lock:
        state.chats.clear()
