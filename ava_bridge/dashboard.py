"""Aggregation layer over the perf, hardware and connector modules.

Read-first data assembled from existing modules (`perf_mgmt`, `state`,
`hardware`, `connectors`, `perf_store`) + systemd. Nothing here writes.

Most of this module served the Vitals and Operations pages and went with them.
What is left has callers elsewhere, and each one is the reason its section
survives:

  * cost — `perf_cost` / `cost_settings` / `invalidate_cost_cache` back
    Setup → Budgets through `hub/cost.py`.
  * services — `ops_services` and `_probe` resolve each connector's declared
    probe and unit. `apps_health` turns that into the sidebar's per-app dot,
    which is the `service.probe` surface a connector manifest derives.
  * turns — `turns_list` backs `/api/turns` for the Agent console.
  * alert metrics — `build_alert_metrics` assembles the flat metric set the
    watchdogs' rules are evaluated against.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

import requests

from . import config, state, perf_mgmt, hardware, connectors, perf_store, settings

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
# Cost & energy
# --------------------------------------------------------------------------- #
def _cost_cfg() -> dict:
    def load():
        path = os.path.join(config.ROOT, "config", "cost.yaml")
        # No `nominal_gpu_watts` default: absent means "nothing declared", which
        # lets perf_cost fall back to the PLATFORM nominal and then to NOT
        # MEASURED. A literal here made every box look like it had declared 180 W.
        cfg = {"electricity_rate_per_kwh": 0,
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
            "nominal_gpu_watts": cfg.get("nominal_gpu_watts"),
            "budgets": {"daily_usd": b.get("daily_usd"),
                        "monthly_usd": b.get("monthly_usd"),
                        "daily_kwh": b.get("daily_kwh")}}


def _power_profile() -> dict:
    """This platform's power source and nominal wattage, cached.

    Wrapped rather than called inline so the platform table is read once per TTL
    instead of once per cost request, and so a broken table degrades to "unknown"
    rather than raising inside a dashboard endpoint.
    """
    def load():
        try:
            from . import platforms
            return platforms.power_profile()
        except Exception:  # noqa: BLE001 — diagnostics must never break the page
            return {"platform_key": None, "power_source": None,
                    "nominal_w": None, "provenance": "unreadable"}
    return _cached("power_profile", 60, load)


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
    # Watts, in order of how much they are worth trusting:
    #   1. real samples from this GPU  ->  power_source "sampled"
    #   2. an explicit nominal in config/cost.yaml (the owner declared it)
    #   3. this PLATFORM's nominal from deploy/platforms.conf
    #   4. nothing defensible -> None, and energy is NOT MEASURED
    # Step 4 is the change that matters. `nominal_gpu_watts` used to default to a
    # flat 180 — a figure measured on one mid-range discrete NVIDIA card, applied
    # to Mac minis (~10-30 W) and APUs (~50-120 W) alike. An estimate built on
    # that was not an estimate of anything in particular.
    declared = cfg.get("nominal_gpu_watts")
    prof = _power_profile()
    nominal = None
    source_kind = None
    if declared not in (None, "", 0):
        nominal, source_kind = float(declared), "declared"
    elif prof.get("nominal_w"):
        nominal, source_kind = float(prof["nominal_w"]), "platform-nominal"

    powers = [h.get("gpu_power") for h in hardware.history()
              if isinstance(h.get("gpu_power"), (int, float))]
    if powers:
        avg_power = sum(powers) / len(powers)
        source_kind = "sampled"
    else:
        avg_power = nominal          # may be None: no defensible figure exists
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
        e = (avg_power * float(secs) / 3600.0) if (secs and avg_power) else 0.0
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
    # No defensible wattage anywhere => energy is NOT MEASURED. Reporting 0.0 kWh
    # would be a claim about free electricity, and reporting a flat 180 W was a
    # claim about someone else's GPU. Null is the only honest third option.
    if avg_power is None:
        energy_state = "unknown"

    # Dollars are gated harder than kilowatt-hours, deliberately. kWh can carry an
    # "(est.)" label and still inform; a currency figure reads as settled, so it is
    # withheld unless the wattage was sampled or the owner declared their own.
    dollars_ok = rate and source_kind in ("sampled", "declared")

    return {
        "ok": True, "since": since, "group": group,
        "spend_usd": round(spend, 4),
        "energy_kwh": round(energy_kwh, 4) if avg_power is not None else None,
        "energy_usd": round(energy_kwh * rate, 4) if dollars_ok else None,
        "energy_state": energy_state,
        "energy_estimated_kwh": (round(estimated_wh / 1000.0, 4)
                                 if avg_power is not None else None),
        # Where the wattage came from: sampled | declared | platform-nominal | None.
        # The UI needs this to say WHY a figure is an estimate — "no sensor on this
        # platform" and "you have not told me your card's draw" need different advice.
        "power_source": source_kind,
        "power_provenance": prof.get("provenance"),
        # Kept for the response contract (qa/test_02_api_contracts.py) and
        # narrowed to what it always claimed to mean: the whole window is
        # sampled. The UI must not present estimated energy as measured.
        "power_measured": energy_state == "measured",
        "power_sampled_now": bool(powers),
        "avg_gpu_watts": round(avg_power, 1) if avg_power is not None else None,
        "by": {k: {"spend_usd": round(v["spend_usd"], 4),
                   "energy_kwh": round(v["energy_wh"] / 1000, 4),
                   "n": v["n"]} for k, v in by.items()},
    }


# --------------------------------------------------------------------------- #
# Work — turns / code turns
# --------------------------------------------------------------------------- #
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
            "error": t.get("error"),
            "reply_preview": (t.get("reply") or "")[:180] or None,
        })
    return {"ok": True, "turns": out}


# Fallback service list if the connector registry is empty. Normally the
# dashboard derives services from connectors.services() (see connectors.py).
# Core-only fallback (no app-specific entries): every app/model/media service
# declares its own `service.probe` in connectors/<id>/connector.yaml and the
# registry provides the rest, so a fresh fork shows only Ava's core services.
def _systemctl(args: List[str], timeout: int = 5) -> str:
    """`systemctl --user` for the service-health probe. Kept when the Operations
    page went: `ops_services()` still resolves each connector's declared unit,
    and the sidebar app-health dot reads that through `apps_health()`."""
    import subprocess
    try:
        r = subprocess.run(["systemctl", *args], capture_output=True,
                           text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


MONITORED_SERVICES = [
    {"name": "Bridge", "unit": "ava-bridge.service",
     "probe": "http://127.0.0.1:8096/api/health"},
    {"name": "Router", "unit": "ava-router.service",
     "probe": "http://127.0.0.1:8010/healthz"},
]


def _probe(url: str, expect: str = "2xx") -> Optional[bool]:
    """Is the service at `url` up? `expect` says which claim the probe is making.

    This used to be `status_code < 500` unconditionally, which paints a green pill
    and counts toward "Services up N/N" for a **401** (probe URL needs auth), a
    **404** (app deleted, route renamed), a **402** (over quota) and a **405**
    (probing a POST-only endpoint with GET). "Something answered" is a real signal
    but it is not the signal the dashboard claims to show, and a confidently wrong
    green is worse than an honest unknown.

    Default is now `2xx` — the service answered, and answered OK. A manifest can
    opt back into the old behaviour with `service.expect: non5xx` when that is
    genuinely what it means (a probe URL that legitimately 401s, say), which makes
    the weaker claim explicit in the manifest rather than implicit in the dashboard.
    `service.expect: <int>` pins one exact status.
    """
    try:
        r = requests.get(url, timeout=2, allow_redirects=False)
    except Exception:  # noqa: BLE001
        return False
    exp = str(expect or "2xx").lower()
    if exp == "non5xx":
        return r.status_code < 500
    if exp.isdigit():
        return r.status_code == int(exp)
    return 200 <= r.status_code < 300


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
        probe_ok = _probe(s["probe"], s.get("expect") or "2xx") if s.get("probe") else None
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


#: Sidebar readiness verdicts, worst-first. The sidebar dot shows ONE of these
#: per app, so the order here is the precedence when several are true at once.
APP_HEALTH = ("off", "down", "partial", "ready")


def apps_health() -> dict:
    """Is each connected app actually ready to use? One verdict per app, plus the
    facts behind it.

    The sidebar dot answers a question no existing endpoint did. `/api/apps` is
    the registry (what exists), `/api/hub/connectors` is management (what is
    wired), `/api/ops/connectors` is telemetry (what is answering) — and
    "can I click this right now?" needs reachability AND wiring together. Rather
    than a fourth opinion, this joins the two that already exist: the 15s-cached
    probe from `ops_services()` and the same deploy-state fields
    `hub.connectors._connector_row` reports.

    FACTS AND ONE CODE, no sentences: per CLAUDE.md the owner-facing copy is the
    frontend's job, so this returns `service`/`auth_set`/`tools_deployed`/… and a
    rolled-up `health`, and the sidebar turns those into words. The code is here
    rather than in the client because the rail and the expanded panel must never
    disagree about what green means.

    The four verdicts:

      off      the owner switched it off. Never red — off-by-choice and crashed
               must not look identical (the same rule `ops_services()` follows).
      down     it declares a health probe and the probe says no. Nothing else
               matters while the app is not answering.
      partial  it is answering (or has nothing to answer with) but something in
               the chain is missing — no credential, tools not deployed, egress
               policy not generated, or a probe that could not be read.
      ready    everything it declares is in place. An app that declares no probe
               can still be ready: there is nothing to be down, and a
               permanently-amber tile is one the owner stops reading.
    """
    smap = {s["name"]: s["status"] for s in (ops_services().get("services") or [])}
    pol_dir = settings.generated_policy_dir()
    tool_root = settings.connector_tools_dir()
    items = []
    # `catalog()`, not `all()`: a disabled app still holds its place in the
    # sidebar, and reporting it as absent would read as "down" by omission.
    for m in connectors.catalog():
        if not isinstance(m.get("ui"), dict):
            continue
        cid = m["id"]
        svc = m.get("service") or {}
        # Keyed by service NAME, the same key `connectors_info` uses — the
        # services list is built from `service.name`, not from the connector id.
        service = smap.get(svc.get("name", m.get("label", cid))) if svc else None
        auth = connectors.auth_env(m)
        expected = connectors.tool_files(cid)
        tools_deployed = all(
            os.path.exists(os.path.join(tool_root, cid, t["name"])) for t in expected)
        try:
            policy_expected = connectors.render_egress_policy(cid) is not None
        except Exception:  # noqa: BLE001
            # A row that cannot render its policy must not take the sidebar down.
            policy_expected = False
        row = {
            "id": cid,
            "enabled": bool(m.get("enabled", True)),
            # null = declares no probe, which is not the same as "did not answer".
            "service": service,
            "auth_env": auth,
            "auth_set": settings.has_env_secret(auth) if auth else True,
            "tools_expected": len(expected),
            "tools_deployed": tools_deployed,
            "policy_expected": policy_expected,
            "policy_present": os.path.exists(os.path.join(pol_dir, f"{cid}.yaml")),
        }
        row["health"] = _app_verdict(row)
        items.append(row)
    return {"ok": True, "apps": items}


def _app_verdict(r: dict) -> str:
    """Roll one app's facts up into a single sidebar colour. See `apps_health`."""
    if not r["enabled"]:
        return "off"
    if r["service"] == "down":
        return "down"
    missing = (
        not r["auth_set"]
        or (r["tools_expected"] and not r["tools_deployed"])
        or (r["policy_expected"] and not r["policy_present"])
        # "unknown" is not "up": we could not read it, so we do not claim green.
        or r["service"] == "unknown"
    )
    return "partial" if missing else "ready"


