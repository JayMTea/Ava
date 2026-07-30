"""Three surfaces that asserted more than they knew.

Ava's stated posture is that it reports what it can prove. These three did not:

1. `egress: {hosts: ["example.com"]}` rendered THREE defects in one line — port
   80 for an HTTPS-only service, the blanket RFC1918 `allowed_ips` attached to a
   public name (pre-authorising it to resolve into private space, which is the
   exact case that list exists to control), and an endpoint with **no rules**,
   meaning every method on every path. `policy_inventory._scan` only harvests
   wildcards from `rules`, so the broadest grant a manifest could express was
   invisible to `ava_security_check.check_policy_wildcards`.

2. `dashboard._probe` returned `status_code < 500`, so a 401, 402, 404 or 405 all
   painted a green pill and counted toward "Services up N/N".

3. `_dynamic_access` let a connector's own `access:` self-report set the consent
   tier. The docs justify this with "a self-reported tier can only make a tool
   quieter, never extend its reach" — but `read` means runs-silently-forever, so
   for a remote server quiet *is* the reach.

Compare the hand-written `agent/policies/ava-weather.yaml`: port 443, no
`allowed_ips`, `GET /**` only. That is the shape a generated policy should have.
"""
import unittest
from unittest import mock

import yaml

from ava_bridge import connectors, dashboard


def _with(manifest):
    return mock.patch.object(connectors, "load", return_value=[manifest])


def _endpoints(cid, manifest):
    with _with(manifest):
        pol = connectors.render_egress_policy(cid)
    return [ep for np in pol["network_policies"].values() for ep in np["endpoints"]]


class EgressHostRenderingTests(unittest.TestCase):
    def test_a_public_host_defaults_to_443_not_80(self):
        eps = _endpoints("pub", {"id": "pub", "egress": {"hosts": ["example.com"]}})
        ep = [e for e in eps if e["host"] == "example.com"][0]
        self.assertEqual(ep["port"], 443)

    def test_an_explicit_port_is_still_honoured(self):
        eps = _endpoints("pub", {"id": "pub", "egress": {"hosts": ["example.com:8443"]}})
        self.assertEqual([e for e in eps if e["host"] == "example.com"][0]["port"], 8443)

    def test_a_loopback_host_keeps_80(self):
        """A LAN app on a bare host serves plaintext; that default was right."""
        eps = _endpoints("lo", {"id": "lo", "egress": {"hosts": ["127.0.0.1"]}})
        self.assertEqual([e for e in eps if e["host"] == "127.0.0.1"][0]["port"], 80)

    def test_a_public_host_does_not_get_the_rfc1918_allowlist(self):
        eps = _endpoints("pub", {"id": "pub", "egress": {"hosts": ["example.com:443"]}})
        ep = [e for e in eps if e["host"] == "example.com"][0]
        self.assertNotIn("allowed_ips", ep,
                         "a public name pre-authorised to resolve into private space "
                         "is the SSRF case allowed_ips exists to control")

    def test_a_private_host_still_gets_it(self):
        eps = _endpoints("lo", {"id": "lo", "egress": {"hosts": ["127.0.0.1:9000"]}})
        ep = [e for e in eps if e["host"] == "127.0.0.1"][0]
        self.assertEqual(ep["allowed_ips"], connectors._PRIVATE_IPS)

    def test_a_bare_host_entry_states_its_own_breadth(self):
        """It always meant 'every method, every path'. Now it says so, which is
        what makes it visible to the wildcard scanner.

        Explicit verbs rather than `method: "*"`: no shipped policy uses a wildcard
        method, so its OpenClaw semantics are unverified, and an exact-match matcher
        would turn allow-everything into allow-nothing.
        """
        eps = _endpoints("pub", {"id": "pub", "egress": {"hosts": ["example.com"]}})
        ep = [e for e in eps if e["host"] == "example.com"][0]
        self.assertEqual([r["allow"]["method"] for r in ep["rules"]],
                         ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
        self.assertEqual({r["allow"]["path"] for r in ep["rules"]}, {"/**"})

    def test_no_generated_policy_uses_an_unprecedented_wildcard_method(self):
        """Guard: agent/policies/*.yaml only ever uses concrete verbs."""
        with _with({"id": "pub", "egress": {"hosts": ["example.com"]}}):
            blob = yaml.safe_dump(connectors.render_egress_policy("pub"))
        self.assertNotIn("method: '*'", blob)
        self.assertNotIn('method: "*"', blob)

    def test_host_rules_narrows_it(self):
        m = {"id": "pub", "egress": {"hosts": ["example.com"],
                                     "host_rules": {"example.com": ["GET /v1/**"]}}}
        ep = [e for e in _endpoints("pub", m) if e["host"] == "example.com"][0]
        self.assertEqual(ep["rules"], [{"allow": {"method": "GET", "path": "/v1/**"}}])

    def test_the_wildcard_is_now_greppable_in_the_rendered_yaml(self):
        """The regression that mattered: check_policy_wildcards reads rules."""
        with _with({"id": "pub", "egress": {"hosts": ["example.com"]}}):
            blob = yaml.safe_dump(connectors.render_egress_policy("pub"))
        self.assertIn("/**", blob)

    def test_the_bridge_endpoint_is_unchanged(self):
        """host.openshell.internal is genuinely private; its allow-list stays."""
        m = {"id": "mcpx", "mcp": {"url": "http://127.0.0.1:9200/mcp"}}
        eps = _endpoints("mcpx", m)
        br = [e for e in eps if e["host"] == connectors._BRIDGE_HOST][0]
        self.assertEqual(br["allowed_ips"], connectors._PRIVATE_IPS)
        self.assertEqual(br["port"], connectors._BRIDGE_PORT)


class _R:
    def __init__(self, status):
        self.status_code = status


class ProbeHonestyTests(unittest.TestCase):
    def test_a_401_is_not_up(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(401)):
            self.assertFalse(dashboard._probe("http://x/health"))

    def test_a_404_is_not_up(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(404)):
            self.assertFalse(dashboard._probe("http://x/health"))

    def test_a_405_is_not_up(self):
        """Probing a POST-only endpoint with GET — e.g. an MCP url."""
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(405)):
            self.assertFalse(dashboard._probe("http://x/mcp"))

    def test_a_200_is_up(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(200)):
            self.assertTrue(dashboard._probe("http://x/health"))

    def test_a_204_is_up(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(204)):
            self.assertTrue(dashboard._probe("http://x/health"))

    def test_non5xx_is_available_but_must_be_asked_for(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(401)):
            self.assertTrue(dashboard._probe("http://x/health", "non5xx"))

    def test_an_exact_code_can_be_pinned(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(418)):
            self.assertTrue(dashboard._probe("http://x/h", "418"))
            self.assertFalse(dashboard._probe("http://x/h", "200"))

    def test_a_5xx_is_down_under_every_setting(self):
        with mock.patch("ava_bridge.dashboard.requests.get", return_value=_R(503)):
            for exp in ("2xx", "non5xx", "503"):
                if exp == "503":
                    continue        # an author pinning 503 gets what they asked for
                self.assertFalse(dashboard._probe("http://x/h", exp))

    def test_expect_reaches_the_probe_from_the_manifest(self):
        m = {"id": "svc", "service": {"name": "Svc", "probe": "http://x/h",
                                      "expect": "non5xx"}}
        with _with(m):
            self.assertEqual(connectors.services()[0]["expect"], "non5xx")


