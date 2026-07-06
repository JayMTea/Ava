"""Connector registry — Ava's pluggable integration layer.

A *connector* is a small manifest (`connector.yaml`) describing one thing Ava
monitors or drives: its health probe, its performance-log source, and (on the
roadmap) its egress policy + agent actions. The dashboard service matrix, the
performance aggregator, and future agent tooling all READ this registry — so
adding an app is dropping in a folder, not editing core code.

Manifests are discovered from (later overrides earlier):
  1. built-in   <repo>/connectors/<id>/connector.yaml    (shipped first-party)
  2. user       $AVA_HOME/connectors/<id>/connector.yaml  (user-added)

Manifest paths may use ${AVA_HOME} ${AVA_LOGS} ${AVA_DATA} ${ROOT} plus any
exported process env var (e.g. an app's own ${MYAPP_ROOT}). See docs/PACKAGING_PLAN.md §5.3.
"""
import os
import time
from typing import Dict, List

from . import config, settings

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None

BUILTIN_DIR = os.path.join(config.ROOT, "connectors")
USER_DIR = settings.home("connectors")

_VARS = {
    "AVA_HOME": str(settings.AVA_HOME),
    "AVA_LOGS": config.LOGS_DIR,
    "AVA_DATA": config.DATA_HOME,
    "ROOT": config.ROOT,
}

_cache: Dict[str, object] = {"ts": 0.0, "list": None}


import re as _re

_ENV_VAR = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(val):
    """Expand ${VAR} references: first the built-in connector vars (AVA_HOME,
    AVA_LOGS, AVA_DATA, ROOT), then any remaining ${NAME} from the process
    environment. So a manifest can reference any exported env var directly.
    """
    if not isinstance(val, str):
        return val
    for k, v in _VARS.items():
        val = val.replace("${%s}" % k, v or "")
    val = _ENV_VAR.sub(lambda m: os.environ.get(m.group(1), ""), val)
    return val


def _static_actions(m: dict) -> list:
    """The statically-declared actions for a connector, tolerating both shapes:
    the list form ``actions: [ {...} ]`` and the dict form
    ``actions: { static: [...], discover: {...} }``.
    """
    acts = m.get("actions")
    if isinstance(acts, list):
        return [a for a in acts if isinstance(a, dict)]
    if isinstance(acts, dict):
        return [a for a in (acts.get("static") or []) if isinstance(a, dict)]
    return []


def _discover_spec(m: dict) -> dict | None:
    """A connector's dynamic tool-discovery spec, or None. Manifest form:
        actions:
          discover: { base: "${APP_URL}", list: "/tools", call: "/call",
                      token_env: APP_TOKEN }
    ``base`` defaults to the connector's base_url(); list/call default to
    /tools and /call.
    """
    acts = m.get("actions")
    if isinstance(acts, dict) and isinstance(acts.get("discover"), dict):
        d = acts["discover"]
        return {"base": _expand(d.get("base")) or None,
                "list": d.get("list") or "/tools",
                "call": d.get("call") or "/call",
                "token_env": d.get("token_env")}
    return None


def _load_dir(base: str) -> dict:
    out: dict = {}
    if yaml is None or not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        if name.startswith(("_", ".")):
            continue
        path = os.path.join(base, name, "connector.yaml")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                m = yaml.safe_load(f) or {}
        except Exception:  # noqa: BLE001 — a bad manifest must not crash boot
            continue
        m["id"] = m.get("id") or name
        out[m["id"]] = m
    return out


def load(force: bool = False) -> List[dict]:
    if not force and _cache["list"] is not None and time.time() - _cache["ts"] < 30:
        return _cache["list"]  # type: ignore[return-value]
    merged: dict = {}
    merged.update(_load_dir(BUILTIN_DIR))
    if os.path.realpath(USER_DIR) != os.path.realpath(BUILTIN_DIR):
        merged.update(_load_dir(USER_DIR))  # user overrides built-in by id
    items = [m for m in merged.values() if m.get("enabled", True)]
    items.sort(key=lambda m: (0 if m.get("kind") == "core" else 1, m.get("id")))
    _cache.update(ts=time.time(), list=items)
    return items


def services() -> List[dict]:
    """Service-matrix entries for the dashboard (name / unit / probe / kind)."""
    out = []
    for m in load():
        s = m.get("service")
        if not s:
            continue
        out.append({
            "id": m["id"],
            "name": s.get("name", m.get("label", m["id"])),
            "unit": s.get("unit"),
            "probe": _expand(s.get("probe")),
            "kind": m.get("kind", "app"),
        })
    return out


