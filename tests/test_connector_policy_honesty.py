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
import os
import unittest
from pathlib import Path
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

    def test_a_private_host_gets_no_allowed_ips_either(self):
        """Changed 2026-08: NemoClaw's policy-add permits `allowed_ips` only on
        the bridge endpoint and rejects the WHOLE preset when a user endpoint
        carries it — so the old private-host branch rendered policies that
        could never apply (found wiring an owner app's 127.0.0.1 sidecar).
        The endpoint's own host:port is the narrow grant; private-space rules
        are the sandbox guard's to enforce."""
        eps = _endpoints("lo", {"id": "lo", "egress": {"hosts": ["127.0.0.1:9000"]}})
        ep = [e for e in eps if e["host"] == "127.0.0.1"][0]
        self.assertNotIn("allowed_ips", ep,
                         "policy-add rejects allowed_ips on user endpoints — "
                         "a policy carrying it never applies")

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
        # Resolved per call, not frozen at import:  and
        # install.sh's own sed both resolve it live, and a module constant here
        # meant the rendered policy allowed one port while the rewrite expected
        # another — with nothing in the sandbox's refusal naming a port.
        self.assertEqual(br["port"], connectors._bridge_port())


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


class UnresolvedPlaceholderTests(unittest.TestCase):
    """`ava device new` writes `base: "http://127.0.0.1:PORT"` and an unedited
    one validated CLEAN: the connector loaded, `load_errors()` was empty, and a
    full egress policy rendered against a URL nothing can dial. The owner's only
    symptom was a connection error at discovery time naming neither the manifest
    nor the placeholder.
    """

    def _errs(self, m):
        out = []
        connectors._validate(m, "t.yaml", out)
        return [e["error"] for e in out]

    def test_the_unedited_template_port_is_an_error(self):
        m = {"id": "d", "actions": {"discover": {"base": "http://127.0.0.1:PORT",
                                                 "list": "/tools", "call": "/call"}}}
        errs = self._errs(m)
        self.assertTrue(errs, "the placeholder must not validate clean")
        self.assertIn("PORT", errs[0])
        self.assertIn("ava device new", errs[0], "name where it came from")

    def test_a_real_port_validates(self):
        m = {"id": "d", "actions": {"discover": {"base": "http://127.0.0.1:9700",
                                                 "list": "/tools", "call": "/call"}}}
        self.assertEqual(self._errs(m), [])

    def test_an_unset_env_var_is_not_an_error(self):
        """Every example that says "export HASS_URL first" expands to nothing on
        a clean checkout. Reporting that as malformed tells the owner their
        example is broken when they have simply not configured it — the first
        version of this check did exactly that to examples/home-assistant."""
        m = {"id": "d", "mcp": {"url": "${DEFINITELY_UNSET_VAR_XYZ}/mcp"}}
        self.assertEqual(self._errs(m), [])

    def test_a_SET_env_var_producing_a_bad_url_is_still_an_error(self):
        m = {"id": "d", "mcp": {"url": "${AVA_TEST_HOST}/mcp"}}
        with mock.patch.dict(os.environ, {"AVA_TEST_HOST": "http://h.local:NOPE"}):
            errs = self._errs(m)
        self.assertTrue(errs, "interpolating to a bad URL must still be caught")
        self.assertIn("NOPE", errs[0])

    def test_the_shipped_registry_has_no_such_error(self):
        connectors.load()
        self.assertEqual(connectors.load_errors(), [])


