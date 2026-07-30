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

import io
import json
import os
import re
import sqlite3
import time
import zipfile
from collections import deque

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from . import audit, chat_store, devices, settings

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


# Human labels for the secrets inventory. Known filenames get an exact purpose;
# anything else falls back to a name-shape heuristic. Only NAMES are labelled —
# no value is ever read.
_SECRET_LABELS = {
    ".secret": "Session signing key",
    ".internal_token": "Internal API token",
    # NOT hashed — auth.set_password writes the value verbatim, 0600. Saying
# "hashed" in a security inventory overstated the protection.
    "auth_password": "Login password (stored 0600, not hashed)",
    "router_token": "Internal router token",
}


def _connector_for_env(var: str) -> str:
    """Which connector declares this env var as its credential, if any.

    Read from the manifests rather than guessed from the name: a var called
    `MYAPP_TOKEN` belongs to whichever connector declares it, and nothing about
    the string says which.
    """
    # `token_env` has more than one home: an MCP connector declares it under its
    # `mcp:` block, the connector template documents `auth.token_env`, and a plain
    # HTTP app puts it under `service:`. Searching just one of those is how the
    # first cut of this labelled four real credentials "no manifest claims it" — a
    # confident-sounding wrong answer, which is worse than the omission it
    # replaced. So walk the manifest for the key wherever it sits.
    def _finds(node) -> bool:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "token_env" and str(v or "").strip() == var:
                    return True
                if _finds(v):
                    return True
        elif isinstance(node, list):
            return any(_finds(x) for x in node)
        return False

    try:
        from . import connectors
        for m in connectors.catalog():
            if _finds(m):
                return str(m.get("label") or m.get("id") or "")
    except Exception:  # noqa: BLE001 — an inventory must not fail on a bad manifest
        pass
    return ""


def _secret_purpose(name: str) -> str:
    if name in _SECRET_LABELS:
        return _SECRET_LABELS[name]
    # `env/<VAR>` — a connector credential saved through the Hub.
    if name.startswith("env" + os.sep) or name.startswith("env/"):
        var = name.split("/")[-1].split(os.sep)[-1]
        who = _connector_for_env(var)
        return (f"Credential for {who} (env {var})" if who
                else f"Connector credential (env {var}) — no manifest claims it")
    low = name.lower()
    if "password" in low:
        return "Password (stored 0600, not hashed)"
    if "token" in low:
        return "Access token"
    if "key" in low or "api" in low or "secret" in low:
        return "Backend API key"
    return "Secret"


def _store(sid: str, label: str, path: str, fmt: str, *, size: int = 0,
           count: int | None = None, last_write: float | None = None,
           locked: bool = False, managed: bool = False, **extra) -> dict:
    # `deletable` + `delete_refused_reason` travel WITH each store so the UI never
    # renders a button the API will refuse, and the refusal's reason is one field
    # away rather than something the frontend has to restate (and drift from).
    refused = _REFUSED.get(sid)
    return {"id": sid, "label": label, "path": _rel(path), "format": fmt,
            "bytes": size, "count": count,
            "last_write": last_write or None, "locked": locked,
            "managed": managed, "deletable": refused is None,
            "delete_refused_reason": refused or "", **extra}


# --------------------------------------------------------------------------- #
# Deletion policy — which stores may be emptied, and why the others may not.
#
# The README markets this page's "audited deletes" and there was no DELETE
# endpoint for any store at all: emptying one meant `rm` by hand under AVA_HOME,
# which left no record and could brick the install. So deletion is offered — and
# three stores REFUSE, each for a reason specific to that store rather than a
# blanket "dangerous". A refusal that cannot say why is indistinguishable from an
# oversight.
_REFUSED = {
    "audit": (
        "The ledger is the record of every other deletion, including the ones this "
        "page performs — a ledger that prunes itself is not a ledger "
        "(ava_bridge/audit.py). Rotation is the operator's business: "
        "deploy/logrotate.conf ships the config, ~3.7 MB/year."),
    "secrets": (
        "Removing these locks you out and breaks the sandbox: they hold the login "
        "password (stored 0600, NOT hashed — see the inventory labels below), the "
        "session-signing key and the agent's internal token. Rotate individually "
        "instead — change the password in Setup → System, or clear one connector "
        "credential in Setup → Connectors."),
    "models": (
        "The voiceprint has its own destruction path (Setup → Voice), which also "
        "resets the derived gate threshold and evicts the copy cached in the "
        "running process — a plain delete here would leave the gate scoring "
        "against a print that no longer exists. It would also remove the 80 MB "
        "public ECAPA weights, which contain nothing about you. "
        "See docs/BIOMETRICS.md."),
}