def perf_sources() -> Dict[str, str]:
    """app-key -> performance.jsonl path, for the performance aggregator."""
    out: Dict[str, str] = {}
    for m in load():
        p = m.get("perf") or {}
        path = _expand(p.get("path"))
        if path:
            out[p.get("app") or m["id"]] = path
    return out


def actions() -> List[dict]:
    """All agent actions declared by connectors (id + description + connector).

    Includes statically-declared actions and, for connectors with a dynamic
    ``discover`` spec, a single synthetic ``<id>`` discovery bridge action.
    """
    out = []
    for m in load():
        for a in _static_actions(m):
            if a.get("id"):
                out.append({"connector": m["id"], "id": a["id"],
                            "description": a.get("description", "")})
        if _discover_spec(m):
            out.append({"connector": m["id"], "id": m["id"],
                        "description": f"Dynamic tools discovered from the {m.get('label', m['id'])} app",
                        "dynamic": True})
    return out


# Where sandboxed agent tools reach the host bridge (OpenClaw's host alias).
_BRIDGE_HOST = "host.openshell.internal"
_BRIDGE_PORT = 8096
_PRIVATE_IPS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
_TOOL_BINARIES = [{"path": "/usr/local/bin/node"}, {"path": "/usr/bin/node"},
                  {"path": "/usr/bin/curl"}]


def render_egress_policy(cid: str) -> dict | None:
    """Render a connector's `egress` block into an OpenClaw egress-policy dict
    (same shape as agent/policies/*.yaml). Returns None if it declares no egress.

    Manifest form:
        egress:
          routes: ["POST /internal/foo", "GET /internal/bar"]  # bridge routes
          hosts:  ["127.0.0.1:9000"]                            # direct endpoints
    Actions that declare a `path` are generic-proxy actions: they call
    ``/internal/connector/<id>/<action>`` on the bridge, so that route is
    allow-listed automatically.
    """
    m = {x["id"]: x for x in load()}.get(cid)
    if not m:
        return None
    eg = m.get("egress") or {}
    endpoints = []
    rules = []
    for r in (eg.get("routes") or []):
        parts = str(r).split()
        method, path = (parts[0], parts[1]) if len(parts) == 2 else ("GET", str(r))
        rules.append({"allow": {"method": method.upper(), "path": path}})
    # Auto-allow the generic proxy route for each generic-proxy action. Both
    # methods are allowed so GET-style tools work as well as POST ones (the route
    # is host-local + internal-token gated).
    for a in _static_actions(m):
        if a.get("id") and a.get("path"):
            path = f"/internal/connector/{cid}/{a['id']}"
            rules.append({"allow": {"method": "GET", "path": path}})
            rules.append({"allow": {"method": "POST", "path": path}})
    # Auto-allow the discovery bridge routes for a dynamic connector.
    if _discover_spec(m):
        rules.append({"allow": {"method": "GET",
                                "path": f"/internal/connector/{cid}/__tools"}})
        rules.append({"allow": {"method": "POST",
                                "path": f"/internal/connector/{cid}/__call"}})
    if rules:
        endpoints.append({
            "host": _BRIDGE_HOST, "port": _BRIDGE_PORT, "protocol": "rest",
            "enforcement": "enforce", "allowed_ips": list(_PRIVATE_IPS),
            "rules": rules,
        })
    for h in (eg.get("hosts") or []):
        host, _, port = str(h).partition(":")
        endpoints.append({"host": host, "port": int(port or 80),
                          "protocol": "rest", "enforcement": "enforce",
                          "allowed_ips": list(_PRIVATE_IPS)})
    if not endpoints:
        return None
    pname = f"ava-{cid}"
    return {
        "preset": {"name": pname,
                   "description": f"Auto-generated egress for the {m.get('label', cid)} connector"},
        "network_policies": {pname: {"name": pname, "endpoints": endpoints,
                                     "binaries": list(_TOOL_BINARIES)}},
    }


def find_action(cid: str, aid: str) -> dict | None:
    m = {x["id"]: x for x in load()}.get(cid) or {}
    for a in _static_actions(m):
        if a.get("id") == aid:
            return a
    return None


def base_url(cid: str) -> str | None:
    """The connector's own host-local API base (for the generic action proxy)."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if m.get("base_url"):
        return _expand(m["base_url"]).rstrip("/")
    probe = _expand((m.get("service") or {}).get("probe") or "")
    if probe:
        from urllib.parse import urlparse
        u = urlparse(probe)
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}"
    return None


def _auth_headers(cid: str) -> dict:
    """Bearer header for a connector's own API, from a top-level
    ``auth: { token_env: ENV }`` block. Empty if none declared."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    tenv = (m.get("auth") or {}).get("token_env")
    tok = os.environ.get(tenv, "") if tenv else ""
    return {"Authorization": "Bearer " + tok} if tok else {}


