"""Internal capability endpoints — let Ava's sandboxed MCP tools read the text
of files the user uploaded to the bridge.

The MCP server runs inside the OpenClaw sandbox and can't see the host's upload
dir or extraction binaries, so document-reading is exposed HERE as a token-gated
host service it calls back into. Text is extracted once at upload time and
cached on the attachment record; this re-extracts from disk only if that cache
is empty.

Only callers presenting the shared X-Ava-Internal-Token (handed to the tools by
agent/install.sh) are served; everything else gets 401.
"""
import hashlib
import hmac
import os
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import (app_perf, approvals, architecture, audit, code_agent,
               config, config_mgmt, connectors, devices, documents, features,
               learning_mgmt, log_mgmt, perf_mgmt, policy_mgmt, state)
# NOTE the alias: `web` is shadowed by nothing here, but phone_bridge.py has
# always referred to this module as `web_access` and the moved handlers use that
# name. Importing it as plain `web` would leave those bodies referencing an
# undefined name — and a grep for `web.` finds nothing, so the break would only
# surface at request time.
from . import web as web_access
from .security import constant_time_equals

# The /internal/* surface. These 25 routes are the sandbox->bridge callback
# boundary: every one is token-gated by `authorized()` below, and the middleware
# (auth.auth_gate -> group_may) additionally enforces the per-route scope table
# in ROUTE_SCOPES. Keeping the routes in the same module as the scope table and
# the token logic means a new route's author sees the thing they must classify.
router = APIRouter()

# Per-capability-group tokens handed to Ava's sandboxed MCP servers by
# agent/install.sh. Each server receives HMAC(root_secret, "ava-internal:<group>")
# so a low-risk tool token is distinct from the root secret. Must stay in sync
# with the `groups` list in agent/install.sh (both derive from the discovered
# mcp_server_<group> categories, core + optional overlay).
_TOKEN_GROUPS = ["admin", "content", "connectors",
                "productivity", "system", "wellness"]
try:  # optional private overlay contributes its own MCP-server capability groups
    from overlay.ava_bridge import personal_config as _personal_config
    _personal_config.extend_token_groups(_TOKEN_GROUPS)
except Exception:  # noqa: BLE001 - overlay is optional/gitignored
    pass
_TOKEN_GROUPS = tuple(_TOKEN_GROUPS)


def _derived_token(group: str) -> str:
    return hmac.new(config.INTERNAL_TOKEN.encode(),
                    f"ava-internal:{group}".encode(),
                    hashlib.sha256).hexdigest()


def _token_group(tok: str) -> str | None:
    """Return the group a presented token authorizes ('root' = full access), or
    None if it matches neither the root secret nor any derived group token."""
    if constant_time_equals(tok, config.INTERNAL_TOKEN):
        return "root"
    for group in _TOKEN_GROUPS:
        if constant_time_equals(tok, _derived_token(group)):
            return group
    return None


def authorized(request: Request, scope=None) -> bool:
    """True if the caller presents the root token or a valid derived group token.

    The MCP servers each hold a scoped token derived from the root secret, so the
    bridge accepts any of them (the root token always passes). When `scope` is
    given (a group name or iterable of names), a derived token is accepted only if
    its group is in that scope, backing least-privilege on sensitive routes.
    """
    if not config.INTERNAL_TOKEN:
        return False
    group = _token_group(request.headers.get("x-ava-internal-token", ""))
    if group is None:
        return False
    if group == "root" or scope is None:
        return True
    allowed = {scope} if isinstance(scope, str) else set(scope)
    return group in allowed


