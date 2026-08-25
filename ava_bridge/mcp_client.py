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

Every `tools/list` entry is NORMALISED on the way in (`normalise_tools`): MCP
has no consent-tier field of its own, so a tool's Ava tier is lifted from where
a real server can carry it — `_meta.access` (the SDK's vendor-field slot) or the
spec's ToolAnnotations hints — to the top-level `access` that `tools_cache`
reads. An explicit top-level `access` is never overridden.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque

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
# Tier normalisation — MCP has no consent-tier field; find one where it hides
# --------------------------------------------------------------------------- #
#: Where a `tools/list` entry may carry Ava's tier when it is not top-level. The
#: MCP SDKs serialise `Tool.meta` as `_meta`; sdk/host/ava_mcp mirrors the
#: facade's tier there as `ava/access` (a namespaced key, the spec's advice for
#: vendor fields), and a hand-rolled server may just write `access`. Checked in
#: this order — the namespaced key first, because it can only mean Ava's tier,
#: while a bare `access` under `_meta` (the spec's free-form vendor slot) may be
#: a third-party server's unrelated notion ("public", "authenticated"). Either
#: is lifted ONLY when it spells one of Ava's five tiers; anything else reads as
#: "the server said nothing" and falls through to the annotations, then to
#: absent. Lifting an unknown word would let tools_cache.update coerce it to
#: `write`, which — for a `role: device` connector — turns the never-grantable
#: `physical` fallback into a grantable prompt.
_META_ACCESS_KEYS = ("ava/access", "access")
_TIERS = frozenset({"read", "sensitive", "write", "destructive", "physical"})


def _normalise_tool(tool):
    """One `tools/list` entry, with its consent tier lifted to top-level `access`.

    `tools_cache.update` — and so the JIT consent gate behind `<cid>_call` —
    reads exactly one field: a top-level `access`. That is what the ava-tools/1
    facade writes, but plain MCP has no such field, so a real MCP server's tools
    all fell through to the manifest default (`write`) and every read started
    asking for permission: the "hand-rolled port silently demotes every read"
    trap docs/CONNECTOR_SDK.md §5 warns about, reproduced by any server that
    carried the tier where MCP lets it. Three sources, in order; the first that
    speaks wins:

      1. an explicit top-level `access` — never overridden, whatever else is set;
      2. `_meta["ava/access"]` / `_meta.access` — the SDK's vendor-field slot,
         honoured only when it spells one of Ava's five tiers;
      3. MCP ToolAnnotations: `readOnlyHint: true` -> `read`;
         `destructiveHint: true` (and not read-only) -> `destructive`.

    Anything else is left WITHOUT `access`, so the manifest's `dynamic_access`
    decides. Absent means "the server said nothing", which the cache treats
    differently from a declared tier (see tools_cache.update) — a `role: device`
    connector's never-grantable `physical` fallback depends on that distinction.

    The annotation mapping is deliberately narrow. `destructiveHint` DEFAULTS to
    true in the MCP spec, so only the explicit `true` counts and a server that
    merely omits it is not accused of anything; `openWorldHint` and
    `idempotentHint` say nothing about consent and are ignored; and no hint ever
    yields `sensitive` or `physical`, which — as with static actions — must be
    declared, because no wire shape implies them.
    """
    if not isinstance(tool, dict):
        return tool
    explicit = tool.get("access")
    if isinstance(explicit, str) and explicit.strip():
        return tool
    meta = tool.get("_meta")
    if isinstance(meta, dict):
        for key in _META_ACCESS_KEYS:
            val = meta.get(key)
            if isinstance(val, str) and val.strip().lower() in _TIERS:
                return {**tool, "access": val.strip().lower()}
    ann = tool.get("annotations")
    if isinstance(ann, dict):
        if ann.get("readOnlyHint") is True:
            return {**tool, "access": "read"}
        if ann.get("destructiveHint") is True:
            return {**tool, "access": "destructive"}
    return tool


def normalise_tools(tools) -> list:
    """A `tools/list` result's `tools`, each with its tier lifted to top-level
    `access` where the server carried one (see `_normalise_tool`). Non-list
    input yields `[]`; non-dict entries pass through untouched."""
    if not isinstance(tools, list):
        return []
    return [_normalise_tool(t) for t in tools]


# --------------------------------------------------------------------------- #
# Sessions — one per connector id, lazily created, restart on failure
# --------------------------------------------------------------------------- #
_sessions: dict[str, "_Session"] = {}
_sessions_lock = threading.Lock()


def _make_session(spec: dict) -> "_Session":
    t = spec["transport"]
    cls = _StdioSession if t == "stdio" else _SseSession if t == "sse" else _HttpSession
    # __new__/__init__ split on purpose. A transport constructor opens real
    # resources BEFORE it can know the connect worked — the SSE transport a
    # socket and a reader thread, stdio a subprocess — and a constructor that
    # raises normally drops the half-built instance without it ever being
    # returned: nothing can close() it afterwards, so whatever it opened leaks
    # until process exit, once per retry. Holding the instance through __init__
    # keeps a handle to those resources on failure; every transport's close()
    # tolerates partially-initialized attributes for exactly this call.
    s = cls.__new__(cls)
    try:
        s.__init__(spec)
    except BaseException:
        try:
            s.close()
        except Exception:  # noqa: BLE001 — best-effort teardown; the connect error wins
            pass
        raise
    return s


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
            # Normalised HERE, so the cached list and every caller see one
            # shape: tools_cache.update reads a top-level `access`, and a real
            # MCP server carries it elsewhere or not at all (see normalise_tools).
            self._tools = normalise_tools((res.get("result") or {}).get("tools"))
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
        # allow_redirects=False. MCP JSON-RPC has no use for redirects, and
        # following one resends the POST BODY — i.e. the tool's arguments — to the
        # redirect target. `requests` strips Authorization across hosts but 307/308
        # preserve the body, so a hijacked or misconfigured server could harvest
        # arguments without ever seeing the bearer.
        r = requests.post(self.spec["url"], json=payload,
                          headers=self._headers(), timeout=timeout,
                          allow_redirects=False)
        sid = r.headers.get("Mcp-Session-Id") or r.headers.get("mcp-session-id")
        if sid:
            self._mcp_session_id = sid
        # Checked BEFORE the notification early-return, deliberately: `>= 400`
        # below is not reached for a notification, so without this a redirected
        # `notifications/initialized` would return None and the session would be
        # marked initialized against a server that never saw it. A 3xx is also not
        # >= 400, so it would otherwise fall through to the JSON parse and surface
        # as a misleading "non-JSON" error.
        if 300 <= r.status_code < 400:
            raise McpError(f"MCP server redirected ({r.status_code}) — refusing to "
                           f"resend tool arguments to {r.headers.get('location','?')}")
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
        # Both exist BEFORE anything that can fail, so close() can always run
        # against a partially-built session — every failure path below relies
        # on that.
        self._stream = None
        self._sock = None
        try:
            import requests
            self._stream = requests.get(
                spec["url"], headers=self._headers(accept="text/event-stream"),
                stream=True, timeout=(_HTTP_TIMEOUT, None),  # connect timeout; stream stays open
                allow_redirects=False)
            if self._stream.status_code >= 400:
                raise McpError(f"SSE connect returned {self._stream.status_code}: "
                               f"{self._stream.text[:200]}")
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
                    raise McpError(self._dead or
                                   "SSE server never sent its endpoint event")
        except BaseException:
            # Tear down BEFORE re-raising. This used to raise with the stream
            # still open, and the half-built session was simply dropped — the
            # response socket and the daemon reader thread outlived the
            # exception with nobody left holding a handle to close them. One
            # socket + one thread leaked PER ATTEMPT against a server that
            # speaks SSE but never announces its endpoint (e.g. a plain
            # keepalive stream at the pasted URL), and the Hub retries this
            # path on every visit. close() shuts the socket down, which pops
            # the reader out of its blocking read so the thread exits too.
            self.close()
            raise

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
        # getattr-guarded throughout: _make_session close()es a session whose
        # __init__ may have failed before these attributes ever existed.
        self._dead = getattr(self, "_dead", None) or "closed"
        # Shut the raw socket down FIRST: response.close() blocks on the buffer
        # lock held by the reader thread's in-flight blocking read, deadlocking
        # the caller (session reset / connector reload). shutdown() unblocks
        # that read with EOF, the reader exits, and close() is then safe.
        sock = getattr(self, "_sock", None)
        if sock is not None:
            try:
                import socket as _socket
                sock.shutdown(_socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001 — already closed / never connected
                pass
        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _rpc(self, payload: dict, timeout: int) -> dict | None:
        import requests
        r = requests.post(self._endpoint, json=payload,
                          headers=self._headers(), timeout=_HTTP_TIMEOUT,
                          allow_redirects=False)
        if r.status_code >= 400:
            raise McpError(f"MCP SSE endpoint returned {r.status_code}: {r.text[:200]}")
        if 300 <= r.status_code < 400:    # see the Streamable-HTTP note on redirects
            raise McpError(f"MCP SSE endpoint redirected ({r.status_code}) — refusing "
                           f"to resend tool arguments to {r.headers.get('location','?')}")
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
        # stderr used to go to DEVNULL. An MCP server that dies on startup —
        # missing module, bad credential, wrong node version — writes its reason
        # there and nowhere else, so the owner got "server exited (rc=1)" and the
        # one line that would have told them what to fix was thrown away. A
        # daemon reader keeps the pipe from filling and holds the tail.
        self._err: deque[str] = deque(maxlen=40)
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=popen_env, text=True, bufsize=1)
        # Values the owner pasted in, so a server that echoes its own token in a
        # startup banner cannot put it in a Hub error message.
        self._masked = sorted(
            (str(v) for v in (spec.get("env") or {}).values() if len(str(v)) >= 8),
            key=len, reverse=True)
        threading.Thread(target=self._drain, daemon=True,
                         name="ava-mcp-stderr").start()

    def _drain(self) -> None:
        try:
            for line in self._proc.stderr:      # ends when the process exits
                line = line.rstrip()
                for secret in self._masked:
                    line = line.replace(secret, "***")
                if line:
                    self._err.append(line[:300])
        except Exception:  # noqa: BLE001 — a closed pipe is the normal ending
            pass

    def _why(self) -> str:
        """The last few stderr lines, for an error the owner can act on."""
        tail = list(self._err)[-3:]
        return (" — it said: " + " | ".join(tail)) if tail else ""

    def alive(self) -> bool:
        return self._proc.poll() is None

    def close(self) -> None:
        # __init__ can fail before Popen ever ran (docker requested but not
        # installed) — _make_session still close()es the half-built session.
        proc = getattr(self, "_proc", None)
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _rpc(self, payload: dict, timeout: int) -> dict | None:
        if not self.alive():
            raise McpError("MCP stdio server exited "
                           f"(rc={self._proc.returncode}){self._why()}")
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        if "id" not in payload:  # notification
            return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                raise McpError("MCP stdio server closed its pipe" + self._why())
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
        raise McpError(f"MCP stdio server timed out after {timeout}s{self._why()}")


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
