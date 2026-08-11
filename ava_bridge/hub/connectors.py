"""Setup -> Connectors panel: the whole life of a connected app.

Add one (paste an address, Detect, save), grant it scoped actions, hold its
secret, deploy its generated tools and egress policy into the sandbox, watch it
live, restyle it, edit its manifest, delete it. Sixteen handlers — the largest
panel in the hub, and the one a forker touches first, since "add your own app
with a manifest and no core-code changes" is the SDK's whole claim.

Two behaviours here are security-relevant and easy to undo by accident:

_probe defaults to running a pasted stdio command inside a Docker sandbox and
FAILS CLOSED with {"needs": "docker"} when no runtime is present. It once built
its spec with no `sandbox` key at all, so mcp_client took the uncontained branch
and handed an arbitrary pasted command the bridge's entire os.environ, including
ANTHROPIC_API_KEY.

The manifest routes never write a rejected block back. A connector with one bad
field is quarantined in memory and reported through load_errors(), because this
panel IS the page you fix a broken manifest on — refusing to render it would
lock the owner out of the only repair tool.
"""
import os
import re
import shutil

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import uuid

import yaml as _yaml

# `connectors` was ALSO bound as `_c` three lines below this, and both aliases were
# live — 36 uses of one, 10 of the other, overlapping on the same private helpers
# (_static_actions, _mcp_spec). A contributor grepping this 50 KB file for
# `connectors._infer_access` found nothing, because that call site said
# `connectors._infer_access`. Residue from the hub_api.py -> hub/ split.
from .. import (audit, connectors, grants, perf_mgmt, provision_job, runtime,
                settings, tools_cache)
from .. import devices as _devices
from .. import internal
from .. import mcp_client

router = APIRouter()

def _proxy_actions(m: dict) -> list[dict]:
    """Generic-proxy actions (id + path) — the ones that get a generated tool
    and an auto-allowed egress route."""
    return [a for a in connectors._static_actions(m) if a.get("id") and a.get("path")]

def _slim_tools(tools) -> list[dict]:
    out = []
    for t in (tools or [])[:60]:
        if isinstance(t, dict) and t.get("name"):
            out.append({"name": str(t["name"])[:80],
                        "description": str(t.get("description") or "")[:200]})
    return out

_OPENAPI_SCALARS = {"string", "integer", "number", "boolean"}


def _inputs_from_operation(path_item: dict, op: dict, path: str) -> dict:
    """The `input:` schema for one OpenAPI operation, from its `parameters` and a
    JSON `requestBody`.

    Without this, `_actions_from_openapi` emitted no `input:` at all, and
    `render_tool` then generated a tool declaring
    `{"properties": {}, "additionalProperties": false}` — so the agent could not
    pass arguments, and a templated path like `GET /api/items/{id}` was requested
    with the literal `{id}` still in it. Worse, the behaviour was asymmetric: at
    >= META_TOOLS_MIN actions the meta-tool path builds schemas via
    `_static_tool_schemas`, which omits `additionalProperties`, so arguments DID
    work — the same manifest behaved differently either side of 16 actions.

    Path- and operation-level `parameters` are merged (path-level applies to every
    operation in the item, per the spec). Only scalar leaf types are surfaced:
    the generated tool passes GET args as query params and POST args as a JSON
    body, so a nested object would not round-trip usefully.
    """
    props: dict = {}
    required: list[str] = []
    params = []
    for src in (path_item.get("parameters"), op.get("parameters")):
        if isinstance(src, list):
            params.extend(p for p in src if isinstance(p, dict))
    for p in params:
        name = str(p.get("name") or "").strip()
        if not name or p.get("in") not in ("path", "query"):
            continue
        sch = p.get("schema") if isinstance(p.get("schema"), dict) else {}
        typ = str(sch.get("type") or "string")
        props[name] = {"type": typ if typ in _OPENAPI_SCALARS else "string"}
        if p.get("description"):
            props[name]["description"] = str(p["description"])[:120]
        # A templated path segment is structurally required — the request cannot be
        # built without it — whatever the spec claims.
        if p.get("required") or ("{" + name + "}") in path:
            required.append(name)
    body = op.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content") if isinstance(body.get("content"), dict) else {}
        js = content.get("application/json") if isinstance(content, dict) else None
        sch = (js or {}).get("schema") if isinstance(js, dict) else None
        if isinstance(sch, dict) and isinstance(sch.get("properties"), dict):
            breq = sch.get("required") if isinstance(sch.get("required"), list) else []
            for name, ps in sch["properties"].items():
                if not isinstance(ps, dict):
                    continue
                typ = str(ps.get("type") or "string")
                if typ not in _OPENAPI_SCALARS:
                    continue        # nested objects/arrays do not round-trip
                props[str(name)] = {"type": typ}
                if ps.get("description"):
                    props[str(name)]["description"] = str(ps["description"])[:120]
                if name in breq:
                    required.append(str(name))
    if not props:
        return {}
    out: dict = {"properties": props}
    if required:
        out["required"] = sorted(set(required))
    return out


