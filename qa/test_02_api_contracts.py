"""Authed sweep of the read-only API surface: every GET endpoint the frontend
polls must answer 200 with the keys the SPA actually destructures (taken from
frontend/src/components/dashboard/dashApi.ts and the view components). This is
the drift net: a renamed/removed field fails here before it 500s a view.
"""
import unittest

import pytest

from qa import helpers

# endpoint -> keys that must exist in the JSON response
CONTRACTS = {
    "/api/health": ["ok"],
    "/api/brand": ["name"],
    "/api/hardware": [],
    "/api/hardware/history": ["samples"],
    "/api/model": ["mode", "backends", "agent_model"],
    "/api/perf/summary": ["ok", "apps", "sources_present"],
    "/api/perf/series?metric=tokens_per_sec": ["ok", "series", "points"],
    "/api/perf/recent": ["ok", "recent"],
    "/api/perf/cost": ["ok", "spend_usd", "energy_kwh", "power_measured", "by"],
    "/api/jobs": ["ok", "jobs"],
    "/api/turns": ["ok", "turns"],
    "/api/code-turns": ["ok", "code_turns"],
    "/api/ops/summary": ["ok", "jobs", "turns", "generations_24h", "learning"],
    "/api/ops/schedule": ["ok", "timers"],
    "/api/ops/services": ["ok", "services", "down"],
    "/api/ops/tools": ["ok", "tools", "total_calls"],
    "/api/ops/connectors": ["ok", "connectors", "action_count"],
    "/api/ops/alerts": ["ok", "active", "metrics"],
    "/api/apps": ["apps"],
    "/api/devices": [],
    "/api/chats": ["chats"],
    "/api/hub/system": ["brand", "version", "code_approval", "retention_days",
                        "env_overrides"],
    "/api/hub/cost": ["currency", "daily_spend_usd", "budgets"],
    "/api/hub/audit": ["events"],
    "/api/hub/approvals": ["pending"],
    "/api/hub/connectors": [],
    "/api/hub/models": [],
    "/api/hub/models/backends": [],
    "/api/hub/agent/status": ["runtime", "enabled", "available", "tools",
                              "sandbox_model", "sandbox_provider"],
    "/api/hub/agent/skills": ["skills", "errors", "summary"],
    "/api/hub/voice/status": [],
    "/api/hub/memory": [],
    "/api/hub/memory/export": [],
    "/api/data/stores": [],
    "/api/data/chats": [],
    "/api/data/maintenance": [],
    "/api/setup/hardware": ["tier"],
    # Was ["vllm", "ollama", "router"] — two hardcoded loopback probes, which
    # inside the Docker image meant the CONTAINER's loopback, where the compose
    # engines are not. Now: the candidates actually probed, in priority order.
    "/api/setup/backends": ["backends", "any_up", "router"],
    "/api/setup/connectors": ["connectors"],
}

# Vitals "Connected apps" panel contract (added 2026-07): each connector row
# must carry these keys or the panel regresses silently.
CONNECTOR_ROW_KEYS = {"id", "label", "kind", "has_service", "has_perf",
                      "perf_app", "perf_present", "status", "egress_routes",
                      "actions"}


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

    def test_ops_connector_rows_carry_vitals_fields(self):
        rows = CLIENT.get("/api/ops/connectors").json()["connectors"]
        self.assertTrue(rows, "no connectors registered at all?")
        for row in rows:
            missing = CONNECTOR_ROW_KEYS - set(row)
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

    def test_perf_summary_shape_matches_frontend_types(self):
        body = CLIENT.get("/api/perf/summary").json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["apps"], list)
        self.assertIsInstance(body["sources_present"], dict)
        # summary present (may be sparse on a fresh home, but the key exists)
        self.assertIn("summary", body)
        self.assertIn("records", body["summary"])

    def test_unknown_paths_are_404_not_500(self):
        c = CLIENT
        for path in ("/api/definitely-not-a-route", "/api/perf/nope"):
            self.assertEqual(c.get(path).status_code, 404, path)
