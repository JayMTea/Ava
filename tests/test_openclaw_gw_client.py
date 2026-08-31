"""The OpenClaw gateway client, driven at the frame level with no socket.

The client replaces a subprocess that could only ever carry one reply. What it
carries instead is a long-lived, ordered, multiplexed stream, and every failure
mode in this file is one the subprocess simply could not have:

  * a late response from a socket that has since died,
  * an event sequence with a hole in it,
  * a gateway that accepts TCP and then rejects every handshake,
  * an admin token pointed at the wrong daemon.

House style (tests/test_runtime_gate.py): stdlib unittest, no bridge, no
network, no sandbox. Every test here drives real client code — the fakes are
sockets, never the client.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-gwclient-test-"))

from ava_bridge.runtime import openclaw_gw_client as gw
from ava_bridge.runtime.errors import GatewayError


class FakeSocket:
    """A websocket that hands back scripted frames and records what was sent."""

    def __init__(self, inbound: list[dict] | None = None):
        self.sent: list[dict] = []
        self._in = list(inbound or [])
        self.closed = False
        # Frames pushed after construction, for the reader-loop tests.
        self.stream: list[dict] = []

    async def recv(self) -> str:
        if not self._in:
            raise AssertionError("the client read more frames than were scripted")
        return json.dumps(self._in.pop(0))

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        async def _gen():
            for f in self.stream:
                yield json.dumps(f)
        return _gen()


def _hello(methods=("chat.send", "sessions.list"), protocol=4, policy=None,
           device_token=None) -> dict:
    payload = {"protocol": protocol,
               "features": {"methods": list(methods)},
               "policy": policy or {"maxPayload": 25 * 1024 * 1024}}
    if device_token:
        payload["auth"] = {"deviceToken": device_token}
    return payload


def _run(coro):
    return asyncio.run(coro)


class HandshakeTests(unittest.TestCase):

    def _client(self) -> gw.OpenClawGatewayClient:
        return gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:18789/",
                                        token_resolver=lambda: "tok")

    def test_the_three_phases_run_in_order_and_capture_the_method_list(self):
        c = self._client()
        sock = FakeSocket([
            {"type": "event", "event": "connect.challenge",
             "payload": {"nonce": "n1", "ts": 123}},
            {"type": "res", "id": None, "ok": True, "payload": _hello()},
        ])
        # The response id is only known once the client has minted it, so the
        # script is patched after the connect frame is sent.
        real_send = sock.send

        async def _send(raw):
            await real_send(raw)
            body = json.loads(raw)
            if body.get("method") == "connect":
                sock._in[0]["id"] = body["id"]
        sock.send = _send

        _run(c._handshake(sock, "tok"))

        connect = sock.sent[0]
        self.assertEqual(connect["method"], "connect")
        self.assertEqual(connect["params"]["role"], "operator")
        # VERIFIED against a live gateway (OpenClaw 2026.7.1, 2026-08-23). Its
        # connect schema refuses unexpected properties, so each of these is a
        # turn-breaking failure if "restored" from the prose docs:
        #   - no `protocol` at the root (minProtocol/maxProtocol only)
        #   - `client.id` is an ENUM ("cli"), and `client.name` is refused
        #   - `client.platform` is REQUIRED
        #   - `auth` takes no `nonce`/`ts`
        self.assertNotIn("protocol", connect["params"])
        self.assertEqual(connect["params"]["minProtocol"], 4)
        self.assertEqual(connect["params"]["maxProtocol"], 4)
        self.assertEqual(connect["params"]["client"]["id"], "cli")
        self.assertNotIn("name", connect["params"]["client"])
        self.assertIn("platform", connect["params"]["client"])
        self.assertIn("tool-events", connect["params"]["caps"],
                      "without this cap the gateway emits no tool events and "
                      "the chain-of-thought renders empty")
        self.assertNotIn("nonce", connect["params"]["auth"])
        self.assertEqual(c.status()["phase"], "ready")
        self.assertIn("chat.send", c.methods())

    def test_a_protocol_mismatch_names_both_numbers_and_does_not_go_ready(self):
        c = self._client()
        sock = FakeSocket([
            {"type": "event", "event": "connect.challenge", "payload": {}},
            {"type": "res", "id": None, "ok": True, "payload": _hello(protocol=7)},
        ])
        real_send = sock.send

        async def _send(raw):
            await real_send(raw)
            body = json.loads(raw)
            if body.get("method") == "connect":
                sock._in[0]["id"] = body["id"]
        sock.send = _send

        with self.assertRaises(GatewayError) as ctx:
            _run(c._handshake(sock, "tok"))
        self.assertEqual(ctx.exception.code, "agent_protocol_mismatch")
        self.assertIn("v7", str(ctx.exception))
        self.assertIn("v4", str(ctx.exception))
        self.assertNotEqual(c.status()["phase"], "ready")

    def test_a_challenge_that_never_arrives_does_not_wedge_the_supervisor(self):
        """A peer that accepts TCP and then says nothing must time out, not hang.

        Without the bound, one unreachable gateway parks the connection thread
        forever and the client never retries.
        """
        c = self._client()

        class Silent(FakeSocket):
            async def recv(self):
                await asyncio.sleep(60)

        with mock.patch.object(gw, "_CHALLENGE_TIMEOUT_S", 0.05):
            with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
                _run(c._handshake(Silent(), "tok"))


class ErrorMappingTests(unittest.TestCase):

    def test_missing_scope_is_its_own_code_not_a_generic_failure(self):
        """`agent_scope_denied` matches neither the `_off` nor the `_down`
        pattern in fixes.ts, which is exactly why it needs its own entry: the
        fix is re-minting a token with the scope, not restarting a service."""
        err = gw._error_from({"code": "FORBIDDEN", "message": "no",
                              "details": {"code": "MISSING_SCOPE",
                                          "missingScope": "operator.admin"}})
        self.assertEqual(err.code, "agent_scope_denied")
        self.assertEqual(err.gw_code, "FORBIDDEN")
        self.assertEqual(err.detail["missingScope"], "operator.admin")

    def test_a_rejected_token_is_distinguishable_from_a_dead_gateway(self):
        err = gw._error_from({"code": "UNAUTHORIZED", "message": "bad token"})
        self.assertEqual(err.code, "agent_token_rejected")

    def test_an_unknown_gateway_error_keeps_the_gateways_own_word(self):
        err = gw._error_from({"code": "WEIRD_THING", "message": "hmm",
                              "retryable": True, "retryAfterMs": 250})
        self.assertEqual(err.code, "gateway_rpc_failed")
        self.assertEqual(err.gw_code, "WEIRD_THING")
        self.assertTrue(err.retryable)
        self.assertEqual(err.as_body()["retry_after_ms"], 250)

    def test_the_failure_body_is_the_shape_coded_routes_return(self):
        body = GatewayError("nope", "gateway_timeout").as_body()
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error_code"], "gateway_timeout")
        self.assertIn("message", body)


class MethodGateTests(unittest.TestCase):

    def _ready(self, methods=("chat.send",), policy=None):
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)
        c._methods = frozenset(methods)
        c._policy = policy or {"maxPayload": 1024}
        c._phase = "ready"
        return c

    def test_an_unadvertised_method_is_refused_locally(self):
        c = self._ready()
        with self.assertRaises(GatewayError) as ctx:
            c._check_method("terminal.open")
        self.assertEqual(ctx.exception.code, "gateway_unsupported_method")

    def test_an_empty_method_list_refuses_everything(self):
        """A terse handshake must not become a blanket grant. The token carries
        operator.admin; forwarding 200 unknown methods because the gateway did
        not enumerate them is a hole, not a degradation."""
        c = self._ready(methods=())
        with self.assertRaises(GatewayError) as ctx:
            c._check_method("chat.send")
        self.assertEqual(ctx.exception.code, "gateway_unsupported_method")
        self.assertIn("did not advertise", str(ctx.exception))

    def test_the_escape_hatch_is_opt_in_and_named(self):
        c = self._ready(methods=())
        with mock.patch.object(gw.config, "AGENT_GATEWAY_TRUST_UNLISTED", True):
            c._check_method("anything.at.all")  # must not raise

    def test_an_oversized_frame_is_refused_here_not_by_the_gateway(self):
        """The gateway's answer to an oversized frame is to close the socket,
        which takes every other in-flight request down with it."""
        c = self._ready(policy={"maxPayload": 32})
        with self.assertRaises(GatewayError) as ctx:
            c._check_size("x" * 100)
        self.assertEqual(ctx.exception.code, "gateway_payload_too_large")
        self.assertEqual(ctx.exception.detail["limit"], 32)


class IdempotencyTests(unittest.TestCase):

    def test_reads_get_no_key_and_writes_do(self):
        self.assertTrue(gw.is_read_method("sessions.list"))
        self.assertTrue(gw.is_read_method("chat.history"))
        self.assertTrue(gw.is_read_method("system.info"))
        self.assertFalse(gw.is_read_method("chat.send"))
        self.assertFalse(gw.is_read_method("plugins.install"))
        self.assertFalse(gw.is_read_method("config.set"),
                         "bias toward write: a misclassified read costs a "
                         "header, a misclassified write costs a duplicate")


class RequestIdTests(unittest.TestCase):

    def test_ids_from_a_dead_socket_cannot_resolve_a_live_future(self):
        """The gateway may legally reuse small integer ids across sockets. The
        per-connection epoch is what keeps a late `res` from the old socket from
        completing a request issued on the new one."""
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)
        c._epoch = "aaaaaaaa"
        first = [c._next_id() for _ in range(3)]
        import itertools
        c._epoch, c._counter = "bbbbbbbb", itertools.count(1)
        second = [c._next_id() for _ in range(3)]
        self.assertEqual(len(set(first) | set(second)), 6)
        self.assertTrue(all(i.startswith("aaaaaaaa-") for i in first))


class FanoutTests(unittest.TestCase):

    def test_a_sequence_gap_is_announced_rather_than_smoothed(self):
        """Buffering or reordering a gap is how a UI ends up rendering a turn
        that never finished. Say so and let each consumer refetch."""
        f = gw.Fanout()
        sub = gw.EventSubscription(f, f.next_key(), None, 10)
        f.add(sub)
        f.dispatch({"event": "run.step", "seq": 1, "payload": {}})
        f.dispatch({"event": "run.step", "seq": 5, "payload": {}})
        topics = []
        while True:
            ev = sub.get(timeout=0)
            if ev is None:
                break
            topics.append(ev["topic"])
        self.assertIn("ava.gateway.gap", topics)
        self.assertEqual(topics, ["run.step", "ava.gateway.gap", "run.step"])

    def test_a_new_socket_is_a_new_sequence(self):
        f = gw.Fanout()
        sub = gw.EventSubscription(f, f.next_key(), None, 10)
        f.add(sub)
        f.dispatch({"event": "a", "seq": 9, "payload": {}})
        f.reset()
        f.dispatch({"event": "b", "seq": 1, "payload": {}})
        topics = []
        while True:
            ev = sub.get(timeout=0)
            if ev is None:
                break
            topics.append(ev["topic"])
        self.assertNotIn("ava.gateway.gap", topics,
                         "seq 1 after a reconnect is not a gap")

    def test_topic_filters_are_honoured(self):
        f = gw.Fanout()
        want = gw.EventSubscription(f, f.next_key(), frozenset({"run.step"}), 10)
        every = gw.EventSubscription(f, f.next_key(), None, 10)
        f.add(want)
        f.add(every)
        f.dispatch({"event": "run.step", "payload": {}})
        f.dispatch({"event": "session.update", "payload": {}})
        self.assertEqual(want.get(timeout=0)["topic"], "run.step")
        self.assertIsNone(want.get(timeout=0))
        self.assertIsNotNone(every.get(timeout=0))
        self.assertIsNotNone(every.get(timeout=0))

    def test_a_slow_consumer_loses_history_never_the_present(self):
        f = gw.Fanout()
        sub = gw.EventSubscription(f, f.next_key(), None, 2)
        f.add(sub)
        for i in range(5):
            f.dispatch({"event": f"e{i}", "payload": {}})
        got = [sub.get(timeout=0)["topic"], sub.get(timeout=0)["topic"]]
        self.assertEqual(got, ["e3", "e4"], "the newest events must survive")
        self.assertEqual(sub.dropped, 3, "and the consumer must be TOLD it lost some")

    def test_close_is_idempotent_and_releases_the_reference(self):
        f = gw.Fanout()
        sub = gw.EventSubscription(f, f.next_key(), None, 4)
        f.add(sub)
        sub.close()
        sub.close()
        f.dispatch({"event": "x", "payload": {}})
        self.assertIsNone(sub.get(timeout=0))


class BackoffTests(unittest.TestCase):

    def test_the_curve_only_resets_after_a_connection_actually_stayed_up(self):
        """Resetting on "connected" lets a gateway that accepts TCP and then
        rejects the handshake hot-loop at half a second, forever."""
        import time as _t
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)
        c._ready_at = _t.time()
        self.assertFalse(c._stayed_ready(), "a moment ago is not long enough")
        c._ready_at = _t.time() - (gw._BACKOFF_RESET_AFTER_S + 1)
        self.assertTrue(c._stayed_ready())
        c._ready_at = None
        self.assertFalse(c._stayed_ready())

    def test_a_token_or_version_problem_waits_the_full_floor(self):
        """Retrying these fast is pure noise — the fix does not arrive by time."""
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)
        c._why_code = "agent_scope_denied"
        self.assertEqual(c._backoff(0), gw._BACKOFF_CAP_S)
        c._why_code = ""
        self.assertLessEqual(c._backoff(0), 0.5)

    def test_the_curve_is_bounded(self):
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)
        for n in range(0, 30):
            self.assertLessEqual(c._backoff(n), gw._BACKOFF_CAP_S)


class BoundaryTests(unittest.TestCase):

    def test_an_admin_token_is_not_sent_off_loopback_by_default(self):
        with mock.patch.object(gw.config, "AGENT_GATEWAY_ALLOW_REMOTE", False):
            gw._refuse_remote("ws://127.0.0.1:18789/")   # fine
            with self.assertRaises(GatewayError) as ctx:
                gw._refuse_remote("ws://192.168.1.9:18789/")
            self.assertEqual(ctx.exception.code, "agent_token_rejected")
            self.assertIn("allow_remote", str(ctx.exception))

    def test_the_opt_in_is_honoured(self):
        with mock.patch.object(gw.config, "AGENT_GATEWAY_ALLOW_REMOTE", True):
            gw._refuse_remote("ws://10.0.0.4:18789/")

    def test_url_class_keeps_private_and_public_distinguishable(self):
        self.assertEqual(gw._url_class("ws://127.0.0.1:1/"), "loopback")
        self.assertEqual(gw._url_class("ws://192.168.4.4:1/"), "private")
        self.assertEqual(gw._url_class("wss://gw.example.com/"), "public")

    def test_the_blocking_call_refuses_to_run_on_an_event_loop(self):
        """A static allowlist in test_no_blocking_routes cannot reliably match a
        name as generic as `rpc`. A runtime refusal cannot be evaded."""
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)

        async def _try():
            with self.assertRaises(RuntimeError) as ctx:
                c.rpc("system.info")
            self.assertIn("arpc", str(ctx.exception))
        _run(_try())


class GatewayPortTests(unittest.TestCase):
    """A real registry record carries BOTH ports, and they are different
    services. This is the bug this suite exists to prevent a second time."""

    RECORD = {"dashboardPort": 18789, "gatewayPort": 8080, "name": "s"}

    def test_the_openclaw_port_is_the_dashboard_port(self):
        from ava_bridge.runtime import nemoclaw_registry as reg
        with mock.patch.object(reg, "registry_record", return_value=self.RECORD):
            self.assertEqual(reg.openclaw_gateway_port("s"), 18789)

    def test_the_openshell_port_is_a_different_service(self):
        from ava_bridge.runtime import nemoclaw_registry as reg
        with mock.patch.object(reg, "registry_record", return_value=self.RECORD):
            self.assertEqual(reg.openshell_gateway_port("s"), 8080)

    def test_the_resolved_url_uses_the_openclaw_port(self):
        from ava_bridge.runtime import nemoclaw_registry as reg
        with mock.patch.object(reg, "registry_record", return_value=self.RECORD), \
             mock.patch.object(gw.config, "AGENT_GATEWAY_URL", ""):
            self.assertEqual(gw._default_url(), "ws://127.0.0.1:18789/")

    def test_an_absent_registry_still_produces_a_usable_default(self):
        from ava_bridge.runtime import nemoclaw_registry as reg
        with mock.patch.object(reg, "registry_record", return_value=None), \
             mock.patch.object(gw.config, "AGENT_GATEWAY_URL", ""):
            self.assertEqual(gw._default_url(), f"ws://127.0.0.1:{gw._DEFAULT_PORT}/")


class TokenTests(unittest.TestCase):

    def test_the_gateway_token_is_never_generated(self):
        """Unlike router_token, this secret is not ours to invent — it must match
        something the gateway will accept. A generated one fails every handshake
        and surfaces as "the gateway rejected our token", which is a lie about
        which side is wrong."""
        import inspect
        src = inspect.getsource(gw._default_token)
        self.assertNotIn("generate=True", src)
        self.assertIn("openclaw_gateway_token", src)

    def test_the_real_unauthorized_shape_is_read_as_a_token_problem(self):
        """CAPTURED FROM THE LIVE GATEWAY (OpenClaw 2026.7.1, 2026-08-23).

        A rejected token does NOT arrive with an auth code at the top level. It
        arrives as the generic `INVALID_REQUEST`, and the only thing naming the
        real cause sits in `details`. Reading `code` alone reported this as
        `gateway_rpc_failed`, which meant the token backoff floor never engaged
        and the refresh never fired: Ava sat on the Direct floor after every
        sandbox restart and called it "the agent is off".
        """
        e = gw._error_from({
            "code": "INVALID_REQUEST",
            "message": "unauthorized: gateway token mismatch",
            "details": {"code": "AUTH_TOKEN_MISMATCH",
                        "authReason": "token_mismatch",
                        "canRetryWithDeviceToken": False,
                        "recommendedNextStep": "update_auth_credentials"},
        })
        self.assertEqual(e.code, "agent_token_rejected")

    def test_a_refresh_that_found_the_same_token_reports_no_progress(self):
        """The supervisor resets its backoff when this returns True, so saying
        True for an unchanged token turns a permanently-wrong credential into a
        hot loop against `nemoclaw`."""
        with mock.patch("shutil.which", return_value="/usr/bin/nemoclaw"), \
                mock.patch("subprocess.run") as run, \
                mock.patch.object(gw.settings, "secret", return_value="same"):
            run.return_value = mock.Mock(returncode=0, stdout="same\n")
            self.assertFalse(gw._refresh_token_from_sandbox())

    def test_a_refresh_refuses_junk_that_could_not_be_a_token(self):
        """`gateway-token` printing a warning or an empty line must not clobber
        a working secret with it."""
        with mock.patch("shutil.which", return_value="/usr/bin/nemoclaw"), \
                mock.patch("subprocess.run") as run, \
                mock.patch.object(gw.settings, "secret", return_value="old"):
            for bad in ("", "   ", "two words", "line\nline"):
                run.return_value = mock.Mock(returncode=0, stdout=bad)
                self.assertFalse(gw._refresh_token_from_sandbox(),
                                 f"accepted {bad!r} as a token")
            run.return_value = mock.Mock(returncode=1, stdout="looks-fine")
            self.assertFalse(gw._refresh_token_from_sandbox(),
                             "a non-zero exit is not a token")

    def test_a_device_token_from_hello_is_offered_on_the_next_connect(self):
        with mock.patch.object(gw.settings, "secret", return_value="dev-1"):
            params = gw._auth_params("tok", {"nonce": "n", "ts": 1})
        self.assertEqual(params["deviceToken"], "dev-1",
                         "reusing the issued device token is what stops a "
                         "reconnect re-consuming a pairing")
        self.assertEqual(params["token"], "tok")
        # The live schema REFUSES `nonce`/`ts` in auth — the challenge is
        # answered by the token alone.
        self.assertNotIn("nonce", params)
        self.assertNotIn("ts", params)


class ReaderTests(unittest.TestCase):

    def test_a_response_completes_its_own_pending_future_only(self):
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)

        async def _drive():
            loop = asyncio.get_running_loop()
            c._loop = loop
            a, b = loop.create_future(), loop.create_future()
            c._pending = {"e-1": a, "e-2": b}
            sock = FakeSocket()
            sock.stream = [{"type": "res", "id": "e-2", "ok": True,
                            "payload": {"who": "b"}}]
            await c._read_forever(sock)
            self.assertTrue(b.done())
            self.assertFalse(a.done(), "an unrelated request must stay pending")
            self.assertEqual(b.result()["payload"], {"who": "b"})
        _run(_drive())

    def test_a_dropped_socket_fails_every_pending_call_at_once(self):
        """A caller told in 0 ms can retry; one told in 600 s already showed a
        spinner."""
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)

        async def _drive():
            loop = asyncio.get_running_loop()
            futs = [loop.create_future() for _ in range(3)]
            c._pending = {f"e-{i}": f for i, f in enumerate(futs)}
            c._fail_pending(GatewayError("dropped", "agent_down", retryable=True))
            self.assertEqual(c._pending, {})
            for f in futs:
                self.assertIsInstance(f.exception(), GatewayError)
                self.assertEqual(f.exception().code, "agent_down")
        _run(_drive())

    def test_a_malformed_frame_does_not_kill_the_reader(self):
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)

        async def _drive():
            sub = c.subscribe()
            sock = FakeSocket()

            class Bad(FakeSocket):
                def __aiter__(self):
                    async def _gen():
                        yield "not json at all"
                        yield json.dumps({"type": "event", "event": "ok",
                                          "payload": {}})
                    return _gen()
            await c._read_forever(Bad())
            self.assertEqual(sub.get(timeout=0)["topic"], "ok")
            del sock
        _run(_drive())


class CallPathTests(unittest.TestCase):
    """`arpc` end to end against a fake socket, including the cross-loop hop.

    A future created on one loop and awaited from another does not raise — it
    simply never completes. That failure mode reads as "the gateway is slow",
    which is the most expensive kind of bug to chase, so it gets a test that
    would actually hang without the fix.
    """

    def _client(self) -> gw.OpenClawGatewayClient:
        c = gw.OpenClawGatewayClient(url_resolver=lambda: "ws://127.0.0.1:1/",
                                     token_resolver=lambda: None)
        c._methods = frozenset({"sessions.list", "chat.send"})
        c._policy = {"maxPayload": 1 << 20}
        c._phase = "ready"
        return c

    def test_a_call_resolves_against_the_matching_response(self):
        c = self._client()

        class Echo(FakeSocket):
            def __init__(self, client):
                super().__init__()
                self._c = client

            async def send(self, raw):
                body = json.loads(raw)
                self.sent.append(body)
                fut = self._c._pending.get(body["id"])
                if fut and not fut.done():
                    fut.set_result({"type": "res", "id": body["id"], "ok": True,
                                    "payload": {"sessions": ["a", "b"]}})

        async def _drive():
            c._loop = asyncio.get_running_loop()
            c._ws = Echo(c)
            got = await c._arpc("sessions.list", {})
            self.assertEqual(got["sessions"], ["a", "b"])
            self.assertNotIn("idempotencyKey", c._ws.sent[0],
                             "a read needs no idempotency key")
        _run(_drive())

    def test_a_write_carries_an_idempotency_key(self):
        c = self._client()

        class Echo(FakeSocket):
            def __init__(self, client):
                super().__init__()
                self._c = client

            async def send(self, raw):
                body = json.loads(raw)
                self.sent.append(body)
                fut = self._c._pending.get(body["id"])
                if fut and not fut.done():
                    fut.set_result({"type": "res", "id": body["id"], "ok": True,
                                    "payload": {}})

        async def _drive():
            c._loop = asyncio.get_running_loop()
            c._ws = Echo(c)
            await c._arpc("chat.send", {"text": "hi"}, idempotency_key="turn:t1")
            # In PARAMS, not at the frame root: the gateway answers a root
            # key with "invalid request frame: at root: unexpected property".
            self.assertNotIn("idempotencyKey", c._ws.sent[0])
            self.assertEqual(c._ws.sent[0]["params"]["idempotencyKey"], "turn:t1",
                             "a retried send must not start two runs")
        _run(_drive())

    def test_a_gateway_error_reaches_the_caller_as_a_coded_failure(self):
        c = self._client()

        class Err(FakeSocket):
            def __init__(self, client):
                super().__init__()
                self._c = client

            async def send(self, raw):
                body = json.loads(raw)
                fut = self._c._pending.get(body["id"])
                if fut and not fut.done():
                    fut.set_result({"type": "res", "id": body["id"], "ok": False,
                                    "error": {"code": "FORBIDDEN",
                                              "message": "nope",
                                              "details": {"code": "MISSING_SCOPE"}}})

        async def _drive():
            c._loop = asyncio.get_running_loop()
            c._ws = Err(c)
            with self.assertRaises(GatewayError) as ctx:
                await c._arpc("chat.send", {})
            self.assertEqual(ctx.exception.code, "agent_scope_denied")
        _run(_drive())

    def test_a_timeout_does_not_close_the_socket(self):
        """One slow method must not disturb the other 199 in flight."""
        c = self._client()

        class Mute(FakeSocket):
            async def send(self, raw):
                self.sent.append(json.loads(raw))

        async def _drive():
            c._loop = asyncio.get_running_loop()
            sock = Mute()
            c._ws = sock
            with self.assertRaises(GatewayError) as ctx:
                await c._arpc("sessions.list", {}, timeout=0.05)
            self.assertEqual(ctx.exception.code, "gateway_timeout")
            self.assertFalse(sock.closed, "a slow call must not drop the socket")
            self.assertEqual(c._pending, {}, "the pending entry must be reaped")
        _run(_drive())

    def test_a_caller_arriving_while_down_is_told_now_not_in_ten_minutes(self):
        c = self._client()
        c._phase = "down"
        c._why = "cannot reach the gateway"

        async def _drive():
            c._loop = asyncio.get_running_loop()
            with mock.patch.object(gw, "_READY_WAIT_S", 0.05):
                with self.assertRaises(GatewayError) as ctx:
                    await c._arpc("sessions.list", {})
            self.assertEqual(ctx.exception.code, "agent_down")
            self.assertTrue(ctx.exception.retryable)
        _run(_drive())

    def test_arpc_bridges_a_call_made_from_another_loop(self):
        """The regression guard for the cross-loop future.

        A future created on the socket's loop and awaited from a request
        handler's loop does not raise — it never completes. So this drives the
        real bridge with a real second loop, bounded, and would time out rather
        than pass if `arpc` went back to awaiting directly.

        The supervisor is deliberately NOT started: it would dial the fake URL,
        fail, and reset the phase and socket this test just installed.
        """
        import threading

        c = self._client()
        loop = asyncio.new_event_loop()
        started = threading.Event()

        def _spin():
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        t = threading.Thread(target=_spin, name="gw-test-loop", daemon=True)
        t.start()
        started.wait(timeout=5)
        c._loop = loop
        # `arpc` lazily calls start(); marking the thread live keeps it from
        # spawning a real supervisor that would replace this loop.
        c._thread = t
        self.addCleanup(lambda: loop.call_soon_threadsafe(loop.stop))

        class Echo(FakeSocket):
            def __init__(self, client):
                super().__init__()
                self._c = client

            async def send(self, raw):
                body = json.loads(raw)
                self.sent.append(body)
                fut = self._c._pending.get(body["id"])
                if fut and not fut.done():
                    fut.set_result({"type": "res", "id": body["id"], "ok": True,
                                    "payload": {"ok": 1}})

        c._ws = Echo(c)

        async def _from_another_loop():
            self.assertIsNot(asyncio.get_running_loop(), loop,
                             "this test is meaningless on the same loop")
            return await asyncio.wait_for(c.arpc("sessions.list", {}), 5.0)

        self.assertEqual(_run(_from_another_loop()), {"ok": 1})

    def test_the_blocking_facade_reaches_the_same_call(self):
        """`rpc()` is what every worker thread uses — turns, provision, the CLI."""
        import threading

        c = self._client()
        loop = asyncio.new_event_loop()
        started = threading.Event()

        def _spin():
            asyncio.set_event_loop(loop)
            loop.call_soon(started.set)
            loop.run_forever()

        t = threading.Thread(target=_spin, name="gw-test-loop2", daemon=True)
        t.start()
        started.wait(timeout=5)
        c._loop = loop
        c._thread = t
        self.addCleanup(lambda: loop.call_soon_threadsafe(loop.stop))

        class Echo(FakeSocket):
            def __init__(self, client):
                super().__init__()
                self._c = client

            async def send(self, raw):
                body = json.loads(raw)
                fut = self._c._pending.get(body["id"])
                if fut and not fut.done():
                    fut.set_result({"type": "res", "id": body["id"], "ok": True,
                                    "payload": {"from": "thread"}})

        c._ws = Echo(c)
        self.assertEqual(c.rpc("sessions.list", {}, timeout=5.0),
                         {"from": "thread"})



if __name__ == "__main__":
    unittest.main()
