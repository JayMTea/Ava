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
        lambda m: settings.env_secret(m.group(1)) or (m.group(2) or ""), val)
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
    """Service-matrix entries for the dashboard (name / unit / probe / kind).
    `feature` optionally names the features.* flag that governs this service —
    when the user turned that feature off, a dead probe means "off by choice",
    not "down", and the dashboard must not paint it red."""
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
            "feature": s.get("feature"),
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
    # is host-local + internal-token gated). Meta-tool connectors skip this: the
    # agent reaches them only through __tools/__call below.
    if not meta_static(m):
        for a in _static_actions(m):
            if a.get("id") and a.get("path"):
                path = f"/internal/connector/{cid}/{a['id']}"
                rules.append({"allow": {"method": "GET", "path": path}})
                rules.append({"allow": {"method": "POST", "path": path}})
    # Auto-allow the discovery bridge routes for a dynamic connector — the
    # HTTP list+call facade or a real MCP server (`mcp:` block) alike. For MCP
    # this IS the whole agent-side surface: the server itself stays outside
    # the sandbox; only these two policed routes are reachable. Large static
    # connectors use the same two routes via their find/call meta tools.
    if _discover_spec(m) or _mcp_spec(m) or meta_static(m):
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


def auth_env(m: dict) -> str | None:
    """The env-var NAME a connector uses for its app credential, across every
    manifest shape — REST ``auth``, an ``mcp`` server, a ``discover`` facade, or
    a browser data-proxy (``ui.api``). Returns the first declared, or ``None`` if
    the app needs no auth. The Setup Hub uses this to show whether a credential is
    saved and to store one keyed by name — the value itself never enters the
    manifest (see settings.env_secret / the Ava-never-has-passwords invariant)."""
    if not isinstance(m, dict):
        return None
    auth = m.get("auth")
    if isinstance(auth, dict) and auth.get("token_env"):
        return str(auth["token_env"])
    mcp = m.get("mcp")
    if isinstance(mcp, dict) and mcp.get("token_env"):
        return str(mcp["token_env"])
    acts = m.get("actions")
    if isinstance(acts, dict) and isinstance(acts.get("discover"), dict) \
            and acts["discover"].get("token_env"):
        return str(acts["discover"]["token_env"])
    ui = m.get("ui")
    if isinstance(ui, dict) and isinstance(ui.get("api"), dict) \
            and ui["api"].get("token_env"):
        return str(ui["api"]["token_env"])
    return None


