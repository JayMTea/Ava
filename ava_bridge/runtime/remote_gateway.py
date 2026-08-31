"""The OpenClaw gateway control plane, reached through the agent shim.

WHY THIS EXISTS RATHER THAN POINTING THE BRIDGE'S OWN CLIENT AT THE AGENT HOST
-----------------------------------------------------------------------------
`openclaw_gw_client._refuse_remote` refuses to send an operator.admin token
anywhere but loopback unless `agent.gateway.allow_remote` is set. Running the
real client INSIDE the shim keeps `_url_class()` at "loopback" and keeps that
refusal intact with no config change — the token never crosses a network at its
default posture.

The tempting alternative is `agent.gateway.url` -> the agent host's tailnet
address plus `allow_remote: true`. That is exactly what the guard exists to
stop, and it ALSO permanently breaks token-rotation recovery: the sandbox mints
a new gateway token on every restart, and the only recovery path
(`_refresh_token_from_sandbox`) shells out to `nemoclaw`, which exists in the
agent image and never in the bridge's.

NOTHING HERE MAY SET agent.gateway.allow_remote.

WHY `status()` NEVER DOES I/O
-----------------------------
`gateway_api` calls it synchronously on the event loop in two places that have
no try/except: once spread into `/api/gateway/status`, and once for the opening
frame of `/ws/gateway` — the second AFTER `ws.accept()`. A raise there produces
an accepted-then-dropped socket that the browser redials forever; a slow call
stalls uvicorn for the whole cross-host hop on every Chats mount. So `status()`
is a pure read of a snapshot a background thread refreshes, and it cannot raise.

THE TWO-SUPERVISOR COMPOSITION
------------------------------
There are two hops, and the phase must be the WORSE of them: "can I reach the
shim" and "what does the shim say about the gateway". `phase: "ready"` gates the
streamed chat path, so a phase that says ready because the SHIM answered would
stream turns into a dead gateway. The `why` always names which hop failed, so
the owner is sent to the right machine.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import time

import httpx
import requests

from .. import config
from .errors import GatewayError
from .openclaw_gw_client import AsyncEventSubscription, EventSubscription, Fanout

#: How often the background thread refreshes the snapshot. Matches
#: `RemoteRuntime.available()`'s own 15s cache closely enough that the two do not
#: disagree for long, without adding a poll storm across the tailnet.
_POLL_S = 5.0
#: Ceiling for one relayed call, mirroring the clamp on both the bridge route and
#: the shim's door. Our HTTP read budget must exceed it, or we report a timeout
#: for a call the far side completed.
_TIMEOUT_MAX = 120.0

_UNSUPPORTED = "gateway_proxy_unsupported"


def _rebuild(body: dict) -> GatewayError:
    """Turn the shim's coded 200-body back into the error it was raised from.

    All six fields, deliberately. `gateway_api._audit_error` classifies
    refusal-vs-failure entirely from `code`, the browser's fix links route on it,
    and a panel prints `gw_code`/`detail` — dropping either loses the gateway's
    own word for what happened. `retryable` defaults False in the constructor, so
    it has to be passed explicitly or every relayed error becomes permanent.
    """
    ra = body.get("retry_after_ms")
    return GatewayError(str(body.get("message") or ""),
                        str(body.get("error_code") or "gateway_rpc_failed"),
                        gw_code=str(body.get("gw_code") or ""),
                        detail=body.get("detail") or {},
                        retryable=bool(body.get("retryable")),
                        retry_after_ms=ra if isinstance(ra, int) else None)


class RemoteGatewayClient:
    """`control_plane()` for `RemoteRuntime`: the same surface, one hop further."""

    def __init__(self) -> None:
        # No I/O here. RemoteRuntime is constructed at import in every process,
        # including ava_cli and every test collection.
        self._fanout = Fanout()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._nudge: queue.Queue = queue.Queue()
        self._aclients: dict[int, httpx.AsyncClient] = {}
        self._snap: dict = {
            "phase": "down", "since": time.time(), "why": "",
            "why_code": "agent_down", "protocol": "", "methods": [],
            "policy": {}, "url_class": "", "last_seq": None,
            "token": {"configured": False, "source": ""},
            "allow_remote": False, "url": "", "events": False,
        }
        self._snap["why"] = "the agent service has not answered yet"

    # ---- plumbing -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return config.AGENT_URL.rstrip("/") + path

    def _headers(self) -> dict:
        return {"X-Ava-Agent-Token": config.AGENT_TOKEN} if config.AGENT_TOKEN else {}

    def _set(self, **kw) -> None:
        with self._lock:
            if "phase" in kw and kw["phase"] != self._snap.get("phase"):
                self._snap["since"] = time.time()
            self._snap.update(kw)

    def _down(self, why_code: str, why: str) -> None:
        self._set(phase="down", why_code=why_code, why=why, methods=[], events=False)

    # ---- the background poller ---------------------------------------------
    def start(self) -> None:
        """Idempotent, never raises, never blocks. Safe from an event loop."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            t = threading.Thread(target=self._poll_loop, name="ava-remote-gw",
                                 daemon=True)
            self._thread = t
        try:
            t.start()
        except RuntimeError:  # interpreter shutting down
            pass

    def _poll_once(self) -> None:
        # /healthz first: it is unauthenticated and carries the capability list,
        # so it distinguishes "this container is too old to proxy" from "the
        # gateway behind it is down" — two different machines to go and fix.
        try:
            r = requests.get(self._url("/healthz"), headers=self._headers(),
                             timeout=5)
        except Exception as e:  # noqa: BLE001
            self._down("agent_down",
                       f"could not reach the agent service at {config.AGENT_URL}: "
                       f"{type(e).__name__}")
            return
        if r.status_code in (401, 403):
            self._down("agent_token_rejected",
                       "the agent service rejected our token — AVA_AGENT_TOKEN "
                       "must be the same on the bridge and agent hosts")
            return
        body = (r.json() or {}) if r.ok else {}
        # /healthz is the ONE unauthenticated route, so a wrong token does not
        # 401 here — it comes back 200 with `authed: false`. Without this the
        # single easiest two-host mistake reports as `agent_down`, sending the
        # owner to check whether a machine is up when the machine is fine and the
        # secret is not. `authed` is absent when no token was offered, so this
        # tests `is False` and never a falsy default.
        if body.get("authed") is False:
            self._down("agent_token_rejected",
                       "the agent service rejected our token — AVA_AGENT_TOKEN "
                       "must be the same on the bridge and agent hosts")
            return
        caps = body.get("capabilities")
        caps = [str(c) for c in caps] if isinstance(caps, list) else []
        if "gateway.proxy" not in caps:
            self._down(_UNSUPPORTED,
                       f"the agent service at {config.AGENT_URL} does not proxy "
                       "the gateway control plane — rebuild and restart the agent "
                       "container on that host")
            return
        try:
            r = requests.get(self._url("/gateway/status"), headers=self._headers(),
                             timeout=8)
        except Exception as e:  # noqa: BLE001
            self._down("agent_down",
                       f"could not reach the agent service at {config.AGENT_URL}: "
                       f"{type(e).__name__}")
            return
        if r.status_code in (401, 403):
            # The authenticated half of the same mistake: an older shim that has
            # no `authed` field on /healthz still 401s here.
            self._down("agent_token_rejected",
                       "the agent service rejected our token — AVA_AGENT_TOKEN "
                       "must be the same on the bridge and agent hosts")
            return
        if r.status_code == 404:
            self._down(_UNSUPPORTED,
                       f"the agent service at {config.AGENT_URL} does not proxy "
                       "the gateway control plane — rebuild and restart the agent "
                       "container on that host")
            return
        if not r.ok:
            self._down("agent_down",
                       f"the agent service answered {r.status_code}")
            return
        st = r.json() or {}
        if not isinstance(st, dict):
            self._down("agent_down",
                       "the agent service sent a status this bridge could not read")
            return
        # The shim's own nine keys, verbatim — `url_class` included, because it
        # describes where the ADMIN TOKEN travels (the agent host's loopback),
        # not this bridge's hop to the shim.
        st["events"] = "gateway.proxy.events" in caps
        with self._lock:
            if st.get("phase") != self._snap.get("phase"):
                st["since"] = st.get("since") or time.time()
            self._snap.update(st)

    def _poll_loop(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception:  # noqa: BLE001 — a poller that dies is a stuck phase
                pass
            try:
                # Doubles as the reconnect nudge: a sentinel wakes us early.
                self._nudge.get(timeout=_POLL_S)
                try:
                    requests.post(self._url("/gateway/reconnect"),
                                  headers=self._headers(), timeout=5)
                except Exception:  # noqa: BLE001 — best effort by contract
                    pass
            except queue.Empty:
                pass

    # ---- the surface gateway_api touches ------------------------------------
    def status(self) -> dict:
        """A pure in-memory read. Never raises, never does I/O."""
        self.start()
        with self._lock:
            return dict(self._snap)

    def methods(self) -> frozenset[str]:
        with self._lock:
            got = self._snap.get("methods") or []
        return frozenset(str(m) for m in got)

    def reconnect(self) -> None:
        """Ask the shim to redial. Returns immediately; never raises."""
        self.start()
        try:
            self._nudge.put_nowait(True)
        except Exception:  # noqa: BLE001
            pass

    def _blocked(self) -> GatewayError | None:
        """The refusal to raise WITHOUT an HTTP call, or None to proceed."""
        with self._lock:
            code, why, phase = (self._snap.get("why_code"), self._snap.get("why"),
                                self._snap.get("phase"))
        if phase == "down" and code in (_UNSUPPORTED, "agent_token_rejected"):
            # Both are permanent until a human acts, so spending a round trip to
            # rediscover them just makes every panel slower on a broken install.
            return GatewayError(str(why or ""), str(code), retryable=False)
        return None

    async def arpc(self, method: str, params: dict | None = None, *,
                   timeout: float | None = None,
                   idempotency_key: str | None = None) -> dict:
        """One call, awaited. Genuinely non-blocking — see the module header.

        `tests/test_no_blocking_routes.py` cannot catch a synchronous call hidden
        behind an `await`, so the obligation is on this method rather than on a
        guard: everything here must be httpx, never requests.
        """
        self.start()
        blocked = self._blocked()
        if blocked is not None:
            raise blocked
        t = float(timeout or 30.0)
        t = max(1.0, min(_TIMEOUT_MAX, t))
        body = {"method": method, "params": params or {}, "timeout": t}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        try:
            client = self._aclient()
            r = await client.post(self._url("/gateway/rpc"), json=body,
                                  headers=self._headers(),
                                  timeout=httpx.Timeout(connect=5.0, read=t + 10.0,
                                                        write=10.0, pool=15.0))
        except (httpx.ConnectError, httpx.ConnectTimeout, OSError) as e:
            raise GatewayError(
                f"could not reach the agent service at {config.AGENT_URL}: "
                f"{type(e).__name__}", "agent_down", retryable=True) from e
        except httpx.ReadTimeout as e:
            raise GatewayError(f"`{method}` did not answer within {t:g}s",
                               "gateway_timeout", retryable=True) from e
        except httpx.HTTPError as e:
            raise GatewayError(
                f"the agent service call failed: {type(e).__name__}",
                "agent_down", retryable=True) from e
        return self._decode(r.status_code, r)

    def _decode(self, code: int, r) -> dict:
        """Shared by arpc and rpc: one HTTP response -> payload or GatewayError."""
        if code in (401, 403):
            raise GatewayError(
                "the agent service rejected our token — AVA_AGENT_TOKEN must be "
                "the same on the bridge and agent hosts",
                "agent_token_rejected", retryable=False)
        if code == 404:
            raise GatewayError(
                f"the agent service at {config.AGENT_URL} does not proxy the "
                "gateway control plane — rebuild and restart the agent container "
                "on that host", _UNSUPPORTED, retryable=False)
        if code >= 400:
            raise GatewayError(f"the agent service answered {code}",
                               "agent_down", retryable=True)
        try:
            body = r.json()
        except Exception as e:
            raise GatewayError(
                "the agent service sent an answer this bridge could not read",
                "gateway_rpc_failed") from e
        if not isinstance(body, dict) or "ok" not in body:
            raise GatewayError(
                "the agent service sent an answer this bridge could not read",
                "gateway_rpc_failed")
        if not body.get("ok"):
            raise _rebuild(body)
        payload = body.get("payload")
        return payload if isinstance(payload, dict) else {}

    def _aclient(self) -> httpx.AsyncClient:
        """One client per running loop.

        A pooled connection reused from a loop that has since closed dies with
        "Event loop is closed" — the failure `phone_bridge._app_client` documents
        under TestClient, where each request gets its own loop.
        """
        loop = asyncio.get_running_loop()
        key = id(loop)
        client = self._aclients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(trust_env=False)
            self._aclients = {key: client}      # drop clients of dead loops
        return client

    # ---- the runtime-level surface (agent_media, identity, approvals) -------
    def rpc(self, method: str, params: dict | None = None, *,
            timeout: float = 30.0, idempotency_key: str | None = None) -> dict:
        """Synchronous by contract (`AgentRuntime.rpc`). Blocks; never on a loop.

        The runtime refusal below is not decoration: a static allowlist cannot
        reliably match a name as generic as `rpc`, and this call blocks for up to
        two minutes. The reference client guards itself the same way.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("RemoteGatewayClient.rpc() blocks and you are on "
                               "an event loop — await arpc() instead.")
        self.start()
        blocked = self._blocked()
        if blocked is not None:
            raise blocked
        t = max(1.0, min(_TIMEOUT_MAX, float(timeout or 30.0)))
        body = {"method": method, "params": params or {}, "timeout": t}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        try:
            r = requests.post(self._url("/gateway/rpc"), json=body,
                              headers=self._headers(), timeout=t + 10.0)
        except requests.Timeout as e:
            raise GatewayError(f"`{method}` did not answer within {t:g}s",
                               "gateway_timeout", retryable=True) from e
        except Exception as e:
            raise GatewayError(
                f"could not reach the agent service at {config.AGENT_URL}: "
                f"{type(e).__name__}", "agent_down", retryable=True) from e
        return self._decode(r.status_code, r)

    # ---- subscriptions ------------------------------------------------------
    #
    # Slice 1 ships these live but empty. That is deliberate and it is what
    # `tests/test_runtime_capability_contract.py` demands of any runtime with a
    # control plane: "empty, but not dead" — `get(timeout=0)` returns None
    # without raising and `close()` works. The bridge keeps its own Fanout, so
    # when the event stream lands the only new thing is something calling
    # `self._fanout.dispatch(...)`; the topic filter, gap synthesis and `dropped`
    # accounting already work here and never have to travel upstream.
    def subscribe(self, topics=None, *, maxlen: int = 1000) -> EventSubscription:
        self.start()
        sub = EventSubscription(self._fanout, self._fanout.next_key(),
                                frozenset(topics) if topics else None, maxlen)
        self._fanout.add(sub)
        return sub

    def asubscribe(self, topics=None, *, maxlen: int = 1000) -> AsyncEventSubscription:
        self.start()
        sub = AsyncEventSubscription(self._fanout, self._fanout.next_key(),
                                     frozenset(topics) if topics else None,
                                     maxlen, asyncio.get_running_loop())
        self._fanout.add(sub)
        return sub