# ── Route scopes ─────────────────────────────────────────────────────────────
# Which capability each /internal route needs. Enforced centrally in
# ava_bridge/auth.auth_gate, not per handler, so a route added tomorrow is
# covered without its author opting in — 24 of 25 handlers passed no scope, which
# is how `authorized()`'s scope parameter came to be documented in three places
# and enforced in none.
#
# The escalation this closes: the `content` group token belongs to the MCP server
# that runs web_fetch — the surface prompt injection actually arrives on — and it
# was accepted by /internal/code-change, i.e. arbitrary governed edits to Ava's
# own source. Least privilege was written down; it just was not wired.
#
# Longest prefix wins. A path with no entry is refused for derived tokens (the
# root token still passes), so forgetting to classify a route fails CLOSED.
ROUTE_SCOPES: dict[str, str] = {
    "/internal/documents": "documents",
    "/internal/extract": "documents",
    "/internal/model": "model",
    "/internal/web": "web",
    "/internal/connector": "connectors",
    "/internal/devices": "connectors",
    "/internal/learning": "learning",
    "/internal/architecture": "architecture",
    "/internal/logs": "logs",
    "/internal/perf": "perf",
    "/internal/config": "config",
    "/internal/policies": "policies",
    "/internal/code-change": "code_change",
}

try:  # the optional overlay adds its own routes (e.g. /internal/studio/*)
    _personal_config.extend_route_scopes(ROUTE_SCOPES)
except Exception:  # noqa: BLE001 — no overlay (a fork), or an older overlay
    pass
try:  # ...and grants those capabilities to the groups whose servers call them
    from .security import INTERNAL_SCOPE_GROUPS as _SCOPE_GROUPS
    _personal_config.extend_scope_groups(_SCOPE_GROUPS)
except Exception:  # noqa: BLE001 — same
    pass


def required_scope(path: str) -> str | None:
    """The capability `path` needs, or None when nothing claims it."""
    best, cap = "", None
    for prefix, capability in ROUTE_SCOPES.items():
        if (path == prefix or path.startswith(prefix + "/")) and len(prefix) > len(best):
            best, cap = prefix, capability
    return cap


def group_may(group: str, path: str) -> bool:
    """May a token from `group` call `path`?"""
    if group == "root":
        return True
    capability = required_scope(path)
    if capability is None:
        # Unclassified route: only the root token gets through. Add an entry to
        # ROUTE_SCOPES (tests/test_internal_scopes.py will tell you to).
        return False
    from .security import INTERNAL_SCOPE_GROUPS
    return capability in INTERNAL_SCOPE_GROUPS.get(group, frozenset())


def _told(message: str, code: str) -> JSONResponse:
    """A coded error the AGENT must be able to read. HTTP 200, deliberately.

    Every sandbox tool reaches the bridge through `agent/mcp_server_*/_lib.mjs`,
    which calls `curl --fail`. Any non-2xx therefore makes curl exit 22 and the
    JSON body is DISCARDED before the tool ever sees it — so a 400 that carefully
    explains what went wrong arrives at Ava as `request failed: exit status 22`,
    and she relays nothing useful to the owner.

    Five routes already knew this and said so in their comments; the other
    twenty-odd returned 4xx/5xx. The one that mattered most was the policy
    management route, whose entire purpose was to tell Ava WHY an edit was
    refused. CLAUDE.md states the rule; nothing enforced it, which is why
    tests/test_internal_error_contract.py now does.

    401 is the deliberate exception and stays non-2xx: an unauthenticated caller
    is not Ava, so there is no one to explain anything to, and the middleware
    already rejects before routing.
    """
    return JSONResponse({"error": message, "error_code": code}, status_code=200)


# ── Inbound "app → Ava" ingest tokens ──────────────────────────────────────
# A third-party device/sensor app is NOT a sandboxed MCP server, so it must not
# hold the internal token or reach the /internal/* tool surface. Instead each
# connector gets its own inbound token — derived from the root secret the same way
# as the MCP group tokens, but under a distinct 'ava-ingest:<cid>' namespace — that
# it presents (Authorization: Bearer <token>) to POST events to Ava. No new secret
# store; rotating the connector id rotates its token.
def ingest_token(cid: str) -> str:
    """The per-connector inbound bearer token an app presents to push events."""
    return hmac.new(config.INTERNAL_TOKEN.encode(),
                    f"ava-ingest:{cid}".encode(),
                    hashlib.sha256).hexdigest()