def _actions_from_openapi(spec: dict, limit: int = 50) -> list[dict]:
    """Turn an OpenAPI/Swagger paths object into ready-to-use connector actions
    so a plain web app is zero-config: the user reviews a pre-filled list instead
    of hand-typing every endpoint. Each operation -> {id, method, path, description}.
    Destructive verbs (DELETE, or a *delete* path) default to confirm=True."""
    if not isinstance(spec, dict):
        return []
    paths = spec.get("paths")
    if not isinstance(paths, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            m = method.upper()
            if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                continue
            if not isinstance(op, dict):
                op = {}
            # A clean, human-readable id from the method + last path segments,
            # dropping params and a leading api/v1 prefix (e.g. GET /api/persona/{key}
            # -> get_persona). The verbose operationId only feeds the description.
            segs = [s for s in str(path).split("/") if s and not s.startswith("{")]
            if len(segs) > 1 and segs[0] in ("api", "v1", "v2", "rest"):
                segs = segs[1:]
            tail = "_".join(segs[-2:]) if segs else "root"
            aid = re.sub(r"[^a-z0-9]+", "_", f"{m}_{tail}".lower()).strip("_")[:32] or f"{m.lower()}_{len(out)}"
            base, n = aid, 2
            while aid in seen:
                aid = f"{base[:29]}_{n}"
                n += 1
            seen.add(aid)
            desc = str(op.get("summary") or op.get("description")
                       or op.get("operationId") or "").strip()[:200]
            # Access tier drives JIT consent (read silent / write asks once /
            # destructive asks always). confirm mirrors destructive for the 🔒.
            if m in ("GET", "HEAD"):
                access = "read"
            elif m == "DELETE" or "delete" in str(path).lower():
                access = "destructive"
            else:
                access = "write"
            entry = {"id": aid, "method": m, "path": str(path),
                     "description": desc, "access": access,
                     "confirm": access == "destructive"}
            inp = _inputs_from_operation(ops, op, str(path))
            if inp:
                entry["input"] = inp
            out.append(entry)
            if len(out) >= limit:
                return out
    return out


@router.get("/connectors/{cid}/grants")
def grants_list(cid: str):
    """One connector's permission sheet (settings page): every static action —
    plus, for an MCP/discover connector, its live dynamic tools (best-effort;
    a down server just means the live set is omitted) — with capability group,
    access tier, and grant state."""
    granted = grants.for_connector(cid)
    m = {x["id"]: x for x in connectors.all()}.get(cid) or {}
    acts = []
    for a in connectors._static_actions(m):
        if not a.get("id"):
            continue
        acts.append({"id": a["id"], "access": connectors._infer_access(a),
                     "capability": connectors.action_capability(a),
                     "method": str(a.get("method") or "POST").upper(),
                     "path": a.get("path", ""),
                     "description": a.get("description", ""),
                     "granted": a["id"] in granted,
                     "grantable": connectors.grantable(cid, a["id"])})
    if connectors._mcp_spec(m) or connectors._discover_spec(m):
        seen = {a["id"] for a in acts}
        for t in (connectors.discover_tools(cid).get("tools") or []):
            name = t.get("name")
            if not name or name in seen:
                continue
            acts.append({"id": name, "access": connectors.action_access(cid, name),
                         "capability": "live tools", "method": "", "path": "",
                         "description": (t.get("description") or "")[:160],
                         "granted": name in granted,
                         "grantable": connectors.grantable(cid, name)})
    return {"grants": granted, "actions": acts}

@router.post("/connectors/{cid}/grants/{action}")
def grants_add(cid: str, action: str):
    """Pre-grant from the settings page — same rule as the "Always allow"
    prompt: write tier only; destructive/author-gated actions can't be granted."""
    if not connectors.grantable(cid, action):
        return JSONResponse({"ok": False, "error":
                             "this action asks every time and can't be always-allowed"},
                            status_code=400)
    grants.grant(cid, action)
    return {"ok": True}

@router.delete("/connectors/{cid}/grants/{action}")
def grants_revoke(cid: str, action: str):
    return {"ok": grants.revoke(cid, action)}

# --------------------------------------------------------------------------- #
# Connectors — list generation state + generate/preview tools & egress policy
# --------------------------------------------------------------------------- #
@router.get("/connectors")
def list_connectors():
    """The connector **management** view for the Setup Hub: every manifest via
    `connectors.catalog()` (INCLUDING disabled ones, so they can be re-enabled),
    each with its edit/deploy state (`enabled`, `builtin`, `has_tools`,
    `has_policy`, `renders_policy`) plus any malformed-manifest `errors`.

    Distinct from `/api/ops/connectors` (`dashboard.connectors_info`), which is the
    read-only dashboard **telemetry** view: enabled connectors only, with health /
    perf / egress-count fields for the Ops panel. Two endpoints, two audiences —
    management vs monitoring — so neither UI carries the other's fields."""
    pol_dir = settings.generated_policy_dir()
    tool_root = settings.connector_tools_dir()
    user_root = settings.home("connectors")
    out = []
    # Collect the validation errors for EVERY manifest, disabled included. This
    # page is the one built to show a broken manifest, and it was the one seeing
    # them unvalidated: `_validate` is a no-op unless an errors list is passed,
    # and `catalog()` passed none.
    errors: list = []
    for m in connectors.catalog(errors):  # includes disabled, so they can be re-enabled
        cid = m["id"]
        try:
            out.append(_connector_row(m, cid, pol_dir, tool_root, user_root))
        except Exception as e:  # noqa: BLE001
            # A malformed manifest must never take down THIS page: it holds the
            # manifest editor and the error list, so breaking it means the only
            # screen that could fix the connector is the one the connector broke.
            # connectors._validate is the actual fix; this is the guarantee that
            # a field it does not yet know about cannot repeat the lockout.
            out.append({"id": cid, "label": cid, "kind": "app", "error": str(e),
                        "actions": 0, "enabled": False, "builtin": True})
    # Whether an embedded app's UI is isolated on its own browser origin. It ships
    # HERE, and not only on /api/apps, because this is the page where an owner
    # decides to give an app a sidebar tile — the moment the answer changes what
    # they are agreeing to. `apps_origin.warning()` has existed since the hole was
    # documented and had no consumer anywhere in the frontend; AppFrame.tsx even
    # claimed "Setup says so", and Setup did not.
    from .. import apps_origin as _apps_origin
    # Merge the fresh catalog errors with the last load's, deduped: a manifest
    # that fails to PARSE never reaches catalog() at all, so `load_errors()` is
    # still the only place those appear.
    seen = {(e.get("path"), e.get("error")) for e in errors}
    errors += [e for e in connectors.load_errors()
               if (e.get("path"), e.get("error")) not in seen]
    return {"connectors": out, "errors": errors,
            "apps_origin": _apps_origin.warning()}

def _connector_row(m: dict, cid: str, pol_dir: str, tool_root: str,
                   user_root: str) -> dict:
    actions = _proxy_actions(m)
    expected = connectors.tool_files(cid)
    has_tools = bool(expected) and all(
        os.path.exists(os.path.join(tool_root, cid, t["name"]))
        for t in expected)
    # Credential state — the env-var NAME the app authenticates with (if any),
    # and whether a value is available (a real env var OR one saved once via
    # the Hub) so the UI can show "credential saved" and never re-prompt on
    # redeploy. The value itself is never returned.
    auth = connectors.auth_env(m)
    return {
        "id": cid,
        "label": m.get("label", cid),
        "kind": m.get("kind", "app"),
        "actions": len(actions),
        "mcp": connectors._mcp_spec(m) is not None,
        "discover": connectors._discover_spec(m) is not None,
        # HOW its tools arrive — mcp | discover | rest | none. The honest
        # name for the wire protocol; `mcp` here means a real MCP server,
        # not "has tools". See connectors.transport().
        "transport": connectors.transport(m),
        # The two connect surfaces (the doctrine: an app is a UI, a tool
        # facade, or both — never raw endpoints): ui block => APP tile.
        "app": isinstance(m.get("ui"), dict),
        # Device identity: `role: device` and/or an inbound `ingest:` block.
        # Setup groups by what a connector IS to the owner, and a device's
        # defining trait is that it pushes to Ava rather than being visited.
        "role": str(m.get("role") or "") or None,
        "ingest": connectors.ingest_enabled(cid),
        # Rail identity, for the Appearance picker (null = uses the auto-pick).
        "icon": (m.get("ui") or {}).get("icon") if isinstance(m.get("ui"), dict) else None,
        "color": str((m["ui"]).get("color")) if isinstance(m.get("ui"), dict) and (m["ui"]).get("color") else None,
        "has_policy": os.path.exists(os.path.join(pol_dir, f"{cid}.yaml")),
        "has_tools": has_tools,
        "renders_policy": connectors.render_egress_policy(cid) is not None,
        "enabled": bool(m.get("enabled", True)),  # from the manifest
        # User-added connectors are editable/removable; shipped ones are read-only.
        "builtin": not os.path.isdir(os.path.join(user_root, cid)),
        # Credential status (never the value): the env-var name it uses, and
        # whether a credential is available / saved in the store.
        "auth_env": auth,
        "auth_set": settings.has_env_secret(auth) if auth else False,
        "auth_stored": settings.env_secret_stored(auth) if auth else False,
    }

@router.get("/connectors/{cid}/live")
def connector_live(cid: str):
    """Actually talk to <cid> right now and report what came back.

    This is what keeps the Setup UI's transport label honest. `list_connectors`
    reads the manifest — it can only tell you what a connector *claims*. This
    performs the real handshake (MCP `initialize` + `tools/list`, or a GET on
    the ava-tools/1 facade) so the UI can say "MCP · 18 tools" only when Ava
    genuinely spoke MCP to it a moment ago, and show the transport error when
    it didn't.

    ``verified`` is the distinction that matters: true means we round-tripped
    just now; false means the count is declared in the manifest (a `rest`
    connector has nothing to hand-shake with) and no promise is being made.
    """
    m = {x["id"]: x for x in connectors.catalog()}.get(cid)
    if not m:
        return {"ok": False, "error": f"no connector {cid}"}
    kind = connectors.transport(m)
    base = {"ok": True, "transport": kind, "verified": False,
            "tools": None, "error": None}

    if not m.get("enabled", True):
        # Never dial a connector the owner switched off — "disabled" is an
        # answer, not a failure.
        return {**base, "ok": False, "error": "disabled"}

    if kind == "rest":
        return {**base, "tools": len(connectors._static_actions(m))}
    if kind == "none":
        return {**base, "tools": 0}

    r = connectors.discover_tools(cid)
    if isinstance(r, dict) and r.get("error"):
        return {**base, "ok": False, "error": str(r["error"])[:300]}
    return {**base, "verified": True, "tools": len(r.get("tools") or [])}

@router.post("/connectors/{cid}/secret")
async def connector_set_secret(cid: str, body: dict):
    """Save (or clear) a connected app's credential VALUE so redeploy never
    re-prompts. Keyed by the manifest's ``token_env`` NAME, the value goes to the
    server-side secret store (``$AVA_HOME/secrets/env/<NAME>``, 0600) — never the
    manifest, never the agent. An empty value clears it. A real env var of the
    same name still wins at read time."""
    m = {x["id"]: x for x in connectors.catalog()}.get(cid)
    if not m:
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    name = connectors.auth_env(m)
    if not name:
        return JSONResponse({"ok": False, "error":
                             "This app declares no auth token. Add "
                             "`auth: { token_env: NAME }` via Edit (or reconnect "
                             "with a token) first."}, status_code=400)
    value = str(body.get("value") or "")
    if value.strip():
        settings.set_env_secret(name, value)
    else:
        settings.clear_env_secret(name)
    return {"ok": True, "auth_env": name,
            "auth_set": settings.has_env_secret(name),
            "auth_stored": settings.env_secret_stored(name)}

# --------------------------------------------------------------------------- #
# Connectors — scaffold a new manifest from the GUI form
# --------------------------------------------------------------------------- #
_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

def _probe(url: str, command: str, token_env: str | None,
           token_value: str | None = None, *, sandbox: str | None = None,
           image: str | None = None, allow_unsandboxed: bool = False) -> dict:
    """Figure out how to talk to an app so the user doesn't have to classify it:
    try MCP (a start command = stdio, or the URL = MCP-over-HTTP), then a
    discovery endpoint (GET <url>/tools). Returns the detected kind + the tools
    we found, or kind='rest' when nothing is auto-discoverable (the caller then
    asks the user to declare actions — the one thing we can't infer).

    ``token_value`` is a credential the owner just pasted in the connect form (not
    yet saved) so the detection call can authenticate; otherwise fall back to the
    saved/env value resolved from ``token_env``."""
    cid = "__probe_" + uuid.uuid4().hex[:6]
    # Credential for the detection request: the just-pasted value wins, else the
    # value already saved for this env-var name (or a real env var).
    tok = (token_value or "").strip() or settings.env_secret(token_env)

    # 1) A start command is unambiguously an MCP stdio server.
    if command:
        # Detection RUNS the command. It used to run it bare, with the bridge's
        # whole environment, before the owner had made any isolation choice — the
        # isolation checkbox only rendered after the probe returned. So "paste its
        # address, click Detect" executed an internet-sourced command on the host.
        # Default to contained, and refuse rather than silently downgrade.
        want_sandbox = "docker" if sandbox is None else sandbox
        if want_sandbox == "docker" and not shutil.which("docker"):
            if not allow_unsandboxed:
                return {"ok": False, "needs": "docker", "kind": "mcp",
                        "error": "This is a start command, so detecting it means "
                                 "RUNNING it. Docker isn't available to contain "
                                 "it. Install Docker, or re-run detection with "
                                 "'run it directly on this host' if you trust "
                                 "this command."}
            want_sandbox = "none"
        spec = {"transport": "stdio", "url": None, "command": command.split(),
                "env": None, "token_env": token_env,
                "sandbox": want_sandbox, "image": image or None}
        try:
            res = mcp_client.list_tools(cid, spec)
        finally:
            mcp_client.reset(cid)
        if res.get("tools"):
            return {"ok": True, "kind": "mcp", "transport": "stdio",
                    "tools": _slim_tools(res["tools"])}
        return {"ok": True, "kind": "unknown", "tools": [],
                "detail": res.get("error", "that command didn't expose MCP tools")}

    if not url:
        return {"ok": False, "error": "give a web address or a start command"}

    # Why each discovery step gave up. Every one of them used to be a bare
    # `except: pass`, so a TLS failure, an expired token and "this app simply has
    # no tool list" all ended at the same sentence: "No tools to auto-discover —
    # tell Ava what this app can do." The owner was then asked to hand-write
    # actions against a host that never answered, and the real cause — wrong port,
    # self-signed cert, missing token — was never printed anywhere.
    trouble: list[str] = []
    seen: dict[str, bool | int] = {"http": False, "auth": 0}

    def _note(step: str, e: Exception) -> None:
        import requests as _rq
        if isinstance(e, _rq.exceptions.SSLError):
            why = "TLS failed (self-signed or wrong host name?)"
        elif isinstance(e, _rq.exceptions.ConnectTimeout):
            why = "timed out connecting"
        elif isinstance(e, _rq.exceptions.Timeout):
            why = "timed out"
        elif isinstance(e, _rq.exceptions.ConnectionError):
            why = "could not connect (nothing listening, or the name did not resolve)"
        else:
            why = f"{type(e).__name__}: {e}".split("\n")[0][:160]
        trouble.append(f"{step}: {why}")

    def _saw(r) -> None:
        """Record that SOMETHING answered, and whether it demanded credentials."""
        seen["http"] = True
        if r.status_code in (401, 403):
            seen["auth"] = r.status_code

    def _serves_html() -> bool:
        """Does the app have its own web UI at the base URL? Drives the
        "Add it to Ava's sidebar" offer (ui.embed: iframe — the embedded-app
        tier from CONNECTOR_SDK.md §3)."""
        try:
            import requests
            r = requests.get(url, timeout=5, headers={"Accept": "text/html"})
            _saw(r)
            ct = r.headers.get("content-type", "")
            return r.status_code < 400 and (
                "text/html" in ct
                or r.text[:200].lstrip().lower().startswith(("<!doctype", "<html")))
        except Exception as e:  # noqa: BLE001
            _note("the base address", e)
            return False

    # 1.5) The app may self-describe: GET /.well-known/ava.json (SDK §5) names
    # the app, its health probe, its UI, and where the facade routes live — the
    # most authoritative signal, so it goes first and prefills the whole form.
    try:
        import requests
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        wk = requests.get(url.rstrip("/") + "/.well-known/ava.json",
                          headers=headers, timeout=5)
        _saw(wk)
        meta = wk.json() if wk.status_code == 200 else None
        if isinstance(meta, dict) and str(meta.get("facade", "")).startswith("ava-tools/"):
            list_path = "/" + str(meta.get("tools") or "/tools").lstrip("/")
            call_path = "/" + str(meta.get("call") or "/call").lstrip("/")
            r = requests.get(url.rstrip("/") + list_path, headers=headers, timeout=8)
            data = r.json()
            tools = data.get("tools") if isinstance(data, dict) else None
            if isinstance(tools, list) and tools:
                health = str(meta.get("health") or "").strip()
                # Self-described token name (SDK §3 Single sign-on): prefill the
                # connect form so the owner pastes only the VALUE, and Ava then
                # presents it to the agent tools AND the embedded UI.
                auth_meta = meta.get("auth") if isinstance(meta.get("auth"), dict) else {}
                token_env_hint = str(auth_meta.get("token_env") or "").strip() or None
                return {"ok": True, "kind": "discover", "tools": _slim_tools(tools),
                        "label": str(meta.get("label") or "").strip()[:60] or None,
                        "health": (url.rstrip("/") + "/" + health.lstrip("/")) if health else None,
                        "discover": {"list": list_path, "call": call_path},
                        "token_env": token_env_hint,
                        "has_ui": bool(meta.get("ui")) or _serves_html()}
    except Exception as e:  # noqa: BLE001
        _note("/.well-known/ava.json", e)

    # 2) Try MCP over HTTP at the URL.
    spec = {"transport": "http", "url": url, "command": None, "env": None,
            "token_env": token_env}
    try:
        res = mcp_client.list_tools(cid, spec)
    finally:
        mcp_client.reset(cid)
    if res.get("tools"):
        return {"ok": True, "kind": "mcp", "transport": "http",
                "tools": _slim_tools(res["tools"])}
    if res.get("error"):
        # MCP transport failures never passed through `unreachable()`, so they
        # carried no code and no text anywhere the owner could see them.
        trouble.append(f"MCP over HTTP: {str(res['error']).splitlines()[0][:160]}")

    # 3) Try a discovery endpoint (our list+call facade / FastMCP-style).
    try:
        import requests
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        r = requests.get(url.rstrip("/") + "/tools", headers=headers, timeout=8)
        _saw(r)
        data = r.json()
        tools = data.get("tools") if isinstance(data, dict) else (
            data if isinstance(data, list) else None)
        if isinstance(tools, list) and tools:
            return {"ok": True, "kind": "discover", "tools": _slim_tools(tools),
                    "has_ui": _serves_html()}
    except Exception as e:  # noqa: BLE001
        _note("GET /tools", e)

    # 3.5) Most web apps (FastAPI/Swagger/anything OpenAPI) publish a
    # machine-readable spec. Read it and pre-fill the actions so the user
    # reviews a list instead of hand-typing endpoints — zero-config.
    try:
        import requests
        headers = {"Authorization": "Bearer " + tok} if tok else {}
        for suffix in ("/openapi.json", "/swagger.json", "/openapi"):
            try:
                r = requests.get(url.rstrip("/") + suffix, headers=headers, timeout=8)
            except Exception as e:  # noqa: BLE001 — try the next well-known path
                _note(f"GET {suffix}", e)
                continue
            _saw(r)
            if r.status_code != 200:
                continue
            try:
                spec = r.json()
            except Exception:  # noqa: BLE001 — not JSON, not a spec
                continue
            actions = _actions_from_openapi(spec)
            if actions:
                return {"ok": True, "kind": "rest", "tools": [], "actions": actions,
                        "has_ui": _serves_html(),
                        "detail": f"Read its API — found {len(actions)} actions."}
    except Exception as e:  # noqa: BLE001
        _note("the OpenAPI spec", e)

    # 4) Nothing was discoverable. WHY decides what the owner is asked to do next.
    has_ui = _serves_html()
    if not seen["http"]:
        # Nothing at this address ever answered. Asking the owner to hand-write
        # actions against it would be asking them to describe a host that is not
        # there.
        #
        # Deliberately NO `error_code`: the guided-fix codes in fixes.ts all
        # resolve to a connector's own row in Setup, and this connector does not
        # exist yet — the probe is what decides whether to create it. `tried` is
        # the fix here, because it names the actual cause.
        return {"ok": False, "kind": "unknown",
                "error": "nothing answered at that address",
                "tried": trouble[:6]}
    if seen["auth"]:
        # It IS there and it wants credentials. Without this the owner was told
        # the app had no tools, which is a statement about the app rather than
        # about the empty token field right above it.
        return {"ok": True, "kind": "rest", "tools": [], "has_ui": has_ui,
                "needs_auth": True, "tried": trouble[:6],
                "detail": f"It answered {seen['auth']} — it wants a token. Add one "
                          "below and detect again, or declare its actions by hand."}
    return {"ok": True, "kind": "rest", "tools": [], "has_ui": has_ui,
            "tried": trouble[:6],
            "detail": "No tools to auto-discover — tell Ava what this app can do."}

@router.post("/connectors/probe")
async def connector_probe(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    url = str(body.get("url") or "").strip()
    command = str(body.get("command") or "").strip()
    token_env = str(body.get("token_env") or "").strip() or None
    token_value = str(body.get("token_value") or "")
    # The isolation choice is a property of HOW to run the command, so it has to
    # arrive with the request that runs it — not be confirmed afterwards.
    sandbox = body.get("sandbox")
    sandbox = str(sandbox).strip().lower() if sandbox is not None else None
    image = str(body.get("image") or "").strip() or None
    allow_unsandboxed = bool(body.get("allow_unsandboxed"))
    return await run_in_threadpool(_probe, url, command, token_env, token_value,
                                   sandbox=sandbox, image=image,
                                   allow_unsandboxed=allow_unsandboxed)

@router.post("/connectors/new")
async def connector_new(body: dict):
    """Write $AVA_HOME/connectors/<id>/connector.yaml from the Hub's form.
    Refuses to overwrite an existing manifest. The generated file uses the same
    schema as `ava connector new` + docs/CONNECTOR_SDK.md."""
    cid = str(body.get("id", "")).strip().lower()
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error":
                             "id must be 2-32 chars: a-z 0-9 _ - (starting with a letter)"},
                            status_code=400)
    if any(m["id"] == cid for m in connectors.all()):
        return JSONResponse({"ok": False, "error": f"connector '{cid}' already exists"},
                            status_code=409)
    label = str(body.get("label") or cid.title()).strip()
    kind = body.get("kind") if body.get("kind") in ("core", "inference", "media", "app") else "app"
    manifest: dict = {"id": cid, "label": label, "kind": kind, "enabled": True}
    if body.get("confirm"):  # connector-level "require my approval for every action"
        manifest["confirm"] = True
    if str(body.get("role") or "").lower() == "device":
        manifest["role"] = "device"  # groups it under Devices; enables the push flow

    # Credential (Ava-never-has-passwords): the manifest stores only the NAME of
    # an env var (token_env). If the owner pasted the actual VALUE in the connect
    # form, we save it ONCE to the server-side secret store below — never to the
    # manifest, never to the agent — so redeploy never re-prompts. When they gave
    # a value but no name, derive a stable one from the id (e.g. NOTES_TOKEN) so a
    # forker never has to think about env-var naming.
    _mcp_body = body.get("mcp") if isinstance(body.get("mcp"), dict) else {}
    _disc_body = body.get("discover") if isinstance(body.get("discover"), dict) else {}
    token_env_name = (str(body.get("token_env") or "").strip()
                      or str(_mcp_body.get("token_env") or "").strip()
                      or str(_disc_body.get("token_env") or "").strip())
    token_value = str(body.get("token_value") or "")
    if token_value and not token_env_name:
        token_env_name = re.sub(r"[^A-Za-z0-9]", "_", cid).upper() + "_TOKEN"

    probe = str(body.get("probe") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    if probe:
        manifest["service"] = {"name": label, "probe": probe}
    if base_url:
        manifest["base_url"] = base_url

    # MCP server mode — wrap a Model Context Protocol server (url or command).
    # Mutually exclusive with REST actions for a scaffold; edit the YAML to mix.
    mcp_in = body.get("mcp") if isinstance(body.get("mcp"), dict) else None
    if mcp_in:
        mcp: dict = {}
        url = str(mcp_in.get("url") or "").strip()
        command = str(mcp_in.get("command") or "").strip()
        if url:
            mcp["url"] = url
        elif command:
            mcp["command"] = command.split()
        else:
            return JSONResponse({"ok": False,
                                 "error": "mcp needs a url (http) or a command (stdio)"},
                                status_code=400)
        if token_env_name:
            mcp["token_env"] = token_env_name
        if command:
            # Containment DEFAULTS ON, and dropping it is an explicit act.
            #
            # This read `sandbox == "docker"` and wrote nothing otherwise, so the
            # manifest was contained only if the client remembered to say so —
            # while `_probe` defaults to Docker and fails closed. The two halves
            # disagreed, and the frontend sends `(isolate && dockerAvail) ?
            # 'docker' : undefined` with `dockerAvail` stuck at `true` when
            # `hub.system()` fails. So a command PROBED inside a container could
            # be PERSISTED to run uncontained, and `mcp_client` then takes the
            # uncontained branch on every later call — handing an arbitrary
            # pasted command the bridge's environment, which is the exact
            # escalation this module's docstring says was closed once already.
            #
            # `allow_unsandboxed` is the opt-out, named so it reads as a decision
            # in the request and in the manifest diff.
            if mcp_in.get("sandbox") == "none" or mcp_in.get("allow_unsandboxed"):
                mcp["sandbox"] = "none"
            else:
                mcp["sandbox"] = "docker"      # run the stdio server contained
            if mcp_in.get("image"):
                mcp["image"] = str(mcp_in["image"])
        manifest["mcp"] = mcp

    # Auto-discovered facade (GET /tools + POST /call) — from the probe's
    # 'discover' result. Ava lists + calls the app's tools live; the agent still
    # reaches only the policed __tools/__call bridge routes.
    disc_in = body.get("discover") if isinstance(body.get("discover"), dict) else None
    if disc_in and not mcp_in:
        d = {"list": str(disc_in.get("list") or "/tools"),
             "call": str(disc_in.get("call") or "/call")}
        b = str(disc_in.get("base") or base_url or "").strip()
        if b:
            d["base"] = b
        if token_env_name:
            d["token_env"] = token_env_name
        manifest["actions"] = {"discover": d}

    # A token but no mcp/discover block to carry it (a plain iframe or REST app):
    # record it as top-level `auth.token_env` so Ava can present it to the agent
    # tools and the embedded UI (SDK §3 Single sign-on).
    if token_env_name and "mcp" not in manifest and "actions" not in manifest:
        manifest["auth"] = {"token_env": token_env_name}

    # Embedded-app tier (CONNECTOR_SDK.md §3): the app has its own web UI and the
    # user asked for a sidebar tile — Ava reverse-proxies it same-origin under
    # /apps/<id>/ so it inherits the session cookie. No app code changes needed.
    if body.get("ui"):
        ui_url = base_url or str((disc_in or {}).get("base") or "").strip()
        if ui_url:
            # No `icon:` key. CLAUDE.md: an undeclared icon comes back null ON
            # PURPOSE so appIcon() can hash the app id into a stable, distinct
            # glyph. Writing "grid" here made every GUI-connected app identical in
            # the rail — the very failure that convention exists to prevent, moved
            # from apps() into the scaffolder. The owner can still pick one in
            # Setup -> Connectors -> Appearance, which writes ui.icon back here.
            manifest["ui"] = {"label": label, "embed": "iframe", "url": ui_url}
    # Split-container apps (nginx SPA + separate API port): the UI lives at a
    # DIFFERENT address than the tool surface. ui.api routes the embedded UI's
    # /api calls to the API origin — /apps/<id>/api/<p> is reconstructed as
    # <api base>/api/<p>, which is the identity mapping for any UI that calls
    # /api/* paths (the only ones this proxy route matches).
    ui_url2 = str(body.get("ui_url") or "").strip()
    if ui_url2:
        # Same reasoning as above — let appIcon() hash the id.
        manifest["ui"] = {"label": label, "embed": "iframe", "url": ui_url2}
        api_base = base_url or str((disc_in or {}).get("base") or "").strip()
        if api_base and api_base.rstrip("/") != ui_url2.rstrip("/"):
            manifest["ui"]["api"] = {"base": api_base, "prefix": "/api"}

    actions = []
    # Cap matches the probe's discovery limit — a silently-dropped tail would
    # read as "connected everything" when it didn't.
    for a in ([] if (mcp_in or disc_in) else (body.get("actions") or []))[:50]:
        aid = str(a.get("id", "")).strip().lower()
        path = str(a.get("path", "")).strip()
        if not (_ID_RE.match(aid) and path.startswith("/")):
            continue
        act = {"id": aid,
               "description": str(a.get("description") or aid.replace("_", " ")).strip(),
               "method": "POST" if str(a.get("method", "POST")).upper() == "POST" else "GET",
               "path": path}
        # Persist the explicit tier: the proxy collapses methods to GET/POST, so
        # without it a DELETE endpoint would infer as read. Tier drives JIT
        # consent (read silent / write asks once / destructive asks always).
        acc = str(a.get("access") or "").lower()
        if acc in ("read", "write", "destructive"):
            act["access"] = acc
        if a.get("confirm"):
            act["confirm"] = True              # per-action human-in-the-loop gate
        actions.append(act)
    if actions:
        manifest["actions"] = actions
        # The app's own bearer token (named env var) — so Ava can authenticate
        # to it. The secret value stays in the server-side secret store, never in
        # the manifest (stored below via settings.set_env_secret).
        if token_env_name:
            manifest["auth"] = {"token_env": token_env_name}
        egress: dict = {}
        if base_url:  # actions call base_url server-side; allow it for the agent
            from urllib.parse import urlparse
            u = urlparse(base_url if "//" in base_url else "http://" + base_url)
            if u.hostname:
                # Scheme-aware default port: https://api.example.com -> :443,
                # not the policy renderer's :80 fallback.
                port = u.port or (443 if u.scheme == "https" else 80)
                egress["hosts"] = [f"{u.hostname}:{port}"]
        manifest["egress"] = egress or {"routes": []}

    # Device push channel — the app may POST events with its ingest token.
    if body.get("ingest"):
        manifest["ingest"] = {"enabled": True}

    # Every Hub-created connector gets a perf source by default, so it shows in
    # Vitals from its first proxied call (app_perf.py writes here). The path is
    # OUTSIDE $AVA_HOME/connectors/<id> on purpose: deleting the connector keeps
    # its history, and re-adding the same id resumes it. Apps that keep their own
    # SDK perf log can point `perf.path` elsewhere by editing the manifest.
    manifest["perf"] = {"app": cid,
                        "path": "${AVA_LOGS}/apps/%s/performance.jsonl" % cid}

    d = os.path.join(settings.home("connectors"), cid)
    path = os.path.join(d, "connector.yaml")
    def _write_manifest() -> None:
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Generated by the Setup Hub — edit freely.\n"
                    "# Full schema: connectors/_template/connector.yaml + docs/CONNECTOR_SDK.md\n")
            _yaml.safe_dump(manifest, f, sort_keys=False)

    try:
        # Threadpooled like every other disk write on an async route — see
        # tests/test_no_blocking_routes.py. run_in_threadpool re-raises, so the
        # OSError handling below still applies.
        await run_in_threadpool(_write_manifest)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not write manifest: {e}"},
                            status_code=500)
    warnings: list[str] = []
    # Save the pasted credential ONCE to the server-side store, keyed by the
    # env-var name the manifest references. Must precede discover seeding below so
    # the very first (authenticated) tool-list call can reach the app. A real env
    # var of the same name still wins at read time (settings.env_secret).
    auth_saved = False
    if token_value and token_env_name:
        # Guarded. An OSError here used to escape as a raw 500 AFTER the manifest
        # was already on disk, so the connector existed with no credential and the
        # owner saw a stack trace instead of "the app is there, the token is not".
        try:
            settings.set_env_secret(token_env_name, token_value)
            auth_saved = True
        except OSError as e:
            warnings.append(f"the manifest was written, but the credential could "
                            f"not be saved: {e}. Add it again from the connector's "
                            f"⋯ menu.")
    connectors.load(force=True)   # pick it up without a restart
    perf_mgmt.refresh_sources()   # …and let Vitals see its perf source now

    # What the loader made of what we just wrote. This route answered `ok: true`
    # for a manifest it had ALREADY seen fail validation: it called load(), threw
    # `load_errors()` away, and returned success — so a typo'd probe URL or a bad
    # access tier landed as "Connected", and the first sign of trouble was Ava
    # failing to use the app later.
    warnings += [e.get("error", "") for e in connectors.load_errors()
                 if e.get("id") == cid]

    if disc_in or mcp_in:
        # Seed the JIT tier cache (ava-tools/1 `access`) so the app's declared
        # tiers apply from the very first agent call — best-effort; a down app
        # just seeds later, on the next discovery.
        seeded = await run_in_threadpool(connectors.discover_tools, cid)
        # …but "best-effort" is not "unmentionable". The result was discarded
        # entirely, so an app that was unreachable at the moment of connecting
        # reported nothing at all.
        if isinstance(seeded, dict) and seeded.get("error"):
            warnings.append(f"connected, but its tools could not be read yet: "
                            f"{seeded['error']}")
    return {"ok": True, "path": path, "manifest": manifest,
            "actions": len(actions),
            "auth_env": token_env_name or None,
            "auth_saved": auth_saved,
            # Not an error — the connector exists — but the owner has to be told,
            # or "Connected" is a claim nothing checked.
            "warnings": warnings}

