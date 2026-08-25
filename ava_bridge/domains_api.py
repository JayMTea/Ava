"""Domain routes (/api/domains/*): the catalogue, one cell, and one series.

WHAT IS DELIBERATELY ABSENT. There is no route above a cell. No
``/api/domains/<axis-value>`` returning a score, no estate-wide index, no
"everything is 84% healthy". Such a number's weights would be assumed, so by
this layer's own provenance rule its floor is `assumed` — it could never be a
north star — and it is maximally gameable through whichever leg is cheapest to
move. The absence IS the enforcement: a number that no endpoint can produce
cannot drift into a dashboard later.

EVERY TOTAL CARRIES ITS GAPS. A subtotal ships with `complete` and, when that is
false, a non-empty `missing`. An incomplete total cannot be transmitted without
the names of what is missing from it, which is the wire-level version of the
rule that absence must survive into the pixels.
"""
from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from . import domains, features, kpi_collect, kpi_store

router = APIRouter()


def _subtotal(observations: list[dict], unit: str) -> dict:
    """Sum only same-unit `ok` observations, and name everything left out.

    Two rules live here. Units must match — dollars plus kilocalories is where
    this whole idea dies. And a contributor that is absent, thin or unmeasured
    does not silently count as zero: it is named, and the result says it is a
    subtotal rather than a total.
    """
    used, missing = [], []
    for o in observations:
        if o.get("unit") != unit:
            missing.append({"metric": o.get("metric"), "why": "different unit"})
        elif o.get("state") == "ok" and o.get("value") is not None:
            used.append(o)
        else:
            missing.append({"metric": o.get("metric"),
                            "why": o.get("why") or o.get("state")})
    return {"unit": unit, "value": round(sum(o["value"] for o in used), 4) if used else None,
            "contributors": len(used), "complete": not missing,
            "missing": missing}


def _ratio(metric: dict, since: int) -> dict:
    """A ratio computed at READ time as sum(num) / sum(den) over the window.

    Never the mean of daily ratios. Averaging ratios weights a day with two
    observations the same as a day with two hundred, which is how a quiet
    Tuesday comes to dominate a monthly rate.

    Days are PAIRED: a day counts only if both legs have an `ok` reading, and a
    day missing either leg is dropped from BOTH sums. Summing whatever each leg
    happens to have would divide a numerator over one set of days by a
    denominator over a different set — an answer to a question nobody asked.
    """
    mid = metric["metric_id"]
    base = {"metric": mid, "unit": metric.get("unit"), "value": None,
            "provenance": None, "n": None, "lo": None, "hi": None}
    num_id, den_id = metric.get("num"), metric.get("den")
    if not (num_id and den_id):
        return {**base, "state": "no_source", "why": "ratio names no legs"}

    def by_day(leg_id):
        out = {}
        for r in kpi_store.rows_for(leg_id):
            if r.get("state") != "ok" or r.get("value") is None:
                continue
            d = r.get("day")
            if d and (d not in out or r.get("observed_at", 0) >= out[d].get("observed_at", 0)):
                out[d] = r
        return out

    num_rows, den_rows = by_day(num_id), by_day(den_id)
    window = set(p["day"] for p in kpi_store.series(num_id, since))
    paired = sorted(window & num_rows.keys() & den_rows.keys())
    n = len(paired)
    min_n = metric.get("min_n")
    if min_n and n < int(min_n):
        return {**base, "state": "insufficient", "n": n,
                "why": f"{n} paired day(s) below min_n={min_n}"}
    if not paired:
        return {**base, "state": "no_source", "n": 0,
                "why": f"no day has an ok reading for both {num_id} and {den_id}"}

    den_sum = sum(den_rows[d]["value"] for d in paired)
    if not den_sum:
        return {**base, "state": "insufficient", "n": n,
                "why": "the denominator sums to zero over the window"}
    num_sum = sum(num_rows[d]["value"] for d in paired)
    order = {"assumed": 0, "derived": 1, "measured": 2}
    provs = [num_rows[d].get("provenance") or "assumed" for d in paired] + \
            [den_rows[d].get("provenance") or "assumed" for d in paired]
    return {**base, "value": round(num_sum / den_sum, 6), "state": "ok", "n": n,
            "provenance": min(provs, key=lambda x: order.get(x, 0)),
            "why": f"sum over {n} paired day(s)"}


