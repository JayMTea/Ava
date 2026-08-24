"""A stand-in for OpenClaw's gateway: JSON-RPC 2.0 over a WebSocket, protocol v4.

The real gateway needs a sandbox, a container runtime and a model. None of that
belongs in a test tier, but the SHAPE does — the handshake, the method list, the
ordered event stream and a run that actually streams — because those are what
Ava's client and the Agent tab are written against.

Two properties this fake exists to make testable, which a mocked HTTP endpoint
could not:

  * **A run is a sequence, not a value.** `chat.send` answers with an id and the
    reply arrives later, over events, in order. Anything that asserts "the chain
    of thought appeared while the turn was running" needs a server that behaves
    that way.
  * **`hello-ok.features.methods` is the SSOT.** Ava fails closed on it, so a
    test for "the UI hides a control the gateway lacks" needs a gateway that can
    genuinely lack it.

Scripting: `.script` is the event sequence a `chat.send` plays back, `.answers`
overrides any method's reply, and `.calls` records everything received.

Pure stdlib plus `websockets`, which requirements.txt already pins.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

import websockets

PROTOCOL = 4

# What a default fake advertises. Deliberately a subset of the real surface:
# a test that needs `terminal.open` should ADD it, so the test says out loud
# which capability it depends on.
def _captured_methods() -> list[str]:
    """The REAL method surface, read from a capture of a live gateway.

    This list used to be hand-written beside the code that called it, which
    meant it advertised whatever the caller assumed — including five methods
    that do not exist (`audit.activity.list`, `plugins.list`,
    `agents.files.read`, `agents.files.write`, `system.diagnostics.stability`).
    A fake that agrees with the caller's mistakes cannot catch it: every
    end-to-end test passed while every real panel failed closed.

    Reading the capture instead means an invented method fails HERE.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "gateway-methods.txt")
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f
                if ln.strip() and not ln.startswith("#")]


DEFAULT_METHODS = _captured_methods()

DEFAULT_ANSWERS = {
    "system.info": {"host": "qa-fake", "uptimeS": 12, "cpu": 3.0},
    "sessions.list": {"sessions": [
        {"id": "s-1", "title": "refactor auth", "kind": "coding",
         "state": "idle", "unread": 0, "updatedAt": 1000},
        {"id": "s-2", "title": "notes", "kind": "other",
         "state": "idle", "unread": 2, "updatedAt": 900},
    ]},
    "chat.history": {"messages": []},
    "models.list": {"models": [{"id": "qa-model", "name": "QA Model"}]},
    "plugins.list": {"plugins": []},
    "tasks.list": {"tasks": []},
    "cron.list": {"jobs": []},
    "agents.list": {"agents": [{"id": "main", "default": True}]},
}

# One scripted run: two steps then a final, in the REAL gateway's vocabulary.
#
# Captured from OpenClaw 2026.7.1 on 2026-08-23. There are four event names and
# the state lives in the payload — `agent` carries lifecycle/tool/reasoning
# under `stream` + `data`, and `chat` carries assistant text under `state`.
# This fake previously scripted `run.step`/`run.finished`, names the gateway has
# never emitted, so every end-to-end test here passed against a fiction. If you
# change these, capture a live run first.
DEFAULT_SCRIPT = [
    {"event": "agent", "payload": {"stream": "lifecycle",
                                   "data": {"phase": "start"}}},
    {"event": "agent", "payload": {"stream": "reasoning",
                                   "data": {"reasoning": "checking the session"}}},
    {"event": "agent", "payload": {"stream": "tool",
                                   "data": {"tool": "read_file",
                                            "args": {"path": "/etc/hosts"}}}},
    {"event": "chat", "payload": {"state": "delta", "deltaText": "Done."}},
    {"event": "agent", "payload": {"stream": "lifecycle",
                                   "data": {"phase": "end",
                                            "stopReason": "stop",
                                            "aborted": False}}},
    {"event": "chat", "payload": {"state": "final", "stopReason": "stop",
                                  "tools": ["read_file"],
                                  "message": {"role": "assistant",
                                              "content": [{"type": "text",
                                                           "text": "Done."}]}}},
]


