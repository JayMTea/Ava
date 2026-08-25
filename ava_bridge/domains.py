"""The domain catalogue: which surfaces exist, and what each one measures.

WHAT A SURFACE IS. A connected app is not the unit of grouping — a *surface* is.
One app whose parts serve different purposes (a personal ledger and a public
product, say) declares several surfaces, each landing in its own cell of the
instance's axes. That is what makes multi-membership expressible without
multi-valued tags: the membership is single-valued, the app just has more than
one surface.

WHERE THE VOCABULARY LIVES — AND WHY NOT HERE. This module ships no axis values,
no surface ids and no metric ids. Every one of them is read from the instance:
the axes and any non-connector surfaces from ``<AVA_HOME>/domains.yaml``, and a
connector's own surfaces from an ``x_domains:`` block in its manifest. Ava's
loader ignores unknown ``x_``-prefixed manifest keys outright, so that block is
inert to every other part of the system and needs no core change to carry.

A fork defines its own taxonomy; the product imposes none. This mirrors how
skill categories are handled, for the same reason.

QUARANTINE, NOT COLLAPSE. A malformed surface is dropped with a named problem
and the rest of the catalogue still loads. The alternative — one bad block
taking the whole catalogue down — is how a hand-edited file becomes a boot
hazard.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from . import settings

_CACHE: dict[str, Any] = {}
_LOCK = threading.Lock()
_TTL_S = 30.0

# Aggregations that combine a metric across TIME. There is deliberately no
# across-surface combiner: summing two surfaces means asserting their units are
# the same thing, which is the assertion this layer exists to refuse.
AGGS = ("last", "sum", "avg", "min", "max", "ratio_of_sums")

# A closed unit vocabulary is what makes "never total across units" checkable
# rather than aspirational.
UNITS = ("usd_cents", "pct", "per_100", "count", "ratio", "g", "kcal", "kg",
         "ms", "bpm", "mmhg", "minutes", "hours", "ppm", "celsius", "score_0_100")

PROVENANCE = ("measured", "derived", "assumed")
STATES = ("ok", "insufficient", "unavailable", "no_source")


def catalogue_path() -> str:
    return os.path.join(settings.home(), "domains.yaml")


def _load_yaml(path: str) -> tuple[dict, str]:
    """(document, problem). A parse error is reported, never raised: this file is
    hand-edited, and a syntax slip must not take the bridge down with it."""
    try:
        import yaml
    except ImportError:                                     # pragma: no cover
        return {}, "PyYAML is not installed"
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        return (doc if isinstance(doc, dict) else {}), ""
    except FileNotFoundError:
        return {}, ""
    except Exception as e:                                  # noqa: BLE001
        return {}, f"{os.path.basename(path)}: {e}"


def _norm_metric(raw: dict, surface_id: str, owner: str, problems: list) -> dict | None:
    """Validate one metric declaration. Returns None if it cannot be trusted."""
    mid = str(raw.get("id") or "").strip()
    if not mid:
        problems.append(f"{surface_id}: a metric has no id")
        return None
    full = f"{owner}.{mid}"

    unit = raw.get("unit")
    if unit is not None and unit not in UNITS:
        problems.append(f"{full}: unit {unit!r} is not in the closed vocabulary")
        return None
    agg = raw.get("agg")
    if agg is not None and agg not in AGGS:
        problems.append(f"{full}: agg {agg!r} is not one of {AGGS}")
        return None
    declares = raw.get("declares")
    if declares is not None and declares not in PROVENANCE:
        problems.append(f"{full}: declares {declares!r} is not one of {PROVENANCE}")
        return None
    state = raw.get("state")
    if state is not None and state not in STATES:
        problems.append(f"{full}: state {state!r} is not one of {STATES}")
        return None

    # A ratio that does not name both legs cannot be re-aggregated over a window
    # without averaging daily ratios, which is the arithmetic this layer refuses.
    if agg == "ratio_of_sums" and not (raw.get("num") and raw.get("den")):
        problems.append(f"{full}: agg ratio_of_sums needs both `num` and `den`")
        return None

    return {**raw, "id": mid, "metric_id": full, "surface": surface_id,
            "owner": owner}


def _collect_surfaces(block: dict, owner: str, extra: dict,
                      axes: dict, problems: list) -> list[dict]:
    realms = (axes.get("realm") or {}).get("order") or []
    domains = (axes.get("domain") or {}).get("order") or []
    by_surface: dict[str, list] = {}
    for raw in block.get("metrics") or []:
        if not isinstance(raw, dict):
            continue
        by_surface.setdefault(str(raw.get("surface") or ""), []).append(raw)

    out: list[dict] = []
    for s in block.get("surfaces") or []:
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or "").strip()
        if not sid:
            problems.append(f"{owner}: a surface has no id")
            continue
        realm, domain = s.get("realm"), s.get("domain")
        if realms and realm not in realms:
            problems.append(f"{sid}: realm {realm!r} is not in axes.realm.order")
            continue
        if domains and domain not in domains:
            problems.append(f"{sid}: domain {domain!r} is not in axes.domain.order")
            continue
        metrics = [m for m in
                   (_norm_metric(r, sid, owner, problems) for r in by_surface.get(sid, []))
                   if m]
        out.append({"id": sid, "realm": realm, "domain": domain, "owner": owner,
                    "label": s.get("label") or sid,
                    "rollup": s.get("rollup"), "metrics": metrics, **extra})
    return out


def _build() -> dict:
    problems: list[str] = []
    inst, prob = _load_yaml(catalogue_path())
    if prob:
        problems.append(prob)
    axes = inst.get("axes") or {}
    surfaces: list[dict] = []

    # 1. Surfaces a connector declares about itself.
    try:
        from . import connectors
        manifests = connectors.load()
    except Exception as e:                                  # noqa: BLE001
        manifests = []
        problems.append(f"connectors unavailable: {e}")
    for m in manifests:
        block = m.get("x_domains")
        if not isinstance(block, dict):
            continue
        surfaces += _collect_surfaces(
            block, m.get("id") or "", 
            {"http_access": block.get("http_access") or {},
             "dims": block.get("dims") or {}, "connector": m.get("id")},
            axes, problems)

    # 2. Surfaces the instance declares that no connector owns (things that are
    #    real parts of the estate but are not connected apps).
    for s in inst.get("surfaces") or []:
        if not isinstance(s, dict):
            continue
        owner = str(s.get("id") or "").split("/")[0]
        surfaces += _collect_surfaces({"surfaces": [s],
                                       "metrics": [{**m, "surface": s.get("id")}
                                                   for m in (s.get("metrics") or [])
                                                   if isinstance(m, dict)]},
                                      owner, {"connector": None}, axes, problems)

    seen: dict[str, str] = {}
    for s in surfaces:
        if s["id"] in seen:
            problems.append(f"{s['id']}: declared twice ({seen[s['id']]} and {s['owner']})")
        seen[s["id"]] = s["owner"]

    known = {m["metric_id"] for s in surfaces for m in s["metrics"]}
    trees = {}
    for key, t in (inst.get("trees") or {}).items():
        if not isinstance(t, dict):
            continue
        unresolved = [mid for role in ("north_star", "component", "influences", "guardrails")
                      for mid in ([t.get(role)] if role == "north_star" else (t.get(role) or []))
                      if mid and mid not in known]
        for mid in unresolved:
            problems.append(f"tree {key}: {mid} does not resolve to a declared metric")
        trees[key] = {**t, "unresolved": unresolved}

    return {"axes": axes, "surfaces": surfaces, "trees": trees,
            "problems": problems, "built": time.time()}


def load(force: bool = False) -> dict:
    """The catalogue, cached briefly. Never raises."""
    with _LOCK:
        fresh = _CACHE.get("doc")
        if fresh and not force and (time.time() - fresh["built"]) < _TTL_S:
            return fresh
        try:
            doc = _build()
        except Exception as e:                              # noqa: BLE001
            doc = {"axes": {}, "surfaces": [], "trees": {},
                   "problems": [f"catalogue failed to build: {e}"],
                   "built": time.time()}
        _CACHE["doc"] = doc
        return doc


def surfaces() -> list[dict]:
    return load()["surfaces"]


def cells() -> list[tuple]:
    """Distinct (realm, domain) pairs that carry at least one included surface."""
    out = []
    for s in surfaces():
        if s.get("rollup") == "excluded":
            continue
        key = (s.get("realm"), s.get("domain"))
        if key not in out:
            out.append(key)
    return out


def metrics_for(realm: str, domain: str) -> list[dict]:
    return [m for s in surfaces()
            if s.get("realm") == realm and s.get("domain") == domain
            and s.get("rollup") != "excluded"
            for m in s["metrics"]]


def all_metrics() -> list[dict]:
    return [m for s in surfaces() if s.get("rollup") != "excluded"
            for m in s["metrics"]]


def metric(metric_id: str) -> dict | None:
    return next((m for m in all_metrics() if m["metric_id"] == metric_id), None)


def tree(realm: str, domain: str) -> dict:
    return load()["trees"].get(f"{realm}/{domain}") or {}
