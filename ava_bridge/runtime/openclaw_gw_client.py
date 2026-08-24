"""The OpenClaw gateway client — one persistent WebSocket, JSON-RPC 2.0, v4.

Replaces `nemoclaw exec -- openclaw agent --json`, which could carry exactly one
thing: a reply and a tool list. Everything else the gateway can do — sessions,
forking, rewind, cron, devices, plugins, approvals, audit — was unreachable
across a subprocess boundary, and the live chain-of-thought had to be recovered
by tailing a file inside the sandbox once a second.

WHY A PRIVATE EVENT LOOP IN ITS OWN THREAD
------------------------------------------
There are two caller populations and they cannot be collapsed:

  * **Sync worker threads** — `turns._run_turn`, `provision.observed()`,
    `hub/agent.agent_status` (a `def` route, so FastAPI threadpools it) and
    `ava_cli.py`. Every one calls the runtime ABC synchronously. Making that
    seam async would mean rewriting `provision.py`, `hub/`, `dashboard.py` and
    the CLI, for no gain.
  * **Async routes** — the RPC passthrough and the `/ws/gateway` relay. If this
    client were sync-only, every browser call would have to `run_in_threadpool`
    a blocking socket wait. That satisfies `tests/test_no_blocking_routes.py`
    while starving Starlette's threadpool the moment a panel fans out reads.
    Satisfying a guard is not the same as being right.

So: one loop, owned by one daemon thread, with `rpc()` for threads and `arpc()`
for coroutines. Not uvicorn's loop — `phone_bridge.app` is imported by test
modules and by `ava_cli.py` with no server running, `ava agent status` must
reach the gateway with no uvicorn at all, and decoding a 25 MB frame on the
request loop turns a gateway stall into a request-serving stall.

`rpc()` refuses to run when a loop is already running, rather than deadlocking
or silently blocking one. A static allowlist in `test_no_blocking_routes` would
be fragile — the bare name `rpc` is far too generic to match reliably — and a
runtime refusal cannot be evaded.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import sys
import queue
import random
import threading
import time
import uuid
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import websockets
from websockets.asyncio.client import connect as ws_connect

# NOTE: the two `audit.record(...)` calls below are written out inline, each with
# its own try/except, rather than routed through a helper. `tests/
# test_event_vocabulary.py` scans for the LITERAL kind next to `audit.record`, so
# a wrapper taking `kind` as a parameter hides the emission from the guard that
# makes sure every kind has owner-facing copy. Two lines of duplication buys a
# ledger that cannot silently grow an unlabelled event type.

from .. import audit, config, settings
from .errors import GatewayError

# The real cap arrives in `hello-ok.policy.maxPayload`, which we only learn
# AFTER the socket is open — so the transport is opened generously and the
# gateway's own number is enforced from that point on.
_BOOTSTRAP_MAX_SIZE = 32 * 1024 * 1024
_CHALLENGE_TIMEOUT_S = 10.0
_READY_WAIT_S = 5.0
_BACKOFF_CAP_S = 30.0
# A connection has to STAY up to count as a success. Resetting the curve on
# "connected" lets a gateway that accepts TCP and then rejects the handshake
# hot-loop at half a second forever.
_BACKOFF_RESET_AFTER_S = 60.0
# Retrying these quickly is pure noise: the fix is a token or a version, and
# neither arrives by waiting.
_SLOW_RETRY_CODES = {"agent_scope_denied", "agent_token_rejected",
                     "agent_protocol_mismatch"}
_DEFAULT_PORT = 18789
_QUEUE_MAX = 1000

# Method-name shape: dotted lowerCamel segments, nothing path-ish or shell-ish.
_READ_SUFFIXES = (".list", ".get", ".status", ".history", ".inspect", ".catalog",
                  ".methods", ".info")


def is_read_method(method: str) -> bool:
    """Is this method a read? PUBLIC, because two callers need the same answer.

    Idempotency keys are minted for writes (here), and the audit ledger records
    writes only (`gateway_api._audit_call`). Those are the same classification,
    and two copies of it would drift into a state where a call is retried
    safely but never recorded — or recorded but retried twice.

    A name rule rather than a table, with the trade-off stated: a misclassified
    read costs one wasted header, a misclassified write costs a duplicate
    operation on retry. So the bias is toward "write" — anything not obviously
    a read gets a key.
    """
    return method.startswith("system.") or method.endswith(_READ_SUFFIXES)


class EventSubscription:
    """A bounded, thread-safe view of the gateway's event stream.

    Deliberately a QUEUE and not a callback. A callback would run on the gateway
    loop, and one blocking callback stalls the socket for every other consumer —
    including the turn path. Making the queue the only interface removes that
    failure mode structurally rather than documenting it.

    `close()` is idempotent and MUST be called; the fan-out holds a strong
    reference until it is.
    """

    def __init__(self, fanout: "_Fanout", key: int, topics: frozenset[str] | None,
                 maxlen: int):
        self._fanout = fanout
        self._key = key
        self.topics = topics
        self._q: queue.Queue = queue.Queue(maxsize=maxlen)
        self.dropped = 0
        self._closed = False

    def get(self, timeout: float | None = None) -> dict | None:
        """Next event, or None on timeout. Never raises on an empty queue."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _put(self, ev: dict) -> None:
        try:
            self._q.put_nowait(ev)
        except queue.Full:
            # Drop the OLDEST. A slow consumer must lose history, never the
            # present — and it is told, so it can refetch rather than quietly
            # rendering a gap.
            try:
                self._q.get_nowait()
                self._q.put_nowait(ev)
            except (queue.Empty, queue.Full):
                pass
            self.dropped += 1

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._fanout.drop(self._key)


