"""Cost budgets, idle-burn metric, and the audit ledger — the flight-recorder
and governed-cost differentiators. Backends only (no HTTP), fast + hermetic.
"""
import os
import tempfile
import unittest
from unittest import mock

# Isolate AVA_HOME so the ledger/tmp files never touch the real store.
os.environ.setdefault("AVA_HOME", tempfile.mkdtemp())

from ava_bridge import alerts, audit, dashboard  # noqa: E402


class TestAuditLedger(unittest.TestCase):
    def test_append_tail_filter_and_perms(self):
        import stat
        audit.record("turn", chat_id="c1", status="done", tools=["get_weather"])
        audit.record("egress", connector="crm", tool="create_lead", status=200)
        audit.record("code_change", outcome="approved", commit="deadbee")
        # 0600
        self.assertEqual(stat.S_IMODE(os.stat(audit._path()).st_mode), 0o600)
        # newest-first
        allev = audit.tail(10)
        self.assertGreaterEqual(len(allev), 3)
        self.assertEqual(allev[0]["kind"], "code_change")
        # kind filter
        egress = audit.tail(10, kind="egress")
        self.assertTrue(egress and all(e["kind"] == "egress" for e in egress))

    def test_record_never_raises(self):
        # An unserializable value must not blow up the caller.
        audit.record("turn", weird=object())  # default=str handles it


class TestBudgetMetrics(unittest.TestCase):
    def _metrics(self, cfg, day):
        with mock.patch.object(dashboard, "_cost_cfg", return_value=cfg), \
             mock.patch.object(dashboard, "perf_cost",
                               side_effect=lambda since="1d", **k: day), \
             mock.patch.object(dashboard, "_all_rows", return_value=[]), \
             mock.patch.object(dashboard, "ops_services", return_value={"down": 0}):
            dashboard.invalidate_cost_cache()
            return dashboard.build_alert_metrics()

    def test_dormant_without_budget(self):
        m = self._metrics({"budgets": {}, "prices": {}, "nominal_gpu_watts": 180},
                          {"spend_usd": 3.0, "energy_kwh": 1.0, "power_measured": True})
        self.assertEqual(m["budget_daily_pct"], 0)
        self.assertEqual(m["budget_energy_pct"], 0)

    def test_pct_computed_when_set(self):
        m = self._metrics(
            {"budgets": {"daily_usd": 10.0, "daily_kwh": 2.0}, "prices": {}},
            {"spend_usd": 8.0, "energy_kwh": 3.0, "power_measured": True})
        self.assertEqual(m["budget_daily_pct"], 80.0)
        self.assertEqual(m["budget_energy_pct"], 150.0)

    def test_alert_rules_fire(self):
        res = alerts.evaluate({"budget_daily_pct": 80.0, "budget_energy_pct": 150.0,
                               "budget_monthly_pct": 0, "idle_burn_tokens_10m": 0})
        ids = {a["id"] for a in res["active"]}
        self.assertIn("budget_daily_warn", ids)   # 80% warn
        self.assertIn("budget_energy", ids)        # over energy cap
        self.assertNotIn("budget_daily", ids)      # <100% so critical stays quiet


class TestIdleBurn(unittest.TestCase):
    def test_idle_tokens_counted_when_away_and_zero_when_active(self):
        from ava_bridge import state
        now = 1_000_000.0
        rows = [{"ts": now, "completion_tokens": 9000}]  # a big background generation
        base = {"budgets": {}, "prices": {}, "nominal_gpu_watts": 180}
        day = {"spend_usd": 0, "energy_kwh": 0, "power_measured": False}

        def metrics():
            with mock.patch.object(dashboard, "_cost_cfg", return_value=base), \
                 mock.patch.object(dashboard, "perf_cost", side_effect=lambda since="1d", **k: day), \
                 mock.patch.object(dashboard, "_all_rows",
                                   side_effect=lambda a, c, s: rows if c == "llm" else []), \
                 mock.patch.object(dashboard, "ops_services", return_value={"down": 0}), \
                 mock.patch.object(dashboard.time, "time", return_value=now + 30):
                dashboard.invalidate_cost_cache()
                return dashboard.build_alert_metrics()

        # Last interaction was long ago (>120s before the row) -> counted as idle burn.
        state.interaction["ts"] = now - 600
        with mock.patch.dict(state.turns, {}, clear=True):
            self.assertEqual(metrics()["idle_burn_tokens_10m"], 9000)

        # Active interaction just now -> the same tokens are NOT idle.
        state.interaction["ts"] = now
        with mock.patch.dict(state.turns, {}, clear=True):
            self.assertEqual(metrics()["idle_burn_tokens_10m"], 0)


if __name__ == "__main__":
    unittest.main()
