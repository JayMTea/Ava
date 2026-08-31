"""The agent gateway's surface to the browser: three HTTP routes and one socket.

WHY ONE PASSTHROUGH AND NOT TWO HUNDRED ROUTES
----------------------------------------------
The gateway advertises ~200 JSON-RPC methods and the Agent tab calls most of
them. Three reasons this is one route rather than a curated set, weightiest
first:

  * `tests/_route_table.json` is a frozen snapshot whose whole value is that it
    stays readable — "anything that moves or gains a method is a diff". Two
    hundred mechanical rows destroy exactly that property.
  * `hello-ok.features.methods` is the SSOT for what exists, and it changes
    without us. A curated Python surface is a second copy that drifts silently
    the day the gateway ships a new version: the route exists, the gateway
    rejects the method.
  * `tests/test_frontend_api_paths.py` matches path SHAPE. One shape called two
    hundred ways keeps that guard honest for every other route in the app.

AUTHORIZATION, SAID OUT LOUD
----------------------------
The owner chose full `operator.admin` gated only by Ava's session cookie. That
is the largest security decision in this feature and it is deliberate, not an
accident — so it is written here rather than left to be inferred.

One consequence worth knowing: with `apps.origin` unset (the default), an
embedded connector app's JavaScript is same-origin with the shell and carries
the session cookie. `ava_bridge/apps_origin.py` documents that at length using
`/api/hub/approvals` as its example; this passthrough is a strictly larger
version of the same example. `GET /api/gateway/status` reports the condition so
the Agent tab can say what is exposed.

The single exception to "no extra gate" is `_DENIED_CONFIG_KEYS` below.
"""
from __future__ import annotations

import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import audit, config, runtime, ws_auth
from .runtime import gateway_policy
from .runtime.errors import GatewayError

router = APIRouter()

# Dotted lowerCamel segments and nothing else. Rejects path-ish and shell-ish
# values before a JSON encoder ever sees them.
_METHOD_RE = re.compile(r"^[a-z][a-zA-Z0-9]*(\.[a-z][a-zA-Z0-9]*)*$")
_METHOD_MAX = 64
# The lesson `agent_runtime_server._EXEC_TIMEOUT_MAX` already learned: an
# unbounded caller-supplied timeout pins a worker for as long as it likes.
_TIMEOUT_MIN, _TIMEOUT_MAX = 1.0, 120.0
# Not a security control — a backstop. A `useEffect` that fans out two hundred
# calls would otherwise trip the gateway's own buffer cap and close the socket
# for the turn path along with everything else.
_RATE, _BURST = 30.0, 60.0

# The deny-list lives in `runtime/gateway_policy.py` now, because the shim's
# `/gateway/rpc` is a SECOND independent door to the same `config.set`. Two
# doors need the same lock, and a copy-pasted predicate is one that drifts.
# Re-exported under the private names because tests/test_gateway_api.py reaches
# both by name, and downstream readers expect them here.
_DENIED_CONFIG_KEYS = gateway_policy.DENIED_CONFIG_KEYS
_CONFIG_WRITES = gateway_policy.CONFIG_WRITES
_asserts_key = gateway_policy.asserts_key
_denied_config_write = gateway_policy.denied_config_write

_buckets: dict[str, list] = {}


def _rate_ok(key: str) -> bool:
    now = time.time()
    tokens, last = _buckets.get(key, [_BURST, now])
    tokens = min(_BURST, tokens + (now - last) * _RATE)
    if tokens < 1.0:
        _buckets[key] = [tokens, now]
        return False
    _buckets[key] = [tokens - 1.0, now]
    return True



def _fail(code: str, message: str, **extra) -> JSONResponse:
    """Coded failures ride as HTTP 200 bodies.

    Same reasoning as `internal._told()` and `agent_runtime_server./run_turn`:
    `frontend/src/lib/api.ts` maps a bodyless 404 to `bridge_outdated`, and a
    caller that cannot see the body cannot show the owner the fix. Real status
    codes stay reserved for BRIDGE-level failures — a malformed body, an
    unauthenticated caller, a client that has never connected.
    """
    return JSONResponse({"ok": False, "error_code": code, "message": message,
                         **extra})


