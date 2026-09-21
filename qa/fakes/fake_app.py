"""A "user's app" — the thing a new user connects via the Setup Hub.

Speaks the shapes the connector layer probes and proxies (docs/CONNECTOR_SDK.md):
    GET  /health        — service probe target
    GET  /tools         — ava-tools/1 discovery list
    POST /call          — {"name", "arguments"} -> result
    GET  /api/ping      — a declared read-tier REST action
    POST /api/echo      — a declared write-tier REST action

and, for the sidebar tile the `ui:` block asks for, its own web UI:
    GET  /, /reports, /settings   — a single-page app, the framed document
    GET  /locked                  — the same page under a CSP that bars inline
    GET  /app.js                  — that page's script, for the locked variant

The UI knows NOTHING about Ava: no postMessage, no `ava:` message, no shell
origin, no protocol of any kind. That is the point. Route memory for an
embedded tile is the BRIDGE's job (ava_bridge/embed_route.py injects a shim into
the app's own document as it proxies it), and this fixture is the app that
proves it works for an author who never wrote a line of it —
qa/e2e/embedded-route-memory.spec.ts drives it through the real bridge.

Everything received is recorded on ``.calls`` so tests can assert the bridge
actually forwarded (or was blocked from forwarding) a call.
"""
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOLS = [
    {"name": "lookup", "description": "Look up a QA record",
     "access": "read",
     "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "mutate", "description": "Change a QA record",
     "access": "write",
     "input_schema": {"type": "object", "properties": {"v": {"type": "string"}}}},
]

# The SPA's whole behaviour. Its destinations are RELATIVE — an app served under
# a mount it does not choose (here `/apps/<cid>/`) cannot write absolute paths,
# and a real basePath app does exactly this. The readout is the app's own idea
# of where it is, so a test can compare it against the address the shell shows.
_SCRIPT = """
var out = document.getElementById('route');
function render() {
  out.textContent = location.pathname + location.search + location.hash;
}
function push(to) { history.pushState(null, '', to); render(); }
document.getElementById('go-reports').onclick = function () { push('reports?tab=open'); };
document.getElementById('go-settings').onclick = function () { push('settings'); };
document.getElementById('go-section').onclick = function () { location.hash = 'section-two'; };
document.getElementById('go-load').onclick = function () { location.assign('reports?tab=closed'); };
document.getElementById('go-locked').onclick = function () { location.assign('locked'); };
document.getElementById('go-home').onclick = function () { push('./'); };
window.addEventListener('popstate', render);
window.addEventListener('hashchange', render);
render();
"""

# `<meta charset>` first, because that is where the injector is documented to
# insert AFTER — a fixture whose head has nothing to get in front of would
# exercise the easy branch only.
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>QA E2E App</title>
</head>
<body>
<h1>QA E2E App</h1>
<output id="route"></output>
<button id="go-reports">Reports</button>
<button id="go-settings">Settings</button>
<button id="go-section">Section two</button>
<button id="go-load">Closed reports</button>
<button id="go-locked">Locked page</button>
<button id="go-home">Home</button>
<input id="draft" />
%s
</body>
</html>
"""

_INLINE_PAGE = (_PAGE % ("<script>%s</script>" % _SCRIPT)).encode()
# The locked variant's own script has to be a file too: its CSP bars inline
# script for everyone, this fixture included.
_LOCKED_PAGE = (_PAGE % '<script src="app.js"></script>').encode()
_SCRIPT_BODY = _SCRIPT.encode()

# Client-side routes. A reload lands the frame on one of these directly, so each
# has to answer with the page — the SPA fallback any framed app needs. Anything
# NOT listed keeps the old JSON 404, so nothing that already relies on this
# fixture's non-HTML behaviour changes.
_PAGES = {"/": _INLINE_PAGE, "/reports": _INLINE_PAGE, "/settings": _INLINE_PAGE,
          "/locked": _LOCKED_PAGE}

# Only inline script is barred, which is what makes the bridge fall back to the
# external form of its shim. The policy is never edited by the proxy, so this is
# also the check that it is passed through untouched.
_LOCKED_CSP = "script-src 'self'"


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:16] + '"'


class FakeApp:
    def __init__(self, port: int):
        self.port = port
        self.calls: list = []
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                srv.calls.append({"method": "GET", "path": self.path})
                route = self.path.split("?", 1)[0]
                if self.path.startswith("/health"):
                    self._send(200, {"ok": True, "app": "qa-fake-app"})
                elif self.path.startswith("/tools"):
                    self._send(200, {"tools": TOOLS})
                elif self.path.startswith("/api/ping"):
                    self._send(200, {"pong": True})
                elif route == "/app.js":
                    self._send_asset(_SCRIPT_BODY, "text/javascript; charset=utf-8")
                elif route in _PAGES:
                    self._send_asset(
                        _PAGES[route], "text/html; charset=utf-8",
                        csp=_LOCKED_CSP if route == "/locked" else "")
                else:
                    self._send(404, {"error": "qa-fake-app: " + self.path})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = {}
                srv.calls.append({"method": "POST", "path": self.path, "body": body})
                if self.path.startswith("/call"):
                    self._send(200, {"ok": True, "tool": body.get("name"),
                                     "echo": body.get("arguments")})
                elif self.path.startswith("/api/echo"):
                    self._send(200, {"ok": True, "echo": body})
                else:
                    self._send(404, {"error": "qa-fake-app: " + self.path})

            def _send(self, code: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_asset(self, body: bytes, ctype: str, csp: str = ""):
                """A static file, WITH an ETag and a real 304 — which is the case
                that decides whether route memory reaches a returning browser.

                An app whose entry document is revalidated rather than refetched
                would otherwise keep serving a cached copy from before the shim
                existed, forever. The bridge namespaces the ETag it hands out and
                unmarks the one that comes back (embed_route), and this fixture
                is the other end of that: it answers 304 to its OWN tag only, so
                a test that gets a working page after a reload has proved the
                round trip rather than a bypass.
                """
                tag = _etag(body)
                if tag in [t.strip() for t in
                           (self.headers.get("If-None-Match") or "").split(",")]:
                    self.send_response(304)
                    self.send_header("ETag", tag)
                    if csp:
                        self.send_header("Content-Security-Policy", csp)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("ETag", tag)
                if csp:
                    self.send_header("Content-Security-Policy", csp)
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="qa-fake-app").start()
        return self

    def reset(self):
        self.calls.clear()
