"""The Setup Hub surfaces: system flags, live approval-mode toggle, retention
governance, cost/budgets round-trip, memory CRUD + export, voice status, and
the models/backends manager (against a mocked backend transport).
"""
import unittest

import pytest

from qa import helpers


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    globals()["CLIENT"] = bridge
    yield


class TestSystemSettings(unittest.TestCase):
    def test_system_snapshot(self):
        body = CLIENT.get("/api/hub/system").json()
        for key in ("brand", "version", "retention_days",
                    "retention_choices", "features"):
            self.assertIn(key, body)

    def test_retention_validated_and_persisted(self):
        c = CLIENT
        r = c.post("/api/hub/system/retention", params={"days": 12345})
        self.assertIn(r.status_code, (400, 422))  # not one of the choices
        r = c.post("/api/hub/system/retention", params={"days": 90})
        self.assertEqual(r.status_code, 200, r.text)
        from ava_bridge import settings
        self.assertEqual(settings.get_int("data.retention_days", 0), 90)

    def test_cost_budgets_round_trip_and_vitals_math(self):
        c = CLIENT
        r = c.post("/api/hub/cost", json={
            "electricity_rate_per_kwh": 0.31, "currency": "USD",
            "budgets": {"daily_usd": 5, "monthly_usd": 50, "daily_kwh": 2}})
        self.assertEqual(r.status_code, 200, r.text)
        body = c.get("/api/hub/cost").json()
        self.assertEqual(body["budgets"]["daily_usd"], 5)
        self.assertEqual(body["budgets"]["daily_kwh"], 2)
        self.assertIn("daily_spend_usd", body)
        # The alert-metric layer picks the budgets up.
        from ava_bridge import dashboard
        self.assertIn("budget_daily_pct", dashboard.build_alert_metrics())
        # Clearing a budget really clears it.
        c.post("/api/hub/cost", json={"budgets": {"daily_usd": None,
                                                  "monthly_usd": None,
                                                  "daily_kwh": None}})
        body = c.get("/api/hub/cost").json()
        self.assertIsNone(body["budgets"]["daily_usd"])

    def test_bad_cost_payload_rejected(self):
        r = CLIENT.post("/api/hub/cost",
                        json={"electricity_rate_per_kwh": "not-a-number"})
        self.assertEqual(r.status_code, 400)

    def test_persona_ships_unset_so_a_fork_inherits_no_personality(self):
        """The product claim, asserted at the API: a fresh instance has no style."""
        body = CLIENT.get("/api/hub/persona").json()
        self.assertEqual(body["style"], "")
        self.assertEqual(body["format"], "chat")
        self.assertFalse(body["adult"])
        # Presets are offered as editable text, never stored as an id — so
        # changing this list upstream cannot alter an existing install.
        self.assertTrue(body["presets"])
        for p in body["presets"]:
            self.assertTrue(p["text"].strip(), p)

    def test_persona_round_trip_and_clear(self):
        c = CLIENT
        r = c.post("/api/hub/persona",
                   json={"style": "dry, understated", "format": "markdown"})
        self.assertEqual(r.status_code, 200, r.text)
        # Saving does not change the live prompt until the agent is re-provisioned.
        self.assertTrue(r.json().get("reprovision_required"))
        from ava_bridge import settings
        self.assertEqual(settings.persona_style(), "dry, understated")
        self.assertEqual(settings.persona_format(), "markdown")
        # An empty style is a real value: "clear it", not "ignore me".
        r = c.post("/api/hub/persona", json={"style": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(c.get("/api/hub/persona").json()["style"], "")
        # Restore the shipped defaults. These tests share one bridge instance and
        # unittest orders methods alphabetically, so leaving format=markdown here
        # would break the "ships unset" assertion that sorts after this one.
        c.post("/api/hub/persona", json={"style": "", "format": "chat"})

    def test_bad_persona_payload_rejected(self):
        c = CLIENT
        self.assertEqual(c.post("/api/hub/persona",
                                json={"format": "interpretive-dance"}).status_code, 400)
        self.assertEqual(c.post("/api/hub/persona",
                                json={"style": 42}).status_code, 400)
        self.assertEqual(c.post("/api/hub/persona",
                                json={"style": "x" * 4001}).status_code, 400)
        self.assertEqual(c.post("/api/hub/persona", json={}).status_code, 400)


class TestMemory(unittest.TestCase):
    def test_memory_crud_and_export(self):
        c = CLIENT
        r = c.post("/api/hub/memory", json={"text": "QA fact: the sky is plaid",
                                            "kind": "fact"})
        self.assertEqual(r.status_code, 200, r.text)
        items = c.get("/api/hub/memory").json()
        self.assertIn("plaid", str(items))
        mid = None
        for it in (items.get("items") or items.get("memories") or []):
            if "plaid" in str(it):
                mid = it.get("id")
        self.assertIsNotNone(mid, f"created memory not found in {str(items)[:300]}")
        # Correct it, then delete it — the owner controls the store.
        r = c.post(f"/api/hub/memory/{mid}",
                   json={"text": "QA fact: the sky is tartan"})
        self.assertEqual(r.status_code, 200, r.text)
        export = c.get("/api/hub/memory/export").json()
        self.assertIn("tartan", str(export))
        r = c.post(f"/api/hub/memory/{mid}/delete")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("tartan", str(c.get("/api/hub/memory").json()))


class TestVoiceAndAgentStatus(unittest.TestCase):
    def test_voice_status_graceful_without_voice_stack(self):
        r = CLIENT.get("/api/hub/voice/status")
        self.assertEqual(r.status_code, 200)   # absent voice is a state, not an error

    def test_agent_status_reports_toolless_floor(self):
        # Status reports the CONFIGURED runtime (nemoclaw) with honest
        # availability; what matters is that tools are off, not an error.
        body = CLIENT.get("/api/hub/agent/status").json()
        self.assertFalse(body.get("tools"), body)
        self.assertIn("runtime", body)


class TestModelsBackends(unittest.TestCase):
    def test_models_list_and_backend_validation(self):
        c = CLIENT
        self.assertEqual(c.get("/api/hub/models").status_code, 200)
        self.assertEqual(c.get("/api/hub/models/backends").status_code, 200)
        # A malformed backend is rejected, not written to config.
        r = c.post("/api/hub/models/backends", json={})
        self.assertGreaterEqual(r.status_code, 400)
