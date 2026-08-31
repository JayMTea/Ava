"""The agent shim's gateway routes: authenticated, validated, and coded.

WHAT THESE GUARD. `agent_runtime_server` now relays the OpenClaw gateway control
plane, which means one HTTP door on the agent host reaches the gateway's cron,
devices, plugins, approvals, `config.*` and `secrets.resolve`. Three properties
have to hold or that door is worse than not having it:

  1. **Authenticated.** Only `/healthz` is public. Nothing else in the repo
     checks this: `tests/test_route_table_stable.py` and `qa/test_01_auth_surface`
     both enumerate `phone_bridge.app`, not this app, so if the middleware ever
     grows a second exemption these tests are the only thing that notices.
  2. **Validated at the door.** This is a second network boundary, and the shim's
     own `_CONNECTOR_ID` comment already states the principle — "the two ends of
     an exec are worth two checks".
  3. **Coded, not thrown.** Failures ride as HTTP 200 bodies carrying all six
     `GatewayError.as_body()` keys, because the bridge rebuilds the error from
     them, `gateway_api._audit_error` classifies refusal-vs-failure entirely from
     `error_code`, and the browser's fix links route on it. A bodyless 500 loses
     `gw_code` and `detail` — the exact pair `tests/test_gateway_api.py` asserts
     survives all the way to a panel.

The token here is a REAL header, not a bypass. `tests/test_remote_scope_handshake`
records why: "Accepting 401 here would have made this test pass without ever
reaching the route — which is exactly what it did on the first cut."

NOTE: this module cannot be imported on Windows — `ava_bridge/audit.py` imports
`fcntl` unconditionally and the shim's import chain reaches it. Run under Linux.

Run: .venv/bin/python -m pytest tests/test_shim_gateway_routes.py -q
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-shim-gw-test-"))

from fastapi.testclient import TestClient

from ava_bridge import agent_runtime_server as shim
from ava_bridge import config as real_config
from ava_bridge.runtime.errors import GatewayError

TOKEN = "t" * 32
AUTH = {"X-Ava-Agent-Token": TOKEN}

GW_ROUTES = (("post", "/gateway/rpc"), ("get", "/gateway/status"),
             ("post", "/gateway/reconnect"))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(shim.app, base_url="http://localhost",
                                 raise_server_exceptions=False)
        p = mock.patch.object(real_config, "AGENT_TOKEN", TOKEN)
        p.start()
        self.addCleanup(p.stop)


class AuthTests(_Base):

    def _call(self, verb, path, headers=None):
        kw = {"headers": headers} if headers else {}
        if verb == "post":
            kw["json"] = {"method": "system.info"}
        return getattr(self.client, verb)(path, **kw)

    def test_every_gateway_route_401s_without_the_token(self):
        for verb, path in GW_ROUTES:
            with self.subTest(route=path):
                self.assertEqual(self._call(verb, path).status_code, 401, path)

    def test_a_wrong_token_is_also_401(self):
        for verb, path in GW_ROUTES:
            with self.subTest(route=path):
                r = self._call(verb, path,
                               {"X-Ava-Agent-Token": "not-the-token"})
                self.assertEqual(r.status_code, 401, path)

    def test_a_non_ascii_token_is_a_401_and_not_a_500(self):
        """`hmac.compare_digest` raises TypeError on a non-ASCII str, and raised
        inside BaseHTTPMiddleware that is a 500 handed to an UNAUTHENTICATED
        caller. `security.constant_time_equals` exists for this bug class.

        Sent as latin-1 BYTES because that is exactly what reaches the app: a
        header arrives as bytes and Starlette decodes it latin-1, producing the
        non-ASCII `str` that blows up compare_digest. Passing a Python `str`
        here would fail in httpx instead and never reach the middleware.
        """
        for verb, path in GW_ROUTES:
            with self.subTest(route=path):
                r = self._call(verb, path,
                               {b"X-Ava-Agent-Token": "tökén".encode("latin-1")})
                self.assertEqual(r.status_code, 401, path)

    def test_healthz_stays_public_and_advertises_the_proxy(self):
        body = self.client.get("/healthz").json()
        self.assertTrue(body["ok"])
        self.assertIn("gateway.proxy", body["capabilities"])

    def test_healthz_leaks_no_gateway_state(self):
        """/healthz is unauthenticated. Whether the gateway is up, what methods
        it offers and whether this host holds a token are all things an anonymous
        prober must not learn."""
        body = self.client.get("/healthz").json()
        for leak in ("phase", "methods", "token", "policy", "url", "why"):
            self.assertNotIn(leak, body, f"/healthz disclosed {leak!r}")


class RpcValidationTests(_Base):

    def _rpc(self, body):
        with mock.patch.object(shim._gw, "rpc") as rpc:
            r = self.client.post("/gateway/rpc", json=body, headers=AUTH)
            return r, rpc

    def test_a_malformed_method_is_a_400_and_never_reaches_the_gateway(self):
        for bad in ("", "../etc/passwd", "System.Info", "a" * 65,
                    "system.info;rm -rf /", "system..info"):
            with self.subTest(method=bad):
                r, rpc = self._rpc({"method": bad})
                self.assertEqual(r.status_code, 400, bad)
                rpc.assert_not_called()

    def test_a_non_object_body_is_a_400(self):
        with mock.patch.object(shim._gw, "rpc") as rpc:
            r = self.client.post("/gateway/rpc", json=[1, 2, 3], headers=AUTH)
        self.assertEqual(r.status_code, 400)
        rpc.assert_not_called()

    def test_non_object_params_are_refused_rather_than_coerced(self):
        """`params or {}` would turn a list, 0 or "" into {} and forward a
        malformed call as though nothing were wrong."""
        for bad in ([1, 2], "raw", 0, 7):
            with self.subTest(params=bad):
                r, rpc = self._rpc({"method": "system.info", "params": bad})
                self.assertEqual(r.status_code, 400)
                rpc.assert_not_called()

    def test_absent_params_are_allowed_and_become_an_empty_object(self):
        with mock.patch.object(shim._gw, "rpc", return_value={"v": 1}) as rpc:
            r = self.client.post("/gateway/rpc", json={"method": "system.info"},
                                 headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"ok": True, "payload": {"v": 1}})
        self.assertEqual(rpc.call_args.args[1], {})

    def test_the_timeout_is_clamped_on_this_side_too(self):
        """A caller holding the shared bearer could otherwise pin a worker on
        THIS host for as long as it liked. The bridge clamps to protect its own."""
        for given, want in ((9999, 120.0), (0.001, 1.0), (30, 30.0)):
            with self.subTest(timeout=given):
                with mock.patch.object(shim._gw, "rpc", return_value={}) as rpc:
                    self.client.post("/gateway/rpc",
                                     json={"method": "system.info",
                                           "timeout": given}, headers=AUTH)
                self.assertEqual(rpc.call_args.kwargs["timeout"], want)

    def test_a_non_numeric_timeout_is_a_400(self):
        r, rpc = self._rpc({"method": "system.info", "timeout": "soon"})
        self.assertEqual(r.status_code, 400)
        rpc.assert_not_called()


class DenyListTests(_Base):

    def _write(self, key, value=True):
        import json
        head, _, rest = key.partition(".")
        mid, _, leaf = rest.partition(".")
        raw = json.dumps({head: {mid: {leaf: value}}})
        with mock.patch.object(shim._gw, "rpc") as rpc:
            r = self.client.post("/gateway/rpc",
                                 json={"method": "config.set",
                                       "params": {"raw": raw}}, headers=AUTH)
        return r, rpc

    def test_asserting_a_denied_key_is_refused_before_the_gateway_sees_it(self):
        """This route is a SECOND, independent door to `config.set` for anything
        holding the shared bearer — on the keys that decide whether the gateway
        authenticates browsers at all. The bridge's deny-list cannot see it."""
        from ava_bridge.runtime import gateway_policy
        self.assertTrue(gateway_policy.DENIED_CONFIG_KEYS)
        for key in gateway_policy.DENIED_CONFIG_KEYS:
            with self.subTest(key=key):
                r, rpc = self._write(key, True)
                self.assertEqual(r.status_code, 200)
                body = r.json()
                self.assertFalse(body["ok"])
                self.assertEqual(body["error_code"], "gateway_key_refused")
                self.assertIn(key, body["message"])
                rpc.assert_not_called()

    def test_writing_the_safe_direction_still_passes(self):
        """Both keys are "dangerously disable" booleans, so setting them FALSE is
        the safe direction — and an owner round-tripping a whole config must not
        be blocked for leaving them alone."""
        from ava_bridge.runtime import gateway_policy
        for key in gateway_policy.DENIED_CONFIG_KEYS:
            with self.subTest(key=key):
                r, rpc = self._write(key, False)
                self.assertTrue(r.json()["ok"], r.text)
                rpc.assert_called_once()

    def test_the_shim_and_the_bridge_share_one_deny_list(self):
        """Two doors, one lock. A copy-pasted predicate is one that drifts, and
        this one is value-aware enough that a drifted copy would look right."""
        from ava_bridge import gateway_api
        from ava_bridge.runtime import gateway_policy
        self.assertIs(gateway_api._DENIED_CONFIG_KEYS,
                      gateway_policy.DENIED_CONFIG_KEYS)
        self.assertIs(gateway_api._CONFIG_WRITES, gateway_policy.CONFIG_WRITES)