# The live gateway validates params against strict JSON schemas
# (additionalProperties: false) and answers INVALID_REQUEST for anything else.
# Captured live against OpenClaw 2026.7.1 on 2026-08-23: chat.send takes
# sessionKey/message (the schema refuses sessionId and text), chat.history
# takes sessionKey (refuses sessionId; short and full 'agent:main:' keys both
# accepted), and sessions.delete takes {key} (refuses sessionKey AND
# sessionId). This fake used to accept any params at all, which is how an
# invented `sessionId` passed every test here and failed in the browser —
# reconcile never recovered a turn and ghost mode never deleted a session.
# Mirroring the strictness makes the invented name fail HERE.
_PARAM_SCHEMAS: dict[str, dict[str, tuple[str, ...]]] = {
    "chat.send": {"required": ("sessionKey", "message"),
                  "allowed": ("sessionKey", "message", "deliver",
                              "timeoutMs", "idempotencyKey")},
    "chat.history": {"required": ("sessionKey",),
                     "allowed": ("sessionKey", "limit")},
    "sessions.delete": {"required": ("key",),
                        "allowed": ("key",)},
}


def _invalid_params(method: str, params: dict) -> str | None:
    """The live error message, or None. Only methods this fake implements."""
    schema = _PARAM_SCHEMAS.get(method)
    if schema is None:
        return None
    problems = [f"must have required property '{name}'"
                for name in schema["required"]
                if not isinstance(params.get(name), str)]
    problems += [f"at root: unexpected property '{name}'"
                 for name in params if name not in schema["allowed"]]
    if problems:
        return f"invalid {method} params: " + "; ".join(problems)
    return None