def verify_ingest(cid: str, presented: str) -> bool:
    """Constant-time check that `presented` is `cid`'s ingest token."""
    if not config.INTERNAL_TOKEN or not presented:
        return False
    return constant_time_equals(presented, ingest_token(cid))


def bearer(request: Request) -> str:
    """Extract a Bearer token from the Authorization header ('' if absent)."""
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def documents_payload() -> dict:
    """List the documents/images the user has uploaded in this bridge session."""
    with state.attachments_lock:
        items = [
            {"id": r["id"], "filename": r["filename"], "kind": r["kind"],
             "chars": r.get("chars", 0), "has_text": bool(r.get("text"))}
            for r in state.attachments.values()
        ]
    return {"documents": items}


def extract_payload(file_id: str, max_chars: int) -> dict | None:
    """Return the extracted text for one uploaded file, or None if unknown."""
    with state.attachments_lock:
        rec = state.attachments.get(file_id)
        rec = dict(rec) if rec else None
    if not rec:
        return None
    text = (rec.get("text") or "").strip()
    name = rec["filename"]
    # Re-extract from disk if the cached text is empty (e.g. cleared on restart).
    if not text:
        upload_dir = os.path.realpath(config.UPLOAD_DIR)
        path = os.path.join(upload_dir, f'{rec["id"]}_{name}')
        ext = os.path.splitext(name)[1].lower()
        real = os.path.realpath(path)
        # Only read a real regular file that lives directly in the upload dir and
        # isn't a symlink — blocks symlink/traversal reads of files elsewhere.
        if (os.path.dirname(real) == upload_dir
                and not os.path.islink(path)
                and os.path.isfile(real)):
            text = (documents.extract_text(real, ext) or "").strip()
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\u2026[truncated]"
    return {"id": rec["id"], "filename": name, "kind": rec["kind"], "text": text}


def _timed_connector_call(fn, cid: str, tool: str, args: dict) -> tuple:
    """Run one connector call and record its latency/status in the app's
    bridge-owned perf log — this is how a Hub-connected app shows up in Vitals
    without writing its own performance.jsonl."""
    t0 = time.time()
    data, status = fn(cid, tool, args)
    app_perf.record_action(cid, tool, time.time() - t0, status)
    return data, status