def _observation(metric: dict) -> dict:
    """The latest observation for one metric.

    A DIMENSIONED metric has no single cell-level value — it was collected once
    per dimension value, and picking one of them, or silently summing across
    them, would both be inventions. It reports what it is and carries the
    per-dimension rows so a card can show them side by side. Saying "never
    collected" about a metric that was collected three times, once per
    dimension, is simply false, and it was the first thing this endpoint got
    wrong.
    """
    mid = metric["metric_id"]
    base = {"metric": mid, "unit": metric.get("unit"), "value": None,
            "provenance": None, "n": None, "lo": None, "hi": None}

    if metric.get("agg") == "ratio_of_sums":
        return _ratio(metric, 30)

    if metric.get("dim"):
        rows = [r for r in kpi_store.rows_for(mid) if r.get("dim")]
        if not rows:
            return {**base, "state": "no_source", "why": "never collected"}
        latest_day = max(r.get("day") or "" for r in rows)
        parts = {}
        for r in rows:
            if r.get("day") == latest_day:
                parts[r["dim"]] = {k: r.get(k) for k in
                                   ("value", "state", "provenance", "n", "why")}
        ok = [p for p in parts.values() if p["state"] == "ok"]
        return {**base, "state": "ok" if ok else "insufficient",
                "day": latest_day, "dim": metric["dim"], "by_dim": parts,
                "why": f"reported per {metric['dim']}; "
                       f"{len(ok)} of {len(parts)} have a reading"}

    row = kpi_store.latest(mid)
    if not row:
        return {**base, "state": "no_source", "why": "never collected"}
    return {k: row.get(k) for k in
            ("metric", "unit", "value", "state", "provenance", "n", "lo", "hi",
             "day", "why")}


def catalogue() -> dict:
    doc = domains.load()
    return {"axes": doc["axes"],
            "surfaces": [{k: s[k] for k in ("id", "realm", "domain", "owner",
                                            "label", "rollup")}
                         | {"metrics": len(s["metrics"])} for s in doc["surfaces"]],
            "cells": [{"realm": r, "domain": d} for r, d in domains.cells()],
            "problems": doc["problems"],
            "pending_grants": kpi_collect.pending_grants()}


def cell(realm: str, domain: str, since: int = 30) -> dict:
    metrics = domains.metrics_for(realm, domain)
    if not metrics:
        return {"ok": False, "error": f"no surfaces in {realm}/{domain}"}
    obs = [_observation(m) for m in metrics]
    tree = domains.tree(realm, domain)

    by_unit: dict[str, list] = {}
    for o in obs:
        if o.get("unit"):
            by_unit.setdefault(o["unit"], []).append(o)

    order = {"assumed": 0, "derived": 1, "measured": 2}
    floors = [o.get("provenance") for o in obs if o.get("provenance")]
    # Absence PARTICIPATES in the floor, and contributes the WEAKEST value there
    # is. Excluding it means the floor can never report absence, so a card that
    # is mostly missing still badges as `measured` — which is the failure this
    # line exists to prevent. A metric nobody could read is weaker evidence than
    # one that was estimated, so it floors the card at `assumed`.
    if any(o.get("state") != "ok" for o in obs):
        floors.append("assumed")

    ns = tree.get("north_star")
    return {
        "ok": True, "realm": realm, "domain": domain, "since_days": since,
        "north_star": next((o for o in obs if o["metric"] == ns), None),
        "metrics": obs,
        "subtotals": [_subtotal(v, u) for u, v in sorted(by_unit.items()) if len(v) > 1],
        "coverage": {
            "metrics_ok": sum(1 for o in obs if o["state"] == "ok"),
            "metrics_declared": len(obs),
            **kpi_store.coverage(since),
        },
        "provenance_floor": (min(floors, key=lambda p: order.get(p, 0))
                             if floors else None),
        "gaps": [{"metric": o["metric"], "state": o["state"], "why": o.get("why") or ""}
                 for o in obs if o["state"] != "ok"],
        "tree": tree,
    }


# The off payload is a well-formed empty catalogue, NOT a 404. A page that gets
# a fault cannot tell the owner WHY it is empty, and "the feature is off" is the
# most likely reason it ever will be.
_OFF: dict = {"enabled": False, "axes": {}, "surfaces": [], "cells": [],
              "problems": [], "pending_grants": [],
              "coverage": {"days_expected": 0, "days_collected": 0,
                           "missing_days": []}}


@router.get("/api/domains")
async def api_domains():
    """The cells that exist. A LIST, never a rollup across them."""
    if not features.enabled("domains"):
        return dict(_OFF)

    def _payload() -> dict:
        # Collector coverage rides HERE, once. `kpi_store.coverage()` takes no
        # realm or domain — it reads one heartbeat ledger and answers "days the
        # collector ran", which is an estate fact. Returning it from a cell
        # would stamp one global number with a domain's name on nine cards.
        return {"enabled": True, **catalogue(), "coverage": kpi_store.coverage(30)}

    return await run_in_threadpool(_payload)


@router.get("/api/domains/series/{metric_id}")
async def api_domain_series(metric_id: str, since: int = 30, dim: str | None = None):
    return await run_in_threadpool(
        lambda: {"metric": metric_id, "dim": dim,
                 "points": kpi_store.series(metric_id, since, dim)})


@router.post("/api/domains/collect")
async def api_domains_collect():
    """Run one collection pass now. Idempotent per day."""
    return await run_in_threadpool(kpi_collect.collect_once)


@router.get("/api/domains/{realm}/{domain}")
async def api_domain_cell(realm: str, domain: str, since: int = 30):
    if not features.enabled("domains"):
        return {"ok": False, "error": "domains is off"}
    return await run_in_threadpool(cell, realm, domain, since)