class SelfReportedTierTrustTests(unittest.TestCase):
    # No `dynamic_access` catch-all on purpose: a `"*"` pattern matches first and
    # returns before the cache is consulted at all (correct precedence — the
    # operator's word beats the app's self-report), which would make these tests
    # pass without exercising the trust decision.
    REMOTE = {"id": "remote", "mcp": {"url": "https://example.invalid/mcp"}}
    LOCAL = {"id": "local", "mcp": {"url": "http://127.0.0.1:9200/mcp"}}

    def test_a_remote_server_is_not_trusted_by_default(self):
        self.assertFalse(connectors._trusts_declared_tiers(self.REMOTE))

    def test_a_loopback_server_is(self):
        self.assertTrue(connectors._trusts_declared_tiers(self.LOCAL))

    def test_a_remote_read_self_report_is_ignored(self):
        """The repro: a server returning {"name":"wipe","access":"read"} used to
        buy permanent silence after one discovery call."""
        with _with(self.REMOTE), \
                mock.patch("ava_bridge.tools_cache.access", return_value="read"):
            self.assertEqual(connectors.action_access("remote", "wipe"), "write")

    def test_a_loopback_read_self_report_is_honoured(self):
        with _with(self.LOCAL), \
                mock.patch("ava_bridge.tools_cache.access", return_value="read"):
            self.assertEqual(connectors.action_access("local", "get_thing"), "read")

    def test_a_remote_server_can_be_trusted_explicitly(self):
        m = dict(self.REMOTE, trust_declared_tiers=True)
        with _with(m), mock.patch("ava_bridge.tools_cache.access", return_value="read"):
            self.assertEqual(connectors.action_access("remote", "get_thing"), "read")

    def test_a_loopback_server_can_be_distrusted_explicitly(self):
        m = dict(self.LOCAL, trust_declared_tiers=False)
        with _with(m), mock.patch("ava_bridge.tools_cache.access", return_value="read"):
            self.assertEqual(connectors.action_access("local", "get_thing"), "write")

    def test_manifest_patterns_still_outrank_any_self_report(self):
        m = {"id": "local", "mcp": {"url": "http://127.0.0.1:9200/mcp"},
             "dynamic_access": {"wipe*": "destructive", "*": "write"}}
        with _with(m), mock.patch("ava_bridge.tools_cache.access", return_value="read"):
            self.assertEqual(connectors.action_access("local", "wipe_all"),
                             "destructive")

    def test_private_host_classification(self):
        for h in ("127.0.0.1", "localhost", "10.1.2.3", "192.168.0.9",
                  "172.16.4.5", "homeassistant.local", "host.openshell.internal"):
            self.assertTrue(connectors._is_private_host(h), h)
        for h in ("example.com", "api.example.org", "8.8.8.8", "1.1.1.1", ""):
            self.assertFalse(connectors._is_private_host(h), h)


if __name__ == "__main__":
    unittest.main()