@router.api_route("/internal/connector/{cid}/{action}", methods=["POST", "GET"])
async def internal_connector(cid: str, action: str, request: Request):
    """Generic connector-action proxy: forwards a generated tool's call to the
    connector's own host-local API. ONE route serves every connector, so adding
    an app needs no new bridge code — just a connector manifest.

    Two reserved actions bridge a connector's DYNAMIC tool set (see the manifest
    `actions.discover` block): __tools lists them, __call invokes one by name."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if action == "__tools":
        q = request.query_params.get("q", "")
        try:
            limit = int(request.query_params.get("limit", "0"))
        except ValueError:
            limit = 0
        return JSONResponse(await run_in_threadpool(connectors.discover_tools, cid, q, limit))
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if action == "__call":
        name = (body.get("name") or "").strip()
        if not name:
            return _told("missing tool name", "bad_request")
        gate = await run_in_threadpool(approvals.gate, cid, name, body.get("arguments") or {})
        if gate not in ("skip", "approved"):
            audit.record("egress", connector=cid, tool=name, status=f"blocked:{gate}")
            return _told(f"not run — awaiting-approval {gate}", "awaiting_approval")
        data, status = await run_in_threadpool(
            _timed_connector_call, connectors.call_discovered, cid, name,
            body.get("arguments") or {})
        # Egress record for the flight recorder: the agent reached out to a
        # connector/MCP tool. This is the closest in-process egress signal we
        # have (the sandbox's network denials live in openclaw, not here).
        audit.record("egress", connector=cid, tool=name, status=status)
        # The APP's status is reported inside the body, not as ours: a 404 from
        # someone else's API must not become a curl failure that hides it.
        return JSONResponse({**data, "app_status": status} if isinstance(data, dict)
                            else {"result": data, "app_status": status},
                            status_code=200)
    # Merge query params (GET-style tools) with any JSON body so declared actions
    # get their args regardless of how the tool passed them.
    args = dict(request.query_params)
    if isinstance(body, dict):
        args.update(body)
    gate = await run_in_threadpool(approvals.gate, cid, action, args)
    if gate not in ("skip", "approved"):
        audit.record("egress", connector=cid, tool=action, status=f"blocked:{gate}")
        return _told(f"not run — awaiting-approval {gate}", "awaiting_approval")
    data, status = await run_in_threadpool(
        _timed_connector_call, connectors.call_action, cid, action, args)
    audit.record("egress", connector=cid, tool=action, status=status)
    return JSONResponse({**data, "app_status": status} if isinstance(data, dict)
                        else {"result": data, "app_status": status},
                        status_code=200)

# ---- Internal capability surface (Ava's sandboxed MCP tools call back here) --
# Token-gated; reached from the sandbox via host.openshell.internal:8096 under
# the ava-knowledge egress policy. Lets Ava read the text of files the user uploaded.
@router.get("/internal/documents")
def internal_documents(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return documents_payload()

@router.get("/internal/model")
def internal_model(request: Request):
    """Which brain Ava is actually thinking with.

    `/internal/model` had a scope entry in ROUTE_SCOPES, auth tests, an egress
    grant, and a shipped tool calling it — and no handler. `get_active_model`
    404'd on every invocation, so the one question Ava could not answer about
    herself was which model she was.

    Two facts, deliberately kept apart, because conflating them is how the model
    chip came to disagree with reality:

      * `configured` is what `models.effective_brain()` resolves — the ONE
        resolver, so this route cannot become a fourth independent derivation of
        the same answer.
      * `answering` is what actually served the last completion, observed from the
        router rather than inferred from config. `null` means nothing has been
        asked yet, which is a real state and not an error.
    """
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from . import agent as _agent
    from . import models as _models
    try:
        brain = _models.effective_brain()
    except Exception as e:  # noqa: BLE001 — a coded answer, never a 500
        return _told(f"could not resolve the configured model: {e}",
                     "model_unreadable")
    try:
        answering = _agent.which_model()
    except Exception:  # noqa: BLE001 — best-effort; absence is a legal answer
        answering = None
    return {
        "configured": {
            "model": brain.get("model_id"), "label": brain.get("label"),
            "engine": brain.get("engine"), "source": brain.get("source"),
            "local": brain.get("local"),
            # True when nothing named a brain and the router's first backend was
            # taken by default — the owner has not chosen this.
            "implicit": brain.get("implicit"),
        },
        "answering": answering,
    }


@router.post("/internal/extract")
async def internal_extract(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    file_id = str(body.get("file_id", "")).strip()
    if not file_id:
        return _told("file_id required", "bad_request")
    try:
        max_chars = int(body.get("max_chars") or 0)
    except (TypeError, ValueError):
        max_chars = 0
    payload = extract_payload(file_id, max_chars)
    if payload is None:
        return _told("no such document", "not_found")
    return payload

@router.get("/internal/devices/events")
def internal_device_events(request: Request):
    """Recent pushed device events, for Ava's `device_events` MCP tool. Token-gated
    (reuses the ava-knowledge /internal egress).

    No per-handler `scope=` here, deliberately. This handler used to pass
    `scope="content"`, which was correct while device_events.mjs lived in
    mcp_server_content — and became a second, contradicting gate the moment the
    tool moved to agent/mcp_server_connectors/. ROUTE_SCOPES says
    /internal/devices needs `connectors`, so auth_gate let the connectors token
    in and this line then 401'd it, while the `content` token was refused 403
    upstream. The result was a route NO derived token could reach, i.e. the
    documented tool could not work at all. Enforcement is central (see
    ROUTE_SCOPES above); this call only asserts that some valid token was shown.
    """
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    cid = (request.query_params.get("connector") or "").strip() or None
    try:
        limit = min(int(request.query_params.get("limit") or 50), 200)
    except (TypeError, ValueError):
        limit = 50
    return {"events": devices.recent(cid, limit=limit)}

# ---- Architecture capability (Ava reads/updates her own SSOT diagrams+code) --
# Same token gate. Lets Ava get the architecture manifest + drift report, render
# the diagrams, and apply manifest edits (auto-committed). Backed by arch.py.
@router.get("/internal/architecture")
def internal_architecture(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return architecture.summary_payload()

@router.get("/internal/architecture/model")
def internal_architecture_model(request: Request):
    """The product's structure — the assembly tree, not this deployment.

    Read-only on purpose. `update_architecture` exists for the manifest because a
    manifest is machine-authored; the model's spine is hand-authored intent, and a
    write path that validates-then-reverts would throw away someone's `why`. Ava
    edits it the way a person does: as source, through review.

    Answers with the same coded-error-as-200 convention the rest of /internal
    uses, because the sandbox helper runs `curl --fail` and swallows non-2xx
    bodies — a 4xx here would reach Ava as silence.
    """
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from . import model as model_mod
        nodes, _ = model_mod.load(include_overlay=True)
        node_id = str(request.query_params.get("node", "")).strip()
        if node_id:
            found = model_mod.describe(node_id)
            if found is None:
                return {"error": f"no such node: {node_id}",
                        "error_code": "model_unknown_node"}
            return {"node": found}
        return {
            "summary": model_mod.summary(),
            "levels": {lvl: model_mod.LEVEL_STANDARDS[lvl] for lvl in model_mod.LEVELS},
            "tree": [
                {"id": n.id, "level": n.level, "parent": n.parent,
                 "title": n.title, "purpose": n.purpose}
                for n in sorted(nodes.values(), key=lambda n: (n.order, n.id))
            ],
            "flows": {fid: {"title": f.title,
                            "steps": [s.node for s in f.steps]}
                      for fid, f in model_mod.flows().items()},
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"could not read the architecture model: {exc}",
                "error_code": "model_unreadable"}


@router.post("/internal/architecture/describe")
async def internal_arch_describe(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    name = str(body.get("name", "")).strip()
    if not name:
        return _told("name required", "bad_request")
    return architecture.describe_payload(name)

@router.post("/internal/architecture/check")
def internal_arch_check(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return architecture.check_payload()

@router.post("/internal/architecture/sync")
async def internal_arch_sync(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    commit = bool(body.get("commit", True))
    return architecture.sync_payload(commit=commit, message=body.get("message"))

@router.post("/internal/architecture/update")
async def internal_arch_update(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    new_yaml = body.get("yaml") or ""
    if not new_yaml.strip():
        return _told("yaml required (the full new manifest)", "bad_request")
    commit = bool(body.get("commit", True))
    return architecture.update_payload(new_yaml, message=body.get("message"), commit=commit)

@router.get("/internal/learning/scripts")
async def internal_learning_read(request: Request):
    """Allow Ava to read current learning digest scripts (read-only access)."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return learning_mgmt.read_digest_scripts()

