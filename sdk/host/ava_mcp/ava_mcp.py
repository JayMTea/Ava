"""ava_mcp — serve any `ava-tools/1` facade as a REAL Model Context Protocol server.

WHY THIS EXISTS. The facade in docs/CONNECTOR_SDK.md §5 (`GET /tools` +
`POST /call`) is deliberately trivial to implement — but it is Ava's own
protocol, so an app that speaks only the facade is wired into Ava and nothing
else. MCP is the same idea with an ecosystem behind it. This adapter closes the
gap without asking app authors to rewrite anything: point it at a facade and it
speaks genuine MCP (JSON-RPC 2.0 over Streamable HTTP) to any MCP client — Ava's
`mcp:` connector block, Claude Desktop, an IDE, anything.

    facade app  ──ava-tools/1──▶  ava_mcp  ──MCP/JSON-RPC──▶  Ava (mcp: url)

Two sources ship, and they cover both ways an app arrives:

  FacadeSource    front an existing facade over HTTP. ZERO code changes in the
                  app — run this as a sidecar next to it.
  RegistrySource  serve an in-process tool registry directly, for a Python app
                  that would rather mount MCP itself than run a second process.

WHAT IT PRESERVES. `access` (read | write | destructive) is the facade's JIT
consent tier and Ava's gate reads it per call. Plain MCP has no such field, so
a naive conversion silently demotes every `read` tool to `write` and the
operator starts getting consent prompts for things that used to run silently.
This adapter carries `access` through on every `tools/list` entry — top-level
(where Ava's tools_cache reads it) and mirrored under `_meta` for MCP clients
that only tolerate spec fields. Tiers still only ever make a tool *quieter*:
egress policy, the operator's gate, and the audit ledger stay on Ava's side.

Stdlib only, like the rest of sdk/host. No dependency on Ava itself.
"""
from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

__all__ = ["FacadeSource", "RegistrySource", "ToolSource", "serve_mcp"]

# The MCP revision we advertise. Clients that ask for another get their own
# echoed back when we can serve it — version negotiation is a courtesy here
# because tools/list + tools/call have been stable across every revision.
PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "ava-mcp-adapter"
SERVER_VERSION = "1"

# JSON-RPC error codes (spec-defined; -32603 covers our transport failures).
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INTERNAL_ERROR = -32603

_TOOL_KEYS = ("name", "description", "inputSchema")


# ── Tool sources ─────────────────────────────────────────────────────────────
class ToolSource:
    """Where the adapter gets its tools. Implement two methods to serve
    anything as MCP."""

    def list_tools(self) -> list[dict]:
        """-> [{name, description, inputSchema, access?}, ...]"""
        raise NotImplementedError

    def call_tool(self, name: str, arguments: dict) -> tuple:
        """-> (result, is_error). `result` is any JSON value; it is rendered
        into MCP's text content block by the server."""
        raise NotImplementedError


class FacadeSource(ToolSource):
    """Front a running `ava-tools/1` facade (docs/CONNECTOR_SDK.md §5).

    >>> FacadeSource("http://127.0.0.1:8097", token="…")

    `token` is the credential for the *upstream app*, sent as
    `Authorization: Bearer …` on both routes — distinct from `auth_token` on
    serve_mcp(), which guards this adapter's own MCP endpoint.
    """

    def __init__(self, base_url: str, token: str | None = None,
                 tools_path: str = "/tools", call_path: str = "/call",
                 timeout: float = 180.0):
        self.base = base_url.rstrip("/")
        self.token = token or None
        self.tools_path = tools_path if tools_path.startswith("/") else "/" + tools_path
        self.call_path = call_path if call_path.startswith("/") else "/" + call_path
        self.timeout = timeout

    def _request(self, path: str, payload: dict | None = None) -> tuple:
        """-> (parsed_json, http_status). Never raises for HTTP errors: the
        facade contract puts real messages in 4xx/5xx bodies and the model is
        entitled to read them."""
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
                status = r.status
        except urllib.error.HTTPError as e:
            body, status = e.read(), e.code
        except Exception as e:  # noqa: BLE001 — app down / DNS / refused
            return {"error": f"facade unreachable: {e}"}, 502
        try:
            return json.loads(body or b"null"), status
        except ValueError:
            return {"text": body.decode("utf-8", "replace")[:4000]}, status

    def list_tools(self) -> list[dict]:
        data, status = self._request(self.tools_path)
        if status >= 400 or not isinstance(data, dict):
            raise RuntimeError(f"facade {self.tools_path} returned {status}: "
                               f"{str(data)[:200]}")
        return [t for t in (data.get("tools") or []) if isinstance(t, dict)]

    def call_tool(self, name: str, arguments: dict) -> tuple:
        data, status = self._request(self.call_path,
                                     {"name": name, "arguments": arguments or {}})
        return data, status >= 400


