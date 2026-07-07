"""Append-only audit ledger — the flight recorder's durable substrate.

One JSONL file, `$AVA_HOME/logs/audit.jsonl`: every agent turn (tools used,
status, errors) and every self-edit event (parked / auto-applied / approved /
rejected) is appended as a single flat JSON line. Unlike the in-memory ops
views (state.turns, pruned hourly) and the 20-cycle Learning window, this file
is never truncated by the app — so "what did my agent do yesterday" stays
answerable across restarts.

Properties: append-only via O_APPEND (atomic line writes at this size on
POSIX), 0600 (contains prompts/diff paths), flock'd against concurrent
writers, best-effort (a ledger failure must never break serving). The agent's
own edit tools cannot touch it (access_policy denies logs/**).

Read side: `tail(n, kind=...)` for the API/CLI. Rotation is the operator's
business (logrotate); the app never deletes audit history.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time

_lock = threading.Lock()


def _path() -> str:
    from . import settings
    return os.path.join(settings.logs_dir(), "audit.jsonl")


def _opener(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def record(kind: str, **fields) -> None:
    """Append one event. Best-effort: never raises into the caller."""
    evt = {"ts": round(time.time(), 3), "kind": kind, **fields}
    try:
        line = json.dumps(evt, ensure_ascii=False, default=str)
        path = _path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8", opener=_opener) as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(line + "\n")
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except Exception:  # noqa: BLE001 — auditing must never take the app down
        pass


def tail(n: int = 200, kind: str | None = None) -> list[dict]:
    """Last n events (newest first), optionally filtered by kind."""
    path = _path()
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-max(n * 4, n):]  # over-read to survive filtering
        for line in reversed(lines):
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if kind and evt.get("kind") != kind:
                continue
            out.append(evt)
            if len(out) >= n:
                break
    except OSError:
        return []
    return out