class NoDuplicateRulesTests(unittest.TestCase):
    """A generated egress policy must not repeat a rule.

    Duplicates allow nothing extra, so nothing breaks and nobody notices — which
    is the problem. A reviewer reading a security policy has to diff the copies
    line by line to be sure they agree, and all three shipped demo manifests had
    every rule twice: they spelled out `GET /internal/connector/<id>/__tools`
    under `egress.routes`, which is the obvious thing to write, and the renderer
    derives the same two routes from the `discover:` block.
    """

    def _rules(self, cid):
        pol = connectors.render_egress_policy(cid) or {}
        return [(r["allow"]["method"], r["allow"]["path"])
                for np in (pol.get("network_policies") or {}).values()
                for ep in (np.get("endpoints") or [])
                for r in (ep.get("rules") or [])]

    def _shipped_manifests(self):
        """Every manifest a fork receives, from BOTH places they ship from.

        `connectors/` holds the infrastructure connectors, none of which declare
        egress — they are the bridge, the router and the local model, and they
        talk to the box rather than out of it. The connectors that DO declare
        egress are the examples, which ship under `examples/` and are copied into
        `$AVA_HOME/connectors/` by hand, so `connectors.load()` never sees them
        on a clean checkout. Reading only the loader is what let this check pass
        locally, where the copies exist, and find nothing at all in CI.
        """
        out = list(connectors.load())
        seen = {m["id"] for m in out}
        root = Path(__file__).resolve().parents[1] / "examples"
        for path in sorted(root.glob("*/connector.yaml")):
            m = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if m.get("id") and m["id"] not in seen:
                out.append(m)
                seen.add(m["id"])
        return out

    def test_no_shipped_connector_renders_a_rule_twice(self):
        checked = 0
        for m in self._shipped_manifests():
            with _with(m):
                rules = self._rules(m["id"])
            if not rules:
                continue
            checked += 1
            self.assertEqual(len(rules), len(set(rules)), (
                f"{m['id']}'s generated policy repeats a rule: "
                f"{sorted({r for r in rules if rules.count(r) > 1})}. Drop it "
                "from egress.routes — a discovery/MCP connector's __tools and "
                "__call routes are derived automatically."))
        self.assertTrue(checked, "no shipped manifest rendered any rule — did the "
                                 "registry, the examples tree or the renderer move?")

    def test_hand_declaring_a_derived_route_is_absorbed(self):
        """The manifests are clean now; this is what keeps a user's copy clean.
        Without _dedupe_rules this renders four rules instead of two."""
        m = {"id": "dup", "mcp": {"url": "http://127.0.0.1:9200/mcp"},
             "egress": {"routes": ["GET /internal/connector/dup/__tools",
                                   "POST /internal/connector/dup/__call"]}}
        with _with(m):
            rules = self._rules("dup")
        self.assertEqual(len(rules), 2, f"expected 2 rules, got {rules}")
        self.assertEqual(len(rules), len(set(rules)))


if __name__ == "__main__":
    unittest.main()


class ConnectorTelemetryHonestyTests(unittest.TestCase):
    """Reported egress and actions must reflect what the sandbox is GRANTED,
    not what the manifest literally spells.

    `egress_routes` summed the manifest's own `routes` + `hosts`, so every
    discover/MCP connector reported 0 — the shape with the MOST egress, since
    `render_egress_policy` auto-allows `__tools`/`__call` for it. And `actions`
    read `m["actions"]` directly, which is legally a list OR a dict
    (`static:` / `discover:`), so every dict-form manifest reported none.

    These were the Operations page's connector rows. That page is gone;
    `_egress_route_count` now feeds the sidebar's per-app health verdict and
    `connectors.actions()` feeds `GET /api/apps/{cid}/actions`, so both bugs
    are still reachable and both guards still apply.
    """

    def test_a_dynamic_connector_does_not_report_zero_egress(self):
        from ava_bridge import dashboard

        m = {"id": "mcpx", "label": "MCPX", "mcp": {"url": "http://127.0.0.1:9200/mcp"}}
        with mock.patch.object(connectors, "all", return_value=[m]), \
             mock.patch.object(connectors, "load", return_value=[m]):
            n = dashboard._egress_route_count(m)
        self.assertGreater(n, 0, "a connector whose tools ride __tools/__call "
                                 "reported no egress at all")

    def test_dict_form_actions_are_counted(self):
        m = {"id": "acme", "label": "Acme",
             "actions": {"static": [{"id": "read_notes", "path": "/notes"}]}}
        with mock.patch.object(connectors, "load", return_value=[m]):
            acts = [a["id"] for a in connectors.actions()
                    if a.get("connector") == "acme"]
        self.assertEqual(acts, ["read_notes"],
                         "a `static:`-form actions block read as empty")

    def test_a_render_failure_falls_back_rather_than_breaking_the_page(self):
        from ava_bridge import dashboard

        m = {"id": "acme", "egress": {"routes": ["POST /x"], "hosts": ["h:1"]}}
        with mock.patch.object(connectors, "render_egress_policy",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(dashboard._egress_route_count(m), 2)


class BridgePortTests(unittest.TestCase):
    def test_the_rendered_policy_follows_a_moved_bridge_port(self):
        """`_BRIDGE_PORT` was a module constant read at import, while
        `provision.port_rewrite` and install.sh's own sed both resolve it live.
        The rendered policy then allowed one port and the rewrite expected
        another — and the sandbox's refusal names no port."""
        with mock.patch.object(connectors.config, "SERVER_PORT", 9455):
            self.assertEqual(connectors._bridge_port(), 9455)
        # …and back, with no restart in between.
        self.assertEqual(connectors._bridge_port(), int(connectors.config.SERVER_PORT))