def call_action(cid: str, aid: str, args: dict | None) -> tuple:
    """Forward a generic-proxy action to the connector's own API (host-local).

    Returns (data, status). Used by the bridge route
    ``/internal/connector/<cid>/<aid>`` that the generated tools call. Supports
    real REST shapes: ``{tmpl}`` path params filled from args (and consumed),
    GET args sent as query params (else JSON body), the action's ``method``, an
    optional per-action ``base`` override, and the connector's bearer ``auth``.
    """
    a = find_action(cid, aid)
    if not a:
        return {"error": f"unknown action {cid}/{aid}"}, 404
    if not a.get("path"):
        return {"error": f"action {cid}/{aid} is not a generic-proxy action"}, 400
    base = _expand(a.get("base")) or base_url(cid)
    if not base:
        return {"error": f"connector {cid} has no base_url/probe to proxy to"}, 400
    args = dict(args or {})
    path = str(a.get("path"))
    for key in _re.findall(r"\{(\w+)\}", path):   # fill {tmpl} path params
        if key in args:
            path = path.replace("{%s}" % key, str(args.pop(key)))
    url = base.rstrip("/") + path
    method = str(a.get("method") or "POST").upper()
    headers = {"Content-Type": "application/json", **_auth_headers(cid)}
    import requests
    try:
        if method == "GET":
            r = requests.request("GET", url, params=args, headers=headers, timeout=60)
        else:
            r = requests.request(method, url, json=args, headers=headers, timeout=200)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"text": r.text[:4000]}
        return data, r.status_code
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}, 502


def render_tool(cid: str, action: dict) -> str:
    """Generate the .mjs source for a connector action's agent tool.

    The tool calls the bridge's generic proxy, which forwards to the connector's
    own API — so a user's app needs NO core-code changes, just a manifest.
    """
    aid = action["id"]
    name = f"{cid}_{aid}"
    desc = (action.get("description") or f"{aid} via the {cid} connector").replace("'", "\\'")
    props = action.get("input") or {}
    schema = {"type": "object", "properties": props, "additionalProperties": False}
    import json as _json
    return f"""// AUTO-GENERATED from connectors/{cid}/connector.yaml (action: {aid}).
// Regenerate with:  ava connector tools {cid} --write
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {{
  name: '{name}',
  description: '{desc}',
  inputSchema: {_json.dumps(schema, indent=2)},
  async handler(args, ctx) {{
    try {{
      const r = await fetch(`${{BRIDGE}}/internal/connector/{cid}/{aid}`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json',
                   'X-Ava-Internal-Token': ctx.internalToken || '' }},
        body: JSON.stringify(args || {{}}),
      }});
      const data = await r.json();
      return typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    }} catch (e) {{
      return `Error calling {name}: ${{e.message}}`;
    }}
  }},
}};
"""


# --- App surface (data-driven nav) ------------------------------------------
# A connector with a `ui:` block appears in Ava's left rail. `embed` selects how
# the shell renders it:
#   native  — a first-party React view compiled into the bundle (view= registry key)
#   iframe  — the app serves its own web UI; Ava reverse-proxies it same-origin
#             under /apps/<id>/ so it inherits the session cookie
#   none    — no UI; Ava renders a generic action console from the manifest actions
def apps() -> List[dict]:
    """Nav entries for connectors that declare a `ui:` block, sorted for display."""
    out = []
    for m in load():
        ui = m.get("ui")
        if not isinstance(ui, dict):
            continue
        embed = str(ui.get("embed") or "none").lower()
        out.append({
            "id": m["id"],
            "label": ui.get("label") or m.get("label") or m["id"],
            "icon": ui.get("icon") or "panel",
            "section": ui.get("section") or "apps",
            "order": ui.get("order", 100),
            "embed": embed,
            "view": ui.get("view"),
            "url": f"/apps/{m['id']}/" if embed == "iframe" else None,
            "has_api": bool(ui.get("api")),
        })
    out.sort(key=lambda a: (0 if a["section"] == "core" else 1, a["order"], a["id"]))
    return out


def app(cid: str) -> dict | None:
    """A connector's `ui:` block, url-expanded — for the same-origin iframe proxy."""
    ui = ({x["id"]: x for x in load()}.get(cid) or {}).get("ui")
    if not isinstance(ui, dict):
        return None
    return {"id": cid, "embed": str(ui.get("embed") or "none").lower(),
            "url": _expand(ui.get("url")), "api": ui.get("api")}


