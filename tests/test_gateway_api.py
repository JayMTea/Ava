"""The gateway passthrough: what it forwards, what it refuses, and what it says.

One route carries ~200 methods, so validation at the door IS the surface. The
alternative — two hundred typed routes — was rejected because
`tests/_route_table.json`'s value is that it stays readable and because
`hello-ok.features.methods` is the SSOT for what exists, which a curated Python
list would silently drift from.

Authorization here is deliberately just Ava's session cookie, with `operator.admin`
behind it. That is the owner's explicit choice and it makes these tests the
place where the remaining limits are actually written down.

House style: stdlib unittest over a real app via TestClient, no network.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-gwapi-test-"))

from fastapi.testclient import TestClient

from ava_bridge import auth, config, gateway_api, runtime

_LOCAL = {"host": "localhost"}


def _client() -> TestClient:
    import phone_bridge
    c = TestClient(phone_bridge.app, base_url="http://localhost")
    c.cookies.set(config.COOKIE_NAME, auth._make_token())
    return c


class FakeClient:
    """Stands in for the gateway client; records what reached it."""

    def __init__(self, payload=None, raise_=None):
        self.calls: list[dict] = []
        self._payload = payload if payload is not None else {"ok": 1}
        self._raise = raise_

    async def arpc(self, method, params, *, timeout=None, idempotency_key=None):
        self.calls.append({"method": method, "params": params,
                           "timeout": timeout, "key": idempotency_key})
        if self._raise:
            raise self._raise
        return self._payload

    def status(self):
        return {"phase": "ready", "protocol": 4, "methods": ["sessions.list"],
                "why": "", "url_class": "loopback", "since": 0, "policy": {},
                "last_seq": None}

    def reconnect(self):
        self.calls.append({"method": "__reconnect__"})


class _FakeRuntime:
    """A stand-in runtime that answers the SEAM, not a private attribute.

    It used to expose only `_client`, which worked while the routes reached in
    with `getattr(rt, "_client", None)`. That is exactly the punch-through the
    seam replaced — and this fake going stale is the reminder that a duck-typed
    double drifts from the contract it is standing in for.
    """

    name = "fake_gw"

    def __init__(self, client):
        self._client = client

    def control_plane(self):
        return self._client


def _with_gateway(client: FakeClient):
    return mock.patch.object(runtime, "configured",
                             return_value=_FakeRuntime(client))


class AuthTests(unittest.TestCase):

    def test_the_passthrough_is_behind_the_session_cookie(self):
        import phone_bridge
        anon = TestClient(phone_bridge.app, base_url="http://localhost")
        r = anon.post("/api/gateway/rpc", json={"method": "sessions.list"},
                      headers=_LOCAL)
        self.assertEqual(r.status_code, 401)

    def test_status_is_gated_too(self):
        import phone_bridge
        anon = TestClient(phone_bridge.app, base_url="http://localhost")
        self.assertEqual(
            anon.get("/api/gateway/status", headers=_LOCAL).status_code, 401)


class ValidationTests(unittest.TestCase):

    def setUp(self):
        self.c = _client()
        gateway_api._buckets.clear()

    def test_a_path_ish_method_name_is_refused_before_anything_sees_it(self):
        for bad in ("../../etc/passwd", "Sessions.List", "sessions..list",
                    "sessions.list;rm -rf /", "", "a" * 80):
            with self.subTest(method=bad):
                r = self.c.post("/api/gateway/rpc", json={"method": bad},
                                headers=_LOCAL)
                self.assertEqual(r.status_code, 400, f"{bad!r} was accepted")

    def test_params_that_are_not_an_object_are_refused_not_coerced(self):
        """`body.get("params") or {}` would turn a list into an empty object and
        forward the call as though nothing were wrong."""
        fake = FakeClient()
        with _with_gateway(fake):
            for bad in ([], [1, 2], "text", 7):
                with self.subTest(params=bad):
                    r = self.c.post("/api/gateway/rpc",
                                    json={"method": "sessions.list",
                                          "params": bad}, headers=_LOCAL)
                    self.assertEqual(r.status_code, 400)
        self.assertEqual(fake.calls, [], "a malformed call reached the gateway")

    def test_absent_params_are_an_empty_object(self):
        fake = FakeClient()
        with _with_gateway(fake):
            r = self.c.post("/api/gateway/rpc", json={"method": "sessions.list"},
                            headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(fake.calls[0]["params"], {})

    def test_a_caller_supplied_timeout_is_clamped(self):
        """The lesson `_EXEC_TIMEOUT_MAX` already learned: an unbounded
        caller-supplied timeout pins a worker for as long as it likes."""
        fake = FakeClient()
        with _with_gateway(fake):
            self.c.post("/api/gateway/rpc",
                        json={"method": "sessions.list", "timeout": 99999},
                        headers=_LOCAL)
            self.c.post("/api/gateway/rpc",
                        json={"method": "sessions.list", "timeout": 0},
                        headers=_LOCAL)
        self.assertLessEqual(fake.calls[0]["timeout"], gateway_api._TIMEOUT_MAX)
        self.assertGreaterEqual(fake.calls[1]["timeout"], gateway_api._TIMEOUT_MIN)

    def test_a_non_object_body_is_refused(self):
        r = self.c.post("/api/gateway/rpc", json=["sessions.list"],
                        headers=_LOCAL)
        self.assertEqual(r.status_code, 400)


class DenyListTests(unittest.TestCase):
    """The single exception to "no extra gate", and it is deliberate.

    These keys govern whether the gateway authenticates browsers at all. A UI
    bug that writes one turns a transient mistake into a permanent posture
    change that survives every restart.
    """

    def setUp(self):
        self.c = _client()
        gateway_api._buckets.clear()

    @staticmethod
    def _raw(**nested):
        """The live config-write shape: the whole doc as a `raw` JSON string."""
        return {"raw": json.dumps(nested)}

    def test_the_device_auth_keys_are_not_writable_from_any_config_method(self):
        # All three write methods take {raw:<whole json>} and must be gated;
        # the key sits NESTED, which the old repr()-substring check missed.
        fake = FakeClient()
        with _with_gateway(fake):
            for method in gateway_api._CONFIG_WRITES:
                for key in gateway_api._DENIED_CONFIG_KEYS:
                    with self.subTest(method=method, key=key):
                        head, _, rest = key.partition(".")
                        doc = {head: {rest.split(".")[0]: {rest.split(".")[1]: True}}}
                        r = self.c.post("/api/gateway/rpc",
                                        json={"method": method,
                                              "params": {"raw": json.dumps(doc)}},
                                        headers=_LOCAL)
                        self.assertEqual(r.json()["error_code"], "gateway_key_refused")
        self.assertEqual(fake.calls, [], "a refused write still reached the gateway")

    def test_a_flattened_dotted_key_is_also_refused(self):
        with _with_gateway(FakeClient()):
            r = self.c.post("/api/gateway/rpc",
                            json={"method": "config.set",
                                  "params": self._raw(**{
                                      gateway_api._DENIED_CONFIG_KEYS[0]: True})},
                            headers=_LOCAL)
        self.assertEqual(r.json()["error_code"], "gateway_key_refused")

    def test_the_refusal_says_where_the_capability_went(self):
        """Silently missing is worse than refused. The owner has to learn that
        the setting exists and how to change it deliberately."""
        head, _, rest = gateway_api._DENIED_CONFIG_KEYS[0].partition(".")
        doc = {head: {rest.split(".")[0]: {rest.split(".")[1]: True}}}
        with _with_gateway(FakeClient()):
            r = self.c.post("/api/gateway/rpc",
                            json={"method": "config.set",
                                  "params": {"raw": json.dumps(doc)}},
                            headers=_LOCAL)
        self.assertIn("nemoclaw", r.json()["message"])

    def test_a_write_that_leaves_the_denied_keys_untouched_passes(self):
        """A deny-list of two, not a ban on config writes. A whole-config
        round-trip that merely MENTIONS the keys in an unrelated string (e.g. a
        doctor-suppression rule — the exact false-positive on the real box) must
        pass, because it does not set them."""
        fake = FakeClient()
        doc = {"theme": "dark",
               "doctor": {"suppress": ["gateway.controlUi.allowInsecureAuth=true"]}}
        with _with_gateway(fake):
            r = self.c.post("/api/gateway/rpc",
                            json={"method": "config.set",
                                  "params": {"raw": json.dumps(doc)}},
                            headers=_LOCAL)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(fake.calls[0]["method"], "config.set")

    def test_the_denied_key_set_to_false_passes(self):
        """Value-aware: writing the dangerous flag OFF is the safe direction and
        must not be refused, or a whole-config round-trip on a box that once had
        it set could never turn it back off."""
        fake = FakeClient()
        head, _, rest = gateway_api._DENIED_CONFIG_KEYS[0].partition(".")
        doc = {head: {rest.split(".")[0]: {rest.split(".")[1]: False}}}
        with _with_gateway(fake):
            r = self.c.post("/api/gateway/rpc",
                            json={"method": "config.set",
                                  "params": {"raw": json.dumps(doc)}},
                            headers=_LOCAL)
        self.assertTrue(r.json()["ok"])

    def test_a_non_parseable_raw_reaches_the_gateway_not_our_refusal(self):
        """We do not own config validation — an unparseable blob is the
        gateway's to reject, so we forward it rather than inventing a refusal."""
        fake = FakeClient()
        with _with_gateway(fake):
            r = self.c.post("/api/gateway/rpc",
                            json={"method": "config.set",
                                  "params": {"raw": "{not json"}},
                            headers=_LOCAL)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(fake.calls[0]["method"], "config.set")


