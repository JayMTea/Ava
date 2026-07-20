"""OpenAI-compatible chat-completions stub — the "model" behind the QA suite.

DirectRuntime (the tool-less floor every fresh install runs on) POSTs
``{"messages": [...], "stream": false}`` to ``AVA_ROUTER_CHAT`` and expects the
standard ``{"choices":[{"message":{"content": ...}}]}`` shape. This stub serves
that contract from a daemon thread with zero dependencies, so the whole chat
pipeline works hermetically: no GPU, no vLLM, no network.

Scriptable per test:
    fake.reply = "canned text"            # or a callable(body) -> str
    fake.fail_next = 500                  # next request returns that status once
    fake.fail_all = 500                   # EVERY request returns that status
    fake.requests                         # every body received, for assertions

`fail_all` models a genuinely-down model: with the intent gate in front of each
turn, a single-shot `fail_next` is consumed by the gate's classify call (which
resiliently falls back to regex), so a test that needs the TURN to see the
failure too must fail every call.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_REPLY = "Hello! I'm the QA fake model, alive and well."


class FakeLLM:
    def __init__(self, port: int):
        self.port = port
        self.requests: list = []
        self.reply = DEFAULT_REPLY
        self.fail_next = None
        self.fail_all = None
        srv = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: N802 — stdlib signature
                pass

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    body = {}
                srv.requests.append({"path": self.path, "body": body})
                if srv.fail_all is not None:
                    self._send(srv.fail_all, {"error": {"message": "qa model down"}})
                    return
                if srv.fail_next is not None:
                    code, srv.fail_next = srv.fail_next, None
                    self._send(code, {"error": {"message": "qa scripted failure"}})
                    return
                text = srv.reply(body) if callable(srv.reply) else srv.reply
                self._send(200, {
                    "id": "qa-fake", "object": "chat.completion",
                    "model": "qa-fake-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": text}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8,
                              "total_tokens": 20},
                })

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
        return f"http://127.0.0.1:{self.port}/v1/chat/completions"

    def start(self):
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name="qa-fake-llm").start()
        return self

    def reset(self):
        self.requests.clear()
        self.reply = DEFAULT_REPLY
        self.fail_next = None
        self.fail_all = None
