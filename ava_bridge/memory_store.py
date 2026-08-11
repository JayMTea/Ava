"""Personal long-term memory — local, inspectable, governed.

One SQLite file at $AVA_HOME/data/memory.db (0600) holding two kinds of items:
  fact — durable notes about the owner (distilled by the learning cycle from
         chat history, or added by hand in the Hub Memory tab)
  doc  — chunks of uploaded documents, indexed at upload time so their content
         is searchable in later conversations, not just the one they were
         attached to

Retrieval is lexical (SQLite FTS5, porter stemming) — no embedding model, no
GPU contention with the Omni brain, nothing to download on a fresh install.
Recall is folded into the outgoing agent text (see augment_with_recall, called
from the chat routes in phone_bridge.py) and every recall that influenced a
turn is written to the audit ledger, so the Hub History tab shows exactly
which memories Ava consulted. The whole store is viewable, editable and
exportable via /api/hub/memory — memory the owner can read is the point.

Everything here is best-effort: memory must never break a chat turn, so all
public functions catch and degrade rather than raise.
"""
import json
import os
import re
import sqlite3
import threading
import time

from . import audit, config, settings

_LOCK = threading.Lock()

# Words too common to discriminate anything in a recall query.
_STOP = frozenset(
    "the and for you your are was were with what when where which this that "
    "have has had can could would should about from they them then than there "
    "here just like want need know how why who will its it's please tell me "
    "show does did not don't a an is in on of to do be my we i".split())

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id      INTEGER PRIMARY KEY,
  kind    TEXT NOT NULL,
  source  TEXT NOT NULL DEFAULT '',
  text    TEXT NOT NULL,
  created REAL NOT NULL,
  updated REAL NOT NULL,
  pinned  INTEGER NOT NULL DEFAULT 0,
  meta    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind, updated);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  text, content='items', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF text ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

#: The schema this build writes. Bump it and add a step to `_MIGRATIONS` in the
#: same commit.
#:
#: There was no version at all: `_SCHEMA` is `CREATE … IF NOT EXISTS` and it was
#: re-executed on EVERY connection, which quietly means "an older database is
#: whatever it already was". A column added in a future release would simply not
#: appear on an existing install, and every read of it would fail into one of
#: this module's swallowing `except` clauses — a store that reports empty rather
#: than broken. `PRAGMA user_version` is free, lives in the file header, and
#: makes the question answerable.
SCHEMA_VERSION = 1

#: `{target_version: (statement, …)}`, applied in order for any database below
#: it. Version 1 is the baseline `_SCHEMA` above, so there is nothing to do yet —
#: the ladder exists so the FIRST migration is a two-line change instead of an
#: archaeology exercise.
_MIGRATIONS: dict = {}

#: A single fact is a sentence or two. Nothing bounded it, so a paste — or a
#: distiller having a bad day — could store a megabyte and FTS-index every word
#: of it. Recall truncates at read time, so the cost was invisible: it lands in
#: the index and the file, not in the prompt.
TEXT_MAX = 8000


def db_path() -> str:
    return os.path.join(settings.data_dir(), "memory.db")


def _prepare(con: sqlite3.Connection) -> None:
    """Bring one connection, and the file behind it, up to spec.

    WAL is the load-bearing line. This store is opened by the bridge, by the
    data-maintenance routes (which build their own connections), and by any `ava`
    process — and the only mutex is a `threading.Lock`, which means nothing
    across processes. Under the default rollback journal a writer blocks every
    reader for the duration; the 5s timeout then expires and the error is
    swallowed into a neutral return, so a concurrent write showed up as "you have
    no memories" rather than as a failure. WAL lets readers proceed against the
    last committed state while a writer works.
    """
    con.execute("PRAGMA journal_mode=WAL")
    # Wait rather than fail: the callers all swallow errors into neutral values,
    # so a lock contention that raises reads as an empty store.
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")

    version = int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
    if version >= SCHEMA_VERSION:
        return
    con.executescript(_SCHEMA)
    for target in sorted(_MIGRATIONS):
        if version < target:
            for stmt in _MIGRATIONS[target]:
                con.execute(stmt)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()


