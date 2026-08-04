"""Per-chat conversation persistence, backed by SQLite (data/chats.db).

Each chat is an independent conversation with its own agent session-id, so Ava
keeps separate memory per chat.

WHY NOT chats.json. The previous store held the entire corpus in one dict and
rewrote the whole file on every append — chats_persist() ran from chat_new,
chat_append, delete and rename, serialising every conversation ever had in order
to record one new message. That is O(corpus) per message, on the hot path of
every single turn, and it only got slower the longer someone used Ava. It also
meant a crash mid-write could take the whole history with it, and that the
corpus had to be small enough to keep resident.

The schema stores role, content and ts as columns and EVERY other message field
in `extra` as JSON. That is deliberate: message shape has grown over time
(atts, model, tools_used, steps, error_code) and will grow
again. A column per feature makes each addition a migration; `extra` makes the
store lossless for fields it has never heard of, which is the property that
matters when the thing being preserved is someone's history.

Ordering is (ts, id). Two messages can share a timestamp, and `id` is monotonic
per insert, so append order survives a tie.

sqlite3 ships with Python and memory_store.py already opens a database
in-process, so this adds no dependency and follows a pattern the repo has.
"""
import json
import os
import sqlite3
import threading
import time
import uuid

from . import audit, config, settings, state  # state: attachments only, never chats