class RegistrySource(ToolSource):
    """Serve an in-process registry — for a Python app mounting MCP itself.

    `tools` are facade-shaped specs; `dispatch(name, arguments)` returns either
    a bare result or a `(result, status)` pair (status >= 400 marks an error),
    which is exactly what an `agent_surface`-style handler already returns.
    """

    def __init__(self, tools: list[dict], dispatch: Callable[[str, dict], object]):
        self._tools = [t for t in tools if isinstance(t, dict)]
        self._dispatch = dispatch

    def list_tools(self) -> list[dict]:
        return list(self._tools)

    def call_tool(self, name: str, arguments: dict) -> tuple:
        res = self._dispatch(name, arguments or {})
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], int):
            return res[0], res[1] >= 400
        return res, False


# ── MCP wire helpers ─────────────────────────────────────────────────────────
def _mcp_tool(spec: dict) -> dict:
    """One facade tool -> one MCP tool, carrying `access` through.

    Top-level `access` is what Ava's tools_cache reads; `_meta` is the
    spec-sanctioned home for vendor fields, so strict clients keep it too."""
    out = {k: spec[k] for k in _TOOL_KEYS if spec.get(k) is not None}
    out.setdefault("name", str(spec.get("name") or ""))
    out.setdefault("inputSchema", {"type": "object", "properties": {}})
    access = str(spec.get("access") or "").strip().lower()
    if access:
        out["access"] = access
        out["_meta"] = {**(spec.get("_meta") or {}), "ava/access": access}
    return out


