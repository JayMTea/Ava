#!/usr/bin/env python3
"""Example third-party DEVICE app — wire your own hardware to Ava.

This is the copy-paste "hello sensor". It stands in for YOUR app: the part that
talks to an Arduino / Nicla / Portenta / ESP32 / a smart-home hub over whatever
protocol your hardware uses (serial, BLE, MQTT, HTTP…). Ava never speaks those
protocols — your app does, and exposes two tiny HTTP contracts:

  PULL  (Ava reads/commands on demand)
    GET  /tools                    -> {"tools":[ ... ]}   MCP-style tool schemas
    POST /call {name, arguments}   -> {"text": "..."}     invoke a tool
    GET  /                         -> a small status UI (optional)
    GET  /health                   -> {"ok": true}        (dashboard probe)

  PUSH  (your app hands Ava an event when IT decides)
    -> POST {AVA_URL}/api/connectors/{CID}/events
       Authorization: Bearer {AVA_INGEST_TOKEN}
       {"name":"motion","message":"...","notify":true}

Run it:
    export AVA_URL=http://localhost:8096          # your Ava
    export AVA_CID=device-demo                     # this connector's id
    export AVA_INGEST_TOKEN=$(ava device token device-demo | ...)   # the bearer token
    python examples/device-app/server.py
Then drop examples/device-app/connector.yaml into $AVA_HOME/connectors/device-demo/,
restart Ava, and ask her to "read the demo temperature" or watch the pushed events.
"""
import json
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("DEVICE_APP_PORT", "8479"))
AVA_URL = os.environ.get("AVA_URL", "http://localhost:8096").rstrip("/")
AVA_CID = os.environ.get("AVA_CID", "device-demo")
AVA_INGEST_TOKEN = os.environ.get("AVA_INGEST_TOKEN", "")

# ── YOUR device state (here: faked; in real life, read from serial/BLE/MQTT) ──
_state = {"temperature": 21.5, "relay": False}


def read_temperature():
    # e.g. ser.write(b"T\n"); return float(ser.readline())
    return _state["temperature"]


def set_relay(on: bool):
    # e.g. ser.write(b"R1\n" if on else b"R0\n")
    _state["relay"] = bool(on)
    return _state["relay"]


# ── PULL: the MCP-style tool surface Ava discovers (actions.discover) ─────────
TOOLS = [
    {"name": "read_temperature", "description": "Read the demo sensor temperature (°C).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "set_relay", "description": "Turn the demo relay on or off.",
     "inputSchema": {"type": "object",
                     "properties": {"on": {"type": "boolean"}}, "required": ["on"]}},
]


def call_tool(name: str, args: dict) -> str:
    if name == "read_temperature":
        return f"The demo temperature is {read_temperature():.1f} °C."
    if name == "set_relay":
        return f"Relay is now {'ON' if set_relay(args.get('on')) else 'OFF'}."
    return f"unknown tool: {name}"


# ── PUSH: hand Ava an event when your app decides something happened ──────────
def push_event(payload: dict) -> None:
    """POST one event to Ava's inbound channel. No-op if not configured."""
    if not AVA_INGEST_TOKEN:
        return
    req = urllib.request.Request(
        f"{AVA_URL}/api/connectors/{AVA_CID}/events",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + AVA_INGEST_TOKEN})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[device-app] push failed: {e}")


def _demo_pusher():
    """Demonstrate the push channel: stream a reading every 30s and simulate a
    motion event once at startup. Replace with your real event logic."""
    time.sleep(2)
    push_event({"name": "motion", "message": "Front door motion detected",
                "severity": "warn", "notify": True})
    while True:
        _state["temperature"] = round(20 + 3 * (0.5 - (time.time() % 60) / 60), 1)
        push_event({"type": "reading", "name": "temperature",
                    "value": _state["temperature"], "unit": "C"})
        time.sleep(30)


PAGE = """<!doctype html><meta charset=utf-8><title>Device App</title>
<style>body{font-family:system-ui;margin:40px;max-width:640px}code{background:#eee;padding:2px 5px;border-radius:4px}</style>
<h1>🔌 Example device app</h1>
<p>This stands in for <b>your</b> app. Ava reads its tools
(<code>read_temperature</code>, <code>set_relay</code>) on demand, and it pushes
events to Ava (<code>motion</code>, <code>temperature</code>) on its own schedule.</p>
<p>Push configured: <b>%s</b></p>""" % ("yes" if AVA_INGEST_TOKEN else "no — set AVA_INGEST_TOKEN")


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
        if path == "/tools":
            return self._send(200, json.dumps({"tools": TOOLS}))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path.split("?")[0] != "/call":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return self._send(400, json.dumps({"error": "invalid json"}))
        text = call_tool((body.get("name") or "").strip(), body.get("arguments") or {})
        self._send(200, json.dumps({"text": text}))

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    threading.Thread(target=_demo_pusher, daemon=True).start()
    print(f"[device-app] serving on http://127.0.0.1:{PORT}  (push -> {AVA_URL}/api/connectors/{AVA_CID}/events)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
