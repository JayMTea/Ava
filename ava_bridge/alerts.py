"""In-app alert engine for the Ava dashboard.

Evaluates the rules in ``config/alerts.yaml`` against a flat metrics dict
(assembled by ``dashboard.build_alert_metrics``) on every tick. Handles debounce
(``for``) and anti-flap (``cooldown``), tracks active-alert state, and reports
raise/clear transitions so the SSE stream can push ``alert.raise`` / ``alert.clear``.

Read-only, single-user, in-process — no external paging (a webhook sink can be
added later without touching callers).
"""
import os
import threading
import time
from typing import Any, Dict, List

from . import config

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

_RULES_PATH = os.path.join(config.ROOT, "config", "alerts.yaml")
_OPS = {
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}

# Fallback rules if the YAML is missing/unparseable (keeps alerts working).
_DEFAULT_RULES = [
    {"id": "gpu_temp", "metric": "gpu_temp", "op": ">", "value": 85,
     "for": 30, "cooldown": 120, "severity": "critical",
     "message": "GPU temperature high"},
    {"id": "service_down", "metric": "service_down_count", "op": ">", "value": 0,
     "for": 0, "cooldown": 60, "severity": "critical",
     "message": "A monitored service is down"},
]

_lock = threading.Lock()
# rule id -> {"breach_since": float|None, "active": bool, "cleared_at": float,
#             "raised_at": float, "value": Any}
_state: Dict[str, dict] = {}
_rules_cache = {"rules": None, "mtime": 0.0}

# App-sourced alerts pushed in from outside the metric-rule engine (e.g. a device
# connector's notify event). Momentary by nature, so each carries an expiry and is
# surfaced only until it lapses — the SSE stream then diffs it out as alert.clear.
# id -> {"expires": float, "alert": dict}
_external: Dict[str, dict] = {}
_EXTERNAL_TTL = float(os.environ.get("AVA_ALERT_EXTERNAL_TTL", "90"))


def push_external(alert_id: str, message: str, severity: str = "warn",
                  ttl: float | None = None, **extra) -> None:
    """Inject a short-lived alert from outside the metric-rule engine.

    Appears in ``active()`` / ``evaluate()`` (and thus the ops SSE stream and the
    dashboard alerts panel) until ``ttl`` seconds elapse. Use a unique ``alert_id``
    per occurrence so each push raises exactly once.
    """
    now = time.time()
    with _lock:
        _external[alert_id] = {
            "expires": now + (ttl if ttl is not None else _EXTERNAL_TTL),
            "alert": {"id": alert_id, "severity": severity, "message": message,
                      "value": extra.get("value"), "metric": extra.get("metric"),
                      "since": now, "source": "device"},
        }


def _active_external(now: float) -> List[dict]:
    """Non-expired external alerts; prunes lapsed ones as a side effect."""
    live = []
    for aid in list(_external.keys()):
        entry = _external[aid]
        if entry["expires"] <= now:
            del _external[aid]
        else:
            live.append(entry["alert"])
    return live


def _load_rules() -> List[dict]:
    try:
        mtime = os.path.getmtime(_RULES_PATH)
    except OSError:
        mtime = 0.0
    if _rules_cache["rules"] is not None and _rules_cache["mtime"] == mtime:
        return _rules_cache["rules"]
    rules = _DEFAULT_RULES
    if yaml is not None:
        try:
            with open(_RULES_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            r = data.get("rules")
            if isinstance(r, list) and r:
                rules = r
        except Exception:  # noqa: BLE001
            pass
    _rules_cache.update(rules=rules, mtime=mtime)
    return rules


def evaluate(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate all rules against `metrics`.

    Returns {"active": [...], "raised": [...], "cleared": [...]} where each alert
    is {id, severity, metric, value, threshold, op, message, since}.
    """
    now = time.time()
    raised, cleared = [], []
    with _lock:
        for rule in _load_rules():
            rid = rule.get("id") or rule.get("metric")
            metric = rule.get("metric")
            op = _OPS.get(rule.get("op", ">"))
            thresh = rule.get("value")
            cur = metrics.get(metric)
            st = _state.setdefault(
                rid, {"breach_since": None, "active": False,
                      "cleared_at": 0.0, "raised_at": 0.0, "value": None})

            breaching = (cur is not None and op is not None
                         and _num(cur) is not None and op(_num(cur), thresh))

            if breaching:
                if st["breach_since"] is None:
                    st["breach_since"] = now
                st["value"] = cur
                held = now - st["breach_since"] >= float(rule.get("for", 0))
                past_cooldown = now - st["cleared_at"] >= float(rule.get("cooldown", 0))
                if held and not st["active"] and past_cooldown:
                    st["active"] = True
                    st["raised_at"] = now
                    raised.append(_alert(rule, cur, st))
            else:
                st["breach_since"] = None
                if st["active"]:
                    st["active"] = False
                    st["cleared_at"] = now
                    cleared.append(_alert(rule, cur, st))

        active = [_alert(r, _state[r.get("id") or r.get("metric")]["value"],
                         _state[r.get("id") or r.get("metric")])
                  for r in _load_rules()
                  if _state.get(r.get("id") or r.get("metric"), {}).get("active")]
        active += _active_external(now)
    return {"active": active, "raised": raised, "cleared": cleared}


def active() -> List[dict]:
    """Currently-active alerts (no re-evaluation)."""
    with _lock:
        out = []
        for rule in _load_rules():
            rid = rule.get("id") or rule.get("metric")
            st = _state.get(rid)
            if st and st.get("active"):
                out.append(_alert(rule, st.get("value"), st))
        out += _active_external(time.time())
    return out


def _alert(rule: dict, value, st: dict) -> dict:
    return {
        "id": rule.get("id") or rule.get("metric"),
        "severity": rule.get("severity", "warn"),
        "metric": rule.get("metric"),
        "value": value,
        "threshold": rule.get("value"),
        "op": rule.get("op", ">"),
        "message": rule.get("message", rule.get("metric")),
        "since": st.get("raised_at") or None,
    }


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
