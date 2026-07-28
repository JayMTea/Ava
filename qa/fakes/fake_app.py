"""A "user's app" — the thing a new user connects via the Setup Hub.

Speaks the shapes the connector layer probes and proxies (docs/CONNECTOR_SDK.md):
    GET  /health        — service probe target
    GET  /tools         — ava-tools/1 discovery list
    POST /call          — {"name", "arguments"} -> result
    GET  /api/ping      — a declared read-tier REST action
    POST /api/echo      — a declared write-tier REST action

Everything received is recorded on ``.calls`` so tests can assert the bridge
actually forwarded (or was blocked from forwarding) a call.
"""
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
                if self.path.startswith("/health"):
                    self._send(200, {"ok": True, "app": "qa-fake-app"})
                elif self.path.startswith("/tools"):
                    self._send(200, {"tools": TOOLS})
                elif self.path.startswith("/api/ping"):
                    self._send(200, {"pong": True})
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
