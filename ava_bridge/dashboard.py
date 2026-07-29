"""Dashboard aggregation layer for the Ava Command Center (Vitals + Operations).

Read-first, cookie-gated `/api/*` data assembled from existing modules
(`perf_mgmt`, `state`, `hardware`, `alerts`) + systemd, so the browser can render
charts/tables/live-feeds without the sandbox internal token. Nothing here writes.

Sections: perf (summary/series/recent/cost) · work (jobs/turns/code) ·
ops (summary/schedule/services/tools/alerts) · SSE snapshot builder.
"""
import os
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests

from . import config, state, perf_mgmt, hardware, alerts, connectors, perf_store

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None


# --------------------------------------------------------------------------- #
# small TTL cache so SSE (1 Hz) can't spam systemctl / probes / file reads
# --------------------------------------------------------------------------- #
_cache: Dict[str, tuple] = {}


def _cached(key: str, ttl: float, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val


def _parse_since(since: Optional[str]) -> Optional[float]:
    if not since:
        return None
    try:
        mult = {"m": 60, "h": 3600, "d": 86400}.get(str(since)[-1].lower())
        return time.time() - float(str(since)[:-1]) * mult if mult else None
    except Exception:  # noqa: BLE001
        return None


def _parse_dur(s: Optional[str], default: float) -> float:
    try:
        mult = {"m": 60, "h": 3600, "d": 86400}.get(str(s)[-1].lower())
        return float(str(s)[:-1]) * mult if mult else default
    except Exception:  # noqa: BLE001
        return default


def _all_rows(app=None, category=None, since=None) -> List[dict]:
    """Raw perf records across apps, filtered + time-sorted. Sources come from
    perf_mgmt.sources() — the LIVE connector registry + remembered ledger — so a
    connector added or removed in the Hub changes these reads immediately, and a
    removed app's history stays visible."""
    cutoff = _parse_since(since)
    rows: List[dict] = []
    for a, path in perf_mgmt.source_files():
        if app and a != app:
            continue
        for r in perf_mgmt._read_file(path):
            r.setdefault("app", a)
            if app and r.get("app") != app:  # shared files: rows keep their writer
                continue
            if category and r.get("category") != category:
                continue
            if cutoff is not None and float(r.get("ts") or 0) < cutoff:
                continue
            rows.append(r)
    rows.sort(key=lambda r: r.get("ts") or 0)
    return rows


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
def perf_summary(app=None, category=None, since=None) -> dict:
    return perf_mgmt.read_performance(app=app, category=category, since=since,
                                      limit=1, summary=True)


def perf_series(metric="tokens_per_sec", bucket="1h", since="24h",
                app=None, category=None) -> dict:
    """Time-bucketed series for charts, split by model label. Stitched: the recent
    (hot) tail comes from raw JSONL; anything older than the hot window comes from
    pre-aggregated rollups (perf_store), so month-range charts stay fast. A single
    boundary bucket may reflect only its hot portion (hot wins on key collision) —
    negligible for a trend line."""
    now = time.time()
    step = int(_parse_dur(bucket, 3600))
    since_ts = now - _parse_dur(since, 86400)
    cutoff = perf_store.cold_boundary(now)
    is_sum = metric in ("tokens", "completion_tokens")
    raw_field = "completion_tokens" if is_sum else metric

    # Cold range [since_ts, cutoff) from rollups (skipped when the window is all-hot).
    by_t: Dict[int, Dict[str, float]] = {}
    if since_ts < cutoff:
        cold = perf_store.cold_series(metric, step, since_ts, cutoff, app, category)
        for pt in cold["points"]:
            t = pt["t"]
            by_t[t] = {k: v for k, v in pt.items() if k != "t"}

    # Hot tail [max(cutoff, since_ts), now) from raw — grouped like the cold side.
    hot_lo = max(cutoff, since_ts)
    hot: Dict[int, Dict[str, list]] = {}
    for r in _all_rows(app, category, None):
        ts = r.get("ts") or 0
        if ts < hot_lo:
            continue
        val = r.get(raw_field)
        if not isinstance(val, (int, float)):
            continue
        bts = int(ts // step * step)
        skey = str(r.get("served_label") or r.get("model") or r.get("app") or "all")
        hot.setdefault(bts, {}).setdefault(skey, []).append(val)
    for bts, labels in hot.items():
        dst = by_t.setdefault(bts, {})
        for skey, vals in labels.items():  # hot wins on collision (fresher, raw)
            dst[skey] = round(sum(vals) if is_sum else sum(vals) / len(vals), 2)

    points = [{"t": t, **vals} for t, vals in sorted(by_t.items())]
    series = sorted({s for vals in by_t.values() for s in vals})
    return {"ok": True, "metric": metric, "bucket": step,
            "series": series, "points": points}


def perf_recent(limit=50, app=None, category=None) -> dict:
    rows = _all_rows(app, category, None)
    return {"ok": True, "recent": rows[-int(limit or 50):]}


# --------------------------------------------------------------------------- #
# Cost & energy
# --------------------------------------------------------------------------- #
def _cost_cfg() -> dict:
    def load():
        path = os.path.join(config.ROOT, "config", "cost.yaml")
        cfg = {"electricity_rate_per_kwh": 0, "nominal_gpu_watts": 180,
               "currency": "$", "prices": {}, "budgets": {}}
        if yaml is not None:
            try:
                with open(path, encoding="utf-8") as f:
                    cfg.update(yaml.safe_load(f) or {})
            except Exception:  # noqa: BLE001
                pass
        # ava.yaml `cost:` overlay — GUI-editable (Setup hub) without source edits.
        from . import settings
        rate = settings.get("cost.electricity_rate_per_kwh", None)
        if rate is not None:
            cfg["electricity_rate_per_kwh"] = rate
        cur = settings.get("cost.currency", None)
        if cur:
            cfg["currency"] = cur
        budgets = settings.get("cost.budgets", None)
        if isinstance(budgets, dict):
            cfg["budgets"] = {**(cfg.get("budgets") or {}), **budgets}
        return cfg
    return _cached("cost_cfg", 30, load)


def invalidate_cost_cache() -> None:
    """Drop cached cost config + budget rollup so a Hub save reflects at once."""
    _cache.pop("cost_cfg", None)
    _cache.pop("budget_pct", None)


def cost_settings() -> dict:
    """Current cost/budget config for the Hub (read side of the editor)."""
    cfg = _cost_cfg()
    b = cfg.get("budgets") or {}
    return {"electricity_rate_per_kwh": cfg.get("electricity_rate_per_kwh", 0),
            "currency": cfg.get("currency", "$"),
            "nominal_gpu_watts": cfg.get("nominal_gpu_watts", 180),
            "budgets": {"daily_usd": b.get("daily_usd"),
                        "monthly_usd": b.get("monthly_usd"),
                        "daily_kwh": b.get("daily_kwh")}}


def _price_for(model: str, prices: dict) -> Optional[dict]:
    ml = (model or "").lower()
    best, best_len = None, -1
    for key, val in (prices or {}).items():
        if key.lower() in ml and len(key) > best_len:
            best, best_len = val, len(key)
    return best


def perf_cost(since="7d", group="model") -> dict:
    """Estimated $ spend + kWh energy over a window, by model|app|category. Stitched:
    the hot tail is computed from raw records (live GPU power from the ring buffer);
    anything older than the hot window is summed from rollups (perf_store), so a
    90-day cost view doesn't rescan raw JSONL. Cost is additive and split at the exact
    cutoff, so there is no boundary double-count."""
    cfg = _cost_cfg()
    prices = cfg.get("prices", {})
    rate = float(cfg.get("electricity_rate_per_kwh", 0) or 0)
    nominal = float(cfg.get("nominal_gpu_watts", 180) or 180)
    powers = [h.get("gpu_power") for h in hardware.history()
              if isinstance(h.get("gpu_power"), (int, float))]
    avg_power = sum(powers) / len(powers) if powers else nominal
    now = time.time()
    since_ts = now - _parse_dur(since, 7 * 86400)
    cutoff = perf_store.cold_boundary(now)
    spend = energy_wh = 0.0
    # Watt-hours we cannot claim to have sampled: everything from the rollups
    # (always nominal — see perf_store.cold_cost) plus the hot tail when the ring
    # buffer had no power readings at all.
    estimated_wh = 0.0
    by: Dict[str, dict] = {}

    # Cold range [since_ts, cutoff) from rollups.
    if since_ts < cutoff:
        cold = perf_store.cold_cost(since_ts, cutoff, group)
        spend += cold["spend_usd"]
        energy_wh += cold["energy_wh"]
        estimated_wh += cold.get("energy_estimated_wh", cold["energy_wh"])
        for key, g in cold["by"].items():
            b = by.setdefault(key, {"spend_usd": 0.0, "energy_wh": 0.0, "n": 0})
            b["spend_usd"] += g["spend_usd"]
            b["energy_wh"] += g["energy_wh"]
            b["n"] += g["n"]

    # Hot tail [max(cutoff, since_ts), now) from raw.
    hot_lo = max(cutoff, since_ts)
    for r in _all_rows(None, None, None):
        if (r.get("ts") or 0) < hot_lo:
            continue
        model = r.get("served_model") or r.get("model") or "?"
        cost = 0.0
        p = _price_for(model, prices)
        if p:
            pt = r.get("prompt_tokens") or 0
            ct = r.get("completion_tokens") or 0
            cost = pt / 1e6 * float(p.get("input", 0)) + ct / 1e6 * float(p.get("output", 0))
        secs = r.get("gen_seconds") or r.get("render_seconds") or r.get("seconds") or 0
        e = avg_power * float(secs) / 3600.0 if secs else 0.0
        spend += cost
        energy_wh += e
        if not powers:
            estimated_wh += e
        key = str(r.get(group) or model)
        b = by.setdefault(key, {"spend_usd": 0.0, "energy_wh": 0.0, "n": 0})
        b["spend_usd"] += cost
        b["energy_wh"] += e
        b["n"] += 1
    energy_kwh = energy_wh / 1000.0
    # Provenance of the energy figure, for THIS window rather than for the live
    # buffer. `bool(powers)` answers "is the GPU reporting power right now", which
    # is a different question and the wrong one: hardware.history() keeps two
    # hours (_SAMPLE_KEEP_S) while perf.hot_window is 48h, so a 7-day figure was
    # labelled "measured" when ~5 days of it came from a static constant.
    #   measured  - every watt-hour in the window came from real samples
    #   partial   - some sampled, some nominal (the ordinary NVIDIA case)
    #   estimated - no samples at all (Mac / AMD / Intel / CPU-only today)
    # Tri-state rather than a boolean, for the same reason skills.py separates
    # `unknown` from `undeployed`: "partly" is a real answer, not a rounding of
    # either neighbour.
    if energy_wh <= 0:
        energy_state = "measured" if powers else "estimated"
    elif estimated_wh <= 0:
        energy_state = "measured"
    elif estimated_wh >= energy_wh:
        energy_state = "estimated"
    else:
        energy_state = "partial"
    return {
        "ok": True, "since": since, "group": group,
        "spend_usd": round(spend, 4),
        "energy_kwh": round(energy_kwh, 4),
        "energy_usd": round(energy_kwh * rate, 4) if rate else None,
        "energy_state": energy_state,
        "energy_estimated_kwh": round(estimated_wh / 1000.0, 4),
        # Kept for the response contract (qa/test_02_api_contracts.py) and
        # narrowed to what it always claimed to mean: the whole window is
        # sampled. The UI must not present estimated energy as measured.
        "power_measured": energy_state == "measured",
        "power_sampled_now": bool(powers),
        "avg_gpu_watts": round(avg_power, 1),
        "by": {k: {"spend_usd": round(v["spend_usd"], 4),
                   "energy_kwh": round(v["energy_wh"] / 1000, 4),
                   "n": v["n"]} for k, v in by.items()},
    }


# --------------------------------------------------------------------------- #
# Work — jobs / turns / code turns
# --------------------------------------------------------------------------- #
def jobs_list(status=None, kind=None, limit=100) -> dict:
    from . import gpu_jobs
    gpu_jobs.reap_stale_jobs()  # flip stalled runners to error; drop old terminals
    with state.jobs_lock:
        js = list(state.jobs.values())
    if status:
        js = [j for j in js if j.get("status") == status]
    if kind:
        js = [j for j in js if j.get("kind") == kind]
    js.sort(key=lambda j: j.get("created", 0), reverse=True)
    return {"ok": True, "jobs": js[:int(limit or 100)]}


def turns_list(limit=50, active=False) -> dict:
    with state.turns_lock:
        ts = list(state.turns.values())
    if active:
        ts = [t for t in ts if t.get("status") == "running"]
    ts.sort(key=lambda t: t.get("created", 0), reverse=True)
    out = []
    for t in ts[:int(limit or 50)]:
        steps = t.get("steps") or []
        out.append({
            "id": t.get("id"),
            "status": t.get("status"),
            "created": t.get("created"),
            "step_count": len(steps),
            "last_step": steps[-1] if steps else None,
            "tools_used": t.get("tools_used") or [],
            "model": t.get("model") or None,
            "ctx_tokens": t.get("ctx_tokens"),
            "has_job": bool(t.get("job")),
            "error": t.get("error"),
            "reply_preview": (t.get("reply") or "")[:180] or None,
        })
    return {"ok": True, "turns": out}


def code_turns_list(limit=30) -> dict:
    with state.code_turns_lock:
        ts = list(state.code_turns.values())
    ts.sort(key=lambda t: t.get("created", 0), reverse=True)
    out = []
    for t in ts[:int(limit or 30)]:
        out.append({
            "id": t.get("id"), "status": t.get("status"),
            "created": t.get("created"), "applied": t.get("applied"),
            "edit_count": len(t.get("edits") or []),
            "error": t.get("error"),
            "summary": (t.get("reply") or "")[:180] or None,
        })
    return {"ok": True, "code_turns": out}


# --------------------------------------------------------------------------- #
# Operations — summary / schedule / services / tools / alerts
# --------------------------------------------------------------------------- #
def _learning_brief() -> dict:
    def brief(sd, lock):
        with lock:
            cycles = sd.get("cycles") or []
            last = sd.get("last_cycle")
            pending = 0
            if cycles:
                for p in (cycles[-1].get("proposals") or []):
                    if p.get("status") in (None, "pending"):
                        pending += 1
        return {"last_cycle": last, "cycles": len(cycles), "pending": pending}
    return {
        "code": brief(state.code_learning_state, state.code_learning_state_lock),
        "chat": brief(state.chat_learning_state, state.chat_learning_state_lock),
    }


def ops_summary() -> dict:
    with state.jobs_lock:
        jobs = list(state.jobs.values())
    with state.turns_lock:
        turns = list(state.turns.values())

    def by_status(items):
        c: Counter = Counter()
        for it in items:
            c[it.get("status") or "?"] += 1
        return dict(c)

    # Cached: this endpoint is polled every 6s per open tab, and a full raw
    # scan across every app's file on each poll doesn't scale with app count.
    gen_24h = _cached("gen24_count", 15, lambda: len(_all_rows(None, None, "1d")))
    return {
        "ok": True,
        "jobs": {"running": sum(1 for j in jobs if j.get("status") == "running"),
                 "total": len(jobs), "by_status": by_status(jobs)},
        "turns": {"running": sum(1 for t in turns if t.get("status") == "running"),
                  "total": len(turns), "by_status": by_status(turns)},
        "generations_24h": gen_24h,
        "learning": _learning_brief(),
        "ts": time.time(),
    }


def _systemctl(args: List[str], timeout: int = 5) -> str:
    import subprocess
    try:
        r = subprocess.run(["systemctl", *args], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def ops_schedule() -> dict:
    def load():
        import json as _json
        raw = _systemctl(["--user", "list-timers", "--all", "-o", "json"])
        timers = []
        try:
            data = _json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001 — older systemd has no -o json
            data = []
        now = time.time()
        for t in data:
            unit = t.get("unit")
            nxt = _to_secs(t.get("next"))
            last = _to_secs(t.get("last"))
            timers.append({
                "unit": unit,
                "activates": t.get("activates"),
                "description": _unit_description(t.get("activates") or unit),
                "next_time": _fmt_abs(nxt),
                "next_rel": _humanize(nxt - now) if nxt else None,
                "last_time": _fmt_abs(last),
                "last_rel": _humanize(now - last) if last else None,
            })
        timers.sort(key=lambda x: x.get("next_time") or "~")
        return {"ok": True, "timers": timers}
    return _cached("schedule", 30, load)


def _to_secs(usec) -> Optional[float]:
    """systemd realtime microseconds -> epoch seconds (0/None -> None)."""
    try:
        u = float(usec)
    except (TypeError, ValueError):
        return None
    if u <= 0:
        return None
    return u / 1e6 if u > 1e14 else u


def _fmt_abs(secs) -> Optional[str]:
    """Epoch seconds -> local (host TZ) 'Jul 05, 03:15'."""
    if not secs:
        return None
    try:
        return time.strftime("%b %d, %H:%M", time.localtime(secs))
    except Exception:  # noqa: BLE001
        return None


def _humanize(seconds) -> Optional[str]:
    """A signed second delta -> human 'in 3h 20m' magnitude like '3h 20m'."""
    try:
        s = int(abs(seconds))
    except (TypeError, ValueError):
        return None
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if h else f"{d}d"


def _unit_description(unit: Optional[str]) -> Optional[str]:
    """Human Description= of a systemd unit (cached), so tasks read plainly."""
    if not unit:
        return None
    return _cached(f"desc:{unit}", 300, lambda: (
        _systemctl(["--user", "show", unit, "-p", "Description", "--value"]).strip()
        or unit))



# Fallback service list if the connector registry is empty. Normally the
# dashboard derives services from connectors.services() (see connectors.py).
# Core-only fallback (no app-specific entries): every app/model/media service
# declares its own `service.probe` in connectors/<id>/connector.yaml and the
# registry provides the rest, so a fresh fork shows only Ava's core services.
MONITORED_SERVICES = [
    {"name": "Bridge", "unit": "ava-bridge.service",
     "probe": "http://127.0.0.1:8096/api/health"},
    {"name": "Router", "unit": "ava-router.service",
     "probe": "http://127.0.0.1:8010/healthz"},
]


def _probe(url: str) -> Optional[bool]:
    try:
        r = requests.get(url, timeout=2)
        return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def ops_services() -> dict:
    def check(s: dict) -> dict:
        # A service whose governing feature the user turned OFF is reported as
        # "off" — a neutral state — never probed into a red "down". Off-by-
        # choice and crashed must not look identical on the dashboard.
        feat = s.get("feature")
        if feat:
            from . import features
            if not features.enabled(feat):
                return {"name": s["name"], "unit": s.get("unit"),
                        "systemd": None, "probe_ok": None, "status": "off",
                        "feature": feat}
        unit_state = _systemctl(["--user", "is-active", s["unit"]]) if s.get("unit") else None
        probe_ok = _probe(s["probe"]) if s.get("probe") else None
        if unit_state == "active" or probe_ok is True:
            status = "up"
        elif unit_state in ("inactive", "failed", "deactivating") or probe_ok is False:
            status = "down"
        else:
            status = "unknown"
        return {"name": s["name"], "unit": s.get("unit"),
                "systemd": unit_state or None, "probe_ok": probe_ok,
                "status": status}

    def load():
        svcs = connectors.services() or MONITORED_SERVICES
        # Probe concurrently: wall-clock is the slowest single check, not the
        # sum — so one hung app can't stall the services panel (or the
        # connectors view, which reuses these statuses) for 2s × N services.
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(svcs)))) as ex:
            out = list(ex.map(check, svcs))
        return {"ok": True, "services": out,
                "down": sum(1 for x in out if x["status"] == "down")}
    return _cached("services", 15, load)


