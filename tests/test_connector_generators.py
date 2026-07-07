"""Golden tests for the connector code generators.

One connector manifest generates BOTH the agent's tools (`.mjs`) AND its egress
security policy (`.yaml`). These tests regenerate from the shipped first-party
manifests and assert byte-equality with the committed generated files — so a
manifest edit that wasn't regenerated, or a drift in the generators
(`render_tool` / `render_egress_policy`), fails CI instead of silently shipping
a stale tool or an over-broad policy.

They also assert the security invariant that every generic-proxy action a tool
can call is allow-listed (GET+POST) in the connector's egress policy — i.e. the
tools and the policy stay in lockstep, which is the whole point of generating
both from one manifest.
"""
import os
import unittest

import yaml

from ava_bridge import connectors, settings

POL_DIR = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated")
TOOL_ROOT = os.path.join(settings.CODE_ROOT, "agent", "mcp_server_content", "connectors")


def _proxy_actions(m):
    """Generic-proxy actions (those with an id + path) are the ones that get a
    generated tool and an auto-allowed egress route."""
    return [a for a in connectors._static_actions(m)
            if a.get("id") and a.get("path")]


class TestEgressPolicyGolden(unittest.TestCase):
    def test_committed_policies_match_regeneration(self):
        """Every committed generated policy must match what the generator
        produces now — catches drift and forgot-to-regenerate. Driven from the
        committed files so it also flags an orphan (a generated policy whose
        connector no longer renders one). Connectors whose egress is hand-curated
        in agent/policies/ (e.g. gpu-service) have no file here and are not required
        to."""
        if not os.path.isdir(POL_DIR):
            self.skipTest("no agent/policies/generated dir")
        checked = 0
        for name in sorted(os.listdir(POL_DIR)):
            if not name.endswith(".yaml"):
                continue
            cid = name[:-len(".yaml")]
            pol = connectors.render_egress_policy(cid)
            self.assertIsNotNone(
                pol,
                f"agent/policies/generated/{name} exists but connector '{cid}' "
                f"no longer renders a policy — remove the stale file or restore "
                f"its manifest egress block")
            regen = yaml.safe_dump(pol, sort_keys=False)
            with open(os.path.join(POL_DIR, name), encoding="utf-8") as f:
                committed = f.read()
            self.assertEqual(
                regen, committed,
                f"agent/policies/generated/{name} is stale — regenerate with "
                f"`ava connector policies {cid} --write`")
            checked += 1
        self.assertGreater(checked, 0, "no committed connector policies were checked")


class TestToolGolden(unittest.TestCase):
    def test_committed_tools_match_regeneration(self):
        checked = 0
        for m in connectors.all():
            cid = m["id"]
            for a in _proxy_actions(m):
                path = os.path.join(TOOL_ROOT, cid, f"{cid}_{a['id']}.mjs")
                regen = connectors.render_tool(cid, a)
                self.assertTrue(
                    os.path.exists(path),
                    f"missing generated tool {path} — run "
                    f"`ava connector tools {cid} --write`")
                with open(path, encoding="utf-8") as f:
                    committed = f.read()
                self.assertEqual(
                    regen, committed,
                    f"{path} is stale — regenerate with "
                    f"`ava connector tools {cid} --write`")
                checked += 1
        self.assertGreater(checked, 0, "no committed connector tools were checked")


class TestToolPolicyLockstep(unittest.TestCase):
    """Security invariant: every generic-proxy action a tool can call must be
    allow-listed for BOTH GET and POST in the connector's egress policy."""

    def test_every_proxy_action_route_is_allowed(self):
        for m in connectors.all():
            cid = m["id"]
            actions = _proxy_actions(m)
            if not actions:
                continue
            pol = connectors.render_egress_policy(cid)
            self.assertIsNotNone(
                pol, f"{cid} has generic-proxy actions but no egress policy")
            allowed = set()
            for np in pol["network_policies"].values():
                for ep in np.get("endpoints", []):
                    for rule in ep.get("rules", []):
                        al = rule.get("allow", {})
                        allowed.add((al.get("method"), al.get("path")))
            for a in actions:
                route = f"/internal/connector/{cid}/{a['id']}"
                for method in ("GET", "POST"):
                    self.assertIn(
                        (method, route), allowed,
                        f"{cid}: action '{a['id']}' route {route} is not "
                        f"allow-listed for {method} in the egress policy")


if __name__ == "__main__":
    unittest.main()