def _conn() -> sqlite3.Connection:
    path = db_path()
    fresh = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    _prepare(con)
    if fresh:
        os.chmod(path, 0o600)  # chat-derived facts are as sensitive as chats.json
    return con


def _row_dict(r: sqlite3.Row) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["pinned"] = bool(d.get("pinned"))
    try:
        d["meta"] = json.loads(d.get("meta") or "{}")
    except ValueError:
        d["meta"] = {}
    return d


# ---- writes ---------------------------------------------------------------- #
def add(kind: str, text: str, source: str = "", meta: dict | None = None) -> int | None:
    """Insert one item; identical (kind, text) refreshes `updated` instead of
    duplicating. Returns the item id, or None on empty text / storage failure.

    Truncated at `TEXT_MAX` rather than refused: a fact that arrives too long is
    still a fact, and dropping it silently would lose it. Document chunks already
    arrive pre-cut at 1200 chars, so this only ever bites a hand-added paste or a
    distiller returning something unreasonable.
    """
    text = " ".join((text or "").split()).strip()[:TEXT_MAX]
    if not text or kind not in ("fact", "doc"):
        return None
    try:
        with _LOCK, _conn() as con:
            row = con.execute("SELECT id FROM items WHERE kind=? AND text=?",
                              (kind, text)).fetchone()
            now = time.time()
            if row:
                con.execute("UPDATE items SET updated=? WHERE id=?", (now, row["id"]))
                return int(row["id"])
            cur = con.execute(
                "INSERT INTO items(kind, source, text, created, updated, meta) "
                "VALUES (?,?,?,?,?,?)",
                (kind, source or "", text, now, now,
                 json.dumps(meta or {}, ensure_ascii=False)))
            return int(cur.lastrowid)
    except Exception:  # noqa: BLE001
        return None


def update_item(mid: int, text: str | None = None, pinned: bool | None = None) -> bool:
    """Edit a memory's text and/or pin state. True if a row actually changed.

    Both arguments are optional and independent: pass one to touch only that
    field. False means either the id does not exist or `text` was whitespace-only
    (a memory is never allowed to become empty), so the caller must not report
    "saved" on a False. Bumps `updated` on whatever it changes. Never raises.
    """
    try:
        with _LOCK, _conn() as con:
            if text is not None:
                text = " ".join(text.split()).strip()
                if not text:
                    return False
                con.execute("UPDATE items SET text=?, updated=? WHERE id=?",
                            (text, time.time(), mid))
            if pinned is not None:
                con.execute("UPDATE items SET pinned=?, updated=? WHERE id=?",
                            (1 if pinned else 0, time.time(), mid))
            return con.execute("SELECT changes()").fetchone()[0] > 0
    except Exception:  # noqa: BLE001
        return False


def _digest(text: str) -> str:
    """A short content hash, so a deletion record can prove WHAT was removed.

    The text itself is gone unrecoverably once deleted — which is the point of a
    delete — so the ledger cannot quote it. A digest lets the record say "an item
    whose content hashed to X existed and was destroyed", which is provable
    without retaining the thing destroyed and is not reversible into it.
    """
    import hashlib
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def delete_item(mid: int, *, reason: str = "") -> bool:
    """Delete one item, and record that it happened.

    **The audit call lives HERE, not in the route.** It used to live only in
    `hub/memory.py`'s handler, which meant any other caller of this function
    deleted owner memories with no ledger trace at all — and one already did (see
    `delete_source`, reached from `index_document`). `docs/MEMORY.md` promises that
    edits and deletions "are logged the same way", so the promise has to be kept
    by the layer that performs the act, not by one of its callers.
    """
    try:
        with _LOCK, _conn() as con:
            row = con.execute("SELECT kind, source, text FROM items WHERE id=?",
                              (mid,)).fetchone()
            con.execute("DELETE FROM items WHERE id=?", (mid,))
            gone = con.execute("SELECT changes()").fetchone()[0] > 0
    except Exception:  # noqa: BLE001
        return False
    if gone:
        audit.record("memory_delete", scope="item", id=mid, rows=1,
                     kind_deleted=(row["kind"] if row else None),
                     source=(row["source"] if row else None),
                     digest=_digest(row["text"] if row else ""),
                     reason=reason or "owner request")
    return gone