def connectors_info() -> dict:
    """The dashboard **telemetry** view of connectors (Ops Connectors panel):
    enabled connectors only (`connectors.all()`), with health / perf / egress-count
    fields for monitoring. Distinct from the Hub's `/api/hub/connectors`
    (`hub_api.list_connectors`), the **management** view that also lists disabled
    connectors and their edit/deploy state. Two audiences, two shapes — see the
    note on `list_connectors`."""
    # Live health by service name (15s-cached probe results) so the Vitals apps
    # panel can show up/down without a second frontend call.
    smap = {s["name"]: s["status"] for s in (ops_services().get("services") or [])}
    srcs = perf_mgmt.sources()
    items = []
    for m in connectors.all():
        svc = m.get("service") or {}
        eg = m.get("egress") or {}
        perf_app = str((m.get("perf") or {}).get("app") or m["id"])
        items.append({
            "id": m["id"],
            "label": m.get("label", m["id"]),
            "kind": m.get("kind", "app"),
            "has_service": bool(svc),
            "has_perf": bool(m.get("perf")),
            # the app-key its perf records aggregate under (Vitals "by app")
            "perf_app": perf_app,
            # true once any of its perf files exist on disk — "reporting yet?"
            "perf_present": any(os.path.isfile(p) for p in srcs.get(perf_app, [])),
            "status": smap.get(svc.get("name", m.get("label", m["id"]))) if svc else None,
            "egress_routes": len(eg.get("routes") or []) + len(eg.get("hosts") or []),
            "actions": [a.get("id") for a in (m.get("actions") or [])
                        if isinstance(a, dict) and a.get("id")],
        })
    return {"ok": True, "connectors": items, "action_count": len(connectors.actions())}


