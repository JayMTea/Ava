"""The pull collector: dial each declared source once a day, record what came back.

PULL, NOT PUSH. Nothing is ever added to an app so that it can report into this
layer. Apps expose what they already expose; the collector reads it and stores
the result on its own side. That is the whole decoupling claim, and it is only
true while this direction is preserved — the moment an app imports a client to
push metrics, the layer has become a dependency of the thing it observes.

WHAT IT WILL NOT DO. It refuses any tool above the `sensitive` tier, and it
refuses anything requiring confirmation outright rather than asking. `gate()`
blocks until a human answers; a daily job that called it would park a thread and
raise a consent prompt in the middle of the night for a number nobody asked for.
A read that needs a person is simply not collectable, and is recorded as such.

ITS OWN TRAFFIC IS EXCLUDED FROM ITS OWN NUMBERS. Calls are tagged so that the
spend and energy this layer reports never include the cost of collecting them.
Visible in the ordinary performance views if it ever runs away; never folded
into the figure it publishes.

IN-PROCESS, NOT A TIMER. Same pattern as the other schedulers here, and for the
same reason: a system timer would need every app credential copied out of the
0600 secret store into a unit file.
"""
from __future__ import annotations

import threading
import time

from . import domains, kpi_read, kpi_store, settings

INTERVAL_S = 6 * 3600          # four attempts a day; a missed one self-heals
TIERS_OK = ("read", "sensitive")
_STARTED = False

# Prefix so the collector's own calls are identifiable in the perf log and can
# be excluded from the spend it reports.
TAG = "kpi"


def _tier_ok(cid: str, tool: str) -> tuple[bool, str]:
    """May the collector call this tool unattended?

    Note what this means in practice: nearly every metric worth having is
    `sensitive`, because a health reading, a portfolio value and a room's
    temperature all DISCLOSE something about the owner. Sensitive tools ask on
    first use, so until the owner has granted one, the collector must decline it
    — and say which grant is missing rather than reporting a vague failure.
    Auto-granting here would silence a consent prompt on exactly the data the
    prompt exists for.
    """
    from . import connectors, grants
    try:
        tier = (connectors.action_access(cid, tool) or "read").lower()
        if tier not in TIERS_OK:
            return False, f"refused: tier {tier}"
        if connectors.needs_confirm(cid, tool):
            if tier == "sensitive" and not grants.has(cid, tool):
                return False, f"awaiting a one-time grant for {cid}.{tool}"
            return False, f"needs confirmation every time ({cid}.{tool})"
    except Exception as e:                                  # noqa: BLE001
        return False, f"tier unresolved: {e}"
    return True, ""


def pending_grants() -> list[dict]:
    """Every sensitive tool the catalogue wants that has not been granted.

    This is the collector's ask-list: the owner reviews it once, grants what
    they are willing to have read on a schedule, and the rest stays refused.
    """
    from . import connectors, grants
    want: dict[tuple, dict] = {}
    for surface in domains.load()["surfaces"]:
        if surface.get("rollup") == "excluded":
            continue
        cid = surface.get("connector")
        if not cid:
            continue
        for metric in surface["metrics"]:
            src = metric.get("source") or {}
            if src.get("kind") not in ("facade", "mcp", "action"):
                continue
            tool = src.get("tool") or src.get("id") or ""
            try:
                tier = (connectors.action_access(cid, tool) or "read").lower()
                if tier == "sensitive" and not grants.has(cid, tool):
                    want[(cid, tool)] = {"connector": cid, "tool": tool,
                                         "tier": tier,
                                         "metrics": want.get((cid, tool), {}).get("metrics", [])}
                    want[(cid, tool)]["metrics"].append(metric["metric_id"])
            except Exception:                               # noqa: BLE001
                continue
    return sorted(want.values(), key=lambda r: (r["connector"], r["tool"]))


def _dim_values(surface: dict, metric: dict) -> list:
    """Values for a metric's dim, read from the surface's for_each source."""
    name = metric.get("dim")
    spec = ((surface.get("dims") or {}).get(name) or {}).get("for_each") or {}
    if not name or not spec:
        return [None]
    body, err = _http(surface, (spec.get("source") or {}).get("path") or "", {})
    if body is None:
        return []
    rd = spec.get("read") or {}
    rows, ok = kpi_read.resolve(body, rd.get("list") or "")
    if not ok or not isinstance(rows, list):
        return []
    field = rd.get("field")
    vals = [r.get(field) for r in rows if isinstance(r, dict) and r.get(field)] \
        if field else [r for r in rows if r]
    return sorted({v for v in vals if v}) or [None]