@router.post("/api/gateway/rpc")
async def gateway_rpc(request: Request):
    """One call to the agent's control plane."""
    rt = runtime.configured()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    method = str(body.get("method") or "")
    if not method or len(method) > _METHOD_MAX or not _METHOD_RE.match(method):
        return JSONResponse({"error": "malformed method name"}, status_code=400)
    # `body.get("params") or {}` would silently turn a list — or 0, or "" — into
    # an empty object and forward the call as if nothing were wrong. Read the raw
    # value, allow only absent-or-object, and refuse the rest.
    params = body.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return JSONResponse({"error": "params must be an object"}, status_code=400)

    denied = _denied_config_write(method, params)
    if denied is not None:
        audit.record("gateway_denied", method=method, reason="device_auth_key")
        return _fail("gateway_key_refused",
                     f"`{denied}` governs whether the gateway authenticates "
                     f"browsers at all, so it is not writable from here. "
                     f"Change it with the nemoclaw CLI if you mean to.",
                     key=denied)

    if not _rate_ok(request.cookies.get(config.COOKIE_NAME, "") or "anon"):
        return _fail("gateway_rate_limited",
                     "too many gateway calls at once", retry_after_ms=1000)

    try:
        timeout = float(body.get("timeout") or config.AGENT_GATEWAY_RPC_TIMEOUT)
    except (TypeError, ValueError):
        return JSONResponse({"error": "timeout must be a number"}, status_code=400)
    timeout = max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, timeout))

    key = body.get("idempotency_key")
    client = rt.control_plane()
    if client is None:
        return _fail("agent_no_gateway",
                     "the configured runtime has no gateway control plane. "
                     "Select it with `agent.runtime: openclaw_gw`.")
    try:
        # `await ...arpc(...)`, not `run_in_threadpool(rt.rpc, ...)`. Both
        # satisfy tests/test_no_blocking_routes.py; only this one avoids burning
        # a threadpool slot for every in-flight browser call.
        payload = await client.arpc(method, params, timeout=timeout,
                                    idempotency_key=key if key else None)
    except GatewayError as e:
        _audit_error(method, e)
        return JSONResponse(e.as_body())
    _audit_call(method, key, ok=True)
    return {"ok": True, "payload": payload}


#: Codes that mean a POLICY said no. Everything else that raises is a FAILURE:
#: nothing decided, the call simply did not happen.
#:
#: The distinction is not cosmetic. `gateway_denied` renders as "Agent call
#: refused", and every GatewayError was recorded as one — so a dropped socket,
#: a timeout or a protocol mismatch all appeared in the owner's ledger as a
#: refusal, blaming a policy that never ran and sending them to look for a
#: permission problem that does not exist.
#:
#: The bias is deliberate: a code is only a refusal when we KNOW something
#: refused it. An unrecognised code reads as a failure, which is the honest
#: default for "we do not know what happened".
_REFUSAL_CODES = frozenset({
    "agent_scope_denied",           # the gateway refused on scope
    "agent_token_rejected",         # the gateway refused our credentials
    "gateway_unsupported_method",   # the gateway declines to serve it at all
})


def _audit_error(method: str, err) -> None:
    """Record a failed call as what it actually was."""
    code = getattr(err, "code", "") or ""
    kind = "gateway_denied" if code in _REFUSAL_CODES else "gateway_failed"
    audit.record(kind, method=method, reason=code or "unknown")


def _audit_call(method: str, key, *, ok: bool) -> None:
    """Record side-effecting calls — the method, never the parameters.

    `secrets.store.*` and `config.set` carry credentials, and the audit ledger
    is a file the owner reads. With a full-admin passthrough and no per-method
    gate, this ledger is the ONLY record of what was done through it, which is
    what makes the omission of `params` a discipline rather than a shortcut.
    """
    from .runtime.openclaw_gw_client import is_read_method
    if is_read_method(method):
        return
    audit.record("gateway_rpc", method=method,
                 idempotency_key=str(key or ""), ok=ok)


@router.get("/api/gateway/status")
async def gateway_status():
    """What the connection is doing, and what it is exposed to."""
    from . import apps_origin
    rt = runtime.configured()
    client = rt.control_plane()
    if client is None:
        return {"ok": True, "configured": False, "runtime": rt.name,
                "phase": "unconfigured",
                "why": ("this runtime has no gateway control plane; select it "
                        "with `agent.runtime: openclaw_gw`")}
    st = client.status()
    return {"ok": True, "configured": True, "runtime": rt.name, **st,
            # The prefix Ava keys gateway sessions with (`chat_session(cid)` =
            # f"{OC_SESSION}-{cid}"). The frontend needs it to map a session
            # key like 'ava-phone-<cid>' back to a chat id; it is configurable
            # (`agent.session_prefix` / AVA_OC_SESSION), so hardcoding it there
            # would break every deployment that set it.
            "session_prefix": config.OC_SESSION,
            # Which agent on the gateway serves turns. Reported because the
            # gateway ECHOES SESSION KEYS BACK PREFIXED WITH IT — send
            # 'ava-phone-x' and its frames say 'agent:main:ava-phone-x'
            # (captured live). A client comparing the two forms without knowing
            # the agent id cannot tell that they name the same session.
            "agent_id": config.OC_AGENT,
            # PREFER THE RELAYED BLOCK, and note it sits AFTER `**st` — which is
            # exactly why a pre-check on `st` did nothing: this literal wins.
            # `_token_source()` reads THIS host's env and secrets dir, and on a
            # two-host install the gateway credential lives only on the agent
            # host by design. So the local read is always empty, and GatewayCard
            # rendered "Without a token the gateway runtime cannot connect" beside
            # a Connection row saying connected. A proxying runtime relays the
            # answer from the machine that actually holds it.
            "token": (st["token"] if isinstance(st.get("token"), dict)
                      else {"configured": bool(_token_source()),
                            "source": _token_source()}),
            "apps_origin": apps_origin.warning()}