def ops_tools(limit=15) -> dict:
    c: Counter = Counter()
    with state.turns_lock:
        turns = list(state.turns.values())
    for t in turns:
        for tool in (t.get("tools_used") or []):
            c[tool] += 1
    return {"ok": True,
            "tools": [{"tool": k, "count": v} for k, v in c.most_common(int(limit or 15))],
            "total_calls": sum(c.values())}


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def build_alert_metrics() -> dict:
    m: Dict[str, Any] = {}
    s = hardware.latest_sample() or {}
    m["gpu_temp"] = s.get("gpu_temp")
    m["gpu_power"] = s.get("gpu_power")
    m["mem_used_pct"] = s.get("mem_used_pct")
    disk = _cached("disk", 15, hardware._disk)
    m["disk_free_pct"] = (100 - disk["used_pct"]) if disk.get("used_pct") is not None else None
    # ONE cached raw scan feeds every 1h metric below. The SSE producer calls
    # this every ~5s per client, and the file count grows with each connected
    # app — re-reading all of them three times per tick doesn't scale.
    all_1h = _cached("rows_1h", 10, lambda: _all_rows(None, None, "1h"))
    llm_1h = [r for r in all_1h if r.get("category") == "llm"]
    tps = [r.get("tokens_per_sec") for r in llm_1h
           if isinstance(r.get("tokens_per_sec"), (int, float))]
    m["tokens_per_sec"] = round(sum(tps) / len(tps), 2) if tps else None
    fo = [1 if r.get("failover") else 0 for r in llm_1h]
    m["failover_rate_1h"] = round(sum(fo) / len(fo), 3) if fo else 0
    errs = [1 if (isinstance(r.get("status"), int) and r["status"] >= 400) else 0
            for r in all_1h]
    m["error_rate_1h"] = round(sum(errs) / len(errs), 3) if errs else 0
    now = time.time()
    stuck = 0
    with state.jobs_lock:
        for j in state.jobs.values():
            if j.get("status") == "running" and now - (j.get("updated") or j.get("created", now)) > 480:
                stuck += 1
    m["job_stuck_count"] = stuck
    m["service_down_count"] = ops_services().get("down", 0)

    # --- model allocation (dormant until models are declared in alloc.models) ---
    # A declared model that is running but has no weights loaded answers its port,
    # so service_down_count above stays 0 and nothing else notices. These metrics
    # exist to make that state alertable. Cached because the SSE producer calls this
    # every ~5s per client and a residency probe shells out.
    try:
        from .alloc import watch as _alloc_watch
        m.update(_cached("alloc_metrics", 30, _alloc_watch.metrics))
    except Exception:  # noqa: BLE001 — allocation is optional; never break the feed
        m.setdefault("alloc_degraded_count", 0)
        m.setdefault("alloc_unfit_count", 0)
        m.setdefault("alloc_unknown_hold_gb", 0)

    # --- cost / energy budgets + idle burn (dormant until a budget is set) ---
    cfg = _cost_cfg()
    budgets = cfg.get("budgets") or {}

    def _budget_pct():
        day = perf_cost("1d")
        mon = perf_cost("30d")
        du, mu, dk = (budgets.get("daily_usd"), budgets.get("monthly_usd"),
                      budgets.get("daily_kwh"))
        return {
            "budget_daily_pct": round(day["spend_usd"] / du * 100, 1) if du else 0,
            "budget_monthly_pct": round(mon["spend_usd"] / mu * 100, 1) if mu else 0,
            "budget_energy_pct": round(day["energy_kwh"] / dk * 100, 1) if dk else 0,
            "daily_spend_usd": day["spend_usd"], "daily_energy_kwh": day["energy_kwh"],
        }
    m.update(_cached("budget_pct", 60, _budget_pct))

    # Idle burn: completion tokens generated in the last 10 min while no turn is
    # running AND >120s after the last interactive activity — i.e. the agent
    # spending on its own while you were away. 0 when you're actively using it.
    with state.turns_lock:
        turn_running = any(t.get("status") == "running" for t in state.turns.values())
    idle_tokens = 0
    if not turn_running:
        last = state.interaction.get("ts", 0)
        cutoff_10m = time.time() - 600
        for r in llm_1h:  # derived from the same cached 1h scan
            if (r.get("ts") or 0) < cutoff_10m:
                continue
            ct = r.get("completion_tokens") or 0
            if ct and (r.get("ts") or 0) > last + 120:
                idle_tokens += int(ct)
    m["idle_burn_tokens_10m"] = idle_tokens
    return m


def ops_alerts() -> dict:
    metrics = build_alert_metrics()
    res = alerts.evaluate(metrics)
    return {"ok": True, "active": res["active"], "metrics": metrics}


# --------------------------------------------------------------------------- #
# SSE snapshot — diffed each tick by the stream producer
# --------------------------------------------------------------------------- #
def live_snapshot() -> dict:
    """Compact state used by the SSE producer to diff + emit live events."""
    with state.turns_lock:
        turns = {t.get("id"): {"status": t.get("status"),
                               "step_count": len(t.get("steps") or []),
                               "tools": len(t.get("tools_used") or [])}
                 for t in state.turns.values()}
    with state.jobs_lock:
        jobs = {j.get("id"): {"status": j.get("status"),
                              "progress": j.get("progress"),
                              "stage": j.get("stage")}
                for j in state.jobs.values()}
    return {"turns": turns, "jobs": jobs, "hw": hardware.latest_sample()}