@router.post("/internal/learning/update")
async def internal_learning_update(request: Request):
    """Allow Ava to update learning digest scripts with improved descriptions/rationales."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    
    script_type = body.get("script_type") or ""
    updates = body.get("updates") or {}
    content = body.get("content")
    
    if not script_type:
        return _told("script_type required (daily, weekly, or both)", "bad_request")
    
    result = learning_mgmt.update_digest_scripts(script_type, updates, content)
    if not result.get("success"):
        result.setdefault("error_code", "refused")
    return JSONResponse(result, status_code=200)   # see _told()

@router.get("/internal/learning/state")
async def internal_learning_state(request: Request, state_type: str = "all", limit: int = 10, patterns: bool = True):
    """Allow Ava to read her own learning state (code/chat cycles, patterns)."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    limit = min(max(limit, 1), 100)
    result = {}
    
    if state_type in ["code", "all"]:
        with state.code_learning_state_lock:
            cycles = state.code_learning_state.get("cycles", [])[-limit:]
            inline_fixes = state.code_learning_state.get("inline_fixes", [])[-limit:]
            result["code"] = {
                "cycles": cycles,
                "inline_fixes": inline_fixes,
                "total_cycles": len(state.code_learning_state.get("cycles", [])),
                "total_fixes": len(state.code_learning_state.get("inline_fixes", [])),
            }
    
    if state_type in ["chat", "all"]:
        with state.chat_learning_state_lock:
            cycles = state.chat_learning_state.get("cycles", [])[-limit:]
            result["chat"] = {
                "cycles": cycles,
                "total_cycles": len(state.chat_learning_state.get("cycles", [])),
            }
    
    return JSONResponse(result)

