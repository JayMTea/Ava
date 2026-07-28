"""Cross-application generation-performance reader for Ava.

Ava and each app she drives write an append-only ``performance.jsonl`` in their
OWN repo (same schema everywhere): the Ava bridge/router (this repo) and any
connected app that writes one (image/video render times, backend tokens/sec).

This module gives Ava read-only access to ALL of them at once so she can answer
"how fast am I generating?" — tokens/sec by model, image steps/sec, render times,
failovers — without shelling into other projects. It never writes perf records;
its only write is the tiny source *ledger* below, which remembers every perf
source ever seen so an app's history stays readable after its connector is
removed (and resumes seamlessly when it's re-added).
"""
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import config, connectors

# Physically-impossible throughput ceiling. When gen_seconds rounds to ~0 (a
# 1-token or cached reply) the logged tokens_per_sec can be thousands-to-millions;
# left in, those outliers blow up avg/max so the summary reads e.g. "556k tok/s".
# Mirror perf_store's MAX_TOK_S clamp so the tool's numbers match the dashboard.
try:
    from . import settings as _settings
    MAX_TOK_S = float(_settings.get("perf.max_tok_s", 2000, env="AVA_PERF_MAX_TOK_S") or 2000)
except Exception:  # noqa: BLE001 — never let config break the reader
    MAX_TOK_S = 2000.0


def _sane_tps(recs: List[dict]) -> List[float]:
    """tokens_per_sec values with impossible outliers dropped (see MAX_TOK_S)."""
    out: List[float] = []
    for r in recs:
        v = r.get("tokens_per_sec")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= MAX_TOK_S:
            out.append(v)
    return out

# --------------------------------------------------------------------------- #
# Perf sources — LIVE registry view + persistent ledger.
#
# `sources()` is the single authority for "which performance.jsonl files exist,
# and which app does each belong to". It is computed per call (short TTL cache)
# from two inputs merged together:
#
#   1. the connector registry (`connectors.perf_sources()`) — so a connector
#      added/removed in the Hub changes Vitals immediately, no restart; and
#   2. an on-disk ledger of every source ever seen — so removing a connector
#      does NOT erase its history from Vitals, and re-adding it under the same
#      id resumes the same history (the rollup store keeps absorbing/pruning
#      remembered files too, so no window of records is ever silently lost).
#
# An app may have several files (its own repo log + the bridge-observed action
# log), hence Dict[app, List[path]]. Values are deduped by realpath in
# `source_files()` so one file shared by two entries can't double-count.
#
# Core-only fallback (no app-specific entries): apps declare their own perf.path
# in connectors/<id>/connector.yaml and the registry provides them. A fresh fork
# therefore shows only Ava's own log, never the author's personal apps.
# --------------------------------------------------------------------------- #
_FALLBACK_SOURCES: Dict[str, List[str]] = {
    # config.LOGS_DIR (== settings.logs_dir()), not config.ROOT/logs: the latter
    # is the CODE root, so on any install where AVA_HOME differs it pointed at a
    # directory perf_log.py never writes to.
    "ava": [os.path.join(config.LOGS_DIR, "performance.jsonl")],
}
# Tests (and debugging) may pin the map here; str values are normalised to [str].
SOURCES_OVERRIDE: Optional[Dict[str, Any]] = None
# Rebindable in tests, like perf_store's ROLLUP_DIR.
LEDGER_PATH: str = os.path.join(config.LOGS_DIR, "perf_sources.json")

_SRC_TTL_S = 15.0
_src_lock = threading.Lock()
_src_cache: Dict[str, Any] = {"ts": 0.0, "map": None}


def _normalize(m: Dict[str, Any]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for app, paths in (m or {}).items():
        if isinstance(paths, str):
            paths = [paths]
        clean = [str(p) for p in (paths or []) if p and isinstance(p, str)]
        if app and clean:
            out[str(app)] = clean
    return out


def _load_ledger() -> Dict[str, List[str]]:
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return _normalize((data or {}).get("apps") or {})
    except (OSError, ValueError, TypeError):
        # Missing or corrupt ledger must never break the readers — start fresh;
        # anything still in the connector registry re-registers on the next call.
        return {}


def _save_ledger(apps: Dict[str, List[str]]) -> None:
    try:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "apps": apps}, f, indent=1, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)  # atomic — a crash can't leave half a ledger
    except OSError:
        pass  # remembering is best-effort; the live registry still works


