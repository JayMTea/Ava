#!/usr/bin/env python3
"""Hearthwire — a FICTIONAL example app that embeds in Ava.

A fictional home hub. It is the example that moves things in the real world, so its write tools are `physical` and the door asks first.

It is invented for the examples and the screenshots; there is no such product, and
nothing here talks to a real service. What it does implement is the whole contract
a third-party app needs in order to earn a left-rail tile, an embedded UI, health
monitoring and live agent tools, with no change to Ava's code:

  GET  /                        its web UI (theme-aware via ?theme=light|dark)
  GET  /health                  {"ok": true}                  <- dashboard probe
  GET  /tools                   {"tools": [...]}               <- agent tool schemas
  POST /call {name, arguments}  {"text": "..."}                <- agent tool invoke

Run it:  python examples/hearthwire/server.py
Then:    cp -r examples/hearthwire "$AVA_HOME/connectors/hearthwire"  and restart Ava.

The tiers in connector.yaml are the part worth reading — see docs/CONNECTOR_SDK.md.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("HEARTHWIRE_PORT", "8483"))
ACCENT = "#e8a33d"

# The ava-tools/1 facade. `access` here is the app's own self-report; for a
# loopback connector Ava takes the MANIFEST's `dynamic_access` as authoritative,
# which is why connector.yaml restates it rather than trusting this field.
TOOLS = [
    {
        "name": "get_temperature",
        "description": "Indoor temperature and thermostat target.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        },
        "access": "read"
    },
    {
        "name": "list_lights",
        "description": "Which lights are on, and at what level.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        },
        "access": "read"
    },
    {
        "name": "run_scene",
        "description": "Activate a scene.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string"
                }
            },
            "required": [
                "name"
            ]
        },
        "access": "physical"
    },
    {
        "name": "unlock_front_door",
        "description": "Unlock the front door.",
        "inputSchema": {
            "type": "object",
            "properties": {}
        },
        "access": "physical"
    }
]

_REPLY = {
    "get_temperature": "Indoor 21.5\u00b0C, target 21.0\u00b0C, heating idle.",
    "list_lights": "On: kitchen 80%, hallway 30%, lounge 45%, porch 100%.",
    "run_scene": "Scene '{name}' applied. Declared `physical` because it switches real relays \u2014 Ava will not infer that tier for you.",
    "unlock_front_door": "This is the one the manifest lists under `confirm:`. A lock is the call that can let a stranger in, so it is not treated like a lamp."
}


def call_tool(name: str, args: dict) -> str:
    if name not in _REPLY:
        return ""
    try:
        return _REPLY[name].format(**args)
    except (KeyError, IndexError):
        return _REPLY[name]


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hearthwire</title><style>
  :root { color-scheme: dark; --bg:#12141c; --fg:#e8ecf4; --mut:#8b93a7;
           --card:#1a1e29; --line:#262b39; --accent:__ACCENT__; }
  html[data-theme="light"] { color-scheme: light; --bg:#f7f8fb; --fg:#171a21;
           --mut:#5c6474; --card:#ffffff; --line:#e4e7ef; }
  * { box-sizing:border-box; }
  body { margin:0; padding:22px; background:var(--bg); color:var(--fg);
          font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif; }
  header { display:flex; align-items:center; gap:11px; margin-bottom:4px; }
  .dot { width:11px; height:11px; border-radius:3px; background:var(--accent); }
  h1 { font-size:19px; margin:0; letter-spacing:-.01em; }
  .tag { color:var(--mut); font-size:13px; margin:0 0 20px 22px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:12px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:11px;
           padding:14px 15px; }
  .k { color:var(--mut); font-size:12px; text-transform:uppercase;
        letter-spacing:.05em; margin-bottom:7px; }
  .v { font-size:25px; font-weight:600; letter-spacing:-.02em; }
  .s { color:var(--mut); font-size:12px; margin-top:4px; }
  h2 { font-size:12px; color:var(--mut); text-transform:uppercase;
        letter-spacing:.05em; margin:0 0 9px; }
  ul { list-style:none; margin:0; padding:0;
        border:1px solid var(--line); border-radius:11px; overflow:hidden; }
  li { display:flex; justify-content:space-between; gap:12px; padding:11px 15px;
        background:var(--card); border-bottom:1px solid var(--line); }
  li:last-child { border-bottom:0; }
  li span:last-child { color:var(--mut); font-variant-numeric:tabular-nums; }
  footer { color:var(--mut); font-size:12px; margin-top:18px; }
</style></head><body>
  <header><div class="dot"></div><h1>Hearthwire</h1></header>
  <p class="tag">Home control · example app</p>
  <div class="grid">__STATS__</div>
  <h2>Scenes</h2>
  <ul>__ROWS__</ul>
  <footer>Fictional demo app served by examples/hearthwire/server.py — no real
  service is contacted.</footer>
<script>
  // Ava passes ?theme=light|dark when it proxies the iframe, so the embedded app
  // matches the host UI instead of fighting it.
  var t = new URLSearchParams(location.search).get('theme');
  if (t) document.documentElement.setAttribute('data-theme', t);
</script>
</body></html>"""

STATS = [('Indoor', '21.5°C', 'target 21.0°C'), ('Lights on', '4', 'of 14'), ('Front door', 'Locked', 'since 22:14')]
ROWS = [('Evening', 'lamps 40% · blinds down'), ('Away', 'all off · lock up'), ('Morning', 'kitchen 80% · heat 21°C')]


def render() -> bytes:
    cards = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v">{v}</div><div class="s">{s}</div></div>'
        for k, v, s in STATS)
    rows = "".join(f"<li><span>{a}</span><span>{b}</span></li>" for a, b in ROWS)
    return (PAGE.replace("__STATS__", cards)
                .replace("__ROWS__", rows)
                .replace("__ACCENT__", ACCENT)).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._json(200, {"ok": True})
        elif path == "/tools":
            self._json(200, {"tools": TOOLS})
        elif path in ("/", "/index.html"):
            self._send(200, render(), "text/html; charset=utf-8")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/call":
            self._json(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "bad JSON"})
            return
        name = str(payload.get("name") or "")
        text = call_tool(name, payload.get("arguments") or {})
        if not text:
            # A 404 rather than an empty 200: an unknown tool is a wiring bug, and
            # answering 200 would let it look like a tool that simply said nothing.
            self._json(404, {"error": f"unknown tool: {name}"})
            return
        self._json(200, {"text": text})

    def log_message(self, *_a) -> None:      # keep the example's output quiet
        pass


if __name__ == "__main__":
    print(f"Hearthwire (example) on http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