class AsyncEventSubscription(EventSubscription):
    """The same subscription, delivered onto the CALLER's event loop.

    The fan-out runs on the gateway's private loop and the `/ws/gateway` handler
    runs on uvicorn's, so frames cross with `call_soon_threadsafe`. Without that
    hop an `asyncio.Queue` would be mutated from the wrong loop, which is
    undefined behaviour that shows up as a hang rather than an error.
    """

    def __init__(self, fanout: "_Fanout", key: int, topics: frozenset[str] | None,
                 maxlen: int, loop: asyncio.AbstractEventLoop):
        super().__init__(fanout, key, topics, maxlen)
        self._loop = loop
        self._aq: asyncio.Queue = asyncio.Queue(maxsize=maxlen)

    async def aget(self, timeout: float | None = None) -> dict | None:
        try:
            if timeout is None:
                return await self._aq.get()
            return await asyncio.wait_for(self._aq.get(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return None

    def _put(self, ev: dict) -> None:
        def _deliver() -> None:
            try:
                self._aq.put_nowait(ev)
            except asyncio.QueueFull:
                try:
                    self._aq.get_nowait()
                    self._aq.put_nowait(ev)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
                self.dropped += 1
        try:
            self._loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            # The consumer's loop is gone (tab closed, server shutting down).
            # Nothing to deliver to and nothing to report to.
            pass


class _Fanout:
    """Topic-filtered delivery to every live subscriber, in `seq` order."""

    def __init__(self):
        self._subs: dict[int, EventSubscription] = {}
        self._keys = itertools.count(1)
        self._lock = threading.Lock()
        self.last_seq: int | None = None
        self.state_version: int | None = None

    def add(self, sub: EventSubscription) -> None:
        with self._lock:
            self._subs[sub._key] = sub

    def drop(self, key: int) -> None:
        with self._lock:
            self._subs.pop(key, None)

    def next_key(self) -> int:
        return next(self._keys)

    def reset(self) -> None:
        """A new socket is a new sequence. Nobody may assume continuity."""
        self.last_seq = None

    def emit(self, topic: str, payload: Any, seq: int | None = None) -> None:
        ev = {"topic": topic, "payload": payload, "seq": seq}
        with self._lock:
            subs = list(self._subs.values())
        for s in subs:
            if s.topics is None or topic in s.topics:
                s._put(ev)

    def dispatch(self, frame: dict) -> None:
        topic = str(frame.get("event") or "")
        payload = frame.get("payload")
        seq = frame.get("seq")
        if isinstance(seq, int):
            prev = self.last_seq
            if prev is not None and seq != prev + 1:
                # Do NOT buffer or reorder. Say so and let each consumer decide:
                # the turn path re-reads chat.history, a panel refetches its
                # list. Smoothing a gap is how a UI ends up showing a turn that
                # never finished.
                self.emit("ava.gateway.gap", {"from": prev, "to": seq})
            self.last_seq = seq
        if isinstance(payload, dict) and isinstance(payload.get("stateVersion"), int):
            self.state_version = payload["stateVersion"]
        self.emit(topic, payload, seq if isinstance(seq, int) else None)


class OpenClawGatewayClient:
    """One connection, shared by every caller in the process."""

    def __init__(self, url_resolver=None, token_resolver=None):
        # Callables, not values: NemoClaw records the gateway port in its own
        # registry and a `rebuild` can move it, so the URL is re-resolved on
        # every dial rather than frozen at construction.
        self._resolve_url = url_resolver or _default_url
        self._resolve_token = token_resolver or _default_token

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._stop = threading.Event()
        self._ws = None
        self._wake: asyncio.Event | None = None

        self._pending: dict[str, asyncio.Future] = {}
        self._epoch = ""
        self._counter = itertools.count(1)
        self._fanout = _Fanout()

        self._phase = "down"
        self._since = time.time()
        self._why = ""
        self._protocol: int | None = None
        self._methods: frozenset[str] = frozenset()
        self._policy: dict = {}
        self._token_refreshed = False
        self._ready_at: float | None = None

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            ready = threading.Event()
            self._thread = threading.Thread(target=self._run_loop, args=(ready,),
                                            name="openclaw-gw", daemon=True)
            self._thread.start()
            ready.wait(timeout=5.0)

    def stop(self) -> None:
        self._stop.set()
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)

    def _run_loop(self, ready: threading.Event) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._wake = asyncio.Event()
        ready.set()
        try:
            loop.run_until_complete(self._supervise())
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    async def _supervise(self) -> None:
        n = 0
        while not self._stop.is_set():
            try:
                await self._connect_once()
                # A clean close still counts as "it stayed up" if it stayed up.
                n = 0 if self._stayed_ready() else n + 1
            except Exception as e:  # noqa: BLE001 — a supervisor never dies
                if not self._stayed_ready():
                    n += 1
                else:
                    n = 0
                # Carry the CODE, not just the sentence. `_backoff` raises its
                # floor to 30s for a token or protocol failure precisely because
                # retrying those fast is noise — the fix does not arrive by
                # waiting. Without this the code was only ever set for a
                # protocol mismatch, so a rejected token hot-looped the curve:
                # the one case the floor exists for was the one it missed.
                self._why_code = getattr(e, "code", "") or ""
                if (self._why_code == "agent_token_rejected"
                        and not self._token_refreshed
                        and _refresh_token_from_sandbox()):
                    # A rotated token is NEW INFORMATION, not another failed
                    # retry, so the curve resets instead of serving the 30s
                    # token floor against a credential we have already fixed.
                    # Once per outage: if the fresh token is also refused, the
                    # problem is not staleness and hammering nemoclaw won't help.
                    self._token_refreshed = True
                    n = 0
                    # Clear the code too, or `_backoff` still serves the 30s
                    # SLOW_RETRY floor meant for "the fix does not arrive by
                    # waiting" — which is exactly wrong here, because the fix
                    # has already arrived. Without this the reboot path takes
                    # ~31s to recover instead of ~1s.
                    self._why_code = ""
                self._set_phase("down", why=_why_of(e))
            self._fail_pending(GatewayError(
                "the gateway connection dropped", "agent_down", retryable=True))
            if self._stop.is_set():
                break
            await asyncio.sleep(self._backoff(n))

    def _stayed_ready(self) -> bool:
        return bool(self._ready_at
                    and (time.time() - self._ready_at) >= _BACKOFF_RESET_AFTER_S)

    def _backoff(self, n: int) -> float:
        if self._code_of_why() in _SLOW_RETRY_CODES:
            return _BACKOFF_CAP_S
        return random.uniform(0, min(_BACKOFF_CAP_S, 0.5 * (2 ** min(n, 8))))

    def _code_of_why(self) -> str:
        return getattr(self, "_why_code", "")

    # ---- one connection ----------------------------------------------------
    async def _connect_once(self) -> None:
        url = self._resolve_url()
        token = self._resolve_token()
        _refuse_remote(url)
        self._set_phase("connecting", why="")
        async with ws_connect(
            url,
            additional_headers=_headers(token),
            open_timeout=config.AGENT_GATEWAY_CONNECT_TIMEOUT,
            ping_interval=20, ping_timeout=20, close_timeout=5,
            max_size=_BOOTSTRAP_MAX_SIZE,
            # Never offer a subprotocol the gateway has not documented: a
            # mismatch is a hard handshake failure that reads to the owner as
            # "the gateway is down".
            subprotocols=None,
        ) as ws:
            self._ws = ws
            self._epoch = uuid.uuid4().hex[:8]
            self._counter = itertools.count(1)
            self._fanout.reset()
            try:
                await self._handshake(ws, token)
                await self._read_forever(ws)
            finally:
                if self._ready_at is not None:
                    # Only a connection that actually reached `ready` counts as
                    # a disconnect. A handshake that never completed is a failed
                    # attempt, and calling it a disconnect would report an
                    # outage as a flapping connection.
                    try:
                        audit.record(
                            "gateway_disconnect", why=self._why or "closed",
                            uptime_s=round(time.time() - self._ready_at, 1))
                    except Exception:  # noqa: BLE001 — see above
                        pass
                self._ws = None
                self._ready_at = None

    async def _handshake(self, ws, token: str | None) -> None:
        self._set_phase("handshaking", why="")
        raw = await asyncio.wait_for(ws.recv(), _CHALLENGE_TIMEOUT_S)
        challenge = _challenge_of(json.loads(raw))
        rid = self._next_id()
        await ws.send(json.dumps({
            "type": "req", "id": rid, "method": "connect",
            "params": {
                "role": "operator",
                "scopes": [s.strip() for s in
                           str(config.AGENT_GATEWAY_SCOPES).split(",") if s.strip()],
                "minProtocol": 4, "maxProtocol": 4,
                # Shape verified against a live gateway (OpenClaw 2026.7.1): the
                # schema rejects a root `protocol`, requires `client.id`, and
                # refuses `client.name`. Do not "restore" them from the docs.
                "client": {
                    # An ENUM, not a free name. `openclaw-control-ui` is
                    # origin-checked (it is the browser UI); a bridge is not a
                    # browser, and claiming to be one means faking an Origin.
                    "id": "cli",
                    "displayName": "ava-bridge",
                    "version": _client_version(),
                    "platform": sys.platform,
                    "mode": "cli",
                    "instanceId": _client_id(),
                },
                # Without this the gateway does not emit tool events, and the
                # step stream Ava renders as chain-of-thought is silently empty.
                "caps": ["tool-events"],
                "auth": _auth_params(token, challenge),
            },
        }))
        hello = await asyncio.wait_for(_await_res(ws, rid), _CHALLENGE_TIMEOUT_S)
        if not hello.get("ok"):
            raise _error_from(hello.get("error") or {})
        payload = hello.get("payload") or {}
        proto = payload.get("protocol")
        if isinstance(proto, int) and proto != 4:
            raise GatewayError(
                f"this gateway speaks protocol v{proto}; Ava speaks v4. "
                f"Upgrade whichever is older.", "agent_protocol_mismatch")
        self._protocol = proto if isinstance(proto, int) else None
        methods = ((payload.get("features") or {}).get("methods")) or []
        self._methods = frozenset(str(m) for m in methods if isinstance(m, str))
        self._policy = payload.get("policy") or {}
        _store_device_token(((payload.get("auth") or {}).get("deviceToken")))
        self._why_code = ""
        self._token_refreshed = False   # a new outage gets a fresh attempt
        self._ready_at = time.time()
        self._set_phase("ready", why="")
        # One line per SUCCESSFUL handshake, never per attempt: an outage
        # retries on a backoff curve, and a line per attempt would bury the
        # ledger under the one event that is least interesting.
        try:
            audit.record("gateway_connect", protocol=self._protocol,
                         method_count=len(self._methods))
        except Exception:  # noqa: BLE001 — a failed ledger write must not take
            pass          #   the connection down with it

    async def _read_forever(self, ws) -> None:
        async for raw in ws:
            try:
                frame = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = frame.get("type")
            if kind == "res":
                fut = self._pending.pop(str(frame.get("id")), None)
                if fut and not fut.done():
                    fut.set_result(frame)
            elif kind == "event":
                self._fanout.dispatch(frame)

    # ---- calls -------------------------------------------------------------
    def _next_id(self) -> str:
        # The epoch is PER CONNECTION. The gateway may legally reuse small
        # integer ids across sockets; without this prefix a late `res` from a
        # dead socket could resolve a live future on the new one.
        return f"{self._epoch}-{next(self._counter)}"

    async def arpc(self, method: str, params: dict | None = None, *,
                   timeout: float | None = None,
                   idempotency_key: str | None = None) -> dict:
        """Await a call from ANY event loop.

        The request future has to be created on, and awaited from, the loop that
        owns the socket — awaiting a future across loops does not raise, it
        simply never completes, which surfaces as a hang rather than an error.
        So the work always runs on the gateway loop and, when the caller is
        somewhere else (a uvicorn request handler), the result is bridged back
        with `wrap_future`.
        """
        self.start()
        loop = self._loop
        if loop is None:
            raise GatewayError("the gateway client is not running", "agent_down",
                               retryable=True)
        if asyncio.get_running_loop() is loop:
            return await self._arpc(method, params, timeout=timeout,
                                    idempotency_key=idempotency_key)
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
            self._arpc(method, params, timeout=timeout,
                       idempotency_key=idempotency_key), loop))

    async def _arpc(self, method: str, params: dict | None = None, *,
                    timeout: float | None = None,
                    idempotency_key: str | None = None) -> dict:
        """The call itself. Runs on the gateway loop, always."""
        timeout = timeout or config.AGENT_GATEWAY_RPC_TIMEOUT
        await self._await_ready()
        self._check_method(method)

        sent = dict(params or {})
        # The gateway refuses `idempotencyKey` at the FRAME ROOT ("invalid
        # request frame: at root: unexpected property"); it belongs in params,
        # and only on the methods that declare it. So it is passed through only
        # when a caller explicitly asked for one, never auto-minted for every
        # write — an unexpected property fails the whole call on a strict schema.
        if idempotency_key and "idempotencyKey" not in sent:
            sent["idempotencyKey"] = idempotency_key
        body: dict = {"type": "req", "id": self._next_id(), "method": method,
                      "params": sent}
        frame = json.dumps(body)
        self._check_size(frame)

        ws = self._ws
        if ws is None:
            raise GatewayError("the gateway connection is not open",
                               "agent_down", retryable=True)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[body["id"]] = fut
        try:
            await ws.send(frame)
            res = await asyncio.wait_for(fut, timeout)
        except (TimeoutError, asyncio.TimeoutError) as e:
            self._pending.pop(body["id"], None)
            # Do NOT close the socket. One slow method must not disturb the
            # other 199 in flight.
            raise GatewayError(f"`{method}` did not answer within {timeout:g}s",
                               "gateway_timeout", retryable=True) from e
        finally:
            self._pending.pop(body["id"], None)

        if not res.get("ok"):
            raise _error_from(res.get("error") or {})
        return res.get("payload") or {}

    def rpc(self, method: str, params: dict | None = None, *,
            timeout: float | None = None,
            idempotency_key: str | None = None) -> dict:
        """Blocking call, for worker threads. Refuses to run on an event loop."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "OpenClawGatewayClient.rpc() blocks and you are on an event "
                "loop — await arpc() instead.")
        self.start()
        loop = self._loop
        if loop is None:
            raise GatewayError("the gateway client is not running", "agent_down",
                               retryable=True)
        fut = asyncio.run_coroutine_threadsafe(
            self._arpc(method, params, timeout=timeout,
                       idempotency_key=idempotency_key), loop)
        budget = (timeout or config.AGENT_GATEWAY_RPC_TIMEOUT) + _READY_WAIT_S + 5.0
        try:
            return fut.result(timeout=budget)
        except (TimeoutError, asyncio.TimeoutError) as e:
            fut.cancel()
            raise GatewayError(f"`{method}` did not answer within {budget:g}s",
                               "gateway_timeout", retryable=True) from e

    async def _await_ready(self) -> None:
        if self._phase == "ready":
            return
        # A BOUNDED wait, not a queue: a caller arriving during a reconnect blip
        # should wait; one arriving after an hour down should be told now.
        deadline = time.time() + _READY_WAIT_S
        while time.time() < deadline:
            if self._phase == "ready":
                return
            await asyncio.sleep(0.05)
        raise GatewayError(self._why or "the gateway is not connected",
                           self._code_of_why() or "agent_down", retryable=True)

    def _check_method(self, method: str) -> None:
        if config.AGENT_GATEWAY_TRUST_UNLISTED:
            return
        if not self._methods:
            raise GatewayError(
                "the gateway did not advertise its method list, so every call "
                "is refused. Set agent.gateway.trust_unlisted_methods: true "
                "only for a build that genuinely does not publish one.",
                "gateway_unsupported_method")
        if method not in self._methods:
            raise GatewayError(f"this gateway does not offer `{method}`",
                               "gateway_unsupported_method")

    def _check_size(self, frame: str) -> None:
        cap = self._policy.get("maxPayload")
        n = len(frame.encode("utf-8"))
        if isinstance(cap, int) and cap > 0 and n > cap:
            # Refused HERE, not by asking: the gateway's answer to an oversized
            # frame is to close the socket, which takes every other in-flight
            # request down with it.
            raise GatewayError(
                f"that call is {n} bytes and the gateway's limit is {cap}",
                "gateway_payload_too_large", detail={"bytes": n, "limit": cap})

    def _fail_pending(self, err: GatewayError) -> None:
        """A caller told in 0 ms can retry; one told in 600 s already showed a
        spinner. So a dropped socket fails everything at once rather than
        letting each request reach its own timeout."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()

    # ---- events ------------------------------------------------------------
    def subscribe(self, topics: Sequence[str] | None = None, *,
                  maxlen: int = _QUEUE_MAX) -> EventSubscription:
        sub = EventSubscription(self._fanout, self._fanout.next_key(),
                                frozenset(topics) if topics else None, maxlen)
        self._fanout.add(sub)
        return sub

    def asubscribe(self, topics: Sequence[str] | None = None, *,
                   maxlen: int = _QUEUE_MAX) -> AsyncEventSubscription:
        sub = AsyncEventSubscription(self._fanout, self._fanout.next_key(),
                                     frozenset(topics) if topics else None,
                                     maxlen, asyncio.get_running_loop())
        self._fanout.add(sub)
        return sub

    # ---- introspection -----------------------------------------------------
    def methods(self) -> frozenset[str]:
        return self._methods

    def status(self) -> dict:
        return {"phase": self._phase, "since": self._since, "why": self._why,
                "why_code": self._code_of_why(), "protocol": self._protocol,
                "methods": sorted(self._methods), "policy": dict(self._policy),
                "url_class": _url_class(self._safe_url()),
                "last_seq": self._fanout.last_seq}

    def _safe_url(self) -> str:
        try:
            return self._resolve_url()
        except Exception:  # noqa: BLE001 — status must never raise
            return ""

    def _set_phase(self, phase: str, *, why: str) -> None:
        if phase != self._phase:
            self._since = time.time()
        self._phase = phase
        self._why = why

    def reconnect(self) -> None:
        """The owner's "try again" — drop the socket and let the supervisor
        redial immediately rather than waiting out the backoff."""
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(ws.close(), loop)


