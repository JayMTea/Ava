"""Minimal independent ava-runtime/1 service: a real, bounded calculator tool.

Run with AVA_EXAMPLE_TOKEN set and point Ava at http://127.0.0.1:9120.
Replace answer() with your own agent. This example executes no shell commands.
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os

PROTOCOL = "ava-runtime/1"


def answer(text: str, history: list) -> dict:
    if text.startswith("sum "):
        values = [float(part) for part in text[4:].split()]
        if not values or len(values) > 1000 or not all(math.isfinite(n) for n in values):
            raise ValueError("Supply between one and 1000 finite numbers")
        result = math.fsum(values)
        if not math.isfinite(result):
            raise ValueError("Result is not finite")
        return {"reply": f"{result:g}", "tools_used": ["sum_numbers"]}
    return {"reply": "Calculator service connected. Try: sum 12 8 5", "tools_used": []}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # never log requests, authorization headers or conversation content

    def send(self, status: int, body: dict):
        payload = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        if not self.server.token or not hmac.compare_digest(self.headers.get("Authorization", ""), expected):
            self.send(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path != "/v1/runtime":
            self.send(404, {"error": "unknown route"})
            return
        self.send(200, {"protocol": PROTOCOL, "ready": True,
                        "capabilities": ["turns", "tools", "sessions.discard"]})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 1024 * 1024:
                raise ValueError("Request exceeds the size limit")
            self.connection.settimeout(10)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            if self.path == "/v1/sessions/discard":
                self.send(200, {"ok": True})  # stateless: no retained sessions
            elif self.path == "/v1/turns":
                if body.get("protocol") != PROTOCOL or not isinstance(body.get("text"), str):
                    raise ValueError("Invalid turn")
                self.send(200, answer(body["text"], body.get("history", [])))
            else:
                self.send(404, {"error": "unsupported route"})
        except (ValueError, OverflowError, OSError):
            self.send(400, {"error": "invalid request"})


def server(port: int = 9120, token: str = "") -> ThreadingHTTPServer:
    service = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    service.token = token
    return service


if __name__ == "__main__":
    token = os.environ.get("AVA_EXAMPLE_TOKEN", "")
    if not token:
        raise SystemExit("Set AVA_EXAMPLE_TOKEN before starting the example")
    with server(int(os.environ.get("AVA_EXAMPLE_PORT", "9120")), token) as service:
        print(f"Example runtime listening on 127.0.0.1:{service.server_port}", flush=True)
        service.serve_forever()
