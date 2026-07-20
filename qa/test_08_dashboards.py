"""Vitals + Operations, fed with realistic seeded data, plus the live SSE
stream. This is the page-level behavior on top of the perf plumbing the unit
tests already cover.
"""
import json
import os
import time
import unittest

import pytest

from qa import helpers
from qa.conftest import QA_HOME


def _seed_perf_rows(n: int = 20):
    """Append realistic llm rows to Ava's own perf log (the 'bridge' connector's
    declared source: ${AVA_LOGS}/performance.jsonl under this AVA_HOME)."""
    path = os.path.join(QA_HOME, "logs", "performance.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    now = time.time()
    with open(path, "a", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({
                "ts": now - i * 60, "category": "llm",
                "served_label": "QA Model", "served_model": "qa-model",
                "tokens_per_sec": 42.0 + i % 5, "ttft_ms": 120 + i,
                "prompt_tokens": 100, "completion_tokens": 80,
                "gen_seconds": 2.0}) + "\n")


@pytest.fixture(scope="module", autouse=True)
def _client(bridge):
    helpers.ensure_authed(bridge)
    _seed_perf_rows()
    helpers.refresh_connectors()
    globals()["CLIENT"] = bridge
    yield


class TestVitalsData(unittest.TestCase):
    def test_perf_summary_reflects_seeded_rows(self):
        s = CLIENT.get("/api/perf/summary?category=llm").json()
        self.assertTrue(s["ok"])
        self.assertGreaterEqual(s["summary"]["records"], 20)
        llm = s["summary"]["llm"]["QA Model"]
        self.assertGreaterEqual(llm["count"], 20)
        self.assertAlmostEqual(llm["tokens_per_sec"]["avg"], 44, delta=4)

    def test_perf_series_buckets(self):
        s = CLIENT.get("/api/perf/series?metric=tokens_per_sec&bucket=5m&since=2h").json()
        self.assertTrue(s["ok"])
        self.assertIn("QA Model", s["series"])
        self.assertTrue(s["points"])

    def test_perf_cost_by_app(self):
        s = CLIENT.get("/api/perf/cost?since=1d&group=app").json()
        self.assertTrue(s["ok"])
        self.assertIn("ava", s["by"])
        self.assertGreater(s["energy_kwh"], 0)

    def test_hardware_history(self):
        s = CLIENT.get("/api/hardware/history?since=1d&bucket=5m").json()
        self.assertTrue(s["ok"])
        self.assertIsInstance(s["samples"], list)


class TestOperationsData(unittest.TestCase):
    def test_ops_summary_counts(self):
        # generations_24h is cached 15s; earlier tests primed it before our
        # seed rows landed — drop the cache entry to read the current truth.
        from ava_bridge import dashboard
        dashboard._cache.pop("gen24_count", None)
        s = CLIENT.get("/api/ops/summary").json()
        self.assertTrue(s["ok"])
        self.assertGreaterEqual(s["generations_24h"], 20)

    def test_ops_services_and_alerts(self):
        s = CLIENT.get("/api/ops/services").json()
        self.assertTrue(s["ok"])
        for svc in s["services"]:
            self.assertIn(svc["status"], ("up", "down", "unknown"))
        a = CLIENT.get("/api/ops/alerts").json()
        self.assertTrue(a["ok"])
        self.assertIn("tokens_per_sec", a["metrics"])


class TestOpsSnapshot(unittest.TestCase):
    def test_live_snapshot_shape_feeding_the_sse_stream(self):
        """The SSE producer diffs dashboard.live_snapshot() at 1 Hz. An infinite
        SSE stream can't be consumed through the in-process portal transport, so
        the snapshot contract is asserted here; the real-socket stream itself is
        proven in qa/test_22_live_sse_static.py."""
        from ava_bridge import dashboard
        snap = dashboard.live_snapshot()
        self.assertIn("turns", snap)
        self.assertIn("jobs", snap)
        self.assertIn("hw", snap)