# One lock for read-modify-write sequences (append-and-retitle, rename). SQLite
# handles concurrent access itself; this serialises the compound operations that
# would otherwise interleave.
_LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chats(
  id      TEXT PRIMARY KEY,
  title   TEXT NOT NULL DEFAULT 'New chat',
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages(
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  role    TEXT NOT NULL,
  content TEXT NOT NULL DEFAULT '',
  ts      REAL NOT NULL,
  extra   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, ts, id);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

# Message keys that are columns; everything else round-trips through `extra`.
_COLUMNS = ("role", "content", "ts")


def db_path() -> str:
    return os.path.join(settings.data_dir(), "chats.db")


def _conn() -> sqlite3.Connection:
    """Open the store. Callers reach this through _db(), which migrates first."""
    path = db_path()
    fresh = not os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    if fresh:
        os.chmod(path, 0o600)  # chat history is as sensitive as it gets
    return con


def _msg(row: sqlite3.Row) -> dict:
    """Reassemble a stored row into the message dict callers expect."""
    out = {"role": row["role"], "content": row["content"], "ts": row["ts"]}
    try:
        extra = json.loads(row["extra"] or "{}")
    except ValueError:
        extra = {}
    if isinstance(extra, dict):
        out.update(extra)
    return out


def _chat(row: sqlite3.Row, messages: list[dict] | None = None) -> dict:
    return {"id": row["id"], "title": row["title"], "created": row["created"],
            "updated": row["updated"], "messages": messages if messages is not None else []}


def _messages_for(con: sqlite3.Connection, cid: str) -> list[dict]:
    return [_msg(r) for r in con.execute(
        "SELECT role, content, ts, extra FROM messages "
        "WHERE chat_id=? ORDER BY ts, id", (cid,))]


def chats_persist() -> None:
    """No-op. Kept because callers still name it, and naming it is now wrong.

    Under the JSON store this serialised the entire corpus, and every mutation
    called it. Writes are committed by the operation that makes them now, so
    there is nothing to flush. It stays as a no-op rather than being deleted so
    a stale caller degrades to doing nothing instead of raising — but nothing in
    this module calls it, and new code should not.
    """
    return None


def chat_session(cid: str) -> str:
    """Per-chat agent session-id so Ava's memory is isolated per conversation."""
    return f"{config.OC_SESSION}-{cid}"


def chat_new(title: str = "New chat") -> dict:
    cid = uuid.uuid4().hex[:12]
    now = time.time()
    with _LOCK, _db() as con:
        con.execute("INSERT INTO chats(id, title, created, updated) VALUES(?,?,?,?)",
                    (cid, title, now, now))
    return {"id": cid, "title": title, "created": now, "updated": now, "messages": []}


def chat_append(cid: str, role: str, content: str,
                 atts: list | None = None,
                 model: dict | None = None,
                 tools_used: list[str] | None = None,
                 steps: list | None = None,
                 error_code: str | None = None,
                 ts: float | None = None) -> None:
    """Append one message. `ts` defaults to now.

    An explicit `ts` is not a testing affordance — it is what lets history be
    written faithfully. Importing an existing corpus has to preserve when things
    were actually said; a store that stamps every message with the moment of the
    write turns a migration into a corpus that all happened at once.

    One INSERT plus one UPDATE, rather than rewriting every conversation.
    """
    when = time.time() if ts is None else float(ts)
    extra: dict = {}
    if atts:
        extra["atts"] = atts
    if error_code:
        # machine-readable ("web_search_off", "web_search_down") — the chat UI
        # derives the guided-fix link from the code pattern
        # (frontend/src/lib/fixes.ts)
        extra["error_code"] = error_code
    if model:
        extra["model"] = model
    if tools_used:
        extra["tools_used"] = [str(x) for x in tools_used if str(x).strip()]
    trimmed = _trim_steps(steps)
    if trimmed:
        extra["steps"] = trimmed  # durable chain-of-thought (survives reload)

    with _LOCK, _db() as con:
        row = con.execute("SELECT title FROM chats WHERE id=?", (cid,)).fetchone()
        if row is None:
            return
        con.execute(
            "INSERT INTO messages(chat_id, role, content, ts, extra) VALUES(?,?,?,?,?)",
            (cid, role, content or "", when,
             json.dumps(extra, ensure_ascii=False) if extra else "{}"))
        # Auto-title an untitled chat from its first user message.
        if (role == "user" and content.strip()
                and row["title"] in (None, "", "New chat")):
            con.execute("UPDATE chats SET updated=?, title=? WHERE id=?",
                        (when, content.strip()[:48], cid))
        else:
            con.execute("UPDATE chats SET updated=? WHERE id=?", (when, cid))


def get(cid: str) -> dict | None:
    """One conversation with its messages, or None."""
    with _db() as con:
        row = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        return _chat(row, _messages_for(con, cid)) if row else None


def snapshot(cid: str) -> dict | None:
    """A detached copy of one conversation, safe to render or serialise.

    Every read already returns freshly-built dicts, so this is `get` — the two
    names are kept apart because callers that render or serialise are asserting
    they need a stable picture, and that intent should survive a future change
    that makes reads cheaper by sharing objects.
    """
    return get(cid)


def delete(cid: str) -> dict | None:
    """Remove a conversation and its messages. Returns the removed chat, or None.

    Audited HERE rather than only in `chats_api`'s route, for the reason
    memory_store's delete functions carry: the route's record covers the route's
    callers and nothing else, so any future caller erases a conversation silently.
    The route's own `chat_delete` record stays — it says the owner asked; this one
    says how many messages actually left the database.
    """
    with _LOCK, _db() as con:
        row = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        if row is None:
            return None
        gone = _chat(row, _messages_for(con, cid))
        con.execute("DELETE FROM messages WHERE chat_id=?", (cid,))
        con.execute("DELETE FROM chats WHERE id=?", (cid,))
    audit.record("chat_delete", scope="store", id=cid,
                 title=(gone.get("title") or "")[:80],
                 messages=len(gone.get("messages") or []))
    return gone


def rename(cid: str, title: str) -> dict | None:
    """Retitle a conversation. Returns its summary, or None if unknown."""
    title = (title or "").strip()[:80]
    with _LOCK, _db() as con:
        row = con.execute("SELECT * FROM chats WHERE id=?", (cid,)).fetchone()
        if row is None:
            return None
        if title:
            con.execute("UPDATE chats SET title=? WHERE id=?", (title, cid))
        n = con.execute("SELECT COUNT(*) c FROM messages WHERE chat_id=?",
                        (cid,)).fetchone()["c"]
    return {"id": cid, "title": title or row["title"], "created": row["created"],
            "updated": row["updated"], "count": n}


def summaries() -> list[dict]:
    """Every conversation as a summary, newest first. One query, no message loads."""
    with _db() as con:
        return [{"id": r["id"], "title": r["title"] or "New chat",
                 "created": r["created"], "updated": r["updated"], "count": r["c"]}
                for r in con.execute(
                    "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) c "
                    "FROM chats c ORDER BY c.updated DESC")]


def counts() -> tuple[int, int]:
    """(conversations, total messages) — for the Data page's store inventory."""
    with _db() as con:
        return (con.execute("SELECT COUNT(*) n FROM chats").fetchone()["n"],
                con.execute("SELECT COUNT(*) n FROM messages").fetchone()["n"])


def usage() -> list[dict]:
    """Per-conversation size accounting for the Data page, newest first.

    `bytes` is the conversation's own JSON weight, which stays meaningful now
    that the corpus is no longer one file whose size could be divided up.
    """
    out = []
    with _db() as con:
        for r in con.execute("SELECT * FROM chats ORDER BY updated DESC"):
            msgs = _messages_for(con, r["id"])
            c = _chat(r, msgs)
            out.append({"id": r["id"], "title": r["title"] or "New chat",
                        "created": r["created"], "updated": r["updated"],
                        "messages": len(msgs),
                        "bytes": len(json.dumps(c, ensure_ascii=False).encode("utf-8"))})
    return out


def export_json(indent: int | None = 2) -> str:
    """The whole corpus as JSON text, in the shape chats.json always had.

    The export format is a promise to anyone who has downloaded a bundle before,
    so it is reconstructed rather than dumped — the storage changed, the archive
    someone keeps should not.
    """
    return json.dumps(_all_chats(), ensure_ascii=False, indent=indent, default=str)


def _all_chats() -> dict:
    with _db() as con:
        return {r["id"]: _chat(r, _messages_for(con, r["id"]))
                for r in con.execute("SELECT * FROM chats ORDER BY updated DESC")}


def reset() -> None:
    """Drop every conversation. Used by tests to start from a known corpus."""
    with _LOCK, _db() as con:
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM chats")


# --------------------------------------------------------------------------- #
# Storage-agnostic helpers, unchanged by the engine swap: they shape data the
# caller already has rather than fetching any.
# --------------------------------------------------------------------------- #
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
# Reads used on the turn hot path. Each is a query now — previously each walked
# the whole resident corpus.
# --------------------------------------------------------------------------- #
def history_for(cid: str, limit: int = 20) -> list[dict]:
    """The last `limit` messages of one conversation, oldest-first.

    Fetched newest-first with LIMIT and reversed, so a long conversation reads
    its tail instead of every message it has ever held.
    """
    if not cid:
        return []
    with _db() as con:
        rows = con.execute(
            "SELECT role, content, ts, extra FROM messages WHERE chat_id=? "
            "ORDER BY ts DESC, id DESC LIMIT ?", (cid, limit)).fetchall()
    return [_msg(r) for r in reversed(rows)]


def recent_messages(since_ts: float, limit: int = 200) -> list[dict]:
    """Messages across ALL conversations newer than `since_ts`, oldest-first.

    The learning distiller's input. One indexed query rather than a walk over
    every conversation, which is what made this the most expensive read in the
    old store.
    """
    with _db() as con:
        rows = con.execute(
            "SELECT chat_id, role, content, ts, extra FROM messages "
            "WHERE ts > ? ORDER BY ts, id LIMIT ?", (float(since_ts), limit)).fetchall()
    out = []
    for r in rows:
        m = _msg(r)
        m["chat_id"] = r["chat_id"]
        out.append(m)
    return out


# --------------------------------------------------------------------------- #
# One-time import from the old chats.json.
# --------------------------------------------------------------------------- #
def _import_legacy_json() -> int:
    """Load data/chats.json into the database, once, if there is anything to load.

    Runs at import time, guarded on a DURABLE MARKER in the meta table, not on
    the chats table being empty. Emptiness is the obvious condition and it is
    wrong: chats.json stays on disk as the rollback, so "no chats yet" is also
    true of an owner who has just deleted every conversation — and they would
    all come back on the next restart. A test written to assert that this does
    not happen is what caught it. The marker records that the import HAPPENED,
    which is the actual question.

    chats.json is deliberately left on disk untouched. It is the rollback: if
    anything about the new store is wrong, the previous release reads that file
    and finds the corpus exactly where it left it. Deleting it would make this a
    one-way door on someone's chat history for the sake of tidiness.

    Timestamps come from the source rows, not from now — see chat_append's `ts`.
    """
    path = getattr(config, "CHATS_FILE", "")
    if not path or not os.path.isfile(path):
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:  # noqa: BLE001 — a broken legacy file must not stop the bridge
        return 0
    if not isinstance(data, dict) or not data:
        return 0

    with _LOCK, _conn() as con:
        if con.execute("SELECT 1 FROM meta WHERE k='legacy_json_imported'").fetchone():
            return 0            # already migrated — never again, however empty
        if con.execute("SELECT 1 FROM chats LIMIT 1").fetchone() is not None:
            # A store already in use predates the marker; record and leave alone.
            con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('legacy_json_imported', ?)",
                        (str(time.time()),))
            return 0
        n = 0
        for cid, c in data.items():
            if not isinstance(c, dict):
                continue
            created = c.get("created") or 0.0
            updated = c.get("updated") or created
            con.execute(
                "INSERT OR REPLACE INTO chats(id, title, created, updated) VALUES(?,?,?,?)",
                (str(cid), c.get("title") or "New chat", float(created), float(updated)))
            for m in (c.get("messages") or []):
                if not isinstance(m, dict):
                    continue
                extra = {k: v for k, v in m.items() if k not in _COLUMNS}
                con.execute(
                    "INSERT INTO messages(chat_id, role, content, ts, extra) "
                    "VALUES(?,?,?,?,?)",
                    (str(cid), m.get("role") or "user", m.get("content") or "",
                     float(m.get("ts") or updated or 0.0),
                     json.dumps(extra, ensure_ascii=False) if extra else "{}"))
                n += 1
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('legacy_json_imported', ?)",
                    (str(time.time()),))
    return n


