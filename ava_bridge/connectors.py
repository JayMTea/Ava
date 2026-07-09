"""Connector registry — Ava's pluggable integration layer.

A *connector* is a small manifest (`connector.yaml`) describing one thing Ava
monitors or drives: its health probe, its performance-log source, its egress
policy, and its agent actions. The dashboard service matrix, the performance
aggregator, and the left-rail app nav read this registry directly (drop in a
folder → they update); the agent's tools + egress policy are generated from the
same manifest by `ava connector tools|policies --write` (then `agent/install.sh`
deploys them). Either way: adding an app is a manifest, not core-code edits.

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

_ENV_VAR = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(val):
    """Expand ${VAR} references: first the built-in connector vars (AVA_HOME,
    AVA_LOGS, AVA_DATA, ROOT), then any remaining ${NAME} from the process
    environment. ${NAME:-default} falls back to `default` when NAME is unset
    or empty — so one manifest can serve bare metal and Docker.
    """
    if not isinstance(val, str):
        return val
    for k, v in _VARS.items():
        val = val.replace("${%s}" % k, v or "")
    val = _ENV_VAR.sub(
        lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), val)
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


# Manifests that failed to parse on the last real load — surfaced so a bad
# connector.yaml doesn't just vanish silently (Hub warning + `ava doctor`).
_load_errors: List[dict] = []


def _load_dir(base: str, errors: list | None = None) -> dict:
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
        except Exception as e:  # noqa: BLE001 — a bad manifest must not crash boot
            if errors is not None:
                errors.append({"id": name, "path": path, "error": str(e)})
            continue
        if not isinstance(m, dict):  # e.g. a YAML list/scalar — not a manifest
            if errors is not None:
                errors.append({"id": name, "path": path,
                               "error": "not a mapping (expected 'key: value' lines)"})
            continue
        m["id"] = m.get("id") or name
        out[m["id"]] = m
    return out


def _merge_all(errors: list | None = None) -> dict:
    merged: dict = {}
    merged.update(_load_dir(BUILTIN_DIR, errors))
    if os.path.realpath(USER_DIR) != os.path.realpath(BUILTIN_DIR):
        merged.update(_load_dir(USER_DIR, errors))  # user overrides built-in by id
    return merged


def load(force: bool = False) -> List[dict]:
    if not force and _cache["list"] is not None and time.time() - _cache["ts"] < 30:
        return _cache["list"]  # type: ignore[return-value]
    global _load_errors
    errors: list = []
    merged = _merge_all(errors)
    _load_errors = errors
    items = [m for m in merged.values() if m.get("enabled", True)]
    items.sort(key=lambda m: (0 if m.get("kind") == "core" else 1, m.get("id")))
    _cache.update(ts=time.time(), list=items)
    return items


def catalog() -> List[dict]:
    """Every manifest INCLUDING disabled ones, for management UIs (so a disabled
    connector can still be seen and re-enabled). Not cached — management is rare."""
    items = list(_merge_all().values())
    items.sort(key=lambda m: (0 if m.get("kind") == "core" else 1, m.get("id")))
    return items


def load_errors() -> List[dict]:
    """Manifests that failed to parse on the last real load (id / path / error)."""
    return list(_load_errors)


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


def chat_pickups() -> List[dict]:
    """Resolved chat-artifact pickup specs for connectors declaring a
    ``chat_pickup:`` block — after a turn used one of the named tools, the
    bridge polls the app's log for artifacts produced during the turn and
    attaches them as chat quick-cards. Manifest form:

        chat_pickup:
          after_actions: [preview]     # this connector's generated tools
          after_tools: [my_raw_tool]   # optional extra raw tool names
          path: "/api/log"             # GET base_url + path
          params: { kind: preview, limit: 12 }
          list_key: log                # JSON key holding rows (newest-first)
          ts_key: ts                   # row field compared to turn start
          fields: { persona: persona, url: url, seed: seed, theme: theme }

    ``url_prefix`` is `/apps/<id>` for iframe apps so app-relative artifact
    URLs resolve through the same-origin app proxy (cookie-authed).
    """
    out: List[dict] = []
    for m in load():
        cp = m.get("chat_pickup")
        if not isinstance(cp, dict):
            continue
        cid = m["id"]
        tools = [f"{cid}_{a}" for a in (cp.get("after_actions") or [])]
        tools += [str(t) for t in (cp.get("after_tools") or [])]
        base = _expand(cp.get("base")) or base_url(cid)
        if not tools or not base or not cp.get("path"):
            continue
        ui = m.get("ui") or {}
        out.append({
            "id": cid,
            "tools": tools,
            "url": base.rstrip("/") + str(cp["path"]),
            "params": cp.get("params") or {},
            "list_key": cp.get("list_key") or "log",
            "ts_key": cp.get("ts_key") or "ts",
            "fields": cp.get("fields") or {},
            "url_prefix": f"/apps/{cid}" if ui.get("embed") == "iframe" else "",
        })
    return out


def job_sources() -> List[dict]:
    """Live-job polling specs for connectors declaring a ``jobs:`` block —
    lets the ops dashboard attribute GPU spikes to named tasks. Manifest form:

        jobs:
          path: "/api/jobs"          # GET base_url + path
          params: { active: 1 }
          list_key: jobs             # rows carry kind/stage/progress
          engine: the GPU service
          labels: { generate: "Content render" }   # kind -> human label
    """
    out: List[dict] = []
    for m in load():
        jb = m.get("jobs")
        if not isinstance(jb, dict):
            continue
        base = _expand(jb.get("base")) or base_url(m["id"])
        if not base or not jb.get("path"):
            continue
        out.append({
            "id": m["id"],
            "url": base.rstrip("/") + str(jb["path"]),
            "params": jb.get("params") or {},
            "list_key": jb.get("list_key") or "jobs",
            "engine": jb.get("engine"),
            "labels": {str(k): str(v) for k, v in (jb.get("labels") or {}).items()},
        })
    return out


def model_hints() -> List[dict]:
    """Loaded-model attribution hints merged from all connectors:

        model_hints:
          - { match: [substr1, substr2], role: "Image rendering" }

    The hardware monitor checks these when labelling what a loaded model is FOR.
    """
    out: List[dict] = []
    for m in load():
        for h in (m.get("model_hints") or []):
            if isinstance(h, dict) and h.get("match") and h.get("role"):
                out.append({"match": [str(s).lower() for s in (h["match"] or [])],
                            "role": str(h["role"])})
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
    # Auto-allow the discovery bridge routes for a dynamic connector — the
    # HTTP list+call facade or a real MCP server (`mcp:` block) alike. For MCP
    # this IS the whole agent-side surface: the server itself stays outside
    # the sandbox; only these two policed routes are reachable.
    if _discover_spec(m) or _mcp_spec(m):
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
    """The connector's own host-local API base (for the generic action proxy).

    Resolution order: top-level `base_url` -> `ui.url` (an iframe app's own
    origin) -> the origin of `service.probe`.
    """
    from urllib.parse import urlparse
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if m.get("base_url"):
        return _expand(m["base_url"]).rstrip("/")
    ui_url = _expand((m.get("ui") or {}).get("url") or "")
    if ui_url:
        return ui_url.rstrip("/")
    probe = _expand((m.get("service") or {}).get("probe") or "")
    if probe:
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


# --- MCP servers -------------------------------------------------------------
# Wrap ANY Model Context Protocol server as a connector: the bridge speaks real
# MCP (JSON-RPC over Streamable HTTP or stdio, see ava_bridge/mcp_client.py),
# while the sandboxed agent reaches only the policed __tools/__call bridge
# routes its auto-generated egress policy allow-lists. Manifest form:
#   mcp:
#     url: "http://127.0.0.1:9200/mcp"      # http transport
#     token_env: MYMCP_TOKEN                 # optional bearer
#     # or a stdio server (spawned by the bridge, host-side):
#     command: ["npx", "-y", "@modelcontextprotocol/server-github"]
#     env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" }
def _mcp_spec(m: dict) -> dict | None:
    """A connector's MCP-server spec, or None."""
    mcp = m.get("mcp")
    if not isinstance(mcp, dict):
        return None
    url = _expand(mcp.get("url") or "") or None
    command = mcp.get("command")
    if isinstance(command, str):
        command = command.split()
    if not url and not command:
        return None
    transport = mcp.get("transport") or ("stdio" if command and not url else "http")
    env = {k: _expand(str(v)) for k, v in (mcp.get("env") or {}).items()} or None
    return {"transport": transport, "url": url, "command": command,
            "env": env, "token_env": mcp.get("token_env"),
            # Container isolation for a stdio server (filesystem-contained):
            # sandbox: docker · image: <base with the runtime> · network: none|bridge
            "sandbox": str(mcp.get("sandbox") or "none").lower(),
            "image": mcp.get("image") or "node:20-slim",
            "network": str(mcp.get("network") or "bridge")}