class ErrorRelayTests(_Base):

    def test_a_gateway_error_comes_back_as_a_200_with_every_field(self):
        err = GatewayError("nope", "gateway_call_refused", gw_code="FORBIDDEN",
                           detail={"missingScope": "operator.admin"},
                           retryable=True, retry_after_ms=250)
        with mock.patch.object(shim._gw, "rpc", side_effect=err):
            r = self.client.post("/gateway/rpc", json={"method": "system.info"},
                                 headers=AUTH)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["ok"], False)
        self.assertEqual(body["error_code"], "gateway_call_refused")
        self.assertEqual(body["gw_code"], "FORBIDDEN")
        self.assertEqual(body["detail"], {"missingScope": "operator.admin"})
        self.assertEqual(body["message"], "nope")
        self.assertIs(body["retryable"], True)
        self.assertEqual(body["retry_after_ms"], 250)

    def test_an_unexpected_exception_is_still_a_coded_200(self):
        """Anything that escapes as a bodyless 500 reaches the browser as
        `GatewayCallError('HTTP 500')`, losing the code the fix links route on
        and the code the audit ledger classifies with."""
        with mock.patch.object(shim._gw, "rpc", side_effect=ValueError("boom")):
            r = self.client.post("/gateway/rpc", json={"method": "system.info"},
                                 headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error_code"], "gateway_rpc_failed")


class StatusTests(_Base):

    def test_status_relays_what_only_this_host_can_answer(self):
        """The bridge's copies of these describe the WRONG MACHINE: the gateway
        url, the off-loopback posture and the operator token all live here."""
        with mock.patch.object(shim._gw, "status",
                               return_value={"phase": "ready", "methods": ["a"]}):
            body = self.client.get("/gateway/status", headers=AUTH).json()
        self.assertEqual(body["phase"], "ready")
        self.assertIn("token", body)
        self.assertEqual(sorted(body["token"]), ["configured", "source"])
        self.assertIn("allow_remote", body)
        self.assertIn("url", body)

    def test_reconnect_answers_ok_even_when_the_nudge_fails(self):
        """The owner asked to try again; "we have asked it to" is the honest
        report. Whether the redial succeeds is what the phase is for."""
        with mock.patch.object(shim._gw, "reconnect", side_effect=OSError("no")):
            r = self.client.post("/gateway/reconnect", headers=AUTH)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])


if __name__ == "__main__":
    unittest.main()