# Deliberately NOT run at import. _conn() -> settings.data_dir() would resolve
# AVA_HOME the moment this module is first imported, and settings FREEZES it on
# first access — so importing chat_store early would pin the home before an
# embedder (qa/conftest.py, `ava` subcommands, tests) has finished setting it.
# That is not theoretical: doing this at import turned the whole QA new-user
# journey into 401s, because the bridge and the auth gate ended up reading two
# different data dirs. The migration runs on FIRST USE instead.
_MIGRATION_DONE = False


def _ensure_migrated() -> None:
    global _MIGRATION_DONE
    if _MIGRATION_DONE:
        return
    _MIGRATION_DONE = True
    try:
        n = _import_legacy_json()
        if n:
            print(f"[ava-bridge] chat history migrated to SQLite: {n} messages "
                  f"(data/chats.json kept as a rollback)", flush=True)
    except Exception as e:  # noqa: BLE001 — never let a migration wedge the bridge
        print(f"[ava-bridge] chat history migration skipped: {e}", flush=True)


def _db() -> sqlite3.Connection:
    """Open the store, running the one-time legacy import on first use."""
    _ensure_migrated()
    return _conn()


def delete_all(*, reason: str = "") -> int:
    """Erase every conversation. Returns the chat count removed.

    The product-path entry point, distinct from `reset()` — which stays an
    unaudited test utility so a fixture does not spray records into the ledger.
    tests/test_destructive_paths_audited.py allowlists `reset()` on exactly that
    basis, and its comment says to remove the entry if a product path ever calls
    it; this function exists so that stays true.
    """
    with _LOCK, _db() as con:
        n = con.execute("SELECT count(*) FROM chats").fetchone()[0]
        con.execute("DELETE FROM messages")
        con.execute("DELETE FROM chats")
    if n:
        audit.record("chat_delete", scope="all", chats=int(n),
                     reason=reason or "owner emptied the store")
    return int(n)