class FailureShapeTests(unittest.TestCase):

    def setUp(self):
        self.c = _client()
        gateway_api._buckets.clear()

    def test_a_gateway_failure_is_a_200_body_not_a_status_code(self):
        """`lib/api.ts` maps a bodyless 404 to `bridge_outdated`, and a caller
        that cannot see the body cannot show the owner the fix. Same reasoning
        as internal._told()."""
        from ava_bridge.runtime.errors import GatewayError
        err = GatewayError("nope", "agent_scope_denied",
                           gw_code="FORBIDDEN",
                           detail={"missingScope": "operator.admin"})
        with _with_gateway(FakeClient(raise_=err)):
            r = self.c.post("/api/gateway/rpc", json={"method": "sessions.list"},
                            headers=_LOCAL)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIs(body["ok"], False)
        self.assertEqual(body["error_code"], "agent_scope_denied")
        self.assertEqual(body["gw_code"], "FORBIDDEN")
        self.assertEqual(body["detail"]["missingScope"], "operator.admin")

    def test_a_runtime_with_no_gateway_says_which_setting_selects_one(self):
        r = _client().post("/api/gateway/rpc", json={"method": "sessions.list"},
                           headers=_LOCAL)
        self.assertEqual(r.json()["error_code"], "agent_no_gateway")
        self.assertIn("openclaw_gw", r.json()["message"])


