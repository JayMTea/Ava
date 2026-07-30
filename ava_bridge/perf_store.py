"""Bounded, self-managing cold storage for Ava's performance log — the productized
"Phase 5" that keeps month-range charts fast without an external TSDB.

The raw `performance.jsonl` (one line per generation, written by `perf_log.py`) is
the hot source of truth for the recent window. This module is the **owner of cold
storage**: it rolls raw records older than the hot window into compact hourly/daily
buckets, retains those cheaply, and prunes the raw segments it has absorbed — so the
dashboard reads a small hot file + pre-aggregated cold buckets and stays fast at any
zoom. It also runs the rollup **in-process** (a daemon thread, like the hardware
sampler), so it works identically on a bare host, in Docker, or on a Mac with zero
setup — no systemd timer to install.

Design notes that matter:
  * **Incremental watermark.** Each run finalizes only records in
    `[watermark, now-hot_window)` and *accumulates* them into existing buckets, then
    advances the watermark. This is why pruning raw is safe: a bucket already absorbed
    its raw, so deleting that raw can't lose history, and re-running can't double-count.
  * **Mergeable bucket form.** Buckets store *sums + counts + histograms*, never bare
    averages — so two buckets (or two time windows) combine by adding, and any range's
    p50/p90/p95 is derived by summing histograms. (Fixed-bucket histograms for their
    legitimate compose-across-windows property; nothing here speaks Prometheus.)
  * **Config-driven, under AVA_HOME.** Windows/retention resolve through
    `settings` (env -> ava.yaml -> default); the rollup dir lives under
    `settings.logs_dir()`.

Read-only helpers (`cold_series`, `cold_cost`) serve the cold range to
`dashboard.perf_series` / `perf_cost`, which stitch them onto the hot (raw) tail.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile as _tempfile
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from . import settings, perf_mgmt

# --------------------------------------------------------------------------- #
# Config (env -> ava.yaml `perf:` -> default). See config.example.yaml.
# --------------------------------------------------------------------------- #
def _dur(s, default: float) -> float:
    """Parse '48h'|'90d'|'1h'|'0' -> seconds; 0 means 'forever' for retention."""
    try:
        s = str(s).strip().lower()
        if s in ("0", "0s", "off", "none", ""):
            return 0.0
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(s[-1])
        return float(s[:-1]) * mult if mult else float(s)
    except (TypeError, ValueError):
        return default


HOT_WINDOW_S = _dur(settings.get("perf.hot_window", "48h", env="AVA_PERF_HOT_WINDOW"), 172800)
ROLLUP_INTERVAL_S = _dur(settings.get("perf.rollup_interval", "1h", env="AVA_PERF_ROLLUP_INTERVAL"), 3600)
# Retention is a single user-facing knob (Setup → System · data.retention_days,
# default ~6 months, 0 == forever). Both rollup tiers are pruned to it, so
# "nothing older than X" holds across the board. `perf.hourly_retention` /
# `perf.daily_retention` still override per-tier for advanced setups (an explicit
# value wins; unset falls back to the unified retention — note _dur("none")==0,
# so we only parse a value that was actually provided).
_RETENTION_S = settings.data_retention_s()


def _tier_retention(key: str, env: str) -> float:
    v = settings.get(key, None, env=env)
    return _dur(v, _RETENTION_S) if v is not None else _RETENTION_S


HOURLY_RETENTION_S = _tier_retention("perf.hourly_retention", "AVA_PERF_HOURLY_RETENTION")
DAILY_RETENTION_S = _tier_retention("perf.daily_retention", "AVA_PERF_DAILY_RETENTION")
# For the hourly-vs-daily read boundary, 0 (forever) means "hourly covers all".
_HOURLY_SPAN_S = HOURLY_RETENTION_S or 10 * 365 * 86400
KEEP = settings.get_int("perf.max_rotated_files", 5, env="AVA_PERF_LOG_KEEP")
MAX_TOK_S = float(_dur(settings.get("perf.max_tok_s", "2000", env="AVA_PERF_MAX_TOK_S"), 2000) or 2000)

ROLLUP_DIR = os.path.join(settings.logs_dir(), "rollups")
HOURLY_FILE = "perf_hourly.jsonl"
DAILY_FILE = "perf_daily.jsonl"
_WATERMARK = os.path.join(ROLLUP_DIR, ".watermark")

# Fixed histogram bounds (ascending; a trailing overflow bin is implied). Sized for a
# local vLLM box. Versioned implicitly by position — do not reorder without a rebuild.
BUCKETS: Dict[str, Sequence[float]] = {
    "tokens_per_sec": (5, 10, 15, 20, 30, 40, 50, 75, 100, 150, 200),
    "ttft_ms": (25, 50, 75, 100, 150, 200, 300, 500, 750, 1000, 2000, 5000),
}

_LOCK = threading.Lock()
_SCHED_STARTED = False


# --------------------------------------------------------------------------- #
# Histograms — compact, mergeable, JSON-safe ({"sum","count","c":[bins..,overflow]})
# --------------------------------------------------------------------------- #
def _hist(values, bounds: Sequence[float]) -> dict:
    c = [0] * (len(bounds) + 1)  # one bin per bound + a trailing overflow bin
    s = 0.0
    n = 0
    for v in values:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            continue
        fv = float(v)
        n += 1
        s += fv
        for i, b in enumerate(bounds):
            if fv <= b:
                c[i] += 1
                break
        else:
            c[-1] += 1
    return {"sum": round(s, 3), "count": n, "c": c}


def _hist_merge(a: Optional[dict], b: Optional[dict]) -> dict:
    if not a:
        return {"sum": b.get("sum", 0.0), "count": b.get("count", 0), "c": list(b.get("c", []))} if b else _hist([], ())
    if not b:
        return {"sum": a.get("sum", 0.0), "count": a.get("count", 0), "c": list(a.get("c", []))}
    ca, cb = a.get("c", []), b.get("c", [])
    n = max(len(ca), len(cb))
    c = [(ca[i] if i < len(ca) else 0) + (cb[i] if i < len(cb) else 0) for i in range(n)]
    return {"sum": round(a.get("sum", 0.0) + b.get("sum", 0.0), 3),
            "count": a.get("count", 0) + b.get("count", 0), "c": c}


def quantile(hist: Optional[dict], bounds: Sequence[float], q: float) -> Optional[float]:
    """q-quantile (0..1) from a compact histogram, linearly interpolated within the
    containing bin — the mergeable-histogram equivalent of PromQL histogram_quantile."""
    if not hist:
        return None
    n = hist.get("count") or 0
    c = hist.get("c") or []
    if n <= 0 or not c:
        return None
    rank = q * n
    cum = 0.0
    prev_le = 0.0
    for i, cnt in enumerate(c):
        le = bounds[i] if i < len(bounds) else float("inf")
        if cum + cnt >= rank:
            if le == float("inf"):
                return round(prev_le, 2) if prev_le else None
            if cnt == 0:
                return round(le, 2)
            frac = (rank - cum) / cnt
            return round(prev_le + (le - prev_le) * frac, 2)
        cum += cnt
        if le != float("inf"):
            prev_le = le
    return None


# --------------------------------------------------------------------------- #
# Raw reading (all segments, unbounded — rollup needs every line, not a tail)
# --------------------------------------------------------------------------- #
def _read_all(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def _segments(path: str) -> List[str]:
    """The live log plus its rotated segments (.1 .. .KEEP), newest first."""
    segs = [path]
    for i in range(1, KEEP + 1):
        p = f"{path}.{i}"
        if os.path.exists(p):
            segs.append(p)
    return segs


def _iter_finalized(lo: float, hi: float):
    """Raw records with lo <= ts < hi across every app source (+ rotated segments).
    Uses the live+remembered source list, so a removed connector's raw files keep
    being absorbed into rollups — its history is preserved, not stranded."""
    for app, path in perf_mgmt.source_files():
        for seg in _segments(path):
            for r in _read_all(seg):
                ts = float(r.get("ts") or 0)
                if ts <= 0 or ts < lo or ts >= hi:
                    continue
                r.setdefault("app", app)
                yield r


# --------------------------------------------------------------------------- #
# Aggregation into mergeable buckets
# --------------------------------------------------------------------------- #
def _cost_cfg() -> dict:
    try:
        import yaml
        from . import config as _cfg
        path = os.path.join(_cfg.ROOT, "config", "cost.yaml")
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 — cost is best-effort; rollups work without it
        return {"prices": {}}


def _price_for(model: str, prices: dict):
    ml = (model or "").lower()
    best, blen = None, -1
    for k, v in (prices or {}).items():
        if k.lower() in ml and len(k) > blen:
            best, blen = v, len(k)
    return best


def _empty_bucket(bts: int, app: str, cat: str, model: str, label: str) -> dict:
    return {"bucket_ts": bts, "app": app, "category": cat, "model": model, "label": label,
            "n": 0, "errors": 0, "failovers": 0, "cost_usd": 0.0, "energy_wh": 0.0,
            "completion_tokens_sum": 0, "render_sum": 0.0, "render_n": 0,
            "steps_sum": 0.0, "steps_n": 0,
            "tok_hist": _hist([], BUCKETS["tokens_per_sec"]),
            "ttft_hist": _hist([], BUCKETS["ttft_ms"])}


def _agg(rows: List[dict], key_ts) -> Dict[tuple, dict]:
    """Bucket rows by (bucket_ts, app, category, model) into mergeable accumulators.
    tokens_per_sec is clamped at read too, so pre-clamp garbage in historical raw
    can't poison a backfill's averages/histograms."""
    cfg = _cost_cfg()
    prices = cfg.get("prices", {})
    # A rollup is written long after the power samples covering it aged out of the
    # 2-hour ring buffer, so cold energy is nominal by construction. What changed is
    # WHOSE nominal: a flat 180 used to be baked in here, so every platform's
    # rollups carried a mid-range discrete NVIDIA card's draw. Now it is the owner's
    # declared figure, else this platform's, else None — and None means the bucket
    # records no energy at all rather than a fabricated one.
    nominal = _nominal_watts(cfg)
    buckets: Dict[tuple, dict] = {}
    for r in rows:
        ts = float(r.get("ts") or 0)
        if not ts:
            continue
        bts = key_ts(ts)
        app = r.get("app", "ava")
        cat = r.get("category", "?")
        model = r.get("served_model") or r.get("model") or "?"
        label = r.get("served_label") or r.get("model") or model
        k = (bts, app, cat, model)
        agg = buckets.get(k) or _empty_bucket(bts, app, cat, model, label)
        buckets[k] = agg
        agg["n"] += 1
        tps = r.get("tokens_per_sec")
        if isinstance(tps, (int, float)) and not isinstance(tps, bool) and tps <= MAX_TOK_S:
            agg["tok_hist"] = _hist_merge(agg["tok_hist"], _hist([tps], BUCKETS["tokens_per_sec"]))
        ttft = r.get("ttft_ms")
        if isinstance(ttft, (int, float)) and not isinstance(ttft, bool):
            agg["ttft_hist"] = _hist_merge(agg["ttft_hist"], _hist([ttft], BUCKETS["ttft_ms"]))
        if isinstance(r.get("render_seconds"), (int, float)):
            agg["render_sum"] += float(r["render_seconds"])
            agg["render_n"] += 1
        if isinstance(r.get("steps_per_sec"), (int, float)):
            agg["steps_sum"] += float(r["steps_per_sec"])
            agg["steps_n"] += 1
        if isinstance(r.get("completion_tokens"), (int, float)):
            agg["completion_tokens_sum"] += int(r["completion_tokens"])
        if isinstance(r.get("status"), int) and r["status"] >= 400:
            agg["errors"] += 1
        if r.get("failover"):
            agg["failovers"] += 1
        p = _price_for(model, prices)
        if p:
            pt = r.get("prompt_tokens") or 0
            ct = r.get("completion_tokens") or 0
            agg["cost_usd"] += pt / 1e6 * float(p.get("input", 0)) + ct / 1e6 * float(p.get("output", 0))
        secs = r.get("gen_seconds") or r.get("render_seconds") or r.get("seconds") or 0
        if secs and nominal:
            agg["energy_wh"] += nominal * float(secs) / 3600.0
    return buckets


def _merge_bucket(a: dict, b: dict) -> None:
    """Accumulate b into a in place (both mergeable form, same key)."""
    for f in ("n", "errors", "failovers", "completion_tokens_sum", "render_n", "steps_n"):
        a[f] = a.get(f, 0) + b.get(f, 0)
    for f in ("cost_usd", "energy_wh", "render_sum", "steps_sum"):
        a[f] = round(a.get(f, 0.0) + b.get(f, 0.0), 6)
    a["tok_hist"] = _hist_merge(a.get("tok_hist"), b.get("tok_hist"))
    a["ttft_hist"] = _hist_merge(a.get("ttft_hist"), b.get("ttft_hist"))
    a.setdefault("label", b.get("label"))


# --------------------------------------------------------------------------- #
# Rollup file IO (keyed dicts persisted as sorted JSONL, temp+replace)
# --------------------------------------------------------------------------- #
def _key(row: dict) -> tuple:
    return (row.get("bucket_ts"), row.get("app"), row.get("category"), row.get("model"))


def _load(name: str) -> Dict[tuple, dict]:
    out: Dict[tuple, dict] = {}
    for row in _read_all(os.path.join(ROLLUP_DIR, name)):
        out[_key(row)] = row
    return out


def _write(name: str, buckets: Dict[tuple, dict]) -> None:
    os.makedirs(ROLLUP_DIR, exist_ok=True)
    rows = sorted(buckets.values(),
                  key=lambda r: (r.get("bucket_ts") or 0, r.get("app") or "",
                                 r.get("category") or "", r.get("model") or ""))
    # mkstemp, not a fixed "<name>.tmp": two processes rolling up at once (the
    # in-process scheduler and ava_perf_rollup.py, which its own docstring
    # advertises as a cron/ops path) both wrote the SAME temp path, so one's
    # partial file could be os.replace'd into place as the other's result. Same
    # reasoning and same shape as settings._write_config. fsync before replace so
    # a crash leaves either the old file or the new one, never a truncated mix.
    fd, tmp = _tempfile.mkstemp(dir=ROLLUP_DIR, prefix="." + name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(ROLLUP_DIR, name))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _retain(buckets: Dict[tuple, dict], retention_s: float, now: float) -> Dict[tuple, dict]:
    if not retention_s:  # 0 == keep forever
        return buckets
    floor = now - retention_s
    return {k: v for k, v in buckets.items() if (v.get("bucket_ts") or 0) >= floor}


def _load_watermark() -> Optional[float]:
    try:
        with open(_WATERMARK, encoding="utf-8") as f:
            return float(json.load(f).get("ts"))
    except (OSError, ValueError, TypeError):
        return None


def _save_watermark(ts: float) -> None:
    os.makedirs(ROLLUP_DIR, exist_ok=True)
    tmp = _WATERMARK + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ts": ts}, f)
    os.replace(tmp, _WATERMARK)


def _prune_raw(cutoff: float) -> int:
    """Delete rotated segments (.1..N — never the live file) fully older than cutoff;
    they've been absorbed into rollups, so this bounds disk without losing history."""
    removed = 0
    for _app, path in perf_mgmt.source_files():
        for i in range(1, KEEP + 1):
            seg = f"{path}.{i}"
            if not os.path.exists(seg):
                continue
            maxts = max((float(r.get("ts") or 0) for r in _read_all(seg)), default=0.0)
            if maxts and maxts < cutoff:
                try:
                    os.remove(seg)
                    removed += 1
                except OSError:
                    pass
    return removed


