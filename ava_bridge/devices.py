"""Device event ingest + store — the inbound "app → Ava" channel.

This is the one capability the Connector SDK was missing: a way for a user's own
device/sensor app to *proactively hand Ava an event* ("motion detected"), rather
than only being polled by Ava's agent. The device logic and the decision to notify
both stay in the user's app; Ava just receives, stores, and surfaces.

A connector that declares ``ingest: {enabled: true}`` may POST to
``/api/connectors/<id>/events`` authenticated with its per-connector ingest token
(see :func:`ava_bridge.internal.ingest_token`). Each accepted event is:

  * **normalised + appended** to ``${AVA_LOGS}/devices/<cid>.jsonl`` — a bounded,
    self-managing hot log using the same rotation pattern as ``perf_log.py`` (no
    external TSDB), so Ava can answer "did anything happen?" across restarts, and
  * **pushed to an in-process ring buffer** with a monotonic sequence so the ops
    SSE stream can surface it live (``event: device.event``) to the dashboard, and
  * for ``notify``/``warn``/``critical`` events, **raised as a short-lived alert**
    via :func:`ava_bridge.alerts.push_external` so it also lands in the alerts UI.

Read-only, single-user, in-process — consistent with the rest of the bridge.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List

try:
    import fcntl  # POSIX advisory locking for cross-process-safe appends
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from . import alerts, config

_DIR = os.path.join(config.LOGS_DIR, "devices")
MAX_BYTES = int(os.environ.get("AVA_DEVICES_LOG_MAX_BYTES", str(8 * 1024 * 1024)))
KEEP = max(1, int(os.environ.get("AVA_DEVICES_LOG_KEEP", "3")))

_SEVERITIES = ("info", "warn", "critical")
_NAME_MAX = 64
_MSG_MAX = 500

# Per-connector ingest rate limit (token bucket). A buggy or hostile device must
# not be able to flood the event log or the alert surface. Steady state allows
# ~_RATE_PER_MIN events/min with bursts up to _RATE_BURST; env-tunable, and
# AVA_DEVICES_RATE_PER_MIN=0 disables it.
_RATE_PER_MIN = int(os.environ.get("AVA_DEVICES_RATE_PER_MIN", "600"))
_RATE_BURST = max(1, int(os.environ.get("AVA_DEVICES_BURST", "60")))
_buckets: Dict[str, tuple] = {}   # cid -> (tokens, last_ts)
_bucket_lock = threading.Lock()

# In-memory ring of the most recent events across all connectors, each tagged with
# a process-monotonic sequence so an SSE generator can emit only what's new to it.
_lock = threading.Lock()
_recent: "deque[dict]" = deque(maxlen=500)
_seq = 0
_last_ts: Dict[str, float] = {}   # cid -> ts of its last event (for `ava device list`)


def _path(cid: str) -> str:
    return os.path.join(_DIR, f"{_clean_id(cid)}.jsonl")


def _clean_id(cid: str) -> str:
    """A filesystem-safe connector id (defensive — cids come from route params)."""
    return "".join(c for c in str(cid) if c.isalnum() or c in ("-", "_")) or "_"


def _rotate_if_needed(path: str) -> None:
    """Bounded, non-destructive rotation identical to perf_log's scheme."""
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = f"{path}.{KEEP}"
        if os.path.exists(oldest):
            os.remove(oldest)
    except OSError:
        pass
    for i in range(KEEP - 1, 0, -1):
        try:
            if os.path.exists(f"{path}.{i}"):
                os.replace(f"{path}.{i}", f"{path}.{i + 1}")
        except OSError:
            pass
    try:
        os.replace(path, f"{path}.1")
    except OSError:
        pass


def normalize(cid: str, payload: dict) -> dict:
    """Coerce a raw ingest payload into a compact, safe event record.

    Raises ValueError on a payload we won't accept (bad type / unusable name).
    Free-form strings are length-capped so a chatty or hostile app can't bloat the
    log or the notification surface.
    """
    if not isinstance(payload, dict):
        raise ValueError("event body must be a JSON object")
    etype = str(payload.get("type") or "event").strip().lower()
    if etype not in ("event", "reading"):
        raise ValueError("type must be 'event' or 'reading'")
    name = str(payload.get("name") or "").strip()[:_NAME_MAX]
    if not name:
        raise ValueError("name is required")

    rec: Dict[str, Any] = {
        "ts": round(float(payload.get("ts") or time.time()), 3),
        "cid": cid,
        "type": etype,
        "name": name,
    }
    if payload.get("value") is not None:
        try:
            rec["value"] = float(payload["value"])
        except (TypeError, ValueError):
            rec["value"] = str(payload["value"])[:_NAME_MAX]
    if payload.get("unit"):
        rec["unit"] = str(payload["unit"])[:16]
    if payload.get("message"):
        rec["message"] = str(payload["message"])[:_MSG_MAX]
    sev = str(payload.get("severity") or "").strip().lower()
    if sev in _SEVERITIES:
        rec["severity"] = sev
    if payload.get("notify"):
        rec["notify"] = True
    return rec