def _http(surface: dict, path: str, query: dict) -> tuple:
    """A declared-path read against a connector's own API. (body, error)."""
    from . import audit, connectors
    cid = surface.get("connector")
    if not cid:
        return None, "surface has no connector"
    tier = (surface.get("http_access") or {}).get(path)
    if tier is None:
        # An http source with no declared tier would let a hand-edited file aim
        # a daemon at any path on the box holding a full-session credential.
        return None, f"no declared tier for {path}"
    if tier not in TIERS_OK:
        return None, f"refused: tier {tier}"
    base = connectors.base_url(cid)
    if not base:
        return None, "connector has no base_url"
    token = ""
    m = {x["id"]: x for x in connectors.load()}.get(cid) or {}
    env = (m.get("auth") or {}).get("token_env")
    if env:
        token = settings.env_secret(env) or ""
    import requests
    t0 = time.time()
    try:
        r = requests.get(base + path, params=query or None, timeout=60,
                         headers={"Authorization": f"Bearer {token}"} if token else {},
                         allow_redirects=False)
        status = r.status_code
        body = r.json() if status < 400 else None
        err = "" if body is not None else f"HTTP {status}"
    except Exception as e:                                  # noqa: BLE001
        status, body, err = 0, None, type(e).__name__
    try:
        from . import app_perf
        app_perf.record_action(cid, f"{TAG}:{path}", time.time() - t0, status)
    except Exception:                                       # noqa: BLE001
        pass
    audit.record("egress", connector=cid, tool=f"{TAG}:{path}",
                 status=status, actor="owner")
    return body, err


def fetch(surface: dict, metric: dict, dim=None) -> tuple:
    """(body, error) for one metric, by source kind."""
    src = metric.get("source") or {}
    kind = src.get("kind")
    cid = surface.get("connector")

    if kind in (None, "manual"):
        return None, "manual — human-entered, never written by the agent"

    if kind == "internal":
        from . import dashboard
        fn = getattr(dashboard, str(src.get("fn") or ""), None)
        if not callable(fn):
            return None, f"no internal function {src.get('fn')!r}"
        try:
            return fn(**(src.get("args") or {})), ""
        except Exception as e:                              # noqa: BLE001
            return None, type(e).__name__

    if kind in ("facade", "mcp", "action"):
        if not cid:
            return None, "surface has no connector"
        tool = src.get("tool") or src.get("id") or ""
        ok, why = _tier_ok(cid, tool)
        if not ok:
            return None, why
        from . import internal
        args = dict(src.get("args") or {})
        if dim is not None:
            args = {k: (dim if "${" in str(v) else v) for k, v in args.items()} or args
        try:
            data, status = internal.call_for_owner(cid, tool, args)
        except Exception as e:                              # noqa: BLE001
            return None, type(e).__name__
        if status and int(status) >= 400:
            return None, f"HTTP {status}"
        return data, ""

    if kind == "http":
        q = {k: (dim if "${" in str(v) else v)
             for k, v in (src.get("query") or {}).items()}
        return _http(surface, src.get("path") or "", q)

    return None, f"unknown source kind {kind!r}"


def collect_once(day: str | None = None) -> dict:
    """One pass over every declared metric. Never raises."""
    day = day or kpi_store.today()
    cat = domains.load(force=True)
    ran = ok = 0
    errors: list[str] = []
    cache: dict = {}

    for surface in cat["surfaces"]:
        if surface.get("rollup") == "excluded":
            continue
        for metric in surface["metrics"]:
            # A ratio is computed from its stored legs at read time, so there is
            # nothing to dial and nothing to store for it here.
            if metric.get("agg") == "ratio_of_sums":
                continue
            dims = _dim_values(surface, metric) if metric.get("dim") else [None]
            for dim in dims:
                ran += 1
                try:
                    if metric.get("state") == "no_source" or \
                            metric.get("instrumentable") is False:
                        obs = kpi_read.observe(metric, None)
                    else:
                        key = (metric.get("metric_id"), str(metric.get("source")), dim)
                        if key not in cache:
                            cache[key] = fetch(surface, metric, dim)
                        body, err = cache[key]
                        if body is None:
                            obs = {"metric": metric["metric_id"],
                                   "unit": metric.get("unit"), "value": None,
                                   "n": None, "lo": None, "hi": None,
                                   "state": "no_source" if "manual" in err else "unavailable",
                                   "provenance": None, "why": err}
                        else:
                            obs = kpi_read.observe(metric, body)
                    kpi_store.write(day, obs, metric, dim=dim,
                                    src=str((metric.get("source") or {}).get("kind") or ""))
                    if obs["state"] == "ok":
                        ok += 1
                except Exception as e:                      # noqa: BLE001
                    errors.append(f"{metric.get('metric_id')}: {type(e).__name__}")

    kpi_store.heartbeat(ran, ok, errors)
    return {"day": day, "metrics": ran, "ok": ok, "errors": errors}


def _loop() -> None:
    while True:
        try:
            from . import features
            if features.enabled("domains"):
                collect_once()
        except Exception:      # noqa: BLE001 — a collector fault never takes the bridge down
            pass
        time.sleep(INTERVAL_S)


def start_scheduler() -> None:
    """Start the in-process collector once (idempotent)."""
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="kpi-collect").start()