class RateLimitTests(unittest.TestCase):

    def test_a_fan_out_is_slowed_rather_than_closing_the_socket(self):
        """Not a security control. The gateway's own buffer cap closes the
        connection when exceeded, and that would take the turn path down with
        the panel that caused it."""
        c = _client()
        gateway_api._buckets.clear()
        with _with_gateway(FakeClient()):
            codes = [c.post("/api/gateway/rpc",
                            json={"method": "sessions.list"},
                            headers=_LOCAL).json().get("error_code")
                     for _ in range(int(gateway_api._BURST) + 15)]
        self.assertIn("gateway_rate_limited", codes)
        self.assertIsNone(codes[0], "the first call must not be limited")


class AuditTests(unittest.TestCase):

    def setUp(self):
        self.c = _client()
        gateway_api._buckets.clear()

    def test_a_write_is_recorded_and_a_read_is_not(self):
        with _with_gateway(FakeClient()), \
             mock.patch.object(gateway_api.audit, "record") as rec:
            self.c.post("/api/gateway/rpc", json={"method": "sessions.list"},
                        headers=_LOCAL)
            self.assertEqual(rec.call_count, 0, "reads would flood the ledger")
            self.c.post("/api/gateway/rpc", json={"method": "plugins.install",
                                                  "params": {"id": "x"}},
                        headers=_LOCAL)
            self.assertEqual(rec.call_args[0][0], "gateway_rpc")

    def test_the_ledger_records_the_method_and_never_the_parameters(self):
        """`secrets.store.*` and `config.set` carry credentials, and the ledger
        is a file the owner reads. With a full-admin passthrough this is the ONLY
        record of what was done, which is what makes the omission a discipline."""
        with _with_gateway(FakeClient()), \
             mock.patch.object(gateway_api.audit, "record") as rec:
            self.c.post("/api/gateway/rpc",
                        json={"method": "secrets.store.set",
                              "params": {"name": "K", "value": "hunter2"}},
                        headers=_LOCAL)
        blob = repr(rec.call_args)
        self.assertIn("secrets.store.set", blob)
        self.assertNotIn("hunter2", blob, "a secret reached the audit ledger")

    def test_a_refusal_is_recorded_too(self):
        head, _, rest = gateway_api._DENIED_CONFIG_KEYS[0].partition(".")
        doc = {head: {rest.split(".")[0]: {rest.split(".")[1]: True}}}
        with _with_gateway(FakeClient()), \
             mock.patch.object(gateway_api.audit, "record") as rec:
            self.c.post("/api/gateway/rpc",
                        json={"method": "config.set",
                              "params": {"raw": json.dumps(doc)}},
                        headers=_LOCAL)
        self.assertEqual(rec.call_args[0][0], "gateway_denied")