@router.get("/internal/learning/chats")
async def internal_learning_chats(request: Request, action: str = "list", chat_id: str = None, limit: int = 50, metadata: bool = True):
    """Allow Ava to read chat history for reflection and pattern analysis."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    limit = min(max(limit, 1), 200)
    
    if action == "list":
        # Return list of chats with metadata
        import json
        chats_file = Path('data/chats.json')
        if not chats_file.exists():
            return JSONResponse({"chats": []})
        try:
            with open(chats_file) as f:
                data = json.load(f)
            chats = []
            for cid, chat in data.items():
                chats.append({
                    "id": cid,
                    "title": chat.get("title", "Untitled"),
                    "created": chat.get("created"),
                    "updated": chat.get("updated"),
                    "message_count": len(chat.get("messages", [])),
                })
            return JSONResponse({"chats": chats[:limit]})
        except Exception:  # noqa: BLE001 — surfaced to the client as a JSON error response
            return JSONResponse({"chats": []})
    
    elif action == "read":
        if not chat_id:
            return _told("chat_id required", "bad_request")
        import json
        chats_file = Path('data/chats.json')
        if not chats_file.exists():
            return _told("no chats", "not_found")
        try:
            with open(chats_file) as f:
                data = json.load(f)
            chat = data.get(chat_id)
            if not chat:
                return _told("chat not found", "not_found")
            
            messages = chat.get("messages", [])[-limit:]
            if not metadata:
                # Strip metadata, keep only role and content
                messages = [{"role": m.get("role"), "content": m.get("content")} for m in messages]
            
            return JSONResponse({
                "chat_id": chat_id,
                "title": chat.get("title"),
                "created": chat.get("created"),
                "updated": chat.get("updated"),
                "messages": messages,
            })
        except Exception:  # noqa: BLE001 — surfaced to the client as a JSON error response
            return _told("failed to read chat", "read_failed")
    
    else:
        return _told("action must be 'list' or 'read'", "bad_request")

@router.get("/internal/learning/code-turns")
async def internal_learning_code_turns(request: Request, cycle_id: str = None, status_filter: str = "all", diffs: bool = True, reasoning: bool = True, limit: int = 10):
    """Allow Ava to read her code editing turns and proposals for learning."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    limit = min(max(limit, 1), 100)
    
    with state.code_learning_state_lock:
        cycles = state.code_learning_state.get("cycles", [])
        if cycle_id:
            cycles = [c for c in cycles if c.get("id") == cycle_id]
        cycles = cycles[-limit:]
    
    result = []
    for cycle in cycles:
        proposals = cycle.get("proposals", [])
        
        # Filter by status if requested
        if status_filter != "all":
            proposals = [p for p in proposals if p.get("status") == status_filter]
        
        cycle_data = {
            "id": cycle.get("id"),
            "timestamp": cycle.get("timestamp"),
            "proposals": proposals,
        }
        
        # Strip diffs if not requested
        if not diffs:
            for p in cycle_data["proposals"]:
                p.pop("diff", None)
        
        # Strip reasoning if not requested
        if not reasoning:
            for p in cycle_data["proposals"]:
                p.pop("reasoning", None)
        
        result.append(cycle_data)
    
    return JSONResponse({"code_turns": result, "count": len(result)})