def refresh_sources() -> None:
    """Drop the TTL cache so the next `sources()` re-reads the registry at once.
    Called by the Hub on connector create/delete/toggle/manifest-save."""
    with _src_lock:
        _src_cache["map"] = None
        _src_cache["ts"] = 0.0


def register_source(app: str, path: str) -> None:
    """Remember an out-of-manifest perf source (e.g. the bridge-observed action
    log for a connector with no `perf:` block) so the readers pick it up."""
    if not app or not path:
        return
    with _src_lock:
        ledger = _load_ledger()
        paths = ledger.setdefault(app, [])
        if path in paths:
            return
        paths.append(path)
        _save_ledger(ledger)
        _src_cache["map"] = None  # next sources() sees it


def sources() -> Dict[str, List[str]]:
    """app-key -> [performance.jsonl paths]: live connector registry merged with
    every source remembered in the ledger (live paths first). Cheap (TTL cache)
    and thread-safe, so every reader can call it per request."""
    if SOURCES_OVERRIDE is not None:
        return _normalize(SOURCES_OVERRIDE)
    with _src_lock:
        now = time.time()
        if _src_cache["map"] is not None and now - _src_cache["ts"] < _SRC_TTL_S:
            return _src_cache["map"]
        live = _normalize(connectors.perf_sources())
        ledger = _load_ledger()
        merged: Dict[str, List[str]] = {}
        for app in list(live) + [a for a in ledger if a not in live]:
            seen: List[str] = []
            for p in live.get(app, []) + ledger.get(app, []):
                if p not in seen:
                    seen.append(p)
            merged[app] = seen
        # Persist anything new the registry introduced, so history survives a
        # later disconnect. The fallback is never persisted (it's not user data).
        if merged and merged != ledger:
            _save_ledger(merged)
        result = merged or dict(_FALLBACK_SOURCES)
        _src_cache.update(ts=now, map=result)
        return result


def source_files() -> List[Tuple[str, str]]:
    """Flat (app, path) pairs with duplicate files removed (first app wins), so
    a path shared between two app keys is read — and counted — exactly once."""
    out: List[Tuple[str, str]] = []
    seen: set = set()
    for app, paths in sources().items():
        for p in paths:
            key = os.path.realpath(p)
            if key in seen:
                continue
            seen.add(key)
            out.append((app, p))
    return out