class StatusTests(unittest.TestCase):

    def test_status_never_returns_the_token_itself(self):
        with _with_gateway(FakeClient()):
            body = _client().get("/api/gateway/status", headers=_LOCAL).json()
        self.assertIn("token", body)
        self.assertEqual(sorted(body["token"]), ["configured", "source"])
        self.assertNotIn("value", body["token"])

    def test_status_carries_the_session_prefix(self):
        """`chat_session(cid)` keys gateway sessions as f"{OC_SESSION}-{cid}"
        and OC_SESSION is env-configurable (AVA_OC_SESSION) — so the frontend
        must read the prefix from here to map a gateway session key like
        'ava-phone-<cid>' back to a chat id, never hardcode it."""
        with _with_gateway(FakeClient()):
            body = _client().get("/api/gateway/status", headers=_LOCAL).json()
        self.assertEqual(body["session_prefix"], config.OC_SESSION)

    def test_status_surfaces_the_same_origin_exposure(self):
        """With apps.origin unset an embedded app's JS is same-origin with the
        shell and carries the session cookie — so it can reach this passthrough.
        The Agent tab has to be able to say so."""
        with _with_gateway(FakeClient()):
            body = _client().get("/api/gateway/status", headers=_LOCAL).json()
        self.assertIn("apps_origin", body)


if __name__ == "__main__":
    unittest.main()


class AuditClassificationTests(unittest.TestCase):
    """A refusal and a failure are different news.

    `gateway_denied` renders as "Agent call refused". Every GatewayError was
    recorded as one, so a dropped socket, a timeout, or a protocol mismatch all
    appeared in the owner's ledger as a refusal — sending them to look for a
    permission problem that does not exist, on a day when the real answer was
    "the gateway is not running".
    """

    def _kind(self, code):
        from ava_bridge import gateway_api
        from ava_bridge.runtime.errors import GatewayError
        seen = {}

        def fake(kind, **fields):
            seen["kind"] = kind
            seen["fields"] = fields

        real = gateway_api.audit.record
        gateway_api.audit.record = fake
        try:
            gateway_api._audit_error("x.y", GatewayError("boom", code))
        finally:
            gateway_api.audit.record = real
        return seen["kind"]

    def test_a_policy_refusal_is_recorded_as_a_refusal(self):
        for code in ("agent_scope_denied", "agent_token_rejected",
                     "gateway_unsupported_method"):
            self.assertEqual(self._kind(code), "gateway_denied", code)

    def test_a_transport_failure_is_not_a_refusal(self):
        """The bug: these were all "Agent call refused"."""
        for code in ("gateway_timeout", "gateway_connect", "gateway_disconnect",
                     "agent_down", "agent_protocol_mismatch",
                     "gateway_rpc_failed"):
            self.assertEqual(self._kind(code), "gateway_failed", code)

    def test_an_unknown_code_reads_as_a_failure(self):
        """The honest default for "we do not know what happened" — calling it a
        refusal asserts that something refused, which we cannot know."""
        self.assertEqual(self._kind("something_new"), "gateway_failed")
        self.assertEqual(self._kind(""), "gateway_failed")

    def test_the_deny_list_still_records_a_refusal(self):
        """Ava refusing on the owner's behalf IS a refusal — that one is real."""
        from ava_bridge import gateway_api
        src = open(gateway_api.__file__, encoding="utf-8").read()
        self.assertIn('audit.record("gateway_denied", method=method, '
                      'reason="device_auth_key")', src)


class SessionIdentityTests(unittest.TestCase):
    """The two facts a client needs to name a session the same way the gateway
    does — and both were once unavailable to it."""

    def test_the_session_prefix_is_a_config_key_not_env_only(self):
        """Every other agent.* setting is an ava.yaml key. This one was a bare
        `os.environ` read, so a fork could not set it in config at all — and it
        is not cosmetic: it namespaces every session key Ava creates, so two
        installs sharing one gateway with the same prefix read each other's
        sessions."""
        from ava_bridge import config as _cfg
        src = open(_cfg.__file__, encoding="utf-8").read()
        self.assertIn('settings.get("agent.session_prefix"', src)
        self.assertNotIn('os.environ.get("AVA_OC_SESSION"', src)

    def test_the_template_documents_it(self):
        """config.example.yaml is the only place a knob is discoverable."""
        import os as _os
        root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        body = open(_os.path.join(root, "config.example.yaml"),
                    encoding="utf-8").read()
        self.assertIn("session_prefix:", body)
        self.assertIn("AVA_OC_SESSION", body)

    def test_status_reports_the_agent_id(self):
        """The gateway echoes session keys back PREFIXED with the agent id —
        send 'ava-phone-x' and its frames say 'agent:main:ava-phone-x'
        (captured live). A client that does not know the id cannot tell the two
        forms name one session."""
        with _with_gateway(FakeClient()):
            body = _client().get("/api/gateway/status", headers=_LOCAL).json()
        self.assertEqual(body["agent_id"], config.OC_AGENT)