class FakeGateway:
    """Runs on its own loop in its own thread; `.url` once started."""

    def __init__(self, *, methods=None, answers=None, script=None,
                 token: str | None = None, protocol: int = PROTOCOL):
        self.methods = list(DEFAULT_METHODS if methods is None else methods)
        self.answers = {**DEFAULT_ANSWERS, **(answers or {})}
        self.script = list(DEFAULT_SCRIPT if script is None else script)
        self.token = token
        self.protocol = protocol
        self.calls: list[dict] = []
        self.connections = 0
        # What this gateway has handed out, so a test can assert a reconnect
        # re-offered OUR token rather than one left behind by another fixture.
        self.issued_device_tokens: list[str] = []
        self.port = 0
        self._seq = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()

    # ---- lifecycle ---------------------------------------------------------
    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/"

    def start(self) -> "FakeGateway":
        threading.Thread(target=self._run, daemon=True,
                         name="qa-fake-gateway").start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("fake gateway did not start")
        return self

    def stop(self) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)

    def reset(self) -> None:
        self.calls.clear()
        self.connections = 0

    def _run(self) -> None:
        async def _main():
            self._stop = asyncio.Event()
            async with websockets.serve(self._handle, "127.0.0.1", 0) as srv:
                self.port = srv.sockets[0].getsockname()[1]
                self._ready.set()
                await self._stop.wait()

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(_main())
        finally:
            self._loop.close()

    # ---- protocol ----------------------------------------------------------
    async def _handle(self, conn) -> None:
        self.connections += 1
        # The gateway speaks FIRST. A client that sends before listening will
        # deadlock against the real one too, which is the point of doing it here.
        await conn.send(json.dumps({
            "type": "event", "event": "connect.challenge",
            "payload": {"nonce": f"n-{self.connections}", "ts": 1}}))
        try:
            async for raw in conn:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if frame.get("type") != "req":
                    continue
                await self._dispatch(conn, frame)
        except websockets.exceptions.ConnectionClosed:
            return

    async def _dispatch(self, conn, frame: dict) -> None:
        method = frame.get("method")
        params = frame.get("params") or {}
        self.calls.append({"method": method, "params": params,
                           "idempotencyKey": params.get("idempotencyKey")})

        if method == "connect":
            return await self._connect(conn, frame, params)

        if method not in self.methods:
            # Same shape the real one uses, so the client's mapping to
            # `gateway_unsupported_method` is exercised rather than assumed.
            return await self._res(conn, frame, ok=False, error={
                "code": "METHOD_NOT_FOUND",
                "message": f"no such method: {method}"})

        bad = _invalid_params(method, params)
        if bad is not None:
            return await self._res(conn, frame, ok=False, error={
                "code": "INVALID_REQUEST", "message": bad})

        if method == "chat.send":
            # The real gateway ADOPTS the caller's idempotencyKey as the runId
            # and takes `sessionKey`, not `sessionId`. Both matter: the turn
            # path correlates events by run id, so a fake that minted its own
            # would let a correlation bug pass.
            run_id = params.get("idempotencyKey") or f"run-{len(self.calls)}"
            await self._res(conn, frame, payload={"runId": run_id,
                                                  "status": "started"})
            return await self._play(conn, run_id, params["sessionKey"])

        if method == "sessions.delete":
            # Response shape verified live: {ok, key: 'agent:main:<short>',
            # deleted} — the full form comes back even when the short key
            # went in.
            key = params["key"]
            full = key if key.startswith("agent:") else f"agent:main:{key}"
            return await self._res(conn, frame, payload={
                "ok": True, "key": full, "deleted": True})

        await self._res(conn, frame, payload=self.answers.get(method, {}))

    async def _connect(self, conn, frame: dict, params: dict) -> None:
        auth = params.get("auth") or {}
        if self.token and auth.get("token") != self.token:
            return await self._res(conn, frame, ok=False, error={
                "code": "UNAUTHORIZED", "message": "bad token"})
        scopes = params.get("scopes") or []
        if "operator.admin" in scopes and self.answers.get("__deny_admin__"):
            return await self._res(conn, frame, ok=False, error={
                "code": "FORBIDDEN", "message": "scope refused",
                "details": {"code": "MISSING_SCOPE",
                            "missingScope": "operator.admin"}})
        device_token = f"dev-{self.port}-{self.connections}"
        self.issued_device_tokens.append(device_token)
        await self._res(conn, frame, payload={
            "protocol": self.protocol,
            "features": {"methods": self.methods},
            "policy": {"maxPayload": 25 * 1024 * 1024,
                       "maxBufferedBytes": 50 * 1024 * 1024},
            "auth": {"deviceToken": device_token}})

    async def _play(self, conn, run_id: str, session_key: str) -> None:
        """Stream one run's events, `seq`-numbered so gaps are detectable."""
        # Captured live: chat events carry `sessionKey` in the FULL
        # 'agent:main:...' form and no sessionId at all; only agent lifecycle
        # frames also carry a `sessionId` uuid. The fake used to stamp every
        # event with `sessionId`, which let the WS relay ship frames whose
        # sessionKey was always None without any test noticing.
        full = (session_key if session_key.startswith("agent:")
                else f"agent:main:{session_key}")
        for step in self.script:
            payload = {**(step.get("payload") or {}),
                       "runId": run_id, "sessionKey": full}
            self._seq += 1
            await conn.send(json.dumps({
                "type": "event", "event": step["event"],
                "seq": self._seq, "payload": payload}))
            await asyncio.sleep(step.get("delay", 0.01))

    async def _res(self, conn, frame: dict, *, payload=None, ok: bool = True,
                   error=None) -> None:
        body = {"type": "res", "id": frame.get("id"), "ok": ok}
        if ok:
            body["payload"] = payload or {}
        else:
            body["error"] = error or {}
        await conn.send(json.dumps(body))

    # ---- test affordances --------------------------------------------------
    def emit(self, event: str, payload: dict) -> None:
        """Push an unsolicited event (a session update, an approval request)."""
        raise NotImplementedError(
            "broadcast to all connections is not needed yet; add it with the "
            "first test that wants it rather than guessing the shape now")

    def skip_seq(self, n: int = 3) -> None:
        """Advance the sequence WITHOUT sending, so the next event reads as a gap.

        The client must announce it rather than smoothing it over — see
        `_Fanout.dispatch`.
        """
        self._seq += n