def _read_file(path: str, max_rows: int = 5000) -> List[dict]:
    rows: List[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows[-max_rows:]


def _pct(sorted_nums: List[float], q: float) -> float:
    """Nearest-rank q-quantile (0..1) of an already-sorted list."""
    n = len(sorted_nums)
    return round(sorted_nums[min(n - 1, int(q * n))], 2)


def _stats(vals: List[Any]) -> Optional[dict]:
    """avg + a full percentile spread. Tail percentiles (p90/p95/p99) matter far
    more than the average for latency (TTFT) and throughput (tok/s): the mean
    hides the slow tail Ava's users actually feel. Exact (sorted in-memory over
    the retained window), no external dependency."""
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    nums.sort()
    n = len(nums)
    return {"n": n, "avg": round(sum(nums) / n, 2),
            "p50": _pct(nums, 0.5), "p90": _pct(nums, 0.9),
            "p95": _pct(nums, 0.95), "p99": _pct(nums, 0.99),
            "min": round(nums[0], 2), "max": round(nums[-1], 2)}


def _summarize(rows: List[dict]) -> dict:
    """Aggregate throughput by category and (for LLM) by model."""
    llm: Dict[str, List[dict]] = {}
    img: List[dict] = []
    vid: List[dict] = []
    up: List[dict] = []
    act: List[dict] = []
    for r in rows:
        cat = r.get("category")
        if cat == "llm":
            key = r.get("served_label") or r.get("served_model") or "?"
            llm.setdefault(key, []).append(r)
        elif cat == "image":
            img.append(r)
        elif cat == "video":
            vid.append(r)
        elif cat == "upscale":
            up.append(r)
        elif cat == "action":
            act.append(r)
    out: Dict[str, Any] = {"records": len(rows)}
    if llm:
        out["llm"] = {
            label: {
                "count": len(recs),
                "tokens_per_sec": _stats(_sane_tps(recs)),
                "ttft_ms": _stats([r.get("ttft_ms") for r in recs]),
                "completion_tokens": _stats([r.get("completion_tokens") for r in recs]),
                "failovers": sum(1 for r in recs if r.get("failover")),
            }
            for label, recs in llm.items()
        }
    if img:
        out["image"] = {"count": len(img),
                        "render_seconds": _stats([r.get("render_seconds") for r in img]),
                        "steps_per_sec": _stats([r.get("steps_per_sec") for r in img])}
    if vid:
        out["video"] = {"count": len(vid),
                        "render_seconds": _stats([r.get("render_seconds") for r in vid]),
                        "steps_per_sec": _stats([r.get("steps_per_sec") for r in vid])}
    if up:
        out["upscale"] = {"count": len(up),
                          "seconds": _stats([r.get("seconds") for r in up])}
    if act:
        # Bridge-observed connector-action calls (see app_perf.py). Kept under
        # `action_seconds` — NOT `seconds` — so API latency never masquerades as
        # GPU render time in the energy/cost estimators.
        out["actions"] = {"count": len(act),
                          "seconds": _stats([r.get("action_seconds") for r in act]),
                          "errors": sum(1 for r in act
                                        if isinstance(r.get("status"), int) and r["status"] >= 400)}
    return out


def read_performance(app: Optional[str] = None, category: Optional[str] = None,
                     since: Optional[str] = None, limit: int = 50,
                     summary: bool = True) -> Dict[str, Any]:
    """Read + summarise generation-performance records across apps.

    Args:
        app: restrict to one app's perf-source key, e.g. 'ava' (default: all).
        category: restrict to 'llm' | 'image' | 'video' | 'upscale' (default: all).
        since: window like '30m' | '6h' | '2d' (default: all time).
        limit: number of most-recent records to return verbatim (1-500).
        summary: include the aggregate throughput summary (default True).
    """
    srcs = sources()
    apps = [app] if app else list(srcs)
    bad = [a for a in apps if a not in srcs]
    if bad:
        return {"ok": False,
                "error": f"unknown app {bad[0]!r}; valid: {', '.join(sorted(srcs))}"}

    cutoff = None
    if since:
        mult = {"m": 60, "h": 3600, "d": 86400}.get(since[-1].lower())
        try:
            cutoff = time.time() - float(since[:-1]) * mult if mult else None
        except (ValueError, TypeError):
            cutoff = None

    limit = max(1, min(int(limit or 50), 500))
    rows: List[dict] = []
    present: Dict[str, bool] = {}
    seen_paths: set = set()
    for a in apps:
        present[a] = any(os.path.isfile(p) for p in srcs[a])
        for path in srcs[a]:
            rp = os.path.realpath(path)
            if rp in seen_paths:  # two app keys sharing a file: count it once
                continue
            seen_paths.add(rp)
            for rec in _read_file(path):
                rec.setdefault("app", a)
                # Row-level attribution wins over file ownership, so a shared
                # file's records stay with the app that actually wrote them.
                if app and rec.get("app") != app:
                    continue
                if category and rec.get("category") != category:
                    continue
                if cutoff is not None and float(rec.get("ts") or 0) < cutoff:
                    continue
                rows.append(rec)

    rows.sort(key=lambda r: r.get("ts") or 0)
    result: Dict[str, Any] = {
        "ok": True,
        "apps": apps,
        "sources_present": present,
        "total": len(rows),
        "recent": rows[-limit:],
    }
    if summary:
        result["summary"] = _summarize(rows)
    return result
