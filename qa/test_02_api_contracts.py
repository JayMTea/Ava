"""Authed sweep of the read-only API surface: every GET endpoint the frontend
polls must answer 200 with the keys the SPA actually destructures (taken from
the view components themselves). This is the drift net: a renamed/removed field
fails here before it 500s a view.
"""
import unittest

import pytest

from qa import helpers

# endpoint -> keys that must exist in the JSON response
CONTRACTS = {
    "/api/health": ["ok"],
    "/api/brand": ["name"],
    "/api/hardware": [],
    "/api/model": ["mode", "backends", "agent_model"],
    "/api/turns": ["ok", "turns"],
    "/api/apps": ["apps"],
    "/api/apps/health": ["ok", "apps"],
    "/api/devices": [],
    "/api/chats": ["chats"],
    "/api/hub/system": ["brand", "version", "retention_days",
                        "env_overrides"],
    "/api/hub/cost": ["currency", "daily_spend_usd", "budgets"],
    "/api/hub/persona": ["style", "format", "presets", "format_choices",
                         "env_overrides"],
    "/api/hub/audit": ["events"],
    "/api/hub/approvals": ["pending"],
    "/api/hub/connectors": [],
    "/api/hub/models": [],
    "/api/hub/models/backends": [],
    "/api/hub/agent/status": ["runtime", "enabled", "available", "tools",
                              "sandbox_model", "sandbox_provider"],
    "/api/hub/agent/skills": ["skills", "errors", "summary"],
    "/api/hub/agent/provision/state": ["state", "scopes", "items", "pending",
                                       "counts", "sandbox", "scopes_to_provision"],
    "/api/hub/voice/status": [],
    "/api/hub/memory": [],
    "/api/hub/memory/export": [],
    "/api/setup/hardware": ["tier"],
    # Was ["vllm", "ollama", "router"] — two hardcoded loopback probes, which
    # inside the Docker image meant the CONTAINER's loopback, where the compose
    # engines are not. Now: the candidates actually probed, in priority order.
    "/api/setup/backends": ["backends", "any_up", "router"],
    "/api/setup/connectors": ["connectors"],
}

# The sidebar's per-app health row contract: each app must carry these keys or
# the nav dot regresses silently.
APP_HEALTH_ROW_KEYS = {"id", "health"}


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestApiContracts(unittest.TestCase):
    def test_every_get_endpoint_answers_with_expected_keys(self):
        c = CLIENT
        failures = []
        for path, keys in CONTRACTS.items():
            r = c.get(path)
            if r.status_code != 200:
                failures.append(f"GET {path} -> {r.status_code}")
                continue
            try:
                body = r.json()
            except ValueError:
                failures.append(f"GET {path} -> not JSON")
                continue
            if isinstance(body, dict):
                missing = [k for k in keys if k not in body]
                if missing:
                    failures.append(f"GET {path} missing keys: {missing}")
        self.assertEqual(failures, [], "\n" + "\n".join(failures))

    def test_app_health_rows_carry_the_sidebar_dot_fields(self):
        rows = CLIENT.get("/api/apps/health").json()["apps"]
        self.assertTrue(rows, "no apps registered at all?")
        for row in rows:
            missing = APP_HEALTH_ROW_KEYS - set(row)
            self.assertFalse(missing, f"{row.get('id')}: missing {missing}")

    def test_agent_skill_rows_carry_ui_fields(self):
        # The Agent-tab Skills panel reads these per-skill fields; missing any
        # regresses the cards silently. Shipped skills must be auto-discovered.
        body = CLIENT.get("/api/hub/agent/skills").json()
        self.assertTrue(body["skills"], "no skills auto-discovered from agent/skills")
        skill_keys = {"id", "title", "summary", "tools", "source", "deployed", "category"}
        for s in body["skills"]:
            self.assertFalse(skill_keys - set(s), f"{s.get('id')}: missing {skill_keys - set(s)}")
            self.assertIn(s["deployed"], ("deployed", "stale", "undeployed", "unknown"))
            self.assertIn(s["source"], ("core", "overlay"))

    def test_provision_state_is_honest_on_a_box_with_no_sandbox(self):
        """The fresh-fork path, and the one most likely to regress.

        The QA box has no nemoclaw and no sandbox, so every scope must report
        `unknown` — "we could not look" — and contribute NOTHING to `pending`.
        The failure this pins is the inverse: reporting `undeployed` for a sandbox
        that was never examined, which shows the owner a to-do list they cannot
        action and an Apply button that dies at the bootstrap guard.
        """
        body = CLIENT.get("/api/hub/agent/provision/state").json()
        legal = {"deployed", "stale", "undeployed", "unknown"}
        for scope, row in body["scopes"].items():
            self.assertIn(row["state"], legal, f"{scope}: illegal drift state")
            self.assertEqual(row["pending"], row["counts"]["stale"]
                             + row["counts"]["undeployed"],
                             f"{scope}: pending disagrees with its own counts")
        self.assertEqual(
            body["pending"],
            body["counts"]["stale"] + body["counts"]["undeployed"],
            "unknown items are leaking into the pending-changes count")
        for item in body["items"]:
            self.assertIn(item["state"], legal)
            self.assertIn(item["scope"], ("persona", "policies", "servers", "skills"))

    def test_agent_skill_detail_returns_full_markdown(self):
        # The expand action lazy-fetches one skill's SKILL.md body.
        first = CLIENT.get("/api/hub/agent/skills").json()["skills"][0]["id"]
        r = CLIENT.get(f"/api/hub/agent/skills/{first}")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["id"], first)
        self.assertTrue(d["body"], "skill body is empty")
        self.assertNotIn("\n---\n", d["body"][:5], "frontmatter leaked into the body")
        self.assertEqual(CLIENT.get("/api/hub/agent/skills/no-such-skill").status_code, 404)

    def test_unknown_paths_are_404_not_500(self):
        c = CLIENT
        for path in ("/api/definitely-not-a-route", "/api/ops/summary"):
            self.assertEqual(c.get(path).status_code, 404, path)
