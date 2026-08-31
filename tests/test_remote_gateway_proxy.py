"""The gateway proxy seam: a real RemoteRuntime against the real shim app.

WHAT THIS EXISTS TO PREVENT. `tests/test_remote_brain_contract.py` was written
after a two-sided contract shipped built on one side only — the bridge read
fields off `/healthz` that the shim never sent, both files were correct alone,
and every `remote` install reported no model for days. The gateway proxy is a
second, larger seam of exactly that shape: the bridge rebuilds a `GatewayError`
from a body the shim composes, and a field dropped on either side degrades into
a wrong fix link rather than an error.

So this file drives BOTH halves at once — `RemoteGatewayClient`'s HTTP is
translated into a `TestClient` over the real `agent_runtime_server.app`. A fake
shim that agrees with the adapter proves only that the adapter agrees with the
fake.

The three states that matter, and they are three because `remote` is the only
adapter whose control plane depends on another machine:

  * the agent container is too old to proxy  -> rebuild it, on THAT host
  * the agent container is unreachable       -> transient; keep retrying
  * the gateway behind it is down            -> a third machine's problem

Reporting any of them as `agent_no_gateway` would tell the owner to select
`agent.runtime: openclaw_gw`, which is the wrong instruction and the wrong box.

NOTE: cannot be imported on Windows — `ava_bridge/audit.py` imports `fcntl`
unconditionally. Run under Linux.

Run: .venv/bin/python -m pytest tests/test_remote_gateway_proxy.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-gw-proxy-test-"))

from fastapi.testclient import TestClient

from ava_bridge import agent_runtime_server as shim
from ava_bridge import config
from ava_bridge.runtime import remote_gateway
from ava_bridge.runtime.errors import GatewayError, GatewayUnsupported
from ava_bridge.runtime.remote import RemoteRuntime

TOKEN = "shared-agent-token"
BASE = "http://agent:9100"

#: What a healthy gateway on the agent host reports. Nine keys, as the reference
#: client returns them.
GW_STATUS = {"phase": "ready", "since": 1.0, "why": "", "why_code": "",
             "protocol": "1", "methods": ["system.info", "sessions.list"],
             "policy": {}, "url_class": "loopback", "last_seq": 7}


class _Seam(unittest.TestCase):
    """A real RemoteRuntime whose transport lands on the real shim app."""

    #: Capabilities the shim advertises. Override to simulate an older container.
    caps: list | None = None
    #: Set to a status code to make every shim call answer that instead.
    force_status: int | None = None
    #: What the bridge actually puts on the wire. Overridden to express a token
    #: MISMATCH: both sides read the same `config` module object in-process, so
    #: patching the constant twice cannot model two hosts disagreeing.
    send_token: str | None = None

    def setUp(self) -> None:
        self.rt = RemoteRuntime()
        self.client = TestClient(shim.app, base_url="http://localhost",
                                 raise_server_exceptions=False)
        self._patches = [
            mock.patch.object(config, "AGENT_URL", BASE),
            mock.patch.object(config, "AGENT_TOKEN", TOKEN),
            mock.patch.object(config, "AGENT_ENABLED", True),
            # Patched on the `requests` MODULE OBJECT, which `remote` and
            # `remote_gateway` both import — so one patch covers the adapter's
            # /healthz probe and the client's own calls. That aliasing is load
            # bearing here, and it is also why a module-scope `mock.patch` in
            # ANOTHER file that rebinds the NAME `requests` inside `remote`
            # silently disables this one. `tests/test_runtime_capability_contract`
            # did exactly that from import until its own teardown; its pin now
            # lives in `setUpModule` so it cannot reach across files.
            mock.patch.object(remote_gateway.requests, "get", self._get),
            mock.patch.object(remote_gateway.requests, "post", self._post),
            mock.patch.object(shim._gw, "status", lambda: dict(GW_STATUS)),
        ]
        if self.caps is not None:
            self._patches.append(
                mock.patch.object(shim, "CAPABILITIES", list(self.caps)))
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    # -- transport translation ------------------------------------------------
    def _rel(self, url):
        return url.replace(BASE, "")

    def _wrap(self, resp):
        resp.ok = resp.is_success       # the attribute the adapter reads
        return resp

    def _hdr(self, headers):
        if self.send_token is None:
            return headers
        h = dict(headers or {})
        h["X-Ava-Agent-Token"] = self.send_token
        return h

    def _get(self, url, headers=None, timeout=None):
        if self.force_status is not None:
            raise OSError("agent host unreachable")
        return self._wrap(self.client.get(self._rel(url),
                                          headers=self._hdr(headers)))

    def _post(self, url, json=None, headers=None, timeout=None):
        if self.force_status is not None:
            raise OSError("agent host unreachable")
        return self._wrap(self.client.post(self._rel(url), json=json,
                                           headers=self._hdr(headers)))

    def cp(self):
        """The control plane, with its snapshot refreshed deterministically.

        `_poll_once` rather than waiting on the background thread: a test that
        sleeps for a poller is a test that is flaky on a loaded machine.
        """
        cp = self.rt.control_plane()
        cp._poll_once()
        return cp


class AHealthyProxy(_Seam):

    def test_the_runtime_has_a_control_plane(self):
        self.assertIsNotNone(self.rt.control_plane())

    def test_the_phase_mirrors_the_gateway_not_the_shim(self):
        """`phase: ready` gates the streamed chat path. A phase that said ready
        because the SHIM answered would stream turns into a dead gateway."""
        st = self.cp().status()
        self.assertEqual(st["phase"], "ready")
        self.assertEqual(st["url_class"], "loopback",
                         "url_class must describe where the ADMIN TOKEN travels "
                         "— the agent host's loopback — not the bridge's hop")

    def test_methods_are_relayed_so_the_bridge_does_not_invent_a_second_allowlist(self):
        """Only the socket holder knows `hello-ok.features.methods`. A bridge-side
        allowlist over an empty relayed list would refuse every call with
        `gateway_unsupported_method`, sending the owner hunting a permission
        problem that does not exist."""
        self.cp()
        self.assertEqual(self.rt.rpc_methods(),
                         frozenset({"system.info", "sessions.list"}))

    def test_a_call_round_trips(self):
        with mock.patch.object(shim._gw, "rpc", return_value={"v": 42}):
            got = self.cp().rpc("system.info")
        self.assertEqual(got, {"v": 42})

    def test_capabilities_translates_the_container_word_into_the_adapter_word(self):
        """Two namespaces, two questions. The shim says `gateway.proxy` about a
        CONTAINER; `AgentRuntime.capabilities()` says `gateway.rpc` about an
        ADAPTER. Conflating them lets a shim route silently re-partition the
        runtime contract test."""
        self.rt._avail_cache.update(ts=0.0, ok=None, caps=[])
        caps = self.rt.capabilities()
        self.assertIn("gateway.proxy", caps)
        self.assertIn("gateway.rpc", caps)

    def test_status_never_performs_io(self):
        """It is called synchronously on the event loop from two routes with no
        try/except — once AFTER `ws.accept()`. Anything slow or throwing there
        becomes an accepted-then-dropped socket the browser redials forever."""
        cp = self.cp()
        # `start()` is neutralised so this observes `status()` ALONE. The
        # background poller does I/O by design — that is what it is for — and
        # leaving it running would make this a test of the poller.
        with mock.patch.object(cp, "start", lambda: None), \
             mock.patch.object(remote_gateway.requests, "get",
                               side_effect=AssertionError("status did I/O")), \
             mock.patch.object(remote_gateway.requests, "post",
                               side_effect=AssertionError("status did I/O")):
            for _ in range(100):
                st = cp.status()
        self.assertEqual(st["phase"], "ready")


class AnErrorRoundTrip(_Seam):

    def test_every_field_survives_the_hop(self):
        """The exact pair `tests/test_gateway_api.py` asserts reaches the browser.
        Dropping `gw_code` or `detail` leaves a panel printing our paraphrase of
        a refusal instead of what the gateway actually said."""
        err = GatewayError("no scope", "gateway_call_refused", gw_code="FORBIDDEN",
                           detail={"missingScope": "operator.admin"},
                           retryable=True, retry_after_ms=500)
        with mock.patch.object(shim._gw, "rpc", side_effect=err):
            with self.assertRaises(GatewayError) as ctx:
                self.cp().rpc("system.info")
        e = ctx.exception
        self.assertEqual(e.code, "gateway_call_refused")
        self.assertEqual(e.gw_code, "FORBIDDEN")
        self.assertEqual(e.detail, {"missingScope": "operator.admin"})
        self.assertIs(e.retryable, True)
        self.assertEqual(e.retry_after_ms, 500)
        self.assertEqual(str(e), "no scope")

    def test_retryable_is_not_silently_dropped(self):
        """It defaults False in the constructor, so a rebuild that forgets to
        pass it turns every transient gateway hiccup into a permanent failure."""
        err = GatewayError("busy", "gateway_rpc_failed", retryable=True)
        with mock.patch.object(shim._gw, "rpc", side_effect=err):
            with self.assertRaises(GatewayError) as ctx:
                self.cp().rpc("system.info")
        self.assertIs(ctx.exception.retryable, True)

    def test_the_deny_list_refuses_before_the_gateway_is_reached(self):
        from ava_bridge.runtime import gateway_policy
        import json
        raw = json.dumps({"gateway": {"controlUi":
                                      {"dangerouslyDisableDeviceAuth": True}}})
        with mock.patch.object(shim._gw, "rpc") as rpc:
            with self.assertRaises(GatewayError) as ctx:
                self.cp().rpc("config.set", {"raw": raw})
        self.assertEqual(ctx.exception.code, "gateway_key_refused")
        self.assertIn(gateway_policy.DENIED_CONFIG_KEYS[0], str(ctx.exception))
        rpc.assert_not_called()


class AnOlderAgentContainer(_Seam):
    """The shim predates the proxy: `gateway.proxy` is simply absent."""

    caps = ["provision.scope", "provision.assert", "provision.connector",
            "health.model"]

    def test_it_is_reported_as_a_container_to_rebuild(self):
        st = self.cp().status()
        self.assertEqual(st["phase"], "down")
        self.assertEqual(st["why_code"], "gateway_proxy_unsupported")
        self.assertIn("rebuild", st["why"])
        self.assertIn(BASE, st["why"], "the message must name the agent host")

    def test_a_call_refuses_without_spending_a_round_trip(self):
        """Permanent until a human acts, so rediscovering it on every call would
        just make every panel slower on a broken install."""
        cp = self.cp()
        with mock.patch.object(remote_gateway.requests, "post",
                               side_effect=AssertionError("made an HTTP call")):
            with self.assertRaises(GatewayError) as ctx:
                cp.rpc("system.info")
        self.assertEqual(ctx.exception.code, "gateway_proxy_unsupported")

    def test_it_is_not_reported_as_having_no_control_plane(self):
        """`agent_no_gateway`'s copy says "select `agent.runtime: openclaw_gw`".
        On a two-host install that is the wrong instruction AND the wrong box."""
        with self.assertRaises(GatewayError) as ctx:
            self.cp().rpc("system.info")
        self.assertNotIsInstance(ctx.exception, GatewayUnsupported)
        self.assertNotEqual(ctx.exception.code, "agent_no_gateway")

    def test_the_adapter_does_not_claim_the_capability(self):
        self.rt._avail_cache.update(ts=0.0, ok=None, caps=[])
        self.assertNotIn("gateway.rpc", self.rt.capabilities())


class AnUnreachableAgentHost(_Seam):

    force_status = 599       # any value: the transport raises OSError instead

    def test_it_is_transient_and_names_the_hop(self):
        """Distinct from an old container: nothing needs rebuilding, and the
        message has to say WHICH machine is not answering."""
        st = self.cp().status()
        self.assertEqual(st["phase"], "down")
        self.assertEqual(st["why_code"], "agent_down")
        self.assertIn(BASE, st["why"])

    def test_a_call_raises_a_retryable_gateway_error(self):
        with self.assertRaises(GatewayError) as ctx:
            self.cp().rpc("system.info")
        self.assertNotIsInstance(ctx.exception, GatewayUnsupported)
        self.assertEqual(ctx.exception.code, "agent_down")
        self.assertIs(ctx.exception.retryable, True)


class ATokenMismatch(_Seam):
    """The bridge sends one secret; the agent host holds another."""

    send_token = "not-the-agents-secret"

    def test_it_names_the_variable_and_both_hosts(self):
        """The single easiest mistake on a two-host install, and the one whose
        symptom (everything 401s) looks nothing like its cause. The message has
        to name the variable AND say it must match across the two machines."""
        st = self.cp().status()
        self.assertEqual(st["why_code"], "agent_token_rejected")
        self.assertIn("AVA_AGENT_TOKEN", st["why"])
        self.assertIn("same", st["why"])
        self.assertEqual(st["phase"], "down")

    def test_a_call_refuses_without_spending_a_round_trip(self):
        """Permanent until a human edits a .env on one of two machines."""
        cp = self.cp()
        with mock.patch.object(remote_gateway.requests, "post",
                               side_effect=AssertionError("made an HTTP call")):
            with self.assertRaises(GatewayError) as ctx:
                cp.rpc("system.info")
        self.assertEqual(ctx.exception.code, "agent_token_rejected")


class SubscriptionsAreLiveButEmpty(_Seam):

    def test_a_subscription_is_empty_not_dead(self):
        """A queue that can never yield is indistinguishable from a quiet agent.
        Slice 1 ships no event stream, so this is the honest shape: real object,
        no frames."""
        sub = self.rt.subscribe(["run.step"])
        try:
            self.assertIsNone(sub.get(timeout=0))
        finally:
            sub.close()
            sub.close()          # idempotent; the relay closes in a finally

    def test_the_runtime_and_its_client_share_one_fanout(self):
        """`runtime.configured()` is a singleton and `/ws/gateway`'s fan-out and
        the RPC path must share one client, or a subscriber registered on one
        would never see events dispatched into the other."""
        self.assertIs(self.rt.control_plane(), self.rt.control_plane())

    def test_events_are_not_advertised_until_the_stream_exists(self):
        """`useChat` gates the streamed path on this. Advertising events without
        a stream shows "Still working…" with no chain-of-thought and lands the
        reply on the 5s reconcile — strictly worse than the polled floor."""
        self.assertFalse(self.cp().status()["events"])


if __name__ == "__main__":
    unittest.main()