@router.post("/connectors/{cid}/generate")
def generate_connector(cid: str, write: int = 0):
    """Render (and optionally write) a connector's agent tools + egress policy
    from its manifest. `write=0` previews; `write=1` writes the files (the same
    output as `ava connector tools|policies --write`) — deploy with
    `cd agent && ./install.sh`."""
    m = {x["id"]: x for x in connectors.all()}.get(cid)
    if not m:
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    pol = connectors.render_egress_policy(cid)
    policy_yaml = _yaml.safe_dump(pol, sort_keys=False) if pol else ""
    # tool_files decides the shape: find/call meta tools for dynamic or large
    # static connectors, else one tool per action.
    tools = connectors.tool_files(cid)
    wrote: list[str] = []
    if write:
        # Both trees live under AVA_HOME, not the code root: they are rendered
        # from a manifest rather than authored, and the code root is an image
        # layer on the install shape most people run. Reported relative to
        # AVA_HOME so the paths read the same on every install shape.
        if pol:
            pdir = settings.generated_policy_dir()
            os.makedirs(pdir, exist_ok=True)
            pp = os.path.join(pdir, f"{cid}.yaml")
            with open(pp, "w", encoding="utf-8") as f:
                f.write(policy_yaml)
            wrote.append(os.path.relpath(pp, settings.AVA_HOME))
        tdir = settings.connector_tools_dir(cid)
        for t in tools:
            os.makedirs(tdir, exist_ok=True)
            tp = os.path.join(tdir, t["name"])
            with open(tp, "w", encoding="utf-8") as f:
                f.write(t["source"])
            wrote.append(os.path.relpath(tp, settings.AVA_HOME))
    return {"ok": True, "policy": policy_yaml, "tools": tools, "wrote": wrote}