# ---- module-level helpers --------------------------------------------------
def _client_id() -> str:
    """A STABLE per-installation id.

    The gateway keys its device records on this, so a value that changed per
    connect would mint a new paired device on every reconnect.
    """
    import os
    from pathlib import Path
    path = Path(settings.secrets_dir()) / "openclaw_client_id"
    try:
        got = path.read_text(encoding="utf-8").strip()
        if got:
            return got
    except OSError:
        pass
    import uuid
    made = str(uuid.uuid4())
    try:
        os.makedirs(settings.secrets_dir(), exist_ok=True)
        path.write_text(made, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass
    return made


def _client_version() -> str:
    from ..version import version
    return version()


def _headers(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _auth_params(token: str | None, challenge: dict) -> dict:
    # No `mode`: the live gateway's schema refuses it (see _handshake).
    out: dict = {}
    if token:
        out["token"] = token
    dev = settings.secret("openclaw_device_token")
    if dev:
        # Prefer the token the gateway itself issued: that is what stops a
        # reconnect re-consuming a pairing every time the socket blips.
        out["deviceToken"] = dev
    # The live schema refuses `nonce`/`ts` in auth; the challenge is answered by
    # the token alone. `challenge` stays in the signature for future schemes.
    return out


def _store_device_token(tok: Any) -> None:
    if not isinstance(tok, str) or not tok.strip():
        return
    if settings.secret("openclaw_device_token") == tok.strip():
        return
    import os
    from pathlib import Path
    path = Path(settings.secrets_dir()) / "openclaw_device_token"
    try:
        os.makedirs(settings.secrets_dir(), exist_ok=True)
        path.write_text(tok.strip(), encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        pass


def _challenge_of(frame: dict) -> dict:
    """Accept either framing the gateway might use for the opening challenge.

    Parsing this defensively is cheap; guessing wrong costs a handshake that
    fails for a reason nobody can see from the outside.
    """
    if frame.get("type") == "event" and frame.get("event") == "connect.challenge":
        return frame.get("payload") or {}
    if frame.get("type") == "connect.challenge":
        return frame.get("payload") or frame
    return frame.get("payload") or {}


async def _await_res(ws, rid: str) -> dict:
    """Read frames until the response to `rid` arrives, dropping the rest.

    Only used during the handshake, before the reader loop owns the socket.
    """
    while True:
        frame = json.loads(await ws.recv())
        if frame.get("type") == "res" and str(frame.get("id")) == rid:
            return frame


def _error_from(err: dict) -> GatewayError:
    gw_code = str(err.get("code") or "")
    detail = err.get("details") or {}
    msg = str(err.get("message") or gw_code or "the gateway refused the call")
    retryable = bool(err.get("retryable"))
    after = err.get("retryAfterMs")
    code = "gateway_rpc_failed"
    if gw_code == "FORBIDDEN" and (detail or {}).get("code") == "MISSING_SCOPE":
        # A different fix from "the gateway is down": the token was minted
        # without the scope. `fixes.ts` needs its own entry for this, because it
        # matches neither the `_off` nor the `_down` pattern.
        code = "agent_scope_denied"
    elif (gw_code in ("UNAUTHORIZED", "AUTH_FAILED", "INVALID_TOKEN")
          or str((detail or {}).get("authReason") or "")
          or str((detail or {}).get("code") or "").startswith("AUTH_")):
        # The live gateway does NOT use a top-level auth code. A rejected token
        # arrives as the generic `INVALID_REQUEST` with the real signal in
        # details:
        #   {"code": "AUTH_TOKEN_MISMATCH", "authReason": "token_mismatch", ...}
        # Reading only `gw_code` reported this as `gateway_rpc_failed`, so the
        # 30s backoff floor for token failures never engaged and, worse, the
        # refresh below never fired — the agent just sat on the Direct floor.
        code = "agent_token_rejected"
    return GatewayError(msg, code, gw_code=gw_code, detail=detail,
                        retryable=retryable,
                        retry_after_ms=after if isinstance(after, int) else None)


def _why_of(e: Exception) -> str:
    if isinstance(e, GatewayError):
        return str(e)
    if isinstance(e, (websockets.exceptions.InvalidStatus,
                      websockets.exceptions.WebSocketException)):
        return f"the gateway refused the connection ({e.__class__.__name__})"
    if isinstance(e, (OSError, ConnectionError)):
        return f"cannot reach the gateway ({e})"
    return f"{e.__class__.__name__}: {e}"


def _url_class(url: str) -> str:
    """loopback | private | public — the same vocabulary ava_security_check
    uses, so "exposed to my phone" and "exposed to the internet" stay
    distinguishable in the status payload."""
    host = (urlparse(url).hostname or "").lower()
    if host in ("127.0.0.1", "::1", "localhost", ""):
        return "loopback"
    if host.startswith(("10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.2", "172.30.", "172.31.")):
        return "private"
    return "public"


def _refuse_remote(url: str) -> None:
    if config.AGENT_GATEWAY_ALLOW_REMOTE:
        return
    if _url_class(url) != "loopback":
        raise GatewayError(
            f"refusing to send an operator.admin token to {url!r}. Set "
            f"agent.gateway.allow_remote: true if that is deliberate.",
            "agent_token_rejected")


def _default_token() -> str | None:
    # generate=False is load-bearing. Unlike router_token, this secret is not
    # ours to invent — it must match something the gateway will accept.
    # Generating one produces a string that fails every handshake and surfaces
    # as "the gateway rejected our token", which is a lie about which side is
    # wrong.
    return settings.secret("openclaw_gateway_token", env="AVA_OC_GATEWAY_TOKEN")


def _refresh_token_from_sandbox() -> bool:
    """Re-read the gateway token from nemoclaw. True only if it CHANGED.

    The sandbox mints a NEW gateway token every time it restarts, so a file
    written once is stale after the next reboot and every handshake fails with
    AUTH_TOKEN_MISMATCH — Ava then falls silently back to the Direct floor,
    which looks like "the agent is off" rather than "the credential rotated".
    A static file is simply the wrong storage for a rotating secret.

    Shelling out is confined to this one path — a rejected token, once per
    outage — so the happy path never pays for it.
    """
    import os
    import shutil
    import subprocess
    from pathlib import Path
    exe = shutil.which("nemoclaw")
    if not exe or not config.OC_SANDBOX:
        return False
    try:
        got = subprocess.run(
            [exe, config.OC_SANDBOX, "gateway-token", "--quiet"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    tok = (got.stdout or "").strip()
    if got.returncode != 0 or not tok or len(tok) > 4096 or any(
            c.isspace() for c in tok):
        return False
    if tok == (settings.secret("openclaw_gateway_token") or ""):
        return False          # unchanged — do not report progress we did not make
    path = Path(settings.secrets_dir()) / "openclaw_gateway_token"
    try:
        os.makedirs(settings.secrets_dir(), exist_ok=True)
        path.write_text(tok, encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def _default_url() -> str:
    if config.AGENT_GATEWAY_URL:
        return config.AGENT_GATEWAY_URL
    port = _registry_gateway_port()
    return f"ws://127.0.0.1:{port or _DEFAULT_PORT}/"


def _registry_gateway_port() -> int | None:
    """The port OpenClaw's gateway is forwarded to, from NemoClaw's registry.

    Preferring the registry over a constant is what keeps a default box working
    with no configuration at all, and keeps working after `nemoclaw rebuild`
    moves the forward. See `nemoclaw_registry.openclaw_gateway_port` for why
    this is `dashboardPort` and emphatically not `gatewayPort`.
    """
    try:
        from . import nemoclaw_registry
        return nemoclaw_registry.openclaw_gateway_port()
    except Exception:  # noqa: BLE001 — no registry is a normal state
        return None


_client: OpenClawGatewayClient | None = None
_client_lock = threading.Lock()


def client() -> OpenClawGatewayClient:
    """The one client for this process."""
    global _client
    with _client_lock:
        if _client is None:
            _client = OpenClawGatewayClient()
        return _client