def _egress_route_count(m: dict) -> int:
    """How many rules this connector's rendered egress policy actually grants.

    Rendering is the only honest answer — the manifest's `egress:` block is one
    input to it, not the output. Falls back to the literal count if rendering
    raises, because a telemetry row must never take down the Ops page.
    """
    try:
        pol = connectors.render_egress_policy(m["id"]) or {}
        return sum(len(ep.get("rules") or [])
                   for np in (pol.get("network_policies") or {}).values()
                   if isinstance(np, dict)
                   for ep in (np.get("endpoints") or [])
                   if isinstance(ep, dict))
    except Exception:  # noqa: BLE001
        eg = m.get("egress") or {}
        return len(eg.get("routes") or []) + len(eg.get("hosts") or [])


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

    # Ava's configured model vs what the engine actually holds. Same reason as
    # above — a router serving an id its engine does not have answers its port
    # perfectly, fails every turn, and shows up in no other metric. Cached
    # alongside the alloc gauges because it costs an engine probe.
    try:
        from . import brain_watch as _brain_watch
        m.update(_cached("brain_metrics", 30, _brain_watch.metrics))
    except Exception:  # noqa: BLE001 — never break the feed
        m.setdefault("brain_drift_count", 0)

    # --- cost / energy budgets + idle burn (dormant until a budget is set) ---
    cfg = _cost_cfg()
    budgets = cfg.get("budgets") or {}

    def _budget_pct():
        day = perf_cost("1d")
        mon = perf_cost("30d")
        du, mu, dk = (budgets.get("daily_usd"), budgets.get("monthly_usd"),
                      budgets.get("daily_kwh"))
        # `energy_kwh` is nullable by design (see perf_cost): a platform with no
        # sampled wattage, no owner-declared figure and no nominal in
        # platforms.conf has nothing to report. A percentage needs a numerator,
        # so the energy meter has THREE states, not two:
        #   0     no cap set          -> dormant, same as the dollar meters
        #   None  cap set, kWh null   -> NOT MEASURED; the meter is unavailable
        #   float cap set, kWh known  -> the real percentage
        # None rather than 0 because 0% of an energy cap is a claim that the box
        # drew no power. This divided unconditionally, so on any platform whose
        # row carries `nominal_w = -` (linux-cpu, linux-gpu, darwin-apple,
        # windows, generic) with a daily_kwh cap set, /api/ops/alerts and every
        # dashboard consumer of it returned 500. alerts.evaluate already skips a
        # None metric, so an unavailable figure raises nothing.
        kwh = day["energy_kwh"]
        if not dk:
            energy_pct = 0
        elif kwh is None:
            energy_pct = None
        else:
            energy_pct = round(kwh / dk * 100, 1)
        return {
            "budget_daily_pct": round(day["spend_usd"] / du * 100, 1) if du else 0,
            "budget_monthly_pct": round(mon["spend_usd"] / mu * 100, 1) if mu else 0,
            "budget_energy_pct": energy_pct,
            "daily_spend_usd": day["spend_usd"], "daily_energy_kwh": kwh,
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