@router.get("/connectors/{cid}/ingest-token")
def connector_ingest_token(cid: str):
    """The inbound push token a device app presents (Authorization: Bearer …) to
    POST events — the same value as `ava device token <cid>`, surfaced in the UI so
    a non-technical user never needs a terminal. Returned only for a known connector."""
    if not any(x["id"] == cid for x in connectors.all()):
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    return {"ok": True, "token": internal.ingest_token(cid),
            "enabled": connectors.ingest_enabled(cid),
            "url": f"/api/connectors/{cid}/events"}

@router.get("/connectors/{cid}/last-event")
def connector_last_event(cid: str):
    """Most recent pushed event for a connector — powers the 'waiting for the first
    reading…' verify step so the user sees their device come alive without a terminal."""
    rows = _devices.recent(cid, limit=1)
    return {"ok": True, "event": rows[0] if rows else None}

@router.post("/connectors/{cid}/delete")
def delete_connector(cid: str):
    """Remove a user-added connector (its manifest + any generated tools/policy).
    Built-in connectors shipped in the repo are not removable from here."""
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    user_dir = os.path.join(settings.home("connectors"), cid)
    if not os.path.isdir(user_dir):
        if any(x["id"] == cid for x in connectors.all()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and can't be deleted here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    # What is about to be destroyed, captured BEFORE destroying it — a record
    # written afterwards cannot say what was there. The manifest is read here for
    # the same reason: once it is gone there is nothing left that knows which
    # env-var name held this app's credential.
    m = next((x for x in connectors.all() if x["id"] == cid), None)
    tdir = settings.connector_tools_dir(cid)
    pol = os.path.join(settings.generated_policy_dir(), f"{cid}.yaml")
    had_tools = os.path.isdir(tdir)
    had_policy = os.path.isfile(pol)
    policy_digest = ""
    if had_policy:
        try:
            import hashlib
            with open(pol, "rb") as f:
                policy_digest = hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            pass

    try:
        shutil.rmtree(user_dir)
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not remove: {e}"},
                            status_code=500)
    # Best-effort cleanup of generated agent surface so the sandbox can be re-synced.
    removed = ["connectors/" + cid]
    try:
        if had_tools:
            shutil.rmtree(tdir)
            removed.append(f"agent/mcp_server_connectors/apps/{cid}")
    except OSError:
        pass
    try:
        if had_policy:
            os.remove(pol)
            removed.append(f"agent/policies/generated/{cid}.yaml")
    except OSError:
        pass

    # Everything else this connector accumulated. Removing the manifest used to
    # be the whole of "delete", which left four things behind — and because
    # connector ids are reusable, every one of them was waiting to be re-applied
    # to whatever took the name next:
    #
    #   * standing grants, so a new app inherited "Always allow" for actions a
    #     DIFFERENT app was granted, with no prompt (grants.py said this was
    #     already handled; nothing did it);
    #   * the saved credential, still readable by anything that resolved the
    #     same env-var name;
    #   * the self-reported tier cache, which decides what runs silently;
    #   * a live stdio MCP subprocess, which just kept running.
    #
    # Perf history is kept ON PURPOSE (see the manifest's perf path above) and is
    # the one thing that should survive a delete.
    forgotten_grants = grants.forget(cid)
    if forgotten_grants:
        removed.append(f"{len(forgotten_grants)} standing grant(s)")
    cred = connectors.auth_env(m) if m else None
    if cred:
        settings.clear_env_secret(cred)   # audits itself
        removed.append(f"credential {cred}")
    if tools_cache.forget(cid):
        removed.append("cached tool tiers")
    mcp_client.reset(cid)                 # a stdio server must not outlive its manifest

    # This deletes an EGRESS POLICY, which is the thing that bounds what the
    # sandboxed agent may reach for that app. Removing one silently meant the
    # security posture of the box changed with nothing in the ledger to show it —
    # and the README markets the Data tab's "audited deletes". Recorded with the
    # policy's digest so the record proves which policy went, not just that one did.
    #
    # NOTE the policy FILE going is not yet the ALLOWANCE going: the sandbox
    # holds what `policy-add` applied until something withdraws it. That is now
    # `provision.retire_policies()`, which the next Apply runs — so this is a
    # revocation that completes on the next provision rather than one that never
    # happens. `policy_still_in_sandbox` says which state the owner is in.
    audit.record("connector_delete", id=cid, removed=removed,
                 had_policy=had_policy, policy_digest=policy_digest,
                 had_tools=had_tools, grants_revoked=forgotten_grants,
                 credential_cleared=bool(cred))
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
    return {"ok": True, "removed": removed,
            "policy_still_in_sandbox": had_policy}