def _token_source() -> str:
    """Where the gateway token came from — never the token itself."""
    import os
    from . import settings
    if os.environ.get("AVA_OC_GATEWAY_TOKEN"):
        return "env"
    return "file" if settings.secret("openclaw_gateway_token") else ""


@router.post("/api/gateway/reconnect")
async def gateway_reconnect():
    """The owner's "try again" — redial now instead of waiting out the backoff."""
    rt = runtime.configured()
    client = rt.control_plane()
    if client is None:
        return _fail("agent_no_gateway", "this runtime has no gateway")
    client.reconnect()
    return {"ok": True}


@router.websocket("/ws/gateway")
async def gateway_events(ws: WebSocket):
    """The event relay. EVENTS ONLY — requests go over /api/gateway/rpc.

    Splitting them means a dropped socket never loses a mutation, and every
    side-effecting call funnels through one place that can audit it.

    The frame vocabulary is AVA'S, deliberately not the gateway's:

        client -> {op:"subscribe"|"unsubscribe", topics:[...]}
                  {op:"resume", after:<seq>} | {op:"ping"}
        server -> {op:"event", topic, seq, payload} | {op:"gap", from, to}
                  {op:"state", phase, why} | {op:"dropped", n} | {op:"pong"}

    A client that learns the gateway's own frame shapes is a client that breaks
    when they change; this is the same translation `AgentRuntime.iter_run` does
    for the turn path.
    """
    why = await ws_auth.guard(ws)
    if why:
        await ws_auth.refuse(ws, why)
        return

    rt = runtime.configured()
    client = rt.control_plane()
    if client is None:
        await ws.accept()
        await ws.send_json({"op": "state", "phase": "unconfigured",
                            "why": "this runtime has no gateway control plane"})
        await ws.close()
        return

    await ws.accept()
    sub = client.asubscribe()
    deadline = (time.time() + config.AGENT_GATEWAY_WS_MAX_LIFETIME
                if config.AGENT_GATEWAY_WS_MAX_LIFETIME else None)
    try:
        st = client.status()
        await ws.send_json({"op": "state", "phase": st.get("phase"),
                            "why": st.get("why") or ""})
        import asyncio
        reader = asyncio.create_task(_pump_client(ws, sub))
        try:
            while True:
                if deadline and time.time() > deadline:
                    # A socket is authorized once, at the handshake, and never
                    # re-checked — so a panel left open all day outlives any
                    # credential TTL. That is standard websocket behaviour
                    # rather than a bug, but an owner who wants periodic
                    # re-authentication gets it from this setting.
                    await ws.send_json({"op": "state", "phase": "expired",
                                        "why": "socket lifetime reached"})
                    break
                ev = await sub.aget(timeout=1.0)
                if ev is None:
                    continue
                topic = ev.get("topic") or ""
                await ws.send_json({"op": "event", "topic": topic,
                                    "seq": ev.get("seq"),
                                    "payload": ev.get("payload")})
                # ALSO publish the translation, when this frame is turn
                # progress. Panels that want the gateway's own topics still get
                # them above; the chat client subscribes to `ava.run` and never
                # learns an OpenClaw event name. The alternative is a second
                # copy of that table in TypeScript, which a rename upstream
                # would break in a language nobody thought to check.
                run = rt.translate_event(topic, ev.get("payload") or {})
                if run is not None:
                    # `sessionKey` is the field chat events actually carry
                    # (captured live: full form 'agent:main:...'); only agent
                    # lifecycle frames also have a `sessionId` uuid. Both ride
                    # along — sessionId stays for compatibility even though it
                    # is usually None here — because the chat client maps the
                    # key back to a chat id and had nothing to map with.
                    await ws.send_json({"op": "event", "topic": "ava.run",
                                        "seq": ev.get("seq"),
                                        "payload": {**run,
                                                    "runId": (ev.get("payload") or {}).get("runId"),
                                                    "sessionId": (ev.get("payload") or {}).get("sessionId"),
                                                    "sessionKey": (ev.get("payload") or {}).get("sessionKey")}})
                if sub.dropped:
                    # Tell the client it lost some, so it refetches instead of
                    # quietly rendering a list with a hole in it.
                    n, sub.dropped = sub.dropped, 0
                    await ws.send_json({"op": "dropped", "n": n})
        finally:
            reader.cancel()
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        # The socket went away mid-send. Not worth raising: the connection is
        # over either way, and a traceback per closed tab is noise.
        pass
    finally:
        sub.close()


async def _pump_client(ws: WebSocket, sub) -> None:
    """Read the client's control frames (subscribe / resume / ping).

    A separate task because the send loop is the one that must not stall: a
    client that never speaks would otherwise block every event behind a receive
    that never completes.
    """
    try:
        while True:
            msg = await ws.receive_json()
            op = str((msg or {}).get("op") or "")
            if op == "subscribe":
                topics = msg.get("topics")
                sub.topics = frozenset(topics) if topics else None
            elif op == "unsubscribe":
                sub.topics = frozenset()
            elif op == "ping":
                await ws.send_json({"op": "pong"})
    except (WebSocketDisconnect, RuntimeError, ValueError):
        return
