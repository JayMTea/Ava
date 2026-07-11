"""Data API — cookie-gated `/api/data/*` routes behind the Data page.

A read-only inventory of everything Ava persists under $AVA_HOME, so the owner
can see exactly what exists, how big it is, and when it was last written.
Browsing and editing reuse the existing governed surfaces (/api/hub/memory*,
/api/chats*); this module never returns file *contents*. Secrets are listed as
counts only — held, never shown, never exported.

Mounted into the bridge, so `auth_gate` covers these automatically (not in
auth._PUBLIC_PATHS — no middleware change).
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import deque

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from . import audit, devices, settings, state

router = APIRouter(prefix="/api/data")

# Line-count files up to this size; beyond it report bytes only rather than
# stall the inventory on a pathological log.
_COUNT_MAX_BYTES = 64 * 1024 * 1024


def _file_stats(path: str) -> tuple[int, float]:
    """(bytes, mtime) for one file; (0, 0.0) if absent/unreadable."""
    try:
        st = os.stat(path)
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0


def _tree_stats(root: str) -> tuple[int, int, float]:
    """(bytes, files, latest mtime) across a directory tree; zeros if absent."""
    total = files = 0
    latest = 0.0
    try:
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                b, m = _file_stats(os.path.join(dirpath, n))
                total += b
                files += 1
                latest = max(latest, m)
    except OSError:
        pass
    return total, files, latest


def _line_count(path: str, size: int) -> int | None:
    """Newline count for a JSONL file, or None when too big / unreadable."""
    if size <= 0 or size > _COUNT_MAX_BYTES:
        return None
    n = 0
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                n += chunk.count(b"\n")
        return n
    except OSError:
        return None


def _rel(path: str) -> str:
    """AVA_HOME-relative display path (absolute only if outside the home)."""
    home = str(settings.AVA_HOME)
    try:
        return os.path.relpath(path, home) if path.startswith(home) else path
    except ValueError:
        return path


def _store(sid: str, label: str, path: str, fmt: str, *, size: int = 0,
           count: int | None = None, last_write: float | None = None,
           locked: bool = False, managed: bool = False, **extra) -> dict:
    return {"id": sid, "label": label, "path": _rel(path), "format": fmt,
            "bytes": size, "count": count,
            "last_write": last_write or None, "locked": locked,
            "managed": managed, **extra}


@router.get("/stores")
def stores():
    """Everything Ava keeps on disk, one entry per store (facts, no contents)."""
    data_dir = settings.data_dir()
    logs_dir = settings.logs_dir()
    out: list[dict] = []

    # Memory — SQLite FTS5 of distilled facts + indexed document chunks.
    from . import memory_store
    mem_path = os.path.join(data_dir, "memory.db")
    size, mtime = _file_stats(mem_path)
    mc = memory_store.counts()
    out.append(_store("memory", "Memory", mem_path, "sqlite", size=size,
                      count=mc.get("total", 0), last_write=mtime,
                      facts=mc.get("facts", 0), doc_chunks=mc.get("doc_chunks", 0),
                      pinned=mc.get("pinned", 0)))

    # Chats — one JSON file, already resident in state.chats.
    chats_path = os.path.join(data_dir, "chats.json")
    size, mtime = _file_stats(chats_path)
    with state.chats_lock:
        n_chats = len(state.chats)
        n_msgs = sum(len(c.get("messages") or []) for c in state.chats.values())
    out.append(_store("chats", "Chats", chats_path, "json", size=size,
                      count=n_chats, last_write=mtime, messages=n_msgs))

    # Audit ledger — append-only flight recorder.
    audit_path = os.path.join(logs_dir, "audit.jsonl")
    size, mtime = _file_stats(audit_path)
    out.append(_store("audit", "Audit ledger", audit_path, "jsonl", size=size,
                      count=_line_count(audit_path, size), last_write=mtime))

    # Performance — raw jsonl (+ rotated segments) and hourly/daily rollups.
    # perf_log resolves its own dir via AVA_PERF_LOG_DIR; mirror that here.
    perf_dir = os.environ.get("AVA_PERF_LOG_DIR", logs_dir)
    perf_size = perf_mtime = 0
    for name in ["performance.jsonl"] + [f"performance.jsonl.{i}" for i in range(1, 6)]:
        b, m = _file_stats(os.path.join(perf_dir, name))
        perf_size += b
        perf_mtime = max(perf_mtime, m)
    roll_size, _n, roll_mtime = _tree_stats(os.path.join(logs_dir, "rollups"))
    out.append(_store("performance", "Performance", os.path.join(perf_dir, "performance.jsonl"),
                      "jsonl", size=perf_size + roll_size,
                      last_write=max(perf_mtime, roll_mtime), managed=True))

    # Hardware history — minute/hour telemetry tiers behind the Vitals charts.
    hw_dir = os.path.join(logs_dir, "hw_history")
    size, files, mtime = _tree_stats(hw_dir)
    out.append(_store("hw_history", "Hardware history", hw_dir, "jsonl",
                      size=size, count=files or None, last_write=mtime,
                      managed=True))

    # Device events — one rotated stream per connector.
    dev_dir = os.path.join(logs_dir, "devices")
    size, _files, mtime = _tree_stats(dev_dir)
    streams = 0
    try:
        streams = sum(1 for n in os.listdir(dev_dir) if n.endswith(".jsonl"))
    except OSError:
        pass
    out.append(_store("devices", "Device events", dev_dir, "jsonl", size=size,
                      count=streams, last_write=mtime, managed=True))

    # Media — generated output and chat uploads (binary blobs).
    gen_dir = settings.media_dir()
    size, files, mtime = _tree_stats(gen_dir)
    out.append(_store("media_gen", "Generated media", gen_dir, "files",
                      size=size, count=files, last_write=mtime))
    up_dir = settings.upload_dir()
    size, files, mtime = _tree_stats(up_dir)
    out.append(_store("uploads", "Uploads", up_dir, "files", size=size,
                      count=files, last_write=mtime))

    # Secrets & keys — inventoried for transparency, never readable from here.
    n_secrets = 0
    for p in (os.path.join(data_dir, ".secret"),
              os.path.join(data_dir, ".internal_token"),
              os.path.join(data_dir, "auth_password")):
        if os.path.isfile(p):
            n_secrets += 1
    try:
        n_secrets += sum(1 for n in os.listdir(settings.secrets_dir())
                         if os.path.isfile(os.path.join(settings.secrets_dir(), n)))
    except OSError:
        pass
    out.append(_store("secrets", "Secrets & keys", settings.secrets_dir(),
                      "locked", count=n_secrets, locked=True))

    return {"home": str(settings.AVA_HOME),
            "total_bytes": sum(s["bytes"] for s in out),
            "retention_days": settings.data_retention_days(),
            "stores": out}


# --------------------------------------------------------------------------- #
# Chats — summaries with on-disk weight, plus per-chat export.
# Deletion stays on the existing DELETE /api/chats/{cid} (now audit-logged).
# --------------------------------------------------------------------------- #
@router.get("/chats")
def chats_summary():
    """Every conversation with its message count and JSON weight, newest first."""
    out = []
    with state.chats_lock:
        for c in state.chats.values():
            out.append({"id": c["id"], "title": c.get("title") or "New chat",
                        "created": c.get("created", 0), "updated": c.get("updated", 0),
                        "messages": len(c.get("messages") or []),
                        "bytes": len(json.dumps(c, ensure_ascii=False).encode("utf-8"))})
    out.sort(key=lambda x: x["updated"], reverse=True)
    return {"chats": out}


def _export_name(title: str, ext: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title or "chat").strip("-").lower()[:48] or "chat"
    return f"ava-chat-{slug}.{ext}"


def _chat_markdown(c: dict) -> str:
    lines = [f"# {c.get('title') or 'New chat'}", ""]
    for m in c.get("messages") or []:
        who = "You" if m.get("role") == "user" else "Ava"
        when = ""
        if m.get("ts"):
            when = time.strftime(" · %Y-%m-%d %H:%M", time.localtime(m["ts"]))
        lines.append(f"**{who}**{when}")
        lines.append("")
        if m.get("content"):
            lines.append(str(m["content"]))
        if m.get("image"):
            lines.append(f"![generated image]({m['image']})")
        for a in m.get("atts") or []:
            lines.append(f"- attachment: {a.get('filename', '?')}")
        lines.append("")
    return "\n".join(lines)


@router.get("/chats/{cid}/export")
def chat_export(cid: str, format: str = "json"):
    """One conversation as a download — JSON (verbatim) or Markdown (readable)."""
    with state.chats_lock:
        c = state.chats.get(cid)
        # Deep-copy inside the lock so rendering happens on a stable snapshot.
        c = json.loads(json.dumps(c, ensure_ascii=False, default=str)) if c else None
    if not c:
        return JSONResponse({"error": "unknown chat"}, status_code=404)
    title = c.get("title") or "New chat"
    if format == "md":
        return PlainTextResponse(
            _chat_markdown(c), media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{_export_name(title, "md")}"'})
    return JSONResponse(
        c, headers={"Content-Disposition": f'attachment; filename="{_export_name(title, "json")}"'})


# --------------------------------------------------------------------------- #
# Log tails — newest-first reads of the append-only stores. Names are a fixed
# whitelist; there is no arbitrary-path read here.
# --------------------------------------------------------------------------- #
def _tail_jsonl(path: str, n: int) -> list[dict]:
    """Last n parsed records of a JSONL file, newest first (bad lines skipped)."""
    rows: deque[dict] = deque(maxlen=n)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(rows)[::-1]


@router.get("/logs/{name}/tail")
def log_tail(name: str, n: int = 100, kind: str = ""):
    """Recent events from one log store: audit | performance | devices."""
    n = max(1, min(int(n), 500))
    if name == "audit":
        return {"events": audit.tail(n, kind=kind or None)}
    if name == "performance":
        perf_dir = os.environ.get("AVA_PERF_LOG_DIR", settings.logs_dir())
        rows = _tail_jsonl(os.path.join(perf_dir, "performance.jsonl"), n)
        if kind:
            rows = [r for r in rows if r.get("category") == kind]
        return {"events": rows}
    if name == "devices":
        # devices.recent already merges the per-connector JSONL files newest-first.
        return {"events": devices.recent(None, limit=n)}
    return JSONResponse({"error": "unknown log (audit|performance|devices)"},
                        status_code=404)
