"""ava_device — board-agnostic host adapter for Ava device connectors.

This is the reusable version of examples/device-app/server.py. It owns the two
tiny HTTP/token contracts Ava speaks, so a device author writes ONLY their
transport code (serial, MQTT, BLE, …) and never re-hand-rolls the plumbing.

Two directions, matching docs/DEVICE_CONNECTORS.md:

  PUSH   your app -> Ava, when your app decides something happened
         AvaDevice(...).reading(...) / .event(...)

  PULL   Ava -> your app, on demand (agent reads a sensor / sends a command)
         serve_pull(...) exposes GET /tools, POST /call, GET /health

Nothing here is board-specific. For a USB Arduino running the AvaClient library's
handleStream(), SerialRelay bridges the two: it answers PULL by round-tripping
over the serial line, and forwards the board's PUSH lines to Ava.

Stdlib only. `pyserial` is imported lazily and only if you use SerialRelay.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

__all__ = ["AvaDevice", "Tool", "serve_pull", "SerialRelay"]


# ── PUSH: hand Ava events/readings ───────────────────────────────────────────
class AvaDevice:
    """Push client for one connector. Get the token from `ava device token <id>`.

    >>> ava = AvaDevice("http://localhost:8096", "greenhouse", TOKEN)
    >>> ava.reading("temperature", 21.5, "C")
    >>> ava.event("motion", "Front door", severity="warn", notify=True)
    """

    def __init__(self, ava_url: str, cid: str, token: str,
                 timeout: float = 5.0, retries: int = 2):
        self.ava_url = ava_url.rstrip("/")
        self.cid = cid
        self.token = token
        self.timeout = timeout
        self.retries = max(0, retries)

    # -- public API -----------------------------------------------------------
    def reading(self, name: str, value, unit: str | None = None,
                ts: float | None = None) -> bool:
        return self._push({"type": "reading", "name": name, "value": value,
                           "unit": unit, "ts": ts})

    def event(self, name: str, message: str | None = None,
              severity: str | None = None, notify: bool = False,
              ts: float | None = None) -> bool:
        return self._push({"type": "event", "name": name, "message": message,
                           "severity": severity, "notify": bool(notify), "ts": ts})

    def push(self, payload: dict) -> bool:
        """Send a raw event payload (see DEVICE_CONNECTORS.md for the schema)."""
        return self._push(payload)

    # -- internals ------------------------------------------------------------
    def _push(self, payload: dict) -> bool:
        body = json.dumps({k: v for k, v in payload.items() if v is not None})
        url = f"{self.ava_url}/api/connectors/{self.cid}/events"
        req = urllib.request.Request(
            url, data=body.encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self.token})
        last = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    r.read()
                    return True
            except urllib.error.HTTPError as e:
                # 4xx won't get better on retry (bad token, bad payload) — stop.
                if 400 <= e.code < 500:
                    print(f"[ava_device] push rejected ({e.code}): {url}")
                    return False
                last = e
            except Exception as e:  # noqa: BLE001 — network flakiness
                last = e
            if attempt < self.retries:
                time.sleep(0.5 * (2 ** attempt))
        print(f"[ava_device] push failed after {self.retries + 1} tries: {last}")
        return False


# ── PULL: the tool surface Ava discovers (actions.discover) ──────────────────
class Tool:
    """One capability Ava can call. `run(arguments) -> str` (human text).

    Sensors take no arguments; commands describe their args via `schema`
    (a JSON-Schema `properties` dict).
    """

    def __init__(self, name: str, description: str,
                 run: Callable[[dict], str],
                 schema: dict | None = None, required: list[str] | None = None):
        self.name = name
        self.description = description
        self.run = run
        self.schema = schema or {}
        self.required = required or []

    def spec(self) -> dict:
        s: dict = {"type": "object", "properties": self.schema}
        if self.required:
            s["required"] = self.required
        return {"name": self.name, "description": self.description, "inputSchema": s}


def serve_pull(tools: list[Tool], host: str = "127.0.0.1", port: int = 9001,
               block: bool = True) -> ThreadingHTTPServer:
    """Run the PULL facade (GET /tools, POST /call, GET /health) Ava points at
    via `actions.discover`. Returns the server; set block=False to run it in a
    background thread (e.g. alongside a serial reader)."""
    index = {t.name: t for t in tools}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/health":
                return self._send(200, {"ok": True})
            if path == "/tools":
                return self._send(200, {"tools": [t.spec() for t in tools]})
            self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != "/call":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                return self._send(400, {"error": "invalid json"})
            tool = index.get((body.get("name") or "").strip())
            if not tool:
                return self._send(200, {"text": f"unknown tool: {body.get('name')}"})
            try:
                text = tool.run(body.get("arguments") or {})
            except Exception as e:  # noqa: BLE001
                text = f"error running {tool.name}: {e}"
            self._send(200, {"text": str(text)})

        def log_message(self, *a):  # quiet
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    if block:
        httpd.serve_forever()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


# ── SerialRelay: bridge a USB Arduino (AvaClient handleStream) to Ava ─────────
class SerialRelay:
    """Bridge a board running AvaClient.handleStream() over a serial line.

    - PULL: `tools()` / `call()` round-trip a request line to the board and read
      one response line back. Feed these into `serve_pull` via Tool wrappers.
    - PUSH: `pump(ava)` runs a reader loop that forwards the board's
      `{"push":{...}}` lines to Ava.

    Line protocol (newline-delimited JSON), matching AvaClient:
      host -> board : {"req":"tools"} | {"req":"call","name":..,"arguments":{..}}
      board -> host : {"resp":"tools","tools":[..]} | {"resp":"call","text":".."}
      board -> host : {"push":{"type":"reading","name":..,"value":..}}
    """

    def __init__(self, port: str, baud: int = 115200, timeout: float = 3.0):
        try:
            import serial  # type: ignore  # pyserial, imported lazily
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("SerialRelay needs pyserial: pip install pyserial") from e
        self._serial = serial.Serial(port, baud, timeout=timeout)
        self._lock = threading.Lock()
        self._pending_push: list[dict] = []

    def _write_line(self, obj: dict) -> None:
        self._serial.write((json.dumps(obj) + "\n").encode("utf-8"))
        self._serial.flush()

    def _read_response(self, want: str, deadline: float) -> dict | None:
        """Read lines until we see the matching {"resp": want}, buffering any
        push lines that arrive in the meantime."""
        while time.time() < deadline:
            raw = self._serial.readline()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8", "ignore").strip() or "{}")
            except ValueError:
                continue
            if isinstance(msg, dict) and "push" in msg:
                self._pending_push.append(msg["push"])
            elif isinstance(msg, dict) and msg.get("resp") == want:
                return msg
        return None

    def tools(self) -> list[dict]:
        with self._lock:
            self._write_line({"req": "tools"})
            resp = self._read_response("tools", time.time() + 3.0)
        return (resp or {}).get("tools", [])

    def call(self, name: str, arguments: dict) -> str:
        with self._lock:
            self._write_line({"req": "call", "name": name, "arguments": arguments})
            resp = self._read_response("call", time.time() + 5.0)
        return (resp or {}).get("text", f"no response from board for {name}")

    def pump(self, ava: AvaDevice, block: bool = True) -> None:
        """Forward the board's push lines to Ava. Also drains any push lines that
        were buffered while waiting on a pull response."""
        def loop():
            while True:
                for p in self._drain_pending():
                    ava.push(p)
                raw = self._serial.readline()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw.decode("utf-8", "ignore").strip() or "{}")
                except ValueError:
                    continue
                if isinstance(msg, dict) and "push" in msg:
                    ava.push(msg["push"])
        if block:
            loop()
        else:
            threading.Thread(target=loop, daemon=True).start()

    def _drain_pending(self) -> list[dict]:
        with self._lock:
            out, self._pending_push = self._pending_push, []
        return out