# --------------------------------------------------------------------------- #
# Rollup entry point
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _rollup_guard():
    """Serialise a rollup against every other rollup, in this process and others.

    `_LOCK` alone was not enough. It is a threading.Lock, so it means nothing to a
    SECOND PROCESS — and a second process is a documented path:
    ava_perf_rollup.py's own docstring offers itself for "an initial backfill, a
    forced rebuild after a schema change, or a cron/ops one-off", and it calls
    run_rollup() directly.

    The sequence below is read-watermark, read-file, MERGE (which ADDS sums and
    counts), write-file, write-watermark. Two overlapping runs both read watermark
    W, both aggregate the same raw rows in [W, cutoff), and both add them into the
    same buckets — so tokens and cost are counted twice and the dashboard's spend
    figure is silently wrong. Because the watermark advances, the double count is
    permanent: nothing reprocesses that window to correct it.

    flock is the right primitive (not a pidfile) because the kernel releases it if
    the holder is killed, so an interrupted cron run cannot wedge the scheduler.
    Blocking rather than try-and-skip: a rollup is idempotent per window, cheap,
    and skipping one silently leaves raw segments unpruned.

    Degrades on platforms without fcntl (Windows): the in-process lock still
    applies, which is the same protection as before this existed.
    """
    with _LOCK:
        os.makedirs(ROLLUP_DIR, exist_ok=True)
        path = os.path.join(ROLLUP_DIR, ".rollup.lock")
        try:
            import fcntl
        except ImportError:
            yield
            return
        with open(path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def run_rollup(now: Optional[float] = None) -> dict:
    """Finalize newly-cold raw records into hourly + daily rollups, apply retention,
    and prune absorbed raw. Idempotent via the watermark.

    Safe to call concurrently from anywhere, including a second process — see
    _rollup_guard for why the in-process lock alone double-counted."""
    with _rollup_guard():
        now = now if now is not None else time.time()
        cutoff = now - HOT_WINDOW_S
        wm = _load_watermark()
        lo = wm if wm is not None else 0.0
        if lo >= cutoff:
            return {"ok": True, "processed": 0, "reason": "nothing newly cold"}

        rows = list(_iter_finalized(lo, cutoff))
        hourly_new = _agg(rows, lambda ts: int(ts // 3600 * 3600))
        daily_new = _agg(rows, lambda ts: int(ts // 86400 * 86400))

        for name, new, retention in (
            (HOURLY_FILE, hourly_new, HOURLY_RETENTION_S),
            (DAILY_FILE, daily_new, DAILY_RETENTION_S),
        ):
            existing = _load(name)
            for k, v in new.items():
                if k in existing:
                    _merge_bucket(existing[k], v)
                else:
                    existing[k] = v
            existing = _retain(existing, retention, now)
            _write(name, existing)

        _save_watermark(cutoff)
        pruned = _prune_raw(cutoff)
        return {"ok": True, "processed": len(rows), "cutoff": cutoff,
                "hourly_buckets": len(hourly_new), "daily_buckets": len(daily_new),
                "pruned_segments": pruned}


# --------------------------------------------------------------------------- #
# In-process scheduler (daemon thread; mirrors hardware.start_sampler)
# --------------------------------------------------------------------------- #
def _rollups_stale() -> bool:
    try:
        age = time.time() - os.path.getmtime(os.path.join(ROLLUP_DIR, HOURLY_FILE))
        return age >= ROLLUP_INTERVAL_S
    except OSError:
        return True  # missing => stale => build on boot


def _scheduler_loop() -> None:
    if _rollups_stale():
        try:
            run_rollup()
        except Exception:  # noqa: BLE001 — a rollup failure must never take the bridge down
            pass
    while True:
        time.sleep(max(60.0, ROLLUP_INTERVAL_S))
        try:
            run_rollup()
        except Exception:  # noqa: BLE001
            pass


def start_scheduler() -> None:
    """Start the in-process rollup scheduler once (idempotent). Portable: no systemd
    timer, runs the same on a host, in Docker, or on a Mac."""
    global _SCHED_STARTED
    if _SCHED_STARTED:
        return
    _SCHED_STARTED = True
    threading.Thread(target=_scheduler_loop, daemon=True, name="perf-rollup").start()


# --------------------------------------------------------------------------- #
# Cold readers — serve the pre-hot-window range to dashboard.perf_series/perf_cost
# --------------------------------------------------------------------------- #
def cold_boundary(now: Optional[float] = None) -> float:
    """The hot/cold split: records at/after this ts are served raw, before it cold."""
    return (now if now is not None else time.time()) - HOT_WINDOW_S


def _metric_sum_count(bucket: dict, metric: str) -> Tuple[float, int]:
    """(sum, count) for a metric from a mergeable bucket, so a group's weighted average
    is Σsum/Σcount. `tokens`/`completion_tokens` returns (sum, 1) — a pure total."""
    if metric == "tokens_per_sec":
        h = bucket.get("tok_hist") or {}
        return float(h.get("sum", 0.0)), int(h.get("count", 0))
    if metric == "ttft_ms":
        h = bucket.get("ttft_hist") or {}
        return float(h.get("sum", 0.0)), int(h.get("count", 0))
    if metric == "render_seconds":
        return float(bucket.get("render_sum", 0.0)), int(bucket.get("render_n", 0))
    if metric == "steps_per_sec":
        return float(bucket.get("steps_sum", 0.0)), int(bucket.get("steps_n", 0))
    if metric in ("tokens", "completion_tokens"):
        return float(bucket.get("completion_tokens_sum", 0)), 1
    return 0.0, 0


def cold_series(metric: str, step: int, since_ts: float, until_ts: float,
                app: Optional[str] = None, category: Optional[str] = None) -> dict:
    """Rollup-backed series for [since_ts, until_ts), re-bucketed to `step` and split
    by model label. Same {points, series} shape dashboard.perf_series emits for hot."""
    use_daily = step >= 86400 or since_ts < time.time() - _HOURLY_SPAN_S
    buckets = _load(DAILY_FILE if use_daily else HOURLY_FILE)
    is_sum = metric in ("tokens", "completion_tokens")
    # (rebucket_ts, label) -> [sum, count]
    acc: Dict[Tuple[int, str], List[float]] = {}
    for b in buckets.values():
        bts = b.get("bucket_ts") or 0
        if bts < since_ts or bts >= until_ts:
            continue
        if app and b.get("app") != app:
            continue
        if category and b.get("category") != category:
            continue
        s, c = _metric_sum_count(b, metric)
        if c <= 0 and not (is_sum and s):
            continue
        label = str(b.get("label") or b.get("model") or b.get("app") or "all")
        rk = (int(bts // step * step), label)
        slot = acc.setdefault(rk, [0.0, 0])
        slot[0] += s
        slot[1] += c
    by_t: Dict[int, Dict[str, float]] = {}
    for (t, label), (s, c) in acc.items():
        val = s if is_sum else (s / c if c else None)
        if val is None:
            continue
        by_t.setdefault(t, {})[label] = round(val, 2)
    points = [{"t": t, **vals} for t, vals in sorted(by_t.items())]
    series = sorted({lbl for vals in by_t.values() for lbl in vals})
    return {"points": points, "series": series}


def cold_cost(since_ts: float, until_ts: float, group: str = "model") -> dict:
    """Rollup-backed cost/energy for [since_ts, until_ts). Uses daily buckets (kept
    forever) so long ranges are cheap. Groups by model|app|category.

    **Every watt-hour returned here is nominal-estimated, never sampled.** `_agg`
    computes `energy_wh` as `nominal_gpu_watts * seconds`, because a rollup is
    written long after the GPU power samples that covered it have aged out of
    `hardware.history()`'s two-hour ring buffer. So the caller must treat all of
    `energy_wh` as an estimate — which is why it is returned separately rather
    than folded into a single total the caller can no longer characterize. This
    function used to return only the total, and `dashboard.perf_cost` therefore
    had nothing to combine and labelled a 7-day figure "measured" on the strength
    of the live buffer alone.
    """
    buckets = _load(DAILY_FILE)
    spend = energy_wh = 0.0
    by: Dict[str, dict] = {}
    for b in buckets.values():
        bts = b.get("bucket_ts") or 0
        if bts < since_ts or bts >= until_ts:
            continue
        cost = float(b.get("cost_usd", 0.0) or 0.0)
        e = float(b.get("energy_wh", 0.0) or 0.0)
        spend += cost
        energy_wh += e
        key = str(b.get(group) or b.get("model") or "?")
        g = by.setdefault(key, {"spend_usd": 0.0, "energy_wh": 0.0, "n": 0})
        g["spend_usd"] += cost
        g["energy_wh"] += e
        g["n"] += int(b.get("n", 0))
    # energy_estimated_wh == energy_wh by construction (see the docstring); it is
    # named separately so the caller carries provenance rather than inferring it.
    return {"spend_usd": spend, "energy_wh": energy_wh,
            "energy_estimated_wh": energy_wh, "by": by}


def _nominal_watts(cfg: dict) -> float | None:
    """Owner-declared wattage, else this platform's nominal, else None.

    None is a real answer: it means no defensible figure exists for this hardware,
    so the rollup records no energy rather than inventing some. Mirrors the same
    resolution in `dashboard.perf_cost`, deliberately duplicated rather than
    imported — `perf_store` is on the write path of a background thread and must
    not gain a dependency on the dashboard layer.
    """
    declared = cfg.get("nominal_gpu_watts")
    if declared not in (None, "", 0):
        try:
            return float(declared)
        except (TypeError, ValueError):
            pass
    try:
        from . import platforms
        w = platforms.power_profile().get("nominal_w")
        return float(w) if w else None
    except Exception:  # noqa: BLE001 — unreadable table means "no figure"
        return None
