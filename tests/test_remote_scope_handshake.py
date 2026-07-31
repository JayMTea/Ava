"""A scoped request must never be silently widened into a full one.

`agent_runtime_server.provision` reads its body with `.get()` and ignores unknown
keys. So before this handshake existed, a bridge that asked an older agent-runtime
container to apply *only* the persona would get a FULL provision — every MCP
server re-pushed, every skill reinstalled, ten minutes — and a `{"ok": true}` back.
The UI would then report "persona applied", which is true, next to a pile of work
nobody asked for.

The fix is a capability list on /healthz. Two properties matter and are pinned
here: it costs no extra round trip (available() already polls /healthz every 15s),
and it FAILS CLOSED by construction — an old shim has no `capabilities` key, so
`.get("capabilities", [])` is empty and the scope is refused, with no version
parsing involved. Version parsing would not have worked anyway: `ava_bridge.
version` reports `0.0.0+dev` on a checkout, so any semver comparison is
meaningless.

House style: stdlib unittest, in-process fake shim, no network.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ava_bridge import provision
from ava_bridge.runtime.remote import RemoteRuntime


class _Resp:
    def __init__(self, payload, ok=True):
        self._payload = payload
        self.ok = ok

    def json(self):
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("http error")


def _runtime_with_caps(caps):
    """A RemoteRuntime whose /healthz answers with (or without) capabilities."""
    rt = RemoteRuntime()
    rt._avail_cache = {"ts": 0.0, "ok": None, "caps": []}
    body = {"ok": True, "ready": True}
    if caps is not None:
        body["capabilities"] = caps
    return rt, body


class HandshakeTests(unittest.TestCase):

    def test_an_old_shim_without_capabilities_refuses_a_scoped_apply(self):
        rt, body = _runtime_with_caps(None)
        with mock.patch("ava_bridge.runtime.remote.requests.get",
                        return_value=_Resp(body)), \
             mock.patch("ava_bridge.runtime.remote.requests.post") as post, \
             mock.patch.object(__import__("ava_bridge.config", fromlist=["config"]),
                               "AGENT_ENABLED", True):
            res = rt.provision(scope="persona")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "remote_scope_unsupported")
        post.assert_not_called()  # the whole point: do not ask at all

    def test_a_capable_shim_gets_the_scope_in_the_body(self):
        rt, body = _runtime_with_caps(["provision.scope"])
        with mock.patch("ava_bridge.runtime.remote.requests.get",
                        return_value=_Resp(body)), \
             mock.patch("ava_bridge.runtime.remote.requests.post",
                        return_value=_Resp({"ok": True, "steps": []})) as post, \
             mock.patch.object(__import__("ava_bridge.config", fromlist=["config"]),
                               "AGENT_ENABLED", True):
            res = rt.provision(scope="persona")
        self.assertTrue(res["ok"])
        self.assertEqual(post.call_args.kwargs["json"]["scope"], "persona")

    def test_scope_all_works_against_an_old_shim(self):
        """The handshake must not break the case that always worked."""
        rt, body = _runtime_with_caps(None)
        with mock.patch("ava_bridge.runtime.remote.requests.get",
                        return_value=_Resp(body)), \
             mock.patch("ava_bridge.runtime.remote.requests.post",
                        return_value=_Resp({"ok": True, "steps": []})) as post, \
             mock.patch.object(__import__("ava_bridge.config", fromlist=["config"]),
                               "AGENT_ENABLED", True):
            res = rt.provision(scope="all")
        self.assertTrue(res["ok"])
        post.assert_called_once()

    def test_a_malformed_capability_list_fails_closed(self):
        rt, body = _runtime_with_caps(None)
        body["capabilities"] = "provision.scope"  # a string, not a list
        with mock.patch("ava_bridge.runtime.remote.requests.get",
                        return_value=_Resp(body)), \
             mock.patch("ava_bridge.runtime.remote.requests.post") as post, \
             mock.patch.object(__import__("ava_bridge.config", fromlist=["config"]),
                               "AGENT_ENABLED", True):
            res = rt.provision(scope="skills")
        self.assertFalse(res["ok"])
        post.assert_not_called()


class ShimTests(unittest.TestCase):

    def test_the_shim_advertises_and_rejects_consistently(self):
        from fastapi.testclient import TestClient
        from ava_bridge import agent_runtime_server as shim

        with mock.patch.object(shim._rt, "available", return_value=True):
            client = TestClient(shim.app)
            body = client.get("/healthz").json()
        self.assertIn("provision.scope", body["capabilities"])

    def test_the_shim_400s_an_unknown_scope_instead_of_widening_it(self):
        from fastapi.testclient import TestClient
        from ava_bridge import agent_runtime_server as shim
        from ava_bridge import config as real_config

        # A REAL token, and send it. Accepting 401 here would have made this test
        # pass without ever reaching the route — which is exactly what it did on
        # the first cut, because the auth middleware rejects an empty token.
        token = "t" * 32
        with mock.patch.object(real_config, "AGENT_TOKEN", token), \
             mock.patch.object(shim._rt, "provision") as prov:
            client = TestClient(shim.app, raise_server_exceptions=False)
            r = client.post("/provision", json={"scope": "everything"},
                            headers={"x-ava-agent-token": token})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["error_code"], "unknown_scope")
        prov.assert_not_called()

    def test_the_shim_passes_a_known_scope_straight_through(self):
        from fastapi.testclient import TestClient
        from ava_bridge import agent_runtime_server as shim
        from ava_bridge import config as real_config

        token = "t" * 32
        with mock.patch.object(real_config, "AGENT_TOKEN", token), \
             mock.patch.object(shim._rt, "provision",
                               return_value={"ok": True, "steps": []}) as prov:
            client = TestClient(shim.app, raise_server_exceptions=False)
            r = client.post("/provision", json={"scope": "skills"},
                            headers={"x-ava-agent-token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(prov.call_args.kwargs["scope"], "skills")

    def test_every_scope_the_bridge_can_send_is_one_the_shim_accepts(self):
        """Guards the class of bug where one side grows a name the other drops."""
        from ava_bridge import agent_runtime_server as shim
        self.assertEqual(set(shim.provision_mod.ALL_SCOPES), set(provision.ALL_SCOPES))


class DirectRuntimeTests(unittest.TestCase):

    def test_direct_stops_claiming_a_successful_provision(self):
        """It has no sandbox, so nothing the owner edited went anywhere. The
        inherited base-class `{"ok": True, "detail": "no provisioning needed"}`
        was technically true and practically a lie."""
        from ava_bridge.runtime.direct import DirectRuntime
        res = DirectRuntime().provision(scope="persona")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "direct_runtime_has_no_sandbox")


if __name__ == "__main__":
    unittest.main()