@router.post("/connectors/{cid}/deploy")
def deploy_connector(cid: str):
    """One-button version of `ava connector generate` + `cd agent && ./install.sh`:
    render + write this connector's tools and egress policy, then load them into the
    agent sandbox. Push-only devices need no deploy (they work the moment they have a
    token), so we say so rather than pretend to do work."""
    m = {x["id"]: x for x in connectors.all()}.get(cid)
    if not m:
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    steps: list[dict] = []
    gen = generate_connector(cid, write=1)
    if isinstance(gen, JSONResponse):
        return gen
    steps.append({"step": "render", "ok": True,
                  "detail": f"wrote {len(gen.get('wrote') or [])} file(s)"})

    # Nothing for the sandbox to load? Then this is a push-only device.
    if not gen.get("policy") and not gen.get("tools"):
        return {"ok": True, "deployed": False, "steps": steps,
                "detail": "Nothing to deploy — this device only pushes events, which "
                          "works as soon as it has its token."}

    rt = runtime.configured()
    if not rt.available():
        steps.append({"step": "install", "ok": False,
                      "detail": "The agent runtime isn't reachable from here. "
                                "Check Setup → Agent, or run `cd agent && "
                                "./install.sh` on the agent host."})
        return {"ok": False, "deployed": False, "steps": steps,
                "detail": "Wrote the files, but couldn't reach the agent to load them."}

    # Hand the sandbox work to the SAME single-slot job that "Apply to the agent"
    # uses, rather than shelling install.sh beside it. Three things came from
    # having a second, unlocked path:
    #
    #   * Two concurrent install.sh runs against one sandbox, racing on the
    #     `rm -rf "$DEST"` that precedes each server's extraction.
    #   * A synchronous POST that blocked for up to ten minutes — the exact
    #     fragility through a proxy or tailnet hop that provision_job's own
    #     docstring says it was written to remove.
    #   * `deploy` refused outright on the `remote` runtime, so the documented
    #     Docker full-agent profile had no working button at all. Going through
    #     the runtime means whatever can provision can now deploy.
    started, snap = provision_job.start(scope="all", auto_install=False)
    if not started:
        return JSONResponse(
            {"ok": False, "deployed": False, "steps": steps,
             "error": "The agent is already being updated. Wait for that run to "
                      "finish, then deploy again.",
             "error_code": "provision_running", "job": snap},
            status_code=409)
    steps.append({"step": "install", "ok": True,
                  "detail": "deploying into the agent sandbox…"})
    return {"ok": True, "deployed": False, "running": True,
            "job_id": snap.get("id"), "steps": steps,
            "detail": "Deploying into the agent sandbox — progress is on this row "
                      "and in Setup → Agent."}

