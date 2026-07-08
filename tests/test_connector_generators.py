"""Tests for the connector code generators — self-contained (fixture-based).

One connector manifest generates BOTH the agent's tools (`.mjs`) AND its egress
security policy (`.yaml`). These tests drive the generators from an in-test
manifest (mocking connectors.load), so they run identically on a fresh checkout
regardless of which connectors are installed — the shipped example/personal
connectors and their generated output are gitignored.

The load-bearing assertions:
  1. Generation is deterministic (regen twice == identical) — the anchor a
     golden/`ava verify` drift check relies on.
  2. tool <-> policy lockstep: every generic-proxy action a tool can call is
     allow-listed (GET+POST) in the connector's egress policy — the whole point
     of generating both from one manifest.
  3. An MCP connector's policy allows EXACTLY the two __tools/__call routes and
     nothing else — the agent never reaches the MCP server directly.
"""
import unittest
from unittest import mock

import yaml

from ava_bridge import connectors

REST_FIXTURE = {
    "id": "acme",
    "label": "Acme CRM",
    "kind": "app",
    "base_url": "http://127.0.0.1:9000",
    "egress": {"hosts": ["127.0.0.1:9000"]},
    "actions": [
        {"id": "create_lead", "description": "Create a lead", "method": "POST",
         "path": "/api/leads", "input": {"name": {"type": "string"}}},
        {"id": "list_leads", "description": "List leads", "method": "GET",
         "path": "/api/leads"},
    ],
}

MCP_FIXTURE = {"id": "mcpsrv", "label": "MCP Server", "kind": "app",
               "mcp": {"url": "http://127.0.0.1:9200/mcp"}}


def _with(manifest):
    """Patch the registry so the generators resolve our fixture by id."""
    return mock.patch.object(connectors, "load", return_value=[manifest])


def _proxy_actions(m):
    return [a for a in connectors._static_actions(m)
            if a.get("id") and a.get("path")]


class TestDeterminism(unittest.TestCase):
    def test_policy_regen_identical(self):
        with _with(REST_FIXTURE):
            a = yaml.safe_dump(connectors.render_egress_policy("acme"), sort_keys=False)
            b = yaml.safe_dump(connectors.render_egress_policy("acme"), sort_keys=False)
        self.assertEqual(a, b)
        self.assertIn("ava-acme", a)

    def test_tool_regen_identical_and_valid(self):
        with _with(REST_FIXTURE):
            for act in _proxy_actions(REST_FIXTURE):
                s1 = connectors.render_tool("acme", act)
                s2 = connectors.render_tool("acme", act)
                self.assertEqual(s1, s2)
                # the generated tool names itself and targets the proxy route
                self.assertIn(act["id"], s1)
                self.assertIn(f"/internal/connector/acme/{act['id']}", s1)


class TestToolPolicyLockstep(unittest.TestCase):
    def test_every_proxy_route_is_allow_listed(self):
        with _with(REST_FIXTURE):
            pol = connectors.render_egress_policy("acme")
            actions = _proxy_actions(REST_FIXTURE)
        allowed = {(r["allow"]["method"], r["allow"]["path"])
                   for np in pol["network_policies"].values()
                   for ep in np.get("endpoints", []) for r in ep.get("rules", [])}
        for a in actions:
            route = f"/internal/connector/acme/{a['id']}"
            self.assertIn(("GET", route), allowed, f"{a['id']} GET not allowed")
            self.assertIn(("POST", route), allowed, f"{a['id']} POST not allowed")

    def test_generated_tool_exists_for_every_proxy_action(self):
        with _with(REST_FIXTURE):
            for a in _proxy_actions(REST_FIXTURE):
                self.assertTrue(connectors.render_tool("acme", a).strip())


class TestMcpEgressContainment(unittest.TestCase):
    def test_mcp_policy_allows_exactly_the_two_bridge_routes(self):
        with _with(MCP_FIXTURE):
            pol = connectors.render_egress_policy("mcpsrv")
        self.assertIsNotNone(pol)
        allowed = {(r["allow"]["method"], r["allow"]["path"])
                   for np in pol["network_policies"].values()
                   for ep in np.get("endpoints", []) for r in ep.get("rules", [])}
        self.assertEqual(allowed, {
            ("GET", "/internal/connector/mcpsrv/__tools"),
            ("POST", "/internal/connector/mcpsrv/__call"),
        })


if __name__ == "__main__":
    unittest.main()
