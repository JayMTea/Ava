#!/usr/bin/env python3
"""Unified generation-performance log for Ava.

Every generation Ava performs appends ONE structured JSON line to
``logs/performance.jsonl`` — the single source of truth for analysing her
throughput over time:

  * LLM completions  (category "llm")     — written by ava_router.py: tokens/sec,
    time-to-first-token, the exact sampling parameters (temperature/top_p/…), how
    the request was served (vLLM), which local brain answered (open-model) and
    whether a failover happened.
  * Image renders    (category "image")   — written by gpu_service.py: checkpoint,
    steps, cfg, sampler/scheduler, resolution, seed, refine_hi settings, steps/sec.
  * Upscales         (category "upscale") — written by gpu_service.py: model + time.

The log is append-only JSONL so it's trivial to analyse later (jq, pandas, or the
built-in ``python perf_log.py`` summary). Writes are best-effort and never raise
into the generation path; the two writer processes (the router and the bridge)
are serialised with an flock so concurrent lines never interleave.

Knobs (env — this file is stdlib-only and shipped per-app, so it takes NO import
from ava_bridge.settings; the packaged installer/compose sets these env vars,
mirrored in ava.yaml's `perf:` block for documentation):
  AVA_PERF_LOG=0            disable logging entirely
  AVA_PERF_LOG_DIR=<dir>    override the log directory (default: ./logs)
  AVA_PERF_LOG_MAX_BYTES=N  rotate the live log past this size (default 32MiB)
  AVA_PERF_LOG_KEEP=N       rotated segments to retain (.1 .. .N; default 5). The
                            live file is renamed down the sequence; only segments
                            past .N are dropped — and the bridge's perf_store rolls
                            those up BEFORE they age out, so history is never lost
                            silently the way a single overwritten .1 used to lose it.
  AVA_PERF_MAX_TOK_S=N      clamp implausible tokens/sec (default 2000) to None, so a
                            divide-by-near-zero (instant/cached completion) can't
                            poison the throughput stats + rollups.
"""
from __future__ import annotations

import json
import os
import socket
import time

try:
    import fcntl  # POSIX advisory locking for cross-process-safe appends
except ImportError:  # pragma: no cover — non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

_HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("AVA_PERF_LOG_DIR", os.path.join(_HERE, "logs"))
LOG_PATH = os.path.join(LOG_DIR, "performance.jsonl")
MAX_BYTES = int(os.environ.get("AVA_PERF_LOG_MAX_BYTES", str(32 * 1024 * 1024)))
KEEP = max(1, int(os.environ.get("AVA_PERF_LOG_KEEP", "5")))
MAX_TOK_S = float(os.environ.get("AVA_PERF_MAX_TOK_S", "2000"))
ENABLED = os.environ.get("AVA_PERF_LOG", "1").strip().lower() not in (
    "0", "false", "no", "off")
_HOST = socket.gethostname()


def _rotate_if_needed() -> None:
    """Rotate the live log into a bounded, NON-destructive sequence once it passes
    MAX_BYTES: performance.jsonl -> .1, .1 -> .2, ... only the segment past .KEEP
    is dropped. Unlike the old overwrite-.1 scheme this never silently discards a
    whole generation of history; the bridge's perf_store rolls each segment up
    before it ages past .KEEP."""
    try:
        if os.path.getsize(LOG_PATH) < MAX_BYTES:
            return
    except OSError:
        return
    # Drop the oldest, then shift each segment down one slot (highest first so we
    # never clobber an occupied slot), then the live file becomes .1.
    try:
        oldest = f"{LOG_PATH}.{KEEP}"
        if os.path.exists(oldest):
            os.remove(oldest)
    except OSError:
        pass
    for i in range(KEEP - 1, 0, -1):
        src = f"{LOG_PATH}.{i}"
        try:
            if os.path.exists(src):
                os.replace(src, f"{LOG_PATH}.{i + 1}")
        except OSError:
            pass
    try:
        os.replace(LOG_PATH, f"{LOG_PATH}.1")
    except OSError:
        pass


# Returns None when tokens or seconds is missing/zero, or when the rate is
# implausibly high (near-zero-second timing artefact) — never raises.
def tok_per_sec(tokens, seconds):
    """tokens / seconds, rounded — or None when either input is missing/zero, or the
    result exceeds AVA_PERF_MAX_TOK_S (a divide-by-near-zero artefact from an
    instant/cached completion, which would otherwise skew throughput stats)."""
    try:
        if tokens and seconds and seconds > 0:
            rate = round(tokens / seconds, 2)
            return rate if rate <= MAX_TOK_S else None
    except Exception:  # noqa: BLE001
        pass
    return None


def log_perf(category: str, **fields) -> None:
    """Append one generation-performance record. Best-effort; never raises.

    Always stamps ``ts`` (epoch), ``iso`` (local wall clock), ``host`` and
    ``category``; ``None`` valued fields are dropped so lines stay compact.
    """
    if not ENABLED:
        return
    try:
        rec = {
            "ts": round(time.time(), 3),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "host": _HOST,
            "category": category,
        }
        rec.update({k: v for k, v in fields.items() if v is not None})
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        os.makedirs(LOG_DIR, exist_ok=True)
        _rotate_if_needed()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
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
    except Exception:  # noqa: BLE001 — logging must never break a generation
        pass


# --------------------------------------------------------------------------- #
# Analysis helper — `python perf_log.py [N]` prints a rolling summary so you can
# eyeball throughput without pulling the JSONL into pandas.
# --------------------------------------------------------------------------- #
def _read(path: str = LOG_PATH) -> list:
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except OSError:
        pass
    return rows


def _stats(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    return {
        "n": n,
        "avg": round(sum(vals) / n, 2),
        "p50": round(vals[n // 2], 2),
        "min": round(vals[0], 2),
        "max": round(vals[-1], 2),
    }


def summarize(rows: list | None = None) -> dict:
    """Aggregate throughput by category / model for quick eyeballing."""
    rows = rows if rows is not None else _read()
    llm, img, up = {}, [], []
    for r in rows:
        cat = r.get("category")
        if cat == "llm":
            key = r.get("served_label") or r.get("served_model") or "?"
            llm.setdefault(key, []).append(r)
        elif cat == "image":
            img.append(r)
        elif cat == "upscale":
            up.append(r)
    out = {"total_records": len(rows), "llm": {}, "image": {}, "upscale": {}}
    for label, recs in llm.items():
        out["llm"][label] = {
            "count": len(recs),
            "tokens_per_sec": _stats([r.get("tokens_per_sec") for r in recs]),
            "ttft_ms": _stats([r.get("ttft_ms") for r in recs]),
            "completion_tokens": _stats([r.get("completion_tokens") for r in recs]),
            "failovers": sum(1 for r in recs if r.get("failover")),
        }
    if img:
        out["image"] = {
            "count": len(img),
            "render_seconds": _stats([r.get("render_seconds") for r in img]),
            "steps_per_sec": _stats([r.get("steps_per_sec") for r in img]),
        }
    if up:
        out["upscale"] = {
            "count": len(up),
            "seconds": _stats([r.get("seconds") for r in up]),
        }
    return out


if __name__ == "__main__":
    import sys

    tail = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0
    data = _read()
    if tail:
        data = data[-tail:]
    print(f"performance.jsonl  ->  {LOG_PATH}")
    print(json.dumps(summarize(data), indent=2))