def app_api(cid: str) -> dict | None:
    """Resolved browser data-proxy config {base, prefix, token} from `ui.api`,
    or None if the connector declares no browser API. Powers /apps/<id>/api/*.
    """
    api = (({x["id"]: x for x in load()}.get(cid) or {}).get("ui") or {}).get("api")
    if not isinstance(api, dict):
        return None
    base = _expand(api.get("base")) or base_url(cid)  # default to the probe host
    if not base:
        return None
    token = os.environ.get(api["token_env"], "") if api.get("token_env") else ""
    return {"base": base.rstrip("/"),
            "prefix": str(api.get("prefix") or "").rstrip("/"),
            "token": token}


# --- Dynamic tool discovery -------------------------------------------------
# For apps that expose an MCP-style list+call API (e.g. a FastMCP facade). Ava's
# bridge lists the app's tools and forwards calls, so a whole dynamic tool set is
# bridged from a manifest with no per-tool wiring.
def _discover_base(cid: str, spec: dict) -> str | None:
    return (spec.get("base") or base_url(cid) or "").rstrip("/") or None


def _discover_headers(spec: dict) -> dict:
    h = {"Content-Type": "application/json"}
    tenv = spec.get("token_env")
    if tenv and os.environ.get(tenv):
        h["Authorization"] = "Bearer " + os.environ[tenv]
    return h


def discover_tools(cid: str) -> dict:
    """GET the connector's dynamic tool list -> {"tools": [...]} or {"error"}."""
    spec = _discover_spec({x["id"]: x for x in load()}.get(cid) or {})
    if not spec:
        return {"error": f"connector {cid} declares no discover spec"}
    base = _discover_base(cid, spec)
    if not base:
        return {"error": f"connector {cid} has no discover base_url"}
    import requests
    try:
        r = requests.get(base + spec["list"], headers=_discover_headers(spec), timeout=20)
        try:
            return r.json()
        except Exception:  # noqa: BLE001
            return {"error": f"{cid} discovery returned {r.status_code}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{cid} discovery unreachable: {e}"}


def call_discovered(cid: str, name: str, args: dict | None) -> tuple:
    """Invoke one dynamically-discovered tool by name. Returns (data, status)."""
    spec = _discover_spec({x["id"]: x for x in load()}.get(cid) or {})
    if not spec:
        return {"error": f"connector {cid} declares no discover spec"}, 400
    base = _discover_base(cid, spec)
    if not base:
        return {"error": f"connector {cid} has no discover base_url"}, 400
    import requests
    try:
        r = requests.post(base + spec["call"],
                          json={"name": name, "arguments": args or {}},
                          headers=_discover_headers(spec), timeout=180)
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"text": r.text[:4000]}
        return data, r.status_code
    except Exception as e:  # noqa: BLE001
        return {"error": f"{cid} call unreachable: {e}"}, 502


# --- Devices (inbound "app → Ava" channel) ---------------------------------
# A connector opts into pushing events to Ava with an `ingest:` block, and may
# mark itself `role: device` so the Devices view groups it. Both are additive:
# the pull path (actions.discover) is unchanged.
#   ingest:
#     enabled: true
#     channels:                 # OPTIONAL, purely descriptive (nicer Devices UI)
#       - { name: temperature, unit: "°C" }
#       - { name: motion, kind: event }
def ingest_enabled(cid: str) -> bool:
    """True if connector <cid> opted into the inbound event channel."""
    ing = ({x["id"]: x for x in load()}.get(cid) or {}).get("ingest")
    return bool(isinstance(ing, dict) and ing.get("enabled"))


def devices() -> List[dict]:
    """Registry rows for connectors that are devices — either ``role: device`` or an
    ``ingest`` block. Powers ``GET /api/devices`` and ``ava device list``."""
    out = []
    for m in load():
        ing = m.get("ingest") if isinstance(m.get("ingest"), dict) else None
        if m.get("role") != "device" and not ing:
            continue
        out.append({
            "id": m["id"],
            "label": m.get("label", m["id"]),
            "role": m.get("role", "device"),
            "ingest": bool(ing and ing.get("enabled")),
            "channels": [c for c in ((ing or {}).get("channels") or [])
                         if isinstance(c, dict)],
            "discover": bool(_discover_spec(m)),
            "probe": _expand((m.get("service") or {}).get("probe") or "") or None,
        })
    return out


def all() -> List[dict]:  # noqa: A003 — deliberate registry accessor
    return load()