def _rm_tree_files(root: str) -> tuple[int, int]:
    """Remove every FILE under `root`, keeping the directory. `(files, bytes)`.

    Files rather than the tree, because the directory is part of the install's
    layout and something is usually watching it — a store that vanishes reappears
    as a permission error at the next write instead of as an empty store.
    """
    n = b = 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                b += os.path.getsize(p)
                os.remove(p)
                n += 1
            except OSError:
                pass
    return n, b


def delete_store(sid: str) -> dict:
    """Empty one store. Returns a receipt; refuses with a reason where required.

    Audited by the function that performs the act (see
    tests/test_destructive_paths_audited.py) with NAMED targets — a record saying
    only "a store was emptied" cannot answer which one, or how much went.
    """
    if sid in _REFUSED:
        return {"ok": False, "refused": True, "store": sid,
                "error": _REFUSED[sid]}

    known = {s["id"] for s in stores()["stores"]}
    if sid not in known:
        return {"ok": False, "store": sid,
                "error": f"unknown store {sid!r} (have: {sorted(known)})"}

    receipt: dict = {"ok": True, "store": sid, "rows": 0, "files": 0, "bytes": 0}

    if sid == "memory":
        from . import memory_store
        receipt["rows"] = memory_store.delete_all(reason="Data page: empty store")
    elif sid == "chats":
        from . import chat_store
        receipt["rows"] = chat_store.delete_all(reason="Data page: empty store")
    else:
        roots = {
            # Same resolution stores() uses, so the delete and the
            # inventory cannot point at different directories.
            "performance": os.environ.get("AVA_PERF_LOG_DIR",
                                          settings.logs_dir()),
            "alloc": settings.logs_dir(),
            "hw_history": os.path.join(settings.logs_dir(), "hw_history"),
            "devices": os.path.join(settings.logs_dir(), "devices"),
            "media_gen": settings.media_dir(),
            "uploads": settings.upload_dir(),
        }
        root = roots.get(sid)
        if not root or not os.path.isdir(root):
            return {"ok": False, "store": sid,
                    "error": f"no directory on disk for {sid!r}"}
        if sid in ("performance", "alloc"):
            # These share logs_dir() with the audit ledger, so a blanket walk here
            # would delete the one store that must never be deleted. Name the
            # segments explicitly instead.
            stem = "performance" if sid == "performance" else "alloc"
            n = b = 0
            for fn in sorted(os.listdir(root)):
                if not (fn == f"{stem}.jsonl" or fn.startswith(f"{stem}.jsonl.")):
                    continue
                p = os.path.join(root, fn)
                try:
                    b += os.path.getsize(p)
                    os.remove(p)
                    n += 1
                except OSError:
                    pass
            if sid == "performance":
                r2, b2 = _rm_tree_files(os.path.join(root, "rollups"))
                n, b = n + r2, b + b2
            receipt["files"], receipt["bytes"] = n, b
        else:
            receipt["files"], receipt["bytes"] = _rm_tree_files(root)
        receipt["path"] = _rel(root)

    audit.record("data_delete", store=sid, rows=receipt["rows"],
                 files=receipt["files"], bytes=receipt["bytes"],
                 path=receipt.get("path", ""))
    return receipt