@router.post("/internal/logs")
async def internal_logs(request: Request):
    """Allow Ava to read system logs for debugging."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
    except:
        return _told("invalid json", "bad_request")
    
    result = log_mgmt.read_logs(
        source=body.get("source", "systemd"),
        service=body.get("service"),
        component=body.get("component"),
        lines=body.get("lines", 50),
        level=body.get("level"),
        since=body.get("since", "1h")
    )
    
    # Always 200: `curl --fail` in the sandbox helper would swallow the
    
    # body, and the body IS the answer. See _told().
    
    if not result.get("ok"):
    
        result.setdefault("error_code", "refused")
    
    return JSONResponse(result, status_code=200)

@router.post("/internal/perf")
async def internal_perf(request: Request):
    """Let Ava read + summarise generation-performance across all her apps."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}

    result = perf_mgmt.read_performance(
        app=body.get("app"),
        category=body.get("category"),
        since=body.get("since"),
        limit=body.get("limit", 50),
        summary=body.get("summary", True),
    )
    # Always 200: `curl --fail` in the sandbox helper would swallow the
    # body, and the body IS the answer. See _told().
    if not result.get("ok"):
        result.setdefault("error_code", "refused")
    return JSONResponse(result, status_code=200)

@router.get("/internal/config")
async def internal_config_get(request: Request, component: str):
    """Allow Ava to read configuration."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    result = config_mgmt.read_config(component)
    # Always 200: `curl --fail` in the sandbox helper would swallow the
    # body, and the body IS the answer. See _told().
    if not result.get("ok"):
        result.setdefault("error_code", "refused")
    return JSONResponse(result, status_code=200)

@router.post("/internal/config")
async def internal_config_post(request: Request):
    """Allow Ava to update configuration."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
    except:
        return _told("invalid json", "bad_request")
    
    result = config_mgmt.update_config(
        component=body.get("component"),
        updates=body.get("updates", {}),
        reason=body.get("reason", "No reason provided")
    )
    
    # Always 200: `curl --fail` in the sandbox helper would swallow the
    
    # body, and the body IS the answer. See _told().
    
    if not result.get("ok"):
    
        result.setdefault("error_code", "refused")
    
    return JSONResponse(result, status_code=200)