def _auth_headers(cid: str) -> dict:
    """Bearer header for a connector's own API, from a top-level
    ``auth: { token_env: ENV }`` block. Empty if none declared."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    tenv = (m.get("auth") or {}).get("token_env")
    tok = settings.env_secret(tenv) if tenv else None
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


# --- Meta tools (dynamic tool loading) ----------------------------------------
# Past ~15 tools, per-action schemas bloat the agent's context on every turn and
# tool selection degrades. Large static connectors therefore swap N generated
# tools for TWO meta tools — <cid>_find_tool (search the action set) and
# <cid>_call (invoke one by name) — the same shape mcp/discover connectors use,
# feeding the same policed __tools/__call bridge routes and approvals gate.
META_TOOLS_MIN = 16


def meta_static(m: dict) -> bool:
    """True when a static connector is big enough to use find/call meta tools."""
    return len([a for a in _static_actions(m)
                if a.get("id") and a.get("path")]) >= META_TOOLS_MIN


def _static_tool_schemas(m: dict) -> list[dict]:
    """Static generic-proxy actions in discovered-tool shape, so __tools serves
    a large static connector exactly like an MCP/discover one."""
    out = []
    for a in _static_actions(m):
        if not (a.get("id") and a.get("path")):
            continue
        method = str(a.get("method") or "POST").upper()
        desc = (a.get("description") or "").strip()
        out.append({"name": a["id"],
                    "description": f"[{method} {a.get('path')}] {desc}".strip(),
                    "inputSchema": {"type": "object",
                                    "properties": a.get("input") or {}},
                    "access": _infer_access(a)})
    return out


def _filter_tools(res: dict, query: str, limit: int) -> dict:
    """Keyword-filter and cap a {"tools": [...]} result (find_tool's search).
    `total` reports the pre-cap match count so the agent knows to refine."""
    tools = res.get("tools") if isinstance(res, dict) else None
    if not isinstance(tools, list):
        return res
    terms = [t for t in str(query or "").lower().split() if t]
    if terms:
        def hits(t: dict) -> int:
            blob = f"{t.get('name', '')} {t.get('description', '')}".lower()
            return sum(1 for term in terms if term in blob)
        tools = sorted((t for t in tools if hits(t)), key=hits, reverse=True)
    total = len(tools)
    if limit and limit > 0:
        tools = tools[:limit]
    return {**res, "tools": tools, "total": total}


def render_find_tool(cid: str) -> str:
    """The .mjs source for <cid>_find_tool: search the connector's tool set."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    label = (m.get("label") or cid).replace("'", "\\'")
    acts = [a for a in _static_actions(m) if a.get("id") and a.get("path")]
    if acts:
        caps = sorted({action_capability(a) for a in acts})
        hint = f"{len(acts)} actions across: {', '.join(caps[:12])}"
    else:
        hint = "its tools are discovered live"
    desc = (f"Search the {label} app\\'s available actions ({hint}). "
            f"ALWAYS call this first with a few keywords for what you want to do, "
            f"then invoke the chosen action with {cid}_call.")
    return f"""// AUTO-GENERATED from connectors/{cid}/connector.yaml (meta: find_tool).
// Regenerate with:  ava connector tools {cid} --write
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {{
  name: '{cid}_find_tool',
  description: '{desc}',
  inputSchema: {{
    type: 'object',
    properties: {{
      query: {{ type: 'string', description: 'keywords for the action you need (e.g. "create persona")' }},
      limit: {{ type: 'number', description: 'max results (default 12)' }},
    }},
    additionalProperties: false,
  }},
  async handler(args, ctx) {{
    try {{
      const q = encodeURIComponent(args.query || '');
      const n = Number(args.limit) > 0 ? Number(args.limit) : 12;
      const r = await fetch(`${{BRIDGE}}/internal/connector/{cid}/__tools?q=${{q}}&limit=${{n}}`, {{
        headers: {{ 'X-Ava-Internal-Token': ctx.internalToken || '' }},
      }});
      const data = await r.json();
      return JSON.stringify(data, null, 2);
    }} catch (e) {{
      return `Error calling {cid}_find_tool: ${{e.message}}`;
    }}
  }},
}};
"""


def render_call_tool(cid: str) -> str:
    """The .mjs source for <cid>_call: invoke one tool found via find_tool."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    label = (m.get("label") or cid).replace("'", "\\'")
    return f"""// AUTO-GENERATED from connectors/{cid}/connector.yaml (meta: call).
// Regenerate with:  ava connector tools {cid} --write
const BRIDGE = process.env.AVA_BRIDGE_URL || 'http://host.openshell.internal:8096';

export default {{
  name: '{cid}_call',
  description: 'Run one {label} action by name — find the name and its input schema with {cid}_find_tool first.',
  inputSchema: {{
    type: 'object',
    properties: {{
      name: {{ type: 'string', description: 'the action name returned by {cid}_find_tool' }},
      arguments: {{ type: 'object', description: 'arguments matching that action\\'s inputSchema' }},
    }},
    required: ['name'],
    additionalProperties: false,
  }},
  async handler(args, ctx) {{
    try {{
      const r = await fetch(`${{BRIDGE}}/internal/connector/{cid}/__call`, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json',
                   'X-Ava-Internal-Token': ctx.internalToken || '' }},
        body: JSON.stringify({{ name: args.name, arguments: args.arguments || {{}} }}),
      }});
      const data = await r.json();
      return typeof data === 'string' ? data : JSON.stringify(data, null, 2);
    }} catch (e) {{
      return `Error calling {cid}_call: ${{e.message}}`;
    }}
  }},
}};
"""


_undeployed_cache: Dict[str, object] = {"ts": 0.0, "list": None}


def undeployed() -> List[dict]:
    """Enabled connectors with an agent surface whose generated tools are NOT
    on disk (the Hub's "tools stale" state — so definitely not in the sandbox).
    Feeds the chat-turn awareness note: Ava must tell the user to Deploy rather
    than shrug or pretend. Cached ~30s; tool_files renders per connector."""
    now = time.time()
    if _undeployed_cache["list"] is not None and now - float(_undeployed_cache["ts"]) < 30:
        return _undeployed_cache["list"]  # type: ignore[return-value]
    root = os.path.join(config.ROOT, "agent", "mcp_server_content", "connectors")
    out = []
    for m in load():
        files = tool_files(m["id"])
        if not files:
            continue
        # NB: an explicit loop — this module defines all() (the registry
        # accessor), which shadows the builtin.
        for t in files:
            if not os.path.exists(os.path.join(root, m["id"], t["name"])):
                out.append({"id": m["id"], "label": m.get("label", m["id"]),
                            "tools": len(files)})
                break
    _undeployed_cache.update(ts=now, list=out)
    return out


def tool_files(cid: str) -> List[dict]:
    """The canonical agent-tool set for a connector: find/call meta tools for
    dynamic (mcp/discover) or large static connectors, else one tool per
    generic-proxy action. Every generator/checker derives from this ONE list."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if _discover_spec(m) or _mcp_spec(m) or meta_static(m):
        return [{"name": f"{cid}_find_tool.mjs", "source": render_find_tool(cid)},
                {"name": f"{cid}_call.mjs", "source": render_call_tool(cid)}]
    return [{"name": f"{cid}_{a['id']}.mjs", "source": render_tool(cid, a)}
            for a in _static_actions(m) if a.get("id") and a.get("path")]


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
            # None (not "grid") when undeclared: the frontend's appIcon() then
            # picks a STABLE per-app glyph by hashing the id, so apps that don't
            # declare ui.icon still differ from each other — mirrors how
            # appAccent() varies the color. Passing "grid" here would defeat that.
            "icon": ui.get("icon") or None,
            "color": str(ui["color"]) if ui.get("color") else None,
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
    token = (settings.env_secret(api["token_env"]) or "") if api.get("token_env") else ""
    return {"base": base.rstrip("/"),
            "prefix": str(api.get("prefix") or "").rstrip("/"),
            "token": token}


def app_token(cid: str) -> str:
    """The saved credential Ava presents to an embedded app on the owner's behalf,
    resolved from the connector's declared ``token_env`` (any manifest shape, see
    ``auth_env``) via ``settings.env_secret``. ``''`` when the app declares no
    credential or none is saved yet.

    This is what lets "connect the app once" mean the human never re-authenticates
    to the app's own UI: the same-origin app proxy injects this as the bearer when
    the browser sends none (a fresh embed has no app session of its own). Resolved
    only here on the bridge when building the egress request — never handed to the
    browser or inherited by the sandboxed agent (Ava-never-has-passwords)."""
    m = {x["id"]: x for x in load()}.get(cid)
    if not m:
        return ""
    name = auth_env(m)
    return (settings.env_secret(name) or "") if name else ""


# --- MCP servers -------------------------------------------------------------
# Wrap ANY Model Context Protocol server as a connector: the bridge speaks real
# MCP (JSON-RPC over Streamable HTTP or stdio, see ava_bridge/mcp_client.py),
# while the sandboxed agent reaches only the policed __tools/__call bridge
# routes its auto-generated egress policy allow-lists. Manifest form:
#   mcp:
#     url: "http://127.0.0.1:9200/mcp"      # http transport
#     token_env: MYMCP_TOKEN                 # optional bearer
#     # transport: sse                       # legacy HTTP+SSE (e.g. Home
#     #                                      # Assistant's MCP Server); inferred
#     #                                      # when the url path ends in /sse
#     # or a stdio server (spawned by the bridge, host-side):
#     command: ["npx", "-y", "@modelcontextprotocol/server-github"]
#     env: { GITHUB_TOKEN: "${GITHUB_TOKEN}" }
def _mcp_spec(m: dict) -> dict | None:
    """A connector's MCP-server spec, or None."""
    mcp = m.get("mcp")
    if not isinstance(mcp, dict):
        return None
    url = _expand(mcp.get("url") or "") or None
    if url and not url.startswith(("http://", "https://")):
        url = None      # a ${VAR} left unset — the manifest is inert until configured
    command = mcp.get("command")
    if isinstance(command, str):
        command = command.split()
    if not url and not command:
        return None
    transport = mcp.get("transport") or (
        "stdio" if command and not url
        else "sse" if url and url.split("?")[0].rstrip("/").endswith("/sse")
        else "http")
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
    tok = settings.env_secret(spec.get("token_env"))
    if tok:
        h["Authorization"] = "Bearer " + tok
    return h


def discover_tools(cid: str, query: str = "", limit: int = 0) -> dict:
    """The connector's dynamic tool list -> {"tools": [...]} or {"error"}.
    Routes to the real-MCP client when the manifest declares `mcp:`, else the
    HTTP list+call discover facade, else (a large static connector's find_tool)
    the manifest's own actions. `query`/`limit` filter the result server-side
    so the agent's find_tool returns a shortlist, not the whole set."""
    m = {x["id"]: x for x in load()}.get(cid) or {}

    def _remember(res: dict) -> dict:
        """Write the declared access tiers through to the JIT cache (best-effort
        — discovery must never fail on cache IO) before any query filtering."""
        try:
            tools = res.get("tools") if isinstance(res, dict) else None
            if isinstance(tools, list):
                from . import tools_cache
                tools_cache.update(cid, tools)
        except Exception:  # noqa: BLE001
            pass
        return res

    mcp = _mcp_spec(m)
    if mcp:
        from . import mcp_client
        return _filter_tools(_remember(mcp_client.list_tools(cid, mcp)), query, limit)
    spec = _discover_spec(m)
    if not spec:
        if _static_actions(m):
            return _filter_tools({"tools": _static_tool_schemas(m)}, query, limit)
        return {"error": f"connector {cid} declares no discover spec"}
    base = _discover_base(cid, spec)
    if not base:
        return {"error": f"connector {cid} has no discover base_url"}
    import requests
    try:
        r = requests.get(base + spec["list"], headers=_discover_headers(spec), timeout=20)
        try:
            return _filter_tools(_remember(r.json()), query, limit)
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
        # A large static connector's <cid>_call: dispatch to the declared action
        # (same generic proxy — and the caller already ran the approvals gate).
        if find_action(cid, name):
            return call_action(cid, name, args)
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


_TIERS = ("read", "write", "destructive", "physical")


def _infer_access(a: dict) -> str:
    """The access tier of one action: explicit ``access:`` wins, else inferred —
    GET/HEAD -> read, DELETE or a *delete* id/path -> destructive, else write.
    ``physical`` (moves something in the real world — relay, lock, valve) is
    never inferred: it must be declared, and it can never be granted away."""
    acc = str(a.get("access") or "").lower()
    if acc in _TIERS:
        return acc
    method = str(a.get("method") or "POST").upper()
    if method in ("GET", "HEAD"):
        return "read"
    if method == "DELETE" or "delete" in f"{a.get('id', '')} {a.get('path', '')}".lower():
        return "destructive"
    return "write"


def _dynamic_access(m: dict, action: str) -> str:
    """Tier for a dynamically-discovered tool (MCP / discover facade), which has
    no static entry to infer from. The manifest classifies them by name pattern:

        dynamic_access:
          GetLiveContext: read      # fnmatch patterns, first match wins
          "*": physical

    Unmatched tools fall back to ``physical`` on a ``role: device`` connector
    (a device's unknown verbs are presumed to move something until the author
    says otherwise) and ``write`` everywhere else."""
    import fnmatch
    da = m.get("dynamic_access")
    if isinstance(da, dict):
        for pattern, tier in da.items():
            t = str(tier or "").lower()
            if t in _TIERS and fnmatch.fnmatch(action, str(pattern)):
                return t
    # The app's own ava-tools/1 declaration (write-through cache filled by
    # discover_tools). Manifest patterns above outrank it — the operator's word
    # beats the app's self-report; it outranks the blanket fallback below.
    from . import tools_cache
    acc = tools_cache.access(str(m.get("id") or ""), action)
    if acc:
        return acc
    return "physical" if m.get("role") == "device" else "write"


def action_access(cid: str, action: str) -> str:
    """Access tier for <action> on <cid>. Static actions infer from the
    declaration; dynamic tools (MCP / discovered) classify via the manifest's
    ``dynamic_access`` patterns (see _dynamic_access for the defaults)."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    for a in _static_actions(m):
        if a.get("id") == action:
            return _infer_access(a)
    return _dynamic_access(m, action)


def _author_confirm(m: dict, action: str) -> bool:
    """Explicit author gate: connector-level ``confirm: true`` / ``confirm:
    [names]`` or ``confirm: true`` on the static action."""
    c = m.get("confirm")
    if c is True:
        return True
    if isinstance(c, list) and action in c:
        return True
    for a in _static_actions(m):
        if a.get("id") == action and a.get("confirm"):
            return True
    return False


def needs_confirm(cid: str, action: str) -> bool:
    """True if <action> on connector <cid> requires operator approval before it
    runs (JIT consent — docs/dev/CONNECTOR_DISCOVERY_UX_PLAN.md):

    - an explicit author ``confirm:`` always asks and can't be granted away;
    - ``read`` actions run silently;
    - ``destructive`` actions ask every time;
    - ``physical`` actions (real-world actuation) ask every time — never
      grantable, so a lock can't become a one-tap-then-silent action;
    - ``write`` actions ask on first use — an "Always allow" grant
      ($AVA_HOME/connector_grants.yaml) silences later calls."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    if _author_confirm(m, action):
        return True
    tier = action_access(cid, action)
    if tier == "read":
        return False
    if tier in ("destructive", "physical"):
        return True
    from . import grants
    return not grants.has(cid, action)


def grantable(cid: str, action: str) -> bool:
    """True when the approval prompt may offer "Always allow": write-tier only
    (read never asks; destructive and physical are never grantable; author
    confirm sticks)."""
    m = {x["id"]: x for x in load()}.get(cid) or {}
    return not _author_confirm(m, action) and action_access(cid, action) == "write"


def action_capability(a: dict) -> str:
    """The UI grouping for one action: explicit ``capability:`` wins, else the
    first meaningful path segment (api/v1 prefixes skipped) — /api/persona/{key}
    groups under "persona"."""
    cap = str(a.get("capability") or "").strip().lower()
    if cap:
        return cap
    segs = [s for s in str(a.get("path") or "").split("/") if s and not s.startswith("{")]
    if len(segs) > 1 and segs[0] in ("api", "v1", "v2", "rest"):
        segs = segs[1:]
    return segs[0] if segs else "general"


def all() -> List[dict]:  # noqa: A003 — deliberate registry accessor
    return load()