def delete_source(source: str, *, reason: str = "") -> int:
    """Remove every item from one source (e.g. before re-indexing an upload).

    Audited for a sharper reason than `delete_item`: this is a BULK delete, and
    `index_document` calls it automatically on every re-upload of the same
    filename. So the single most frequent path by which owner memories were
    destroyed left no trace whatsoever, and it was not even user-initiated.

    `reason` distinguishes a re-index from an erasure. Without it the ledger would
    show what looks like someone deleting their notes when they actually just
    re-uploaded a file — a true record that misleads is barely better than none.
    """
    try:
        with _LOCK, _conn() as con:
            digests = [_digest(r["text"]) for r in con.execute(
                "SELECT text FROM items WHERE source=?", (source,)).fetchall()]
            cur = con.execute("DELETE FROM items WHERE source=?", (source,))
            rows = cur.rowcount
    except Exception:  # noqa: BLE001
        return 0
    if rows:
        audit.record("memory_delete", scope="source", source=source, rows=rows,
                     digests=digests[:64], reason=reason or "owner request")
    return rows


# ---- document ingestion ----------------------------------------------------- #
def chunk_text(text: str, size: int = 1200) -> list[str]:
    """Split on paragraph boundaries into ~size-char chunks (hard-split any
    single paragraph longer than 2*size)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks: list[str] = []
    cur = ""
    for p in paras:
        while len(p) > size * 2:  # pathological wall of text
            chunks.append(p[:size])
            p = p[size:]
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def index_document(att_id: str, filename: str, text: str, max_chunks: int = 120) -> int:
    """Index an uploaded document's extracted text as searchable chunks.
    Re-uploading the same filename replaces its previous chunks."""
    if not config.MEMORY_ENABLED or not (text or "").strip():
        return 0
    source = f"upload:{filename}"
    # Named so the ledger reads as a re-index rather than as the owner erasing
    # their own notes — this is the automatic bulk delete that had no trace at all.
    delete_source(source, reason="reindex: document re-uploaded")
    n = 0
    for i, chunk in enumerate(chunk_text(text)[:max_chunks]):
        if add("doc", f"[{filename}] {chunk}", source=source,
               meta={"attachment": att_id, "chunk": i}) is not None:
            n += 1
    return n


# ---- reads ------------------------------------------------------------------ #
def _fts_query(q: str) -> str:
    """Turn free text into a safe FTS5 OR-query (raw text is not valid MATCH
    syntax). Empty when nothing usable remains."""
    toks = [t.lower() for t in re.findall(r"[A-Za-z0-9_]{3,}", q or "")]
    toks = [t for t in toks if t not in _STOP][:12]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(toks))


def search(query: str, k: int = 4, kind: str = "") -> list[dict]:
    """Best matches for free text: FTS5 bm25 rank, pinned items first."""
    fq = _fts_query(query)
    if not fq:
        return []
    sql = ("SELECT i.*, bm25(items_fts) AS rank FROM items_fts "
           "JOIN items i ON i.id = items_fts.rowid WHERE items_fts MATCH ?")
    args: list = [fq]
    if kind:
        sql += " AND i.kind=?"
        args.append(kind)
    sql += " ORDER BY i.pinned DESC, rank ASC LIMIT ?"
    args.append(max(1, int(k)))
    try:
        with _conn() as con:
            return [_row_dict(r) for r in con.execute(sql, args)]
    except Exception:  # noqa: BLE001
        return []


def list_items(kind: str = "", query: str = "", limit: int = 100, offset: int = 0) -> list[dict]:
    if query:
        return search(query, k=limit, kind=kind)
    sql, args = "SELECT * FROM items", []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    sql += " ORDER BY pinned DESC, updated DESC LIMIT ? OFFSET ?"
    args += [max(1, min(int(limit), 500)), max(0, int(offset))]
    try:
        with _conn() as con:
            return [_row_dict(r) for r in con.execute(sql, args)]
    except Exception:  # noqa: BLE001
        return []


def counts() -> dict:
    try:
        with _conn() as con:
            rows = con.execute("SELECT kind, COUNT(*) n FROM items GROUP BY kind")
            by = {r["kind"]: r["n"] for r in rows}
            pinned = con.execute(
                "SELECT COUNT(*) FROM items WHERE pinned=1").fetchone()[0]
        return {"facts": by.get("fact", 0), "doc_chunks": by.get("doc", 0),
                "pinned": pinned, "total": sum(by.values())}
    except Exception:  # noqa: BLE001
        return {"facts": 0, "doc_chunks": 0, "pinned": 0, "total": 0}


def size() -> dict:
    """What the store is costing, so growth is visible before it is a problem.

    There is deliberately no automatic retention here. `data.retention_days`
    prunes media and telemetry — things Ava produced — and applying it to MEMORY
    would delete facts the owner told her, on a timer, to save disk. That is a
    product decision and not one a durability fix gets to make quietly. What was
    missing is the ability to SEE the growth and to act on it deliberately, which
    is what this and `prune` are.
    """
    out = {"bytes": 0, "rows": 0, "oldest": None, "schema_version": 0}
    try:
        out["bytes"] = os.path.getsize(db_path())
    except OSError:
        pass
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT COUNT(*) n, MIN(created) oldest FROM items").fetchone()
            out["rows"] = int(row["n"] or 0)
            out["oldest"] = row["oldest"]
            out["schema_version"] = int(
                con.execute("PRAGMA user_version").fetchone()[0] or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def prune(*, older_than_days: float, kind: str = "", keep_pinned: bool = True,
          write: bool = False) -> int:
    """Remove items older than a cut-off. Dry-run unless `write=True`.

    Explicit, and never on a schedule — see `size()`. `keep_pinned` defaults to
    True because pinning is the owner saying "this one matters", and an age rule
    that overrides that would make pinning meaningless.

    Age is `updated`, not `created`: a fact re-confirmed last week is current
    however long ago Ava first learned it, and `add()` refreshes `updated` on a
    duplicate precisely so that stays true.
    """
    cutoff = time.time() - max(0.0, float(older_than_days)) * 86400
    where = ["updated < ?"]
    args: list = [cutoff]
    if kind in ("fact", "doc"):
        where.append("kind = ?")
        args.append(kind)
    if keep_pinned:
        where.append("pinned = 0")
    clause = " AND ".join(where)
    try:
        with _LOCK, _conn() as con:
            n = int(con.execute(
                f"SELECT COUNT(*) FROM items WHERE {clause}", args).fetchone()[0])
            if not write or not n:
                return n
            con.execute(f"DELETE FROM items WHERE {clause}", args)
    except Exception:  # noqa: BLE001
        return 0
    # Audited in the STORE, like every other destructive path here — a route that
    # records it instead is how index_document() came to bulk-delete memories
    # with no trace.
    # NOT `kind=` — `audit.record`'s own first parameter is called that.
    audit.record("memory_delete", rows=n, item_kind=kind or "all",
                 older_than_days=older_than_days, reason="owner prune")
    return n


def export_all() -> dict:
    """Whole store as plain JSON — the owner's data leaves in one click."""
    try:
        with _conn() as con:
            items = [_row_dict(r) for r in
                     con.execute("SELECT * FROM items ORDER BY id")]
    except Exception:  # noqa: BLE001
        items = []
    return {"exported": time.time(), "count": len(items), "items": items}