# --- Dynamic tool discovery -------------------------------------------------
# For apps that expose an MCP-style list+call API (e.g. a FastMCP facade). Ava's
# bridge lists the app's tools and forwards calls, so a whole dynamic tool set is
# bridged from a manifest with no per-tool wiring. Real MCP servers use the
# `mcp:` block above instead; both feed the same __tools/__call bridge routes.
def _discover_base(cid: str, spec: dict) -> str | None:
    return (spec.get("base") or base_url(cid) or "").rstrip("/") or None


def _discover_headers(spec: dict) -> dict:
    h = {"Content-Type": "application/json"}
    tenv = spec.get("token_env")
    if tenv and os.environ.get(tenv):
        h["Authorization"] = "Bearer " + os.environ[tenv]
    return h


def discover_tools(cid: str) -> dict:
    """The connector's dynamic tool list -> {"tools": [...]} or {"error"}.
    Routes to the real-MCP client when the manifest declares `mcp:`, else the
    HTTP list+call discover facade."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    mcp = _mcp_spec(m)
    if mcp:
        from . import mcp_client
        return mcp_client.list_tools(cid, mcp)
    spec = _discover_spec(m)
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
    m = {x["id"]: x for x in load()}.get(cid) or {}
    mcp = _mcp_spec(m)
    if mcp:
        from . import mcp_client
        return mcp_client.call_tool(cid, mcp, name, args)
    spec = _discover_spec(m)
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


def needs_confirm(cid: str, action: str) -> bool:
    """True if <action> on connector <cid> requires operator approval before it
    runs. Sources: connector-level ``confirm: true`` / ``confirm: [names]``, or
    ``confirm: true`` on the specific static action."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    c = m.get("confirm")
    if c is True:
        return True
    if isinstance(c, list) and action in c:
        return True
    for a in _static_actions(m):
        if a.get("id") == action and a.get("confirm"):
            return True
    return False


def all() -> List[dict]:  # noqa: A003 — deliberate registry accessor
    return load()