@router.get("/internal/policies")
async def internal_policies_get(request: Request):
    """Allow Ava to list all policies."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    result = policy_mgmt.list_policies()
    return JSONResponse(result, status_code=200)

@router.get("/internal/policies/{policy_name}")
async def internal_policy_read(request: Request, policy_name: str):
    """Allow Ava to read a specific policy."""
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    
    result = policy_mgmt.read_policy(policy_name)
    # Always 200: `curl --fail` in the sandbox helper would swallow the
    # body, and the body IS the answer. See _told().
    if not result.get("ok"):
        result.setdefault("error_code", "refused")
    return JSONResponse(result, status_code=200)

# There is deliberately NO `POST /internal/policies`.
#
# One existed. It let the sandboxed agent rewrite `agent/policies/**` — the
# egress rules that are the boundary containing it — and it had never worked
# once: `policy_mgmt.update_policy` validated a `{name, rules}` shape while every
# real policy is `{preset, network_policies}`, so every call was rejected by its
# own validator. Nothing else called it: no hub route, no CLI, no owner UI.
#
# It also contradicted the layer above it. `access_policy.py` puts
# `agent/policies/**` in the OWNER-APPROVAL tier, so the system's own stated
# position is that a human confirms a policy edit. Two enforcement layers giving
# opposite answers about one asset is worse than either answer alone, and the
# only reason it was not exploitable is a bug.
#
# Reading policies stays — `list_policies` / `read_policy` are how Ava explains
# what she is allowed to reach, and they change nothing. If the agent should ever
# be able to PROPOSE a policy change, that is a feature to design against the
# approvals gate, not a route to restore.

@router.post("/internal/code-change")
async def internal_code_change(request: Request):
    """Hand off a code-change request to Claude (uses ANTHROPIC_API_KEY).

    Ava's `code_change_request` MCP tool calls this. code_agent drives Claude
    through the repo, then applies the access policy: safe files are written +
    committed autonomously; protected files (auth/config/policy/deploy) are
    parked as a pending proposal in the user's approval bucket on the Learning page;
    secrets/binaries are refused. The Claude tool loop runs synchronously and can
    take a while, so run it off the event loop.
    """
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _told("invalid json", "bad_request")

    request_text = (body.get("request") or "").strip()
    context_text = (body.get("context") or "").strip()
    files_list = body.get("files") or []
    project = (body.get("project") or "ava").strip() or "ava"
    actor = (body.get("actor") or "Ava").strip() or "Ava"
    if not request_text:
        return _told("empty request", "bad_request")

    print(f"[ava-bridge] [DEBUG] /internal/code-change received: "
          f"project={project!r} actor={actor!r} request={request_text[:80]!r} files={files_list}", flush=True)

    try:
        result = await run_in_threadpool(
            code_agent.run_change_request,
            request_text, context_text, files_list, "code_change_request",
            None, project, actor,
        )
    except Exception as e:  # noqa: BLE001
        return _told(f"code change failed: {e}", "code_change_failed")

    if result.get("status") == "error":
        result.setdefault("error_code", "code_change_failed")
    return JSONResponse(result, status_code=200)   # see _told()

# Bespoke connected-app routes were removed: connected apps ride the generic
# connector proxy (/internal/connector/<id>/* for agent tools, /apps/<id>/api/*
# for the browser tab) — see each connector's connector.yaml.
# --- Web access (self-hosted search + SSRF-guarded reader) -------------------
# HOST-MEDIATED: Ava's sandbox tools call these token-gated routes (reusing the
# ava-knowledge egress policy — no new open egress). The HOST runs the private
# SearXNG search and the SSRF-guarded page fetch; Ava never touches the internet
# directly and no API keys ever enter the sandbox.
@router.post("/internal/web/search")
async def internal_web_search(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # features.web_search is the single switch for BOTH web routes: a
    # deliberate OFF must actually stop the path, with a coded error the chat
    # turns into a fix-it link. Coded errors ship as HTTP 200 — the sandbox
    # tool helper (curl --fail) swallows non-200 bodies, and the message must
    # reach Ava so she can tell the user how to fix it.
    pf = features.preflight("web_search")
    if pf:
        return {"error": pf[1], "error_code": pf[0]}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _told("invalid json", "bad_request")
    query = (body or {}).get("query") or (body or {}).get("q") or ""
    count = (body or {}).get("count")
    try:
        return JSONResponse(await run_in_threadpool(web_access.search, query, count))
    except web_access.SearchUnreachableError as e:
        return {"error": str(e), "error_code": "web_search_down"}
    except web_access.WebAccessError as e:
        return _told(str(e), "bad_request")
    except Exception as e:  # noqa: BLE001
        return _told(f"web search failed: {e}", "web_search_failed")

@router.post("/internal/web/fetch")
async def internal_web_fetch(request: Request):
    if not authorized(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    pf = features.preflight("web_search")  # guarded fetch rides the same switch
    if pf:
        return {"error": pf[1], "error_code": pf[0]}  # 200: see /internal/web/search
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _told("invalid json", "bad_request")
    url = (body or {}).get("url") or ""
    try:
        return JSONResponse(await run_in_threadpool(web_access.fetch, url))
    except web_access.WebAccessError as e:
        # Blocked/invalid target — a 400 so Ava learns the URL was refused.
        return _told(str(e), "bad_request")
    except Exception as e:  # noqa: BLE001
        return _told(f"web fetch failed: {e}", "web_fetch_failed")