def _content(result) -> list[dict]:
    """Render a tool result as MCP content. Text passes through; everything
    else becomes compact JSON — models read it fine and it stays small."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict) and isinstance(result.get("text"), str) \
            and set(result) == {"text"}:
        text = result["text"]          # the facade's own {"text": …} shape
    else:
        text = json.dumps(result, separators=(",", ":"), default=str)
    return [{"type": "text", "text": text}]


# ── Server ───────────────────────────────────────────────────────────────────
def serve_mcp(source: ToolSource, host: str = "127.0.0.1", port: int = 9300,
              path: str = "/mcp", auth_token: str | None = None,
              server_name: str = SERVER_NAME, block: bool = True,
              instructions: str | None = None) -> ThreadingHTTPServer:
    """Serve `source` as MCP over Streamable HTTP at http://host:port{path}.

    `auth_token` guards this endpoint: when set, every MCP request must carry
    `Authorization: Bearer <auth_token>`. Point Ava at it with:

        mcp:
          url: "http://127.0.0.1:9300/mcp"
          token_env: MYAPP_MCP_TOKEN

    `GET /health` is always open (unauthenticated) so a connector's
    `service.probe` can see the adapter is up without holding a credential.
    Set block=False to run it on a background thread.
    """
    mcp_path = path if path.startswith("/") else "/" + path
    sessions: set = set()
    lock = threading.Lock()

    def handle(msg: dict) -> dict | None:
        """One JSON-RPC message -> one response (None for notifications)."""
        method = str(msg.get("method") or "")
        mid = msg.get("id")
        is_notification = "id" not in msg

        def ok(result) -> dict | None:
            return None if is_notification else {"jsonrpc": "2.0", "id": mid,
                                                 "result": result}

        def fail(code: int, message: str) -> dict | None:
            return None if is_notification else {"jsonrpc": "2.0", "id": mid,
                                                 "error": {"code": code,
                                                           "message": message}}

        if method == "initialize":
            want = str((msg.get("params") or {}).get("protocolVersion") or "")
            info: dict = {
                "protocolVersion": want or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": server_name, "version": SERVER_VERSION},
            }
            if instructions:
                info["instructions"] = instructions
            return ok(info)

        # Post-init handshake + keepalive: acknowledge and move on.
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return ok({})

        if method == "tools/list":
            try:
                tools = [_mcp_tool(t) for t in source.list_tools()]
            except Exception as e:  # noqa: BLE001 — upstream is not our process
                return fail(_INTERNAL_ERROR, f"tools/list failed: {e}")
            return ok({"tools": tools})

        if method == "tools/call":
            params = msg.get("params") or {}
            name = str(params.get("name") or "").strip()
            if not name:
                return fail(_INVALID_REQUEST, "tools/call requires a tool name")
            try:
                result, is_error = source.call_tool(name, params.get("arguments") or {})
            except Exception as e:  # noqa: BLE001 — a crashing tool is data, not a 500
                return ok({"content": _content(f"error running {name}: {e}"),
                           "isError": True})
            # Tool-level failures ride back as isError, NOT as a JSON-RPC error:
            # the model is meant to read them and try something else.
            return ok({"content": _content(result), "isError": bool(is_error)})

        return fail(_METHOD_NOT_FOUND, f"unknown method: {method}")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, obj=None, session: str | None = None) -> None:
            data = b"" if obj is None else json.dumps(obj).encode("utf-8")
            self.send_response(code)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if session:
                self.send_header("Mcp-Session-Id", session)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def _authorized(self) -> bool:
            if not auth_token:
                return True
            got = (self.headers.get("Authorization") or "").strip()
            return got.startswith("Bearer ") and secrets.compare_digest(
                got[7:].strip(), auth_token)

        def do_GET(self):
            route = self.path.split("?")[0]
            if route == "/health":
                return self._send(200, {"ok": True, "server": server_name})
            if route == mcp_path:
                # Streamable HTTP allows a server→client SSE stream on GET. We
                # are request/response only (tools have nothing to push), so say
                # so plainly rather than holding a socket open.
                return self._send(405, {"error": "this MCP endpoint is POST-only"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path.split("?")[0] != mcp_path:
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return self._send(401, {"error": "missing or invalid bearer token"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                msg = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": _PARSE_ERROR,
                                                  "message": "invalid JSON"}})
            if not isinstance(msg, dict):
                # JSON-RPC batches are legal but nothing Ava sends; refusing is
                # honest and keeps the adapter one message deep.
                return self._send(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": _INVALID_REQUEST,
                                                  "message": "batch requests are not supported"}})
            session = (self.headers.get("Mcp-Session-Id") or "").strip() or None
            if msg.get("method") == "initialize":
                session = secrets.token_urlsafe(16)
                with lock:
                    sessions.add(session)
            resp = handle(msg)
            # A notification gets 202 and an empty body — clients (Ava's
            # included) don't read one and must not block waiting for it.
            if resp is None:
                return self._send(202, None, session)
            self._send(200, resp, session)

        def log_message(self, *a):  # quiet by default; the app owns the logs
            pass

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    if block:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
    else:
        threading.Thread(target=httpd.serve_forever, daemon=True,
                         name=f"ava-mcp-{port}").start()
    return httpd
