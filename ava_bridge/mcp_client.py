"""Minimal MCP client — wrap any Model Context Protocol server as a connector.

Speaks real MCP (JSON-RPC 2.0): `initialize` -> `notifications/initialized` ->
`tools/list` / `tools/call`, over any of three transports:

  http   Streamable HTTP — POST JSON-RPC to the server URL; handles both plain
         JSON and text/event-stream responses, and echoes Mcp-Session-Id.
  sse    legacy HTTP+SSE (MCP 2024-11-05) — GET a long-lived event stream, the
         server announces a per-session POST endpoint in an `endpoint` event,
         and JSON-RPC responses arrive as `message` events on the stream. This
         is what Home Assistant's MCP Server integration speaks.
  stdio  newline-delimited JSON-RPC over a spawned subprocess (how most public
         MCP servers ship, e.g. `npx -y @modelcontextprotocol/server-*`).

The security story (why this lives behind the bridge): Ava's sandboxed agent
NEVER talks to the MCP server. It reaches only the policed bridge routes
(`/internal/connector/<id>/__tools|__call`) that its auto-generated egress
policy allow-lists; the bridge speaks MCP server-side. A stdio server runs as a
host process the operator declared in the manifest — the same trust model as
every MCP desktop client — but the agent's blast radius stays the two routes.

Deliberately dependency-free (requests + subprocess). No streaming, resources,
or prompts — tools only, which is what the connector SDK bridges.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time

from . import settings
from .version import __version__

PROTOCOL_VERSION = "2025-03-26"
_CLIENT_INFO = {"name": "ava-bridge", "version": __version__}

_LIST_TTL_S = 60.0          # tools/list cache
_HTTP_TIMEOUT = 30
_CALL_TIMEOUT = 180
_STDIO_START_TIMEOUT = 30


class McpError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Sessions — one per connector id, lazily created, restart on failure
# --------------------------------------------------------------------------- #
_sessions: dict[str, "_Session"] = {}
_sessions_lock = threading.Lock()


def _make_session(spec: dict) -> "_Session":
    t = spec["transport"]
    if t == "stdio":
        return _StdioSession(spec)
    if t == "sse":
        return _SseSession(spec)
    return _HttpSession(spec)


def _session(cid: str, spec: dict) -> "_Session":
    with _sessions_lock:
        s = _sessions.get(cid)
        if s is None or not s.alive() or s.spec != spec:
            if s is not None:
                s.close()
            s = _make_session(spec)
            _sessions[cid] = s
        return s


def reset(cid: str | None = None) -> None:
    """Drop cached session(s) — used by tests and connector reloads."""
    with _sessions_lock:
        for key in ([cid] if cid else list(_sessions)):
            s = _sessions.pop(key, None)
            if s:
                s.close()


class _Session:
    """Shared shell: id counter, init handshake state, tools cache."""

    def __init__(self, spec: dict):
        self.spec = spec
        self._id = 0
        self._lock = threading.Lock()
        self._initialized = False
        self._tools: list | None = None
        self._tools_ts = 0.0

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    # transport hook: send one JSON-RPC message; return the response for `id`
    # (None for notifications).
    def _rpc(self, payload: dict, timeout: int) -> dict | None:
        raise NotImplementedError

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        res = self._rpc({"jsonrpc": "2.0", "id": self._next_id(),
                         "method": "initialize",
                         "params": {"protocolVersion": PROTOCOL_VERSION,
                                    "capabilities": {},
                                    "clientInfo": _CLIENT_INFO}},
                        timeout=_STDIO_START_TIMEOUT)
        if not res or "error" in res:
            raise McpError(f"initialize failed: {(res or {}).get('error')}")
        self._rpc({"jsonrpc": "2.0", "method": "notifications/initialized"},
                  timeout=_HTTP_TIMEOUT)
        self._initialized = True

    def list_tools(self) -> list:
        with self._lock:
            now = time.time()
            if self._tools is not None and now - self._tools_ts < _LIST_TTL_S:
                return self._tools
            self._ensure_init()
            res = self._rpc({"jsonrpc": "2.0", "id": self._next_id(),
                             "method": "tools/list", "params": {}},
                            timeout=_HTTP_TIMEOUT)
            if not res or "error" in res:
                raise McpError(f"tools/list failed: {(res or {}).get('error')}")
            self._tools = (res.get("result") or {}).get("tools") or []
            self._tools_ts = now
            return self._tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        with self._lock:
            self._ensure_init()
            res = self._rpc({"jsonrpc": "2.0", "id": self._next_id(),
                             "method": "tools/call",
                             "params": {"name": name, "arguments": arguments}},
                            timeout=_CALL_TIMEOUT)
        if not res:
            raise McpError("tools/call: no response")
        if "error" in res:
            raise McpError(f"tools/call failed: {res['error']}")
        return res.get("result") or {}


# --------------------------------------------------------------------------- #
# HTTP transport (Streamable HTTP)
# --------------------------------------------------------------------------- #
class _HttpSession(_Session):
    def __init__(self, spec: dict):
        super().__init__(spec)
        self._mcp_session_id: str | None = None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        tok = settings.env_secret(self.spec.get("token_env"))
        if tok:
            h["Authorization"] = "Bearer " + tok
        if self._mcp_session_id:
            h["Mcp-Session-Id"] = self._mcp_session_id
        return h

    def _rpc(self, payload: dict, timeout: int) -> dict | None:
        import requests
        r = requests.post(self.spec["url"], json=payload,
                          headers=self._headers(), timeout=timeout)
        sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        if sid:
            self._mcp_session_id = sid
        if "id" not in payload:      # notification — 202/204, no body expected
            return None
        if r.status_code >= 400:
            raise McpError(f"MCP server returned {r.status_code}: {r.text[:200]}")
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        if ctype == "text/event-stream":
            return self._parse_sse(r.text, payload["id"])
        try:
            return r.json()
        except ValueError:
            raise McpError(f"MCP server returned non-JSON ({ctype})") from None

    @staticmethod
    def _parse_sse(text: str, want_id) -> dict | None:
        """Extract the JSON-RPC response matching `want_id` from an SSE body."""
        for chunk in text.split("\n\n"):
            data = "\n".join(line[5:].strip() for line in chunk.splitlines()
                             if line.startswith("data:"))
            if not data:
                continue
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id:
                return msg
        return None


# --------------------------------------------------------------------------- #
# SSE transport (legacy HTTP+SSE, MCP 2024-11-05 — Home Assistant's MCP server)
# --------------------------------------------------------------------------- #
class _SseSession(_Session):
    """GET a long-lived SSE stream; the server's first `endpoint` event names
    the per-session POST URL; JSON-RPC requests are POSTed there and their
    responses arrive as `message` events on the stream (correlated by id)."""

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._endpoint: str | None = None
        self._responses: dict = {}
        self._sse_cv = threading.Condition()
        self._dead: str | None = None
        import requests
        self._stream = requests.get(
            spec["url"], headers=self._headers(accept="text/event-stream"),
            stream=True, timeout=(_HTTP_TIMEOUT, None))  # connect timeout; stream stays open
        if self._stream.status_code >= 400:
            body = self._stream.text[:200]
            self._stream.close()
            raise McpError(f"SSE connect returned {self._stream.status_code}: {body}")
        # Grab the raw socket NOW (urllib3 detaches the connection later):
        # close() must shutdown() it to unblock the reader thread's read.
        try:
            self._sock = self._stream.raw._fp.fp.raw._sock
        except Exception:  # noqa: BLE001 — stack shape varies across versions
            self._sock = None
        threading.Thread(target=self._reader, daemon=True,
                         name=f"mcp-sse-{spec['url'][:40]}").start()
        with self._sse_cv:
            self._sse_cv.wait_for(lambda: self._endpoint or self._dead,
                                  timeout=_STDIO_START_TIMEOUT)
            if not self._endpoint:
                raise McpError(self._dead or "SSE server never sent its endpoint event")

    def _headers(self, accept: str = "application/json") -> dict:
        h = {"Accept": accept}
        tok = settings.env_secret(self.spec.get("token_env"))
        if tok:
            h["Authorization"] = "Bearer " + tok
        return h

    def _reader(self) -> None:
        """Consume the stream: `endpoint` announces the POST URL, `message`
        carries JSON-RPC; anything else (pings, server notices) is ignored.

        Line splitting is done by hand: iter_lines' default 512-byte read
        blocks until the buffer fills (starving short events), and with byte
        reads it emits phantom blank lines on CRLF endings (Home Assistant
        terminates SSE lines with \\r\\n) — a phantom blank dispatches an
        event before its data arrives. Byte reads drain the buffered socket,
        so chunk_size=1 stays cheap."""
        event, data = "", []
        buf = b""
        try:
            for chunk in self._stream.iter_content(chunk_size=1):
                if not chunk:
                    continue
                buf += chunk
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = buf[:nl].rstrip(b"\r").decode("utf-8", "replace")
                    buf = buf[nl + 1:]
                    if line == "":                   # blank line ends one event
                        self._dispatch(event or "message", "\n".join(data))
                        event, data = "", []
                    elif line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].lstrip())
        except Exception as e:  # noqa: BLE001 — stream death is reported to waiters
            self._dead = self._dead or f"SSE stream error: {e}"
        finally:
            with self._sse_cv:
                self._dead = self._dead or "SSE stream closed by server"
                self._sse_cv.notify_all()

    def _dispatch(self, event: str, data: str) -> None:
        if event == "endpoint" and data:
            from urllib.parse import urljoin
            with self._sse_cv:
                self._endpoint = urljoin(self.spec["url"], data.strip())
                self._sse_cv.notify_all()
            return
        if event != "message" or not data:
            return
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("id") is not None:
            with self._sse_cv:
                self._responses[msg["id"]] = msg
                self._sse_cv.notify_all()

    def alive(self) -> bool:
        return self._dead is None

    def close(self) -> None:
        self._dead = self._dead or "closed"
        # Shut the raw socket down FIRST: response.close() blocks on the buffer
        # lock held by the reader thread's in-flight blocking read, deadlocking
        # the caller (session reset / connector reload). shutdown() unblocks
        # that read with EOF, the reader exits, and close() is then safe.
        if self._sock is not None:
            try:
                import socket as _socket
                self._sock.shutdown(_socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 — already closed / never connected
                pass
        try:
            self._stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _rpc(self, payload: dict, timeout: int) -> dict | None:
        import requests
        r = requests.post(self._endpoint, json=payload,
                          headers=self._headers(), timeout=_HTTP_TIMEOUT)
        if r.status_code >= 400:
            raise McpError(f"MCP SSE endpoint returned {r.status_code}: {r.text[:200]}")
        if "id" not in payload:          # notification — nothing comes back
            return None
        want = payload["id"]
        with self._sse_cv:
            self._sse_cv.wait_for(lambda: want in self._responses or self._dead,
                                  timeout=timeout)
            if want in self._responses:
                return self._responses.pop(want)
        raise McpError(self._dead or f"MCP SSE response timed out after {timeout}s")


# --------------------------------------------------------------------------- #
# stdio transport (newline-delimited JSON-RPC over a subprocess)
# --------------------------------------------------------------------------- #
def _minimal_env(spec: dict) -> dict:
    """The environment an UNSANDBOXED stdio MCP server gets.

    It used to be `{**os.environ, **spec["env"]}` — the bridge's entire
    environment, including ANTHROPIC_API_KEY and every connector credential,
    handed to a command the owner pasted from a README. The connector docs use
    `npx -y @modelcontextprotocol/server-github` as the worked example, so the
    pasted string is routinely something fetched from the network at run time.

    A server needs enough to find its interpreter and a writable temp dir; it does
    not need the host's secrets. Anything it legitimately requires is declared in
    the manifest's `env:` block, which is the point at which the owner decides.
    """
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM",
            # Node/Python need these to locate themselves on some installs.
            "NODE_PATH", "NVM_DIR", "SYSTEMROOT")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update(spec.get("env") or {})
    return env


def _docker_wrap(spec: dict) -> list[str]:
    """Wrap a stdio MCP command to run inside a throwaway container, so an
    untrusted server can't touch the host filesystem. Secrets are passed via
    -e (from spec.env); network defaults to the bridge (many servers need it,
    e.g. a GitHub server), set network: none in the manifest to fully cut it."""
    image = spec.get("image") or "node:20-slim"
    network = spec.get("network") or "bridge"
    argv = ["docker", "run", "--rm", "-i", "--network", network,
            "--cpus", "1", "--memory", "512m", "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp", "--tmpfs", "/root",
            "--security-opt", "no-new-privileges"]
    for k, v in (spec.get("env") or {}).items():
        argv += ["-e", f"{k}={v}"]
    return argv + [image] + list(spec["command"])


class _StdioSession(_Session):
    def __init__(self, spec: dict):
        super().__init__(spec)
        if (spec.get("sandbox") or "none").lower() == "docker":
            if not shutil.which("docker"):
                raise McpError("sandbox: docker requested but Docker isn't installed")
            cmd = _docker_wrap(spec)
            popen_env = os.environ            # secrets passed via -e inside the wrap
        else:
            cmd = spec["command"]
            popen_env = _minimal_env(spec)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=popen_env, text=True, bufsize=1)

    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _rpc(self, payload: dict, timeout: int) -> dict | None:
        if not self.alive():
            raise McpError("MCP stdio server exited "
                           f"(rc={self._proc.returncode})")
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        if "id" not in payload:  # notification
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise McpError("MCP stdio server closed its pipe")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # non-protocol noise on stdout
            if isinstance(msg, dict) and msg.get("id") == payload["id"]:
                return msg
            # server-initiated requests/notifications are ignored (tools-only client)
        raise McpError(f"MCP stdio server timed out after {timeout}s")


# --------------------------------------------------------------------------- #
# Public API — mirrors the discover facade so connectors.py routes cleanly
# --------------------------------------------------------------------------- #
def _session_lost(e: Exception) -> bool:
    """True when the server rejected our cached Mcp-Session-Id.

    A stateful Streamable HTTP server (FastMCP, and our own sdk/host/ava_mcp)
    forgets every session when it restarts, so the first call afterwards fails
    with 404 "Session not found" even though the server is perfectly healthy.
    The session is dropped and re-established on the next call, but that meant
    one bogus "unreachable" after every restart of the app — exactly the false
    signal the Setup transport chip exists to avoid."""
    msg = str(e)
    return "Session not found" in msg or "session not found" in msg


def list_tools(cid: str, spec: dict) -> dict:
    """-> {"tools": [{name, description, inputSchema}, ...]} or {"error": ...}."""
    for retry in (True, False):
        try:
            return {"tools": _session(cid, spec).list_tools()}
        except Exception as e:  # noqa: BLE001 — transport errors become {"error"}
            reset(cid)
            if retry and _session_lost(e):
                continue    # re-handshake against the restarted server
            return {"error": f"{cid} mcp: {e}"}


def call_tool(cid: str, spec: dict, name: str, arguments: dict | None) -> tuple:
    """-> (result, status). MCP tool errors (isError) pass through as data —
    they're the model's to read — transport failures return 502."""
    for retry in (True, False):
        try:
            return _session(cid, spec).call_tool(name, arguments or {}), 200
        except Exception as e:  # noqa: BLE001 — transport errors become 502
            reset(cid)
            # Only ever retried on session loss, which the server rejects
            # BEFORE dispatching the tool — so this cannot double-execute a
            # side-effecting call. Every other failure is reported as-is.
            if retry and _session_lost(e):
                continue
            return {"error": f"{cid} mcp: {e}"}, 502