# ---- distiller bookkeeping (kv) --------------------------------------------- #
def kv_get(key: str, default: str = "") -> str:
    try:
        with _conn() as con:
            row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
            return row["v"] if row else default
    except Exception:  # noqa: BLE001
        return default


def kv_set(key: str, value: str) -> None:
    """Upsert one small scalar in the store's kv table. Never raises.

    Swallowing the error is deliberate: the only callers are cursors and
    bookkeeping (the distiller's high-water mark), and a failed write there must
    degrade to "reprocess that window next cycle" rather than abort the cycle. The
    consequence to know is that a silent failure is INDISTINGUISHABLE from a
    success — if you need to know the value landed, read it back.
    """
    try:
        with _LOCK, _conn() as con:
            con.execute("INSERT INTO kv(k, v) VALUES (?,?) "
                        "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))
    except Exception:  # noqa: BLE001
        pass


# ---- the chat-turn seam ------------------------------------------------------ #
def recall(user_text: str, k: int | None = None,
           max_chars: int | None = None) -> tuple[str, list[dict]]:
    """(formatted recall block, hits) for one user message; ("", []) if nothing
    relevant. Pure — no audit write — so it is unit-testable."""
    hits = search(user_text, k=k or config.MEMORY_RECALL_K)
    if not hits:
        return "", []
    budget = max_chars or config.MEMORY_RECALL_MAX_CHARS
    lines, used = [], []
    for h in hits:
        t = h["text"]
        if len(t) > 600:
            t = t[:600] + " …"
        line = f"- {t}" + (f"  ({h['source']})" if h["source"].startswith("upload:") else "")
        if sum(map(len, lines)) + len(line) > budget:
            break
        lines.append(line)
        used.append(h)
    if not lines:
        return "", []
    block = ("[Long-term memory — notes Ava saved earlier that look relevant. "
             "They may be stale; prefer the user's current message on conflict.]\n"
             + "\n".join(lines))
    return block, used


def augment_with_recall(agent_text: str, user_text: str, chat_id: str = "") -> str:
    """Fold relevant memories into the outgoing agent text and record the
    recall in the audit ledger. Never raises; returns agent_text unchanged
    when memory is disabled, empty, or unavailable."""
    if not config.MEMORY_ENABLED:
        return agent_text
    try:
        block, used = recall(user_text)
        if not block:
            return agent_text
        audit.record("memory_recall", chat_id=chat_id, count=len(used),
                     ids=[h["id"] for h in used],
                     sources=sorted({h["source"] for h in used if h["source"]}))
        return f"{agent_text}\n\n{block}"
    except Exception:  # noqa: BLE001
        return agent_text


def self_check() -> tuple[bool, str]:
    """For `ava verify`: prove the store opens and FTS5 round-trips."""
    try:
        with _conn() as con:
            con.execute("SELECT * FROM items_fts LIMIT 0")
        c = counts()
        return True, f"{c['facts']} facts · {c['doc_chunks']} doc chunks"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def delete_all(*, reason: str = "") -> int:
    """Erase EVERY memory item. Returns the row count removed.

    Separate from `delete_source`/`delete_item` because it is the whole-store
    erasure the Data page offers, and it records a count rather than per-item
    digests: hashing every row of a store the owner is deliberately emptying would
    put the entire memory corpus's fingerprints in the ledger, which is a privacy
    regression dressed as an audit improvement.
    """
    try:
        with _LOCK, _conn() as con:
            n = con.execute("SELECT count(*) FROM items").fetchone()[0]
            con.execute("DELETE FROM items")
    except Exception:  # noqa: BLE001
        return 0
    if n:
        audit.record("memory_delete", scope="all", rows=n,
                     reason=reason or "owner emptied the store")
    return int(n)
