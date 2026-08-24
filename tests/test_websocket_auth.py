"""Every WebSocket route gates itself, because the HTTP middleware does not.

THE HAZARD
----------
`auth.auth_gate` is registered `app.middleware("http")` (phone_bridge.py), and
Starlette's `BaseHTTPMiddleware` forwards any scope whose `type` is not `"http"`
straight through. So a `@app.websocket` route is reached with none of it applied:
no Host allowlist, no apps-origin split, no session cookie, no audit actor.

That was harmless for as long as this repo had zero websocket routes. It stopped
being harmless the moment `/ws/gateway` landed, and the failure is SILENT — the
route simply works, for everybody, including a caller who never authenticated.

WHY THIS FILE IS THE ONLY THING WATCHING
----------------------------------------
`qa/test_01_auth_surface.py::_routes()` enumerates by `methods`, and a websocket
route has none — so the generated auth sweep, which is the real safety net for
every other route in the app, cannot see these at all. Neither can
`tests/test_auth.py`, for the same reason.

Style: `git ls-files` + AST, no bridge, no network — the same shape as
tests/test_no_blocking_routes.py, whose docstring makes the same argument about
guards that stop checking when code moves.
"""
import ast
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tracked_py() -> list[str]:
    """Every tracked module that could define a route — discovered, not listed.

    A fixed file list stops checking when code moves away from it, which is the
    worst failure mode a guard has.
    """
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines()
            if ln and not ln.startswith(("tests/", "qa/", "sdk/", "tools/"))]


def _is_websocket_decorator(node: ast.AST) -> bool:
    """Matches `@app.websocket(...)` / `@router.websocket(...)`."""
    func = node.func if isinstance(node, ast.Call) else node
    return isinstance(func, ast.Attribute) and func.attr == "websocket"


def _websocket_handlers() -> list[tuple[str, ast.AST]]:
    found = []
    for rel in _tracked_py():
        try:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if any(_is_websocket_decorator(d) for d in node.decorator_list):
                found.append((f"{rel}:{node.name}", node))
    return found