_MANIFEST_HEADER = "# Managed by the Setup Hub — edit freely.\n"


def _edit_manifest(path: str, edits: list):
    """Apply scalar edits to a user manifest. Returns `(manifest, None)` or
    `(None, JSONResponse)`.

    Three things the two callers each got wrong on their own:

      * **`yaml.YAMLError` escaped an `except OSError`.** A manifest with a typo
        in it answered with an unhandled 500, so the one connector whose YAML was
        broken was also the one you could not disable — from the screen whose job
        is to fix it. It is a 409 now, with the same shape `save_patch` uses for
        an unparsable ava.yaml, and it names the file.
      * **The write was truncating.** A crash mid-write left a half-manifest that
        the loader then refuses. `settings.atomic_write` makes it all-or-nothing.
      * **The whole file went through the YAML emitter**, so ticking a checkbox
        deleted every comment in the owner's hand-written manifest.
        `patch_manifest_text` edits the one line and verifies by round-trip;
        re-dumping is the fallback for a shape it will not touch, not the default.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return None, JSONResponse({"ok": False, "error": f"could not read: {e}"},
                                  status_code=500)
    try:
        m = _yaml.safe_load(text) or {}
    except _yaml.YAMLError as e:
        return None, JSONResponse(
            {"ok": False, "error":
             f"{os.path.basename(path)} does not parse, so it cannot be changed "
             f"from here: {str(e).splitlines()[0][:200]}. Fix it in the manifest "
             "editor, or on disk."}, status_code=409)
    if not isinstance(m, dict):
        return None, JSONResponse({"ok": False, "error": "manifest is not a mapping"},
                                  status_code=400)

    patched = text
    for keys, value in edits:
        step = connectors.patch_manifest_text(patched, keys, value) if patched else None
        patched = step
        if patched is None:
            break
        # Keep the in-memory copy in step for the caller's response body.
        target = m
        for k in keys[:-1]:
            target = target.setdefault(k, {})
        if value is connectors.REMOVE:
            target.pop(keys[-1], None)
        else:
            target[keys[-1]] = value

    if patched is None:
        # A shape the surgical patch declines to touch. Correct, lossy, and rare.
        for keys, value in edits:
            target = m
            for k in keys[:-1]:
                target = target.setdefault(k, {})
            if value is connectors.REMOVE:
                target.pop(keys[-1], None)
            else:
                target[keys[-1]] = value
        patched = _MANIFEST_HEADER + _yaml.safe_dump(m, sort_keys=False)

    try:
        settings.atomic_write(path, patched)
    except OSError as e:
        return None, JSONResponse({"ok": False, "error": f"could not update: {e}"},
                                  status_code=500)
    return m, None


def _user_manifest_path(cid: str) -> str:
    # connectors.USER_DIR, not a second settings.home("connectors") call. Same
    # value, but the loader's own notion of the user root is the one that decides
    # whether a manifest here shadows a built-in — and set_connector_enabled now
    # compares USER_DIR against BUILTIN_DIR, so both sides must read the same
    # variable or the collapse check can disagree with where the file lands.
    return os.path.join(connectors.USER_DIR, cid, "connector.yaml")

@router.post("/connectors/{cid}/enabled")
def set_connector_enabled(cid: str, body: dict):
    """Flip a connector's `enabled` flag (the switch `load()` honors).

    A built-in gets an OVERRIDE STUB in `$AVA_HOME/connectors/<cid>/` rather than an
    edit to the shipped file. `_merge_all` already resolves the user root over the
    built-in root by id, so a two-line `{id, enabled: false}` stub is enough to
    disable one — no new mechanism, and the shipped manifest is never rewritten
    (the owner asked for that YAML; a loader that "repairs" it loses whatever they
    were mid-edit).

    This existed because "the owner's file is never rewritten" was read as "a
    built-in has no off switch". The consequence was that a shipped connector able
    to spend money — publish an app, call a paid API — could not be turned off from
    the UI at all.

    Refused when the two roots are the SAME directory (AVA_HOME unset, so it
    resolves to the code root): there the "stub" would overwrite the built-in
    manifest it is meant to shadow. That collapse is real on a single-box install
    and is exactly why a multi-user control plane keeps AVA_HOME outside
    the checkout.
    """
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    path = _user_manifest_path(cid)
    if not os.path.isfile(path):
        builtin = next((x for x in connectors.catalog() if x["id"] == cid), None)
        if not builtin:
            return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                                status_code=404)
        if os.path.realpath(connectors.USER_DIR) == os.path.realpath(connectors.BUILTIN_DIR):
            return JSONResponse(
                {"ok": False, "error":
                 f"'{cid}' is built-in and AVA_HOME is the code root, so an override "
                 "stub would overwrite the shipped manifest. Set AVA_HOME to a "
                 "separate directory, or edit connectors/" + cid + "/connector.yaml "
                 "directly."}, status_code=409)
        if bool(body.get("enabled", True)):
            # Enabling a built-in that has no stub is already its default state;
            # writing one would be a no-op file that shadows future shipped edits.
            return {"ok": True, "enabled": True, "builtin": True, "stub": False}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Managed by the Setup Hub — override stub for a BUILT-IN\n"
                        "# connector. The user connector root shadows the shipped one\n"
                        "# by id (ava_bridge/connectors.py:_merge_all), so this file\n"
                        "# disables it without touching connectors/" + cid + "/.\n"
                        "# Delete this folder to restore the shipped connector.\n")
                _yaml.safe_dump({"id": cid, "enabled": False}, f, sort_keys=False)
        except OSError as e:
            return JSONResponse({"ok": False, "error": f"could not write stub: {e}"},
                                status_code=500)
        connectors.load(force=True)
        perf_mgmt.refresh_sources()
        return {"ok": True, "enabled": False, "builtin": True, "stub": True}
    want = bool(body.get("enabled", True))
    m, err = _edit_manifest(path, [(("enabled",), want)])
    if err is not None:
        return err
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
    return {"ok": True, "enabled": m["enabled"]}

# Rail identity: a glyph name (see frontend lib/icons), and a color that is
# either an author's brand #hex or one of Ava's theme-aware palette slots
# (`var(--app-accent-N)`, which the Hub picker writes so the accent still
# adapts between light and dark). Validated before the file is touched, so a
# bad value can never land in YAML.
_ICON_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

_COLOR_RE = re.compile(r"^(?:#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})"
                       r"|var\(--app-accent-[0-7]\))$")

@router.post("/connectors/{cid}/appearance")
def set_connector_appearance(cid: str, body: dict):
    """Patch a user app's rail identity — `ui.icon` and/or `ui.color` — in its
    manifest, the single source of truth `apps()` reads for /api/apps. Only keys
    present in the body are touched; a null/empty value CLEARS that key so the
    tile falls back to the frontend's stable auto-pick. Built-in connectors are
    read-only here, and a connector with no `ui:` block has no tile to restyle."""
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    path = _user_manifest_path(cid)
    if not os.path.isfile(path):
        if any(x["id"] == cid for x in connectors.catalog()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and can't be restyled here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"},
                            status_code=404)
    has_icon, icon = "icon" in body, body.get("icon")
    has_color, color = "color" in body, body.get("color")
    if not (has_icon or has_color):
        return JSONResponse({"ok": False, "error": "nothing to update"}, status_code=400)
    if has_icon and icon not in (None, "") and not _ICON_RE.match(str(icon)):
        return JSONResponse({"ok": False, "error": "bad icon name"}, status_code=400)
    if has_color and color not in (None, "") and not _COLOR_RE.match(str(color)):
        return JSONResponse({"ok": False, "error": "color must be a #hex value"}, status_code=400)
    # Read once up front so "no tile to restyle" is answered before anything is
    # written — the same refusal the route always gave, just ahead of the edit.
    try:
        with open(path, encoding="utf-8") as f:
            probe = _yaml.safe_load(f.read()) or {}
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not read: {e}"},
                            status_code=500)
    except _yaml.YAMLError as e:
        return JSONResponse(
            {"ok": False, "error":
             f"{os.path.basename(path)} does not parse, so it cannot be restyled "
             f"from here: {str(e).splitlines()[0][:200]}."}, status_code=409)
    if not isinstance(probe, dict) or not isinstance(probe.get("ui"), dict):
        return JSONResponse({"ok": False, "error":
                             "this connector has no app tile to restyle"},
                            status_code=400)

    edits = []
    for key, present, value in (("icon", has_icon, icon), ("color", has_color, color)):
        if not present:
            continue
        # Clearing REMOVES the key: an explicit `icon: null` is a different
        # manifest from one with no icon, and the frontend's auto-pick keys on
        # absence.
        edits.append((("ui", key),
                      connectors.REMOVE if value in (None, "") else str(value)))
    m, err = _edit_manifest(path, edits)
    if err is not None:
        return err
    ui = m.get("ui") or {}
    connectors.load(force=True)
    return {"ok": True, "icon": ui.get("icon"), "color": ui.get("color")}

@router.get("/connectors/{cid}/manifest")
def get_connector_manifest(cid: str):
    """The raw connector.yaml for editing. `editable` is false for built-ins
    (shown read-only) — a bad edit shouldn't be possible on a shipped connector."""
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    upath = _user_manifest_path(cid)
    if os.path.isfile(upath):
        try:
            with open(upath, encoding="utf-8") as f:
                return {"ok": True, "yaml": f.read(), "editable": True}
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    bpath = os.path.join(settings.CODE_ROOT, "connectors", cid, "connector.yaml")
    if os.path.isfile(bpath):
        try:
            with open(bpath, encoding="utf-8") as f:
                return {"ok": True, "yaml": f.read(), "editable": False}
        except OSError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"}, status_code=404)

