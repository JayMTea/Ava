#!/usr/bin/env python3
"""Minimal example third-party app that embeds in Ava.

Runs a tiny stdlib-only web server that Ava reverse-proxies same-origin under
/apps/hello/. It serves:
  GET  /            -> its web UI (theme-aware via ?theme=light|dark)
  GET  /health      -> {"ok": true}                       (dashboard probe)
  GET  /tools       -> {"tools": [ ... ]}                  (agent tool schemas)
  POST /call {name, arguments} -> {"text": "..."}          (agent tool invoke)

This is the whole contract a third-party app implements to gain: a left-rail
tile, embedded UI, health monitoring, and live agent tools — with no changes to
Ava's code. Run it:  python examples/hello-app/server.py
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HELLO_APP_PORT", "8477"))

# The ava-tools/1 facade (docs/CONNECTOR_SDK.md §5): name, description,
# inputSchema, and an access tier that drives Ava's JIT consent — `read` runs
# silently, `write` asks the operator on first use, `destructive` always asks.
TOOLS = [
    {"name": "hello_ping", "description": "Health check — returns pong.",
     "inputSchema": {"type": "object", "properties": {}},
     "access": "read"},
    {"name": "hello_echo", "description": "Echo back a message.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]},
     "access": "read"},
]


def call_tool(name: str, args: dict) -> str:
    if name == "hello_ping":
        return "pong — the Hello App tool was reached through Ava's connector proxy."
    if name == "hello_echo":
        return f"echo: {args.get('text', '')}"
    return f"unknown tool: {name}"


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hello App</title><style>
  :root {{ --bg:#ffffff; --fg:#111827; --muted:#6b7280; --card:#f3f4f6; --line:#e5e7eb; }}
  [data-theme="dark"] {{ --bg:#0b0d10; --fg:#e6e8eb; --muted:#9aa4b2; --card:#15181d; --line:#242a31; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
         background:var(--bg); color:var(--fg); display:flex; min-height:100vh;
         align-items:center; justify-content:center; padding:24px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:16px;
          padding:32px 36px; max-width:520px; text-align:center; }}
  h1 {{ margin:0 0 8px; font-size:22px; }}
  p {{ color:var(--muted); line-height:1.6; margin:8px 0; }}
  button {{ margin-top:16px; padding:10px 18px; border-radius:10px; border:1px solid var(--line);
           background:transparent; color:var(--fg); cursor:pointer; font-size:14px; }}
  code {{ background:var(--bg); padding:2px 6px; border-radius:6px; }}
  #out {{ margin-top:14px; font-size:13px; color:var(--muted); min-height:18px; }}
</style></head><body>
  <div class="card">
    <h1>Hello from a third-party app</h1>
    <p>This page is served by <code>examples/hello-app/server.py</code> on its own
       port, and embedded in Ava <b>same-origin</b> via the connector proxy at
       <code>/apps/hello/</code> — so it inherited Ava's session with no login.</p>
    <p>Ava also discovered this app's tools (<code>hello_ping</code>,
       <code>hello_echo</code>). Ask her to "ping the hello app".</p>
    <button onclick="ping()">Call /call hello_ping</button>
    <div id="out"></div>
  </div>
<script>
  // Match Ava's theme (passed as ?theme=).
  var theme = new URLSearchParams(location.search).get('theme');
  if (theme) document.documentElement.setAttribute('data-theme', theme);
  async function ping() {{
    var r = await fetch('call', {{ method:'POST', headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ name:'hello_ping', arguments:{{}} }}) }});
    var d = await r.json();
    document.getElementById('out').textContent = d.text || JSON.stringify(d);
  }}
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, json.dumps({"ok": True}))
        if path == "/.well-known/ava.json":
            # Self-description (SDK §5): lets Ava's Detect prefill its form.
            return self._send(200, json.dumps({
                "facade": "ava-tools/1", "label": "Hello App",
                "tools": "/tools", "call": "/call",
                "health": "/health", "ui": True}))
        if path == "/tools":
            return self._send(200, json.dumps({"facade": "ava-tools/1",
                                               "tools": TOOLS}))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.split("?")[0] != "/call":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return self._send(400, json.dumps({"error": "invalid json"}))
        name = (body.get("name") or "").strip()
        if not any(t["name"] == name for t in TOOLS):
            # ava-tools/1: unknown names are a 404 with an error message.
            return self._send(404, json.dumps({"error": f"unknown tool '{name}'"}))
        text = call_tool(name, body.get("arguments") or {})
        self._send(200, json.dumps({"text": text}))

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    print(f"[hello-app] serving on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