def _calls_in(node: ast.AST) -> list[tuple[str, int]]:
    """(dotted-name, lineno) for every call in the body, in source order."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            out.append((f"{base}.{f.attr}" if base else f.attr, n.lineno))
        elif isinstance(f, ast.Name):
            out.append((f.id, n.lineno))
    return sorted(out, key=lambda x: x[1])


def test_the_scan_actually_finds_the_websocket_routes() -> None:
    """A guard with no subjects passes vacuously.

    `tests/test_event_vocabulary.py` carries the same self-check for the same
    reason: the day someone moves the routes, this file must fail rather than
    quietly start approving nothing.
    """
    handlers = _websocket_handlers()
    assert handlers, (
        "no @app.websocket / @router.websocket handlers found. Either they "
        "moved out of the scanned paths, or the decorator shape changed — "
        "either way this guard has stopped guarding.")


def test_every_websocket_handler_gates_before_accept() -> None:
    """`ws_auth.guard(ws)` must be awaited BEFORE any `accept()`.

    Order matters, not just presence. Refusing before accept makes Starlette
    answer the handshake with HTTP 403 and never upgrade, which a browser can
    diagnose. A 4xxx close AFTER a successful upgrade looks like the app
    crashed, and by then the handler has already run.
    """
    offenders = []
    for where, node in _websocket_handlers():
        calls = _calls_in(node)
        guard_at = next((ln for name, ln in calls
                         if name in ("ws_auth.guard", "guard")), None)
        accept_at = next((ln for name, ln in calls if name.endswith(".accept")),
                         None)
        if guard_at is None:
            offenders.append(f"{where}: never calls ws_auth.guard()")
        elif accept_at is not None and accept_at < guard_at:
            offenders.append(
                f"{where}: accept() at line {accept_at} runs before "
                f"ws_auth.guard() at line {guard_at}")
    assert not offenders, (
        "auth_gate is HTTP-only — Starlette forwards non-HTTP scopes past it "
        "untouched — so a websocket route is PUBLIC unless it gates itself. "
        "Await ws_auth.guard(ws) before ws.accept():\n  "
        + "\n  ".join(sorted(offenders)))


def test_the_gate_reuses_the_http_checks_rather_than_reimplementing_them() -> None:
    """Two answers to "is this caller the owner" is how they drift apart.

    `ws_auth` must call the same functions `auth_gate` calls. A hand-rolled
    cookie comparison there would pass every test in this file and still be
    wrong the first time the session format changes.
    """
    src = (ROOT / "ava_bridge" / "ws_auth.py").read_text(encoding="utf-8")
    for needed in ("auth.host_is_trusted", "auth.is_authed",
                   "apps_origin.refuses", "apps_origin.authorize",
                   "audit.set_actor"):
        assert needed in src, (
            f"ws_auth.py does not call {needed}, so the websocket gate and the "
            f"HTTP gate no longer agree on what an authorised caller is.")
    assert "COOKIE_NAME" not in src and "constant_time_equals" not in src, (
        "ws_auth.py appears to verify the session cookie itself. Call "
        "auth.is_authed() — one verifier, one answer.")


def test_the_gate_refuses_without_accepting() -> None:
    """`close()` before `accept()` is what makes the handshake a clean 403."""
    src = (ROOT / "ava_bridge" / "ws_auth.py").read_text(encoding="utf-8")
    refuse = src[src.index("async def refuse("):]
    assert ".accept(" not in refuse, (
        "ws_auth.refuse() accepts the socket before closing it, which upgrades "
        "the connection first and turns a refusal into a mystery disconnect.")


def test_the_hazard_is_written_down_where_the_next_author_will_look() -> None:
    """The next websocket route will be added by someone who has not read this
    file. The note has to be where they are working."""
    bridge = (ROOT / "phone_bridge.py").read_text(encoding="utf-8")
    assert "ws_auth.guard" in bridge and "middleware(\"http\")" in bridge, (
        "phone_bridge.py no longer warns that auth_gate is HTTP-only and that "
        "websocket routes must gate themselves. That comment is load-bearing.")


# ---------------------------------------------------------------------------
# The live half. The static scans above prove the gate is CALLED; these prove it
# WORKS — which is a different claim, and the one that matters if the shared
# auth helpers ever change shape under it.
# ---------------------------------------------------------------------------

def _ws_app():
    import phone_bridge
    return phone_bridge.app


# TestClient's websocket_connect sends `Host: testserver` no matter what
# base_url says, and Ava's rebinding defence correctly refuses that — which is
# itself proof the host allowlist covers sockets. Every test below therefore
# states the Host it means, so a refusal is never ambiguous about WHICH check
# fired.
_LOCAL = {"host": "localhost"}


def test_an_unauthenticated_socket_is_refused_at_the_handshake() -> None:
    """The whole point. Before `ws_auth`, this connection succeeded — for
    anybody who could reach the port."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    client = TestClient(_ws_app(), base_url="http://localhost")
    try:
        with client.websocket_connect("/ws/gateway", headers=_LOCAL):
            raise AssertionError(
                "an unauthenticated websocket was ACCEPTED. auth_gate is "
                "HTTP-only, so /ws/gateway is public unless ws_auth.guard "
                "refuses it before accept().")
    except WebSocketDisconnect:
        pass          # closed before accept — correct
    except Exception as e:
        # Starlette surfaces a pre-accept close as a handshake failure; the
        # exact type varies by version, so assert the OUTCOME (no session) and
        # not the class.
        assert "403" in str(e) or "denied" in str(e).lower() or "reject" in str(e).lower(), (
            f"the socket failed, but not in a way that reads as a refusal: {e!r}")


def test_an_authenticated_socket_is_accepted() -> None:
    """The gate must not be a wall. A guard that refuses everyone passes the
    test above and ships a broken feature."""
    from fastapi.testclient import TestClient

    from ava_bridge import auth, config

    client = TestClient(_ws_app(), base_url="http://localhost")
    client.cookies.set(config.COOKIE_NAME, auth._make_token())
    with client.websocket_connect("/ws/gateway", headers=_LOCAL) as ws:
        first = ws.receive_json()
        assert first.get("op") == "state", (
            f"expected an opening state frame, got {first!r}")


def test_an_untrusted_host_is_refused_before_the_cookie_is_even_read() -> None:
    """The DNS-rebinding defence covers sockets too.

    A rebound request is same-origin to the browser, so the cookie is sent and
    looks valid — only the server noticing it was asked for under a name it does
    not answer to can catch it. That check has to run FIRST, which is why
    ws_auth mirrors auth_gate's order rather than inventing its own.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from ava_bridge import auth, config

    client = TestClient(_ws_app(), base_url="http://localhost")
    client.cookies.set(config.COOKIE_NAME, auth._make_token())
    try:
        with client.websocket_connect(
                "/ws/gateway", headers={"host": "evil.example.com"}):
            raise AssertionError(
                "a socket for an untrusted Host was accepted, so the "
                "rebinding defence does not cover websockets")
    except WebSocketDisconnect:
        pass
    except Exception as e:
        assert "403" in str(e) or "denied" in str(e).lower() or "reject" in str(e).lower(), (
            f"refused, but not recognisably: {e!r}")