@router.delete("/stores/{sid}")
def delete_store_ep(sid: str):
    """Empty one store. Refuses `audit`, `secrets` and `models` with a reason."""
    res = delete_store(sid)
    if res.get("ok"):
        return res
    return JSONResponse(res, status_code=403 if res.get("refused") else 400)


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

    # Chats. The corpus is SQLite now, so this reports data/chats.db — reporting
    # chats.json would show a file that stops growing the moment the migration
    # runs, i.e. an inventory that looks healthy while describing a rollback
    # artefact rather than the live store.
    chats_path = chat_store.db_path()
    size, mtime = _file_stats(chats_path)
    n_chats, n_msgs = chat_store.counts()
    out.append(_store("chats", "Chats", chats_path, "sqlite", size=size,
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

    # Allocation decisions — the log an operator reads before allowing the
    # allocator to act. It grew forever and appeared on no inventory, so the one
    # file you are told to judge enforcement from was the one nobody could see
    # the size of. Self-rotating (broker._rotate_log_if_needed), like performance.
    alloc_size = alloc_mtime = 0
    for name in ["alloc.jsonl"] + [f"alloc.jsonl.{i}" for i in range(1, 6)]:
        b, m = _file_stats(os.path.join(logs_dir, name))
        alloc_size += b
        alloc_mtime = max(alloc_mtime, m)
    if alloc_size:
        alloc_path = os.path.join(logs_dir, "alloc.jsonl")
        out.append(_store("alloc", "Allocation decisions", alloc_path, "jsonl",
                          size=alloc_size,
                          count=_line_count(alloc_path, alloc_size),
                          last_write=alloc_mtime, managed=True))

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
    # managed=True now that prune_media() applies data.retention_days here —
    # the flag tells the Data page this store has automatic retention.
    out.append(_store("media_gen", "Generated media", gen_dir, "files",
                      size=size, count=files, last_write=mtime, managed=True))
    up_dir = settings.upload_dir()
    size, files, mtime = _tree_stats(up_dir)
    out.append(_store("uploads", "Uploads", up_dir, "files", size=size,
                      count=files, last_write=mtime, managed=True))

    # Secrets & keys — inventoried for transparency. We surface each secret's
    # NAME and purpose so the owner knows exactly what's held; the values are
    # never opened, displayed, or exported.
    secret_items: list[dict] = []
    for fname in (".secret", ".internal_token", "auth_password"):
        if os.path.isfile(os.path.join(data_dir, fname)):
            secret_items.append({"name": fname, "what": _secret_purpose(fname)})
    # RECURSIVE. `os.listdir` + `isfile` skipped `secrets/env/`, which is where
    # every connector credential lives (settings._env_secret_path). Measured on the
    # development box: four real connector tokens, invisible to the page the README calls
    # "the transparency page a cloud assistant can't give you", while the count
    # beside it was presented as complete.
    secret_bytes = 0
    try:
        sdir = settings.secrets_dir()
        for root, _dirs, names in os.walk(sdir):
            for n in sorted(names):
                fp = os.path.join(root, n)
                if not os.path.isfile(fp):
                    continue
                rel = os.path.relpath(fp, sdir)
                try:
                    secret_bytes += os.path.getsize(fp)
                except OSError:
                    pass
                secret_items.append({"name": rel, "what": _secret_purpose(rel)})
    except OSError:
        pass
    for fname in (".secret", ".internal_token", "auth_password"):
        fp = os.path.join(data_dir, fname)
        if os.path.isfile(fp):
            try:
                secret_bytes += os.path.getsize(fp)
            except OSError:
                pass
    # `bytes` was never passed, so this store reported 0 on a page that sums a
    # total. The SIZE of a secret is not the secret; the values stay unopened.
    out.append(_store("secrets", "Secrets & keys", settings.secrets_dir(),
                      "locked", size=secret_bytes, count=len(secret_items),
                      locked=True, items=secret_items))

    # The biometric. This page is documented as "every store Ava keeps" and the
    # README calls it "the transparency page a cloud assistant can't give you",
    # yet the one store holding GDPR Art. 9 special-category data — the owner's
    # voiceprint — was the single thing it did not list. Locked like `secrets`
    # (the bytes are never served), but present, sized and path-stamped, because
    # an inventory that omits the most sensitive item is not an inventory.
    mdir = settings.models_dir()
    import speaker as _spk
    vp_present = any(os.path.isfile(p) for p in _spk.voiceprint_paths())
    model_items = []
    if vp_present:
        model_items.append({"name": "voiceprint.npy",
                            "what": "your enrolled voiceprint (biometric). "
                                    "Delete it in Setup → Voice."})
    if os.path.isdir(os.path.join(mdir, "ecapa")):
        model_items.append({"name": "ecapa/",
                            "what": "public pretrained speaker-embedding weights "
                                    "— identical for every install, nothing about you"})
    m_bytes, m_files, m_last = _tree_stats(mdir)
    out.append(_store("models", "Models & voiceprint", mdir, "locked",
                      size=m_bytes, count=m_files, last_write=m_last,
                      locked=True, items=model_items,
                      biometric_enrolled=vp_present))

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
    return {"chats": chat_store.usage()}


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
    # Deep-copied inside the store's lock so rendering works from a stable
    # picture rather than a conversation another request may be appending to.
    c = chat_store.snapshot(cid)
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


# --------------------------------------------------------------------------- #
# Maintenance — memory.db health plus the everything-archive. Retention stays
# on POST /api/hub/system/retention (settings.save_patch, restart to apply).
# --------------------------------------------------------------------------- #
_INTEGRITY_KV = "data_page.integrity_last"


def _db_stats() -> dict:
    from . import memory_store
    path = memory_store.db_path()
    size, _mtime = _file_stats(path)
    reclaimable = 0
    try:
        con = sqlite3.connect(path, timeout=5)
        try:
            page = con.execute("PRAGMA page_size").fetchone()[0]
            free = con.execute("PRAGMA freelist_count").fetchone()[0]
            reclaimable = int(page) * int(free)
        finally:
            con.close()
    except sqlite3.Error:
        pass
    last = None
    raw = memory_store.kv_get(_INTEGRITY_KV)
    if raw:
        try:
            last = json.loads(raw)
        except ValueError:
            last = None
    return {"path": _rel(path), "bytes": size,
            "reclaimable": reclaimable, "last_check": last}


def prune_media(retention_days: int | None = None, *, dry_run: bool = False) -> dict:
    """Delete generated media and uploads older than `data.retention_days`.

    Media was the one store with no retention at all: `data.retention_days`
    reached the telemetry stores but never the blobs, and generated images are by
    far the largest thing Ava writes — the author's own media/out reached 7.9 GB
    in a month. Everything else was bounded, so the setting *looked* like it
    governed disk use while the biggest consumer grew forever.

    Mirrors hardware._prune_jsonl's contract: 0 (or None) means keep forever.
    Returns the counts either way so the UI can show the win before committing.
    """
    days = settings.data_retention_days() if retention_days is None else retention_days
    out = {"days": days, "removed": 0, "bytes": 0, "dry_run": dry_run}
    if not days or days <= 0:
        return out
    cutoff = time.time() - days * 86400
    for root in (settings.media_dir(), settings.upload_dir()):
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                    if st.st_mtime >= cutoff:
                        continue
                    out["removed"] += 1
                    out["bytes"] += st.st_size
                    if not dry_run:
                        os.unlink(p)
                except OSError:
                    # A file that vanished or is unreadable is not a failure of
                    # the sweep; skip it rather than aborting the whole prune.
                    continue
    return out


@router.get("/maintenance")
def maintenance():
    return {"db": _db_stats(),
            "retention": {"days": settings.data_retention_days(),
                          "choices": list(settings.DATA_RETENTION_CHOICES)},
            "media_reclaimable": prune_media(dry_run=True)}


@router.post("/maintenance/prune-media")
def maintenance_prune_media():
    """Apply `data.retention_days` to generated media + uploads."""
    res = prune_media()
    audit.record("data_maintenance", action="prune_media",
                 removed=res["removed"], bytes=res["bytes"], days=res["days"])
    return {"ok": True, **res}


@router.post("/maintenance/integrity")
def maintenance_integrity():
    """PRAGMA integrity_check on memory.db; the result is kept for the panel."""
    from . import memory_store
    path = memory_store.db_path()
    try:
        con = sqlite3.connect(path, timeout=30)
        try:
            rows = [str(r[0]) for r in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
    except sqlite3.Error as e:
        rows = [str(e)]
    ok = rows == ["ok"]
    result = {"ts": time.time(), "ok": ok,
              "detail": "ok" if ok else "; ".join(rows)[:500]}
    memory_store.kv_set(_INTEGRITY_KV, json.dumps(result))
    audit.record("data_maintenance", action="integrity_check", ok=ok)
    return {"ok": ok, "result": result, "db": _db_stats()}


@router.post("/maintenance/vacuum")
def maintenance_vacuum():
    """VACUUM memory.db — returns bytes before/after so the win is visible."""
    from . import memory_store
    path = memory_store.db_path()
    before, _m = _file_stats(path)
    try:
        # Plain autocommit connection: VACUUM cannot run inside a transaction.
        con = sqlite3.connect(path, timeout=30)
        try:
            con.execute("VACUUM")
        finally:
            con.close()
    except sqlite3.Error as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    after, _m = _file_stats(path)
    audit.record("data_maintenance", action="vacuum", before=before, after=after)
    return {"ok": True, "before": before, "after": after, "db": _db_stats()}


@router.get("/export")
def export_archive():
    """Everything readable, one .zip: memories, chats, the audit ledger, and
    ava.yaml. Secrets/keys are never included; media stays on disk (a full
    backup is a copy of $AVA_HOME)."""
    from . import memory_store
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("memory.json",
                   json.dumps(memory_store.export_all(), ensure_ascii=False, indent=2))
        z.writestr("chats.json", chat_store.export_json())
        for arc, path in (("audit.jsonl", os.path.join(settings.logs_dir(), "audit.jsonl")),
                          ("ava.yaml", str(settings.CONFIG_PATH))):
            if os.path.isfile(path):
                z.write(path, arc)
        z.writestr("manifest.json", json.dumps({
            "exported": time.time(),
            "contents": ["memory.json", "chats.json", "audit.jsonl", "ava.yaml"],
            "note": ("Secrets and keys are never exported. Media is not bundled — "
                     "for a full backup, copy the AVA_HOME folder."),
        }, indent=2))
    data = buf.getvalue()
    audit.record("data_export", target="archive", bytes=len(data))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="ava-export.zip"'})
