"""The KPI ledger: append-only, daily grain, keyed by (day, metric, dim).

THE KEY IS NOT THE GROUPING. A row records which METRIC was observed, never
which domain it rolled up into. That one choice is what makes the taxonomy
cheap to change: re-cutting the axes, renaming a grouping or moving a surface
rewrites how history *groups* at read time, retroactively, with no migration and
no rewritten bytes. The only expensive rename is a metric id — which is why the
definitions log exists.

DELETING AN APP KEEPS ITS HISTORY. The ledger lives under the logs root, not
inside any connector's directory, and every row carries a `def` hash into
`definitions.jsonl`. So a series remains readable — and its units and meaning
remain recoverable — after the app that produced it is gone.

WHY JSONL. The series is one row per metric per day: a few thousand rows a year.
A database would be a schema to migrate, a lock to contend and a file to
corrupt, in exchange for query features nothing here needs. Append-only text is
also the format most likely to still be readable in ten years, which is the
actual design horizon for a record like this.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from . import settings

_LOCK = threading.Lock()

LEDGER = "ledger.jsonl"
DEFINITIONS = "definitions.jsonl"
HEARTBEAT = "heartbeat.jsonl"

# Fields that change what a number MEANS. A change to any of them mints a new
# definition hash, so a chart can show where the meaning shifted instead of
# silently splicing two different measurements into one line.
_DEF_FIELDS = ("unit", "agg", "source", "read", "scale", "min_n", "num", "den",
               "declares", "decode", "definition")


def kpi_dir() -> str:
    return os.path.join(settings.logs_dir(), "kpi")


def _path(name: str) -> str:
    return os.path.join(kpi_dir(), name)


def _append(name: str, row: dict) -> None:
    d = kpi_dir()
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")


def _read(name: str) -> list[dict]:
    try:
        with open(_path(name), encoding="utf-8") as fh:
            out = []
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue        # a torn line must not lose the whole file
            return out
    except FileNotFoundError:
        return []


def definition_hash(metric: dict) -> str:
    blob = json.dumps({k: metric.get(k) for k in _DEF_FIELDS},
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def ensure_definition(metric: dict) -> str:
    """Record this metric's definition if it is new; return its hash."""
    h = definition_hash(metric)
    with _LOCK:
        known = {r.get("def") for r in _read(DEFINITIONS)}
        if h not in known:
            _append(DEFINITIONS, {
                "def": h, "metric": metric.get("metric_id"),
                "recorded_at": round(time.time(), 3),
                **{k: metric.get(k) for k in _DEF_FIELDS if metric.get(k) is not None}})
    return h


def write(day: str, obs: dict, metric: dict, *, dim: str | None = None,
          src: str = "") -> dict:
    """Append one observation. Idempotent per (day, metric, dim): re-running a
    day replaces nothing — the newest row for a key wins on read, and the older
    one stays as evidence that the value was revised."""
    row = {"day": day, "metric": obs.get("metric"), "dim": dim,
           "value": obs.get("value"), "unit": obs.get("unit"),
           "state": obs.get("state"), "provenance": obs.get("provenance"),
           "n": obs.get("n"), "lo": obs.get("lo"), "hi": obs.get("hi"),
           "def": ensure_definition(metric), "src": src,
           "observed_at": round(time.time(), 3)}
    if obs.get("why"):
        row["why"] = obs["why"]
    with _LOCK:
        _append(LEDGER, row)
    return row


def heartbeat(ran: int, ok: int, errors: list | None = None) -> None:
    """One row per collector run, whether or not it achieved anything.

    This is what separates "nothing happened that day" from "the collector was
    not running that day" — a distinction the ledger alone cannot make, because
    a day with no observations looks identical either way.
    """
    with _LOCK:
        _append(HEARTBEAT, {"at": round(time.time(), 3),
                            "day": today(), "metrics": ran, "ok": ok,
                            "errors": (errors or [])[:20]})


def today(tz_offset_h: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=tz_offset_h)).date().isoformat()


def _days(since_days: int, end: str | None = None) -> list[str]:
    last = date.fromisoformat(end) if end else datetime.now(timezone.utc).date()
    return [(last - timedelta(days=i)).isoformat()
            for i in range(max(1, since_days) - 1, -1, -1)]


def series(metric_id: str, since_days: int = 30, dim: str | None = None,
           end: str | None = None) -> list[dict]:
    """A DENSE day axis: one slot per day in the window, whether or not anything
    was recorded. A gap must render as a gap — compressing the axis to the days
    that happen to have rows turns three missing weeks into an unbroken line."""
    latest: dict[str, dict] = {}
    for r in _read(LEDGER):
        if r.get("metric") != metric_id or r.get("dim") != dim:
            continue
        d = r.get("day")
        if not d or (d in latest and r.get("observed_at", 0) < latest[d].get("observed_at", 0)):
            continue
        latest[d] = r
    out = []
    for d in _days(since_days, end):
        r = latest.get(d)
        out.append(r if r else {"day": d, "metric": metric_id, "dim": dim,
                                "value": None, "state": "unavailable",
                                "provenance": None, "n": None,
                                "why": "no collector run recorded for this day"})
    return out


def latest(metric_id: str, dim: str | None = None) -> dict | None:
    best = None
    for r in _read(LEDGER):
        if r.get("metric") != metric_id or r.get("dim") != dim:
            continue
        if best is None or (r.get("day"), r.get("observed_at", 0)) > (best.get("day"), best.get("observed_at", 0)):
            best = r
    return best


def rows_for(metric_id: str, day: str | None = None) -> list[dict]:
    """Every stored row for one metric, optionally for one day.

    Public because a dimensioned metric has no single value: its reader needs
    the per-dimension rows, and reaching into this module's private tail-reader
    to get them would fork a contract across two files.
    """
    return [r for r in _read(LEDGER)
            if r.get("metric") == metric_id and (day is None or r.get("day") == day)]


def coverage(since_days: int = 30) -> dict:
    """Days the collector actually ran, over days it should have.

    Reported separately from metric coverage because they fail differently: a
    dead scheduler and a dead app produce the same empty chart, and only this
    tells them apart.
    """
    want = set(_days(since_days))
    ran = {r.get("day") for r in _read(HEARTBEAT) if r.get("day") in want}
    return {"days_expected": len(want), "days_collected": len(ran),
            "missing_days": sorted(want - ran)}


def stats() -> dict:
    """Facts for the store inventory — never contents."""
    out = {"rows": 0, "bytes": 0, "metrics": 0, "first_day": None,
           "last_day": None, "last_write": None}
    rows = _read(LEDGER)
    out["rows"] = len(rows)
    if rows:
        days = sorted(r.get("day") for r in rows if r.get("day"))
        out["first_day"], out["last_day"] = (days[0], days[-1]) if days else (None, None)
        out["metrics"] = len({r.get("metric") for r in rows})
        out["last_write"] = max((r.get("observed_at") or 0) for r in rows) or None
    for name in (LEDGER, DEFINITIONS, HEARTBEAT):
        try:
            out["bytes"] += os.path.getsize(_path(name))
        except OSError:
            pass
    return out