def allow(cid: str) -> bool:
    """Token-bucket rate limit per connector. True if this event is within budget,
    False if `cid` is pushing faster than the configured rate (caller returns 429)."""
    if _RATE_PER_MIN <= 0:
        return True
    rate = _RATE_PER_MIN / 60.0
    now = time.time()
    with _bucket_lock:
        tokens, last = _buckets.get(cid, (float(_RATE_BURST), now))
        tokens = min(float(_RATE_BURST), tokens + (now - last) * rate)
        if tokens < 1.0:
            _buckets[cid] = (tokens, now)
            return False
        _buckets[cid] = (tokens - 1.0, now)
        return True


def record_event(cid: str, payload: dict) -> dict:
    """Validate, persist, buffer, and (if flagged) alert on one pushed event.

    Returns the normalised record. Raises ValueError if the payload is rejected.
    """
    global _seq
    rec = normalize(cid, payload)

    # Persist (best-effort; a full disk must not 500 the app's push).
    try:
        os.makedirs(_DIR, exist_ok=True)
        path = _path(cid)
        _rotate_if_needed(path)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(path, "a", encoding="utf-8") as f:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            f.write(line)
            f.flush()
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — never fail the push on a storage hiccup
        pass

    # Buffer for the live SSE stream.
    with _lock:
        _seq += 1
        out = dict(rec, seq=_seq)
        _recent.append(out)
        _last_ts[cid] = rec["ts"]

    # Surface urgent/opt-in events in the dashboard alerts panel too. The event
    # already rides the device.event SSE frame for a toast; this additionally gives
    # it a place in the persistent active-alerts list (and future paging).
    if rec.get("notify") or rec.get("severity") in ("warn", "critical"):
        msg = rec.get("message") or f"{cid}: {rec['name']}"
        alerts.push_external(f"device:{cid}:{out['seq']}", msg,
                             severity=rec.get("severity", "warn"),
                             metric=f"{cid}.{rec['name']}", value=rec.get("value"))
    return out


def live_since(seq: int) -> tuple:
    """(new_seq, events) — buffered events with seq > `seq`, for the SSE producer."""
    with _lock:
        if _seq <= seq:
            return _seq, []
        return _seq, [e for e in _recent if e["seq"] > seq]


def current_seq() -> int:
    with _lock:
        return _seq


def recent(cid: str | None = None, limit: int = 50) -> List[dict]:
    """Most-recent persisted events (newest first), across all device connectors or
    one. Reads the JSONL so it survives restarts — used by the agent tool + CLI."""
    files: List[str]
    if cid:
        files = [_path(cid)]
    else:
        try:
            files = [os.path.join(_DIR, n) for n in os.listdir(_DIR)
                     if n.endswith(".jsonl")]
        except OSError:
            files = []
    rows: List[dict] = []
    for path in files:
        # The log is append-ordered (chronological); reverse the tail so it's
        # newest-first, then a stable sort by ts keeps that order for equal-ts ties.
        rows.extend(reversed(_tail_jsonl(path, limit)))
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:limit]


def last_event_ts(cid: str) -> float | None:
    with _lock:
        if cid in _last_ts:
            return _last_ts[cid]
    rows = _tail_jsonl(_path(cid), 1)
    return rows[-1]["ts"] if rows else None


def _tail_jsonl(path: str, limit: int) -> List[dict]:
    """Last `limit` parsed rows of an append-only JSONL log, in file order.
    (A falsy `limit` keeps its legacy meaning: every row in the file.)

    Reads BACKWARDS in bounded chunks rather than line-by-line from the top.
    This runs per device on every /api/devices refresh, and the log grows to
    MAX_BYTES (8MB by default) before rotation — parsing the whole file to hand
    back the newest handful cost ~119ms per device per request at the ceiling,
    all of it spent on rows the caller was about to throw away. Seeking to the
    tail reads only what the answer needs: one 64KB window in the common case,
    doubling (capped at the file size) for the rare log whose rows are so large
    or so corrupt that the window holds fewer than `limit` parsable rows.
    """
    chunk = 64 * 1024
    rows: List[dict] = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if not limit:
                chunk = max(size, 1)     # "give me everything": one full read
            while True:
                offset = max(0, size - chunk)
                f.seek(offset)
                lines = f.read(size - offset).split(b"\n")
                if offset > 0:
                    # The seek almost certainly landed mid-line, so the first
                    # split element is a line's TAIL, not a record. (Landing
                    # exactly on a boundary drops one whole line — harmless:
                    # it is older than everything kept, and if it were still
                    # needed the widening below re-reads it.)
                    del lines[0]
                rows = []
                # Walk from the END and stop at `limit`, so only rows actually
                # returned are ever JSON-decoded. Blank lines and unparsable
                # ones — a torn trailing line mid-append, a corrupt row — are
                # skipped exactly as the old forward reader skipped them.
                for raw in reversed(lines):
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rows.append(json.loads(raw))
                    except Exception:  # noqa: BLE001 — torn/corrupt line, skip
                        continue
                    if limit and len(rows) >= limit:
                        break
                if offset == 0 or (limit and len(rows) >= limit):
                    break
                chunk = min(chunk * 2, size)   # widen the window and retry
    except OSError:
        return []
    rows.reverse()                             # back to file (chronological) order
    return rows
