"""Cross-application generation-performance reader for Ava.

Ava and each app she drives write an append-only ``performance.jsonl`` in their
OWN repo (same schema everywhere): the Ava bridge/router (this repo) and any
connected app that writes one (image/video render times, backend tokens/sec).

This module gives Ava read-only access to ALL of them at once so she can answer
"how fast am I generating?" — tokens/sec by model, image steps/sec, render times,
failovers — without shelling into other projects. It never writes.
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

from . import config, connectors

# app-key -> the on-disk performance.jsonl each project appends to. Derived from
# the connector registry (each connector may declare a `perf.path`); the literal
# dict below is a safety fallback if no connectors are present.
# Core-only fallback (no app-specific entries): apps declare their own perf.path
# in connectors/<id>/connector.yaml and the registry provides them. A fresh fork
# therefore shows only Ava's own log, never the author's personal apps.
_FALLBACK_SOURCES: Dict[str, str] = {
    "ava": os.path.join(config.ROOT, "logs", "performance.jsonl"),
}
SOURCES: Dict[str, str] = connectors.perf_sources() or _FALLBACK_SOURCES


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
    out: Dict[str, Any] = {"records": len(rows)}
    if llm:
        out["llm"] = {
            label: {
                "count": len(recs),
                "tokens_per_sec": _stats([r.get("tokens_per_sec") for r in recs]),
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
    apps = [app] if app else list(SOURCES)
    bad = [a for a in apps if a not in SOURCES]
    if bad:
        return {"ok": False,
                "error": f"unknown app {bad[0]!r}; valid: {', '.join(SOURCES)}"}

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
    for a in apps:
        path = SOURCES[a]
        present[a] = os.path.isfile(path)
        for rec in _read_file(path):
            rec.setdefault("app", a)
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
