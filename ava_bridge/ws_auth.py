"""The gate for WebSocket routes — because the HTTP one does not cover them.

THE HAZARD, STATED PLAINLY
--------------------------
`auth.auth_gate` is registered `app.middleware("http")` (phone_bridge.py).
Starlette's `BaseHTTPMiddleware` forwards any scope whose `type` is not `"http"`
untouched, so a `@app.websocket` route is reached with **none** of it applied:

    * no Host allowlist        (the DNS-rebinding defence)
    * no apps-origin split     (the second-origin isolation)
    * no session cookie check  (the entire auth model)
    * no audit actor           (so nothing it did is attributable)

Nothing in this repo has ever exercised that, because until now there were zero
websocket routes. That is exactly why it needs a module and a guard rather than
a comment: the next person to add one will not know, and the failure is silent —
the route simply works, for everybody.

`qa/test_01_auth_surface.py::_routes()` skips routes with no `methods`, so
websocket routes are invisible to the generated auth sweep too.
`tests/test_websocket_auth.py` is the only thing standing between the next
websocket route and being accidentally public.

THE RULE
--------
Every websocket handler awaits `guard(ws)` BEFORE `accept()`, and returns when
it gets a reason back. Refusing before accept makes Starlette answer the
handshake with HTTP 403 and never upgrade — a browser can see that in the
network panel. A 4xxx close code after a successful upgrade is much harder to
diagnose, and looks like the app crashed rather than like it said no.

Every check below calls the SAME function `auth_gate` calls, in the same order.
Reimplementing any of them here would give the app two answers to one question,
free to drift apart in exactly the direction nobody tests.
"""
from __future__ import annotations

from starlette.websockets import WebSocket

from . import apps_origin, audit, auth


async def guard(ws: WebSocket) -> str | None:
    """Refusal reason, or None to proceed. MUST be awaited BEFORE ws.accept().

    Mirrors `auth.auth_gate`'s order exactly:
      1. Host allowlist        -> 421-equivalent
      2. apps-origin split     -> wrong origin
      3. apps-origin authorize -> the embed token IS the gate over there
      4. session cookie        -> the owner
    """
    path = ws.url.path

    ok, why = auth.host_is_trusted(ws)
    if not ok:
        return f"untrusted host: {why}"

    refused = apps_origin.refuses(ws, path)
    if refused is not None:
        return f"wrong origin: {refused}"

    if apps_origin.configured() and apps_origin.on_apps_host(ws):
        # Reached only for /apps/*. There is no session cookie on the apps
        # origin by design, so the embed token is the whole gate — and unlike
        # the HTTP path this one can only READ a token, never mint one: a
        # WebSocket handshake response cannot reliably carry a Set-Cookie the
        # browser will keep. The document load that precedes it is where the
        # cookie comes from.
        allowed, _set, reason = apps_origin.authorize(ws, path)
        if not allowed:
            return f"forbidden: {reason}"
        audit.set_actor("agent")
        return None

    if not auth.is_authed(ws):
        return "not authenticated"

    # For the socket's lifetime. Everything it does is the owner's doing, and
    # the ledger has to say so — a websocket that writes audit lines with no
    # actor is worse than one that writes none.
    audit.set_actor("owner")
    return None


async def refuse(ws: WebSocket, why: str) -> None:
    """Close without accepting, so the handshake fails with HTTP 403.

    Deliberately does NOT send the reason to the client: it can name a
    configured host or an origin policy, and the caller is by definition
    unauthenticated. It goes to the server's own log instead.
    """
    try:
        await ws.close(code=1008)
    except RuntimeError:
        # Already closed, or the peer went away mid-handshake. Nothing to do
        # and nothing worth raising — the connection is refused either way.
        pass