@router.post("/connectors/{cid}/manifest")
def put_connector_manifest(cid: str, body: dict):
    """Overwrite a user connector's manifest with validated YAML. Rejects invalid
    YAML, non-mappings, id changes, and edits to built-ins (delete+recreate instead)."""
    if not _ID_RE.match(cid):
        return JSONResponse({"ok": False, "error": "bad id"}, status_code=400)
    text = str(body.get("yaml") or "")
    try:
        m = _yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"invalid YAML: {e}"}, status_code=400)
    if not isinstance(m, dict):
        return JSONResponse({"ok": False, "error":
                             "manifest must be a mapping (key: value lines)"}, status_code=400)
    if str(m.get("id") or cid) != cid:
        return JSONResponse({"ok": False, "error":
                             "changing the id isn't supported — delete and recreate instead"},
                            status_code=400)
    upath = _user_manifest_path(cid)
    if not os.path.isfile(upath):
        # No user manifest: either a read-only built-in, or an unknown id.
        if any(x["id"] == cid for x in connectors.catalog()):
            return JSONResponse({"ok": False, "error":
                                 f"'{cid}' is a built-in connector and is read-only here"},
                                status_code=400)
        return JSONResponse({"ok": False, "error": f"unknown connector '{cid}'"}, status_code=404)
    try:
        with open(upath, "w", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
    except OSError as e:
        return JSONResponse({"ok": False, "error": f"could not write: {e}"}, status_code=500)
    connectors.load(force=True)
    perf_mgmt.refresh_sources()
    return {"ok": True}

