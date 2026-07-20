"""Tests for the live perf-source layer (perf_mgmt.sources + app_perf).

Covers the properties that make Vitals trustworthy for a self-hosted product:
a connector added in the Hub is visible with NO restart; removing it does NOT
erase its history; re-adding the same id resumes the same history; a corrupt
ledger never breaks the readers; shared files never double-count; and the
bridge-observed action writer is safe (bounded, never raises, never pollutes
the GPU-energy estimate).

Runs against a throwaway temp dir by rebinding module globals (perf_store's
test style). Standalone: `python tests/test_perf_sources.py` or via pytest.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ava_bridge import app_perf, perf_mgmt


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class PerfSourcesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfsrc-")
        self._saved = {
            "LEDGER_PATH": perf_mgmt.LEDGER_PATH,
            "SOURCES_OVERRIDE": perf_mgmt.SOURCES_OVERRIDE,
            "APPS_DIR": app_perf.APPS_DIR,
        }
        perf_mgmt.LEDGER_PATH = os.path.join(self.tmp, "perf_sources.json")
        perf_mgmt.SOURCES_OVERRIDE = None
        app_perf.APPS_DIR = os.path.join(self.tmp, "apps")
        app_perf._registered.clear()
        perf_mgmt.refresh_sources()

    def tearDown(self):
        perf_mgmt.LEDGER_PATH = self._saved["LEDGER_PATH"]
        perf_mgmt.SOURCES_OVERRIDE = self._saved["SOURCES_OVERRIDE"]
        app_perf.APPS_DIR = self._saved["APPS_DIR"]
        app_perf._registered.clear()
        perf_mgmt.refresh_sources()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _with_registry(self, reg):
        """Patch the connector registry's perf-source view."""
        return mock.patch.object(perf_mgmt.connectors, "perf_sources",
                                 return_value=dict(reg))

    # -- liveness -------------------------------------------------------------
    def test_new_connector_appears_without_restart(self):
        with self._with_registry({"ava": "/x/ava.jsonl"}):
            perf_mgmt.refresh_sources()
            self.assertEqual(list(perf_mgmt.sources()), ["ava"])
        with self._with_registry({"ava": "/x/ava.jsonl", "myapp": "/x/myapp.jsonl"}):
            perf_mgmt.refresh_sources()  # what the Hub calls after a deploy
            self.assertIn("myapp", perf_mgmt.sources())

    def test_ttl_cache_serves_without_reload(self):
        with self._with_registry({"ava": "/x/ava.jsonl"}) as reg:
            perf_mgmt.refresh_sources()
            perf_mgmt.sources()
            perf_mgmt.sources()
            self.assertEqual(reg.call_count, 1)  # second call hit the TTL cache

    # -- history retention ----------------------------------------------------
    def test_removed_connector_history_stays_readable(self):
        with self._with_registry({"myapp": "/x/myapp.jsonl"}):
            perf_mgmt.refresh_sources()
            perf_mgmt.sources()  # persists to the ledger
        with self._with_registry({}):  # connector deleted in the Hub
            perf_mgmt.refresh_sources()
            self.assertEqual(perf_mgmt.sources().get("myapp"), ["/x/myapp.jsonl"])

    def test_reconnect_same_id_resumes_same_source(self):
        with self._with_registry({"myapp": "/x/myapp.jsonl"}):
            perf_mgmt.refresh_sources()
            perf_mgmt.sources()
        with self._with_registry({}):
            perf_mgmt.refresh_sources()
            perf_mgmt.sources()
        with self._with_registry({"myapp": "/x/myapp.jsonl"}):  # re-added
            perf_mgmt.refresh_sources()
            self.assertEqual(perf_mgmt.sources()["myapp"], ["/x/myapp.jsonl"])

    def test_manifest_path_change_keeps_old_history(self):
        with self._with_registry({"myapp": "/old/myapp.jsonl"}):
            perf_mgmt.refresh_sources()
            perf_mgmt.sources()
        with self._with_registry({"myapp": "/new/myapp.jsonl"}):
            perf_mgmt.refresh_sources()
            paths = perf_mgmt.sources()["myapp"]
            self.assertEqual(paths[0], "/new/myapp.jsonl")  # live path first
            self.assertIn("/old/myapp.jsonl", paths)        # history retained

    # -- robustness -----------------------------------------------------------
    def test_corrupt_ledger_tolerated(self):
        os.makedirs(os.path.dirname(perf_mgmt.LEDGER_PATH), exist_ok=True)
        with open(perf_mgmt.LEDGER_PATH, "w", encoding="utf-8") as f:
            f.write("{not json")
        with self._with_registry({"ava": "/x/ava.jsonl"}):
            perf_mgmt.refresh_sources()
            self.assertEqual(perf_mgmt.sources()["ava"], ["/x/ava.jsonl"])
        # and the ledger self-heals to valid JSON
        with open(perf_mgmt.LEDGER_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["apps"]["ava"], ["/x/ava.jsonl"])

    def test_empty_registry_falls_back_without_persisting(self):
        with self._with_registry({}):
            perf_mgmt.refresh_sources()
            self.assertIn("ava", perf_mgmt.sources())  # fallback
        self.assertFalse(os.path.exists(perf_mgmt.LEDGER_PATH))

    def test_override_normalises_str_values(self):
        perf_mgmt.SOURCES_OVERRIDE = {"ava": "/x/a.jsonl"}
        self.assertEqual(perf_mgmt.sources(), {"ava": ["/x/a.jsonl"]})

    def test_shared_file_counted_once(self):
        shared = os.path.join(self.tmp, "shared.jsonl")
        _write_jsonl(shared, [{"ts": 1, "category": "llm"}])
        perf_mgmt.SOURCES_OVERRIDE = {"a": [shared], "b": [shared]}
        files = perf_mgmt.source_files()
        self.assertEqual(len(files), 1)

    # -- read_performance over the live map ------------------------------------
    def test_read_performance_row_level_app_filter(self):
        f = os.path.join(self.tmp, "mix.jsonl")
        _write_jsonl(f, [
            {"ts": 1, "category": "action", "app": "myapp", "action": "ping",
             "action_seconds": 0.1, "status": 200},
            {"ts": 2, "category": "action", "app": "other", "action": "pong",
             "action_seconds": 0.2, "status": 200},
        ])
        perf_mgmt.SOURCES_OVERRIDE = {"myapp": [f], "other": [f]}
        res = perf_mgmt.read_performance(app="myapp")
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["recent"][0]["action"], "ping")
        self.assertEqual(res["summary"]["actions"]["count"], 1)

    # -- bridge-observed action writer -----------------------------------------
    def test_record_action_writes_and_registers(self):
        app_perf.record_action("myapp", "get_weather", 0.42, 200)
        path = app_perf.app_log_path("myapp")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["category"], "action")
        self.assertEqual(rec["app"], "myapp")
        self.assertEqual(rec["action_seconds"], 0.42)
        self.assertTrue(rec["ok"])
        # `seconds` must stay absent so action latency never counts as GPU energy
        self.assertNotIn("seconds", rec)
        # registered in the ledger -> visible to the readers with no manifest
        with self._with_registry({}):
            perf_mgmt.refresh_sources()
            self.assertIn(path, perf_mgmt.sources()["myapp"])

    def test_record_action_error_status_and_bad_input_never_raise(self):
        app_perf.record_action("myapp", "boom", 1.0, 503)
        app_perf.record_action("myapp", "weird", 1.0, "not-a-status")
        app_perf.record_action("", "no-app", 1.0, 200)      # ignored, no crash
        app_perf.record_action("myapp", "", 1.0, 200)       # ignored, no crash
        rows = []
        with open(app_perf.app_log_path("myapp"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["ok"])            # 503 -> not ok
        self.assertIsNone(rows[1]["status"])       # unparsable status -> None

    def test_record_action_rotation_bounded(self):
        saved = app_perf.MAX_BYTES
        app_perf.MAX_BYTES = 200  # force rotation quickly
        try:
            for _ in range(50):
                app_perf.record_action("myapp", "spin", 0.01, 200)
            base = app_perf.app_log_path("myapp")
            segs = [p for p in (f"{base}.{i}" for i in range(1, app_perf.KEEP + 1))
                    if os.path.exists(p)]
            self.assertTrue(segs)                          # rotated
            self.assertLessEqual(len(segs), app_perf.KEEP)  # bounded
        finally:
            app_perf.MAX_BYTES = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
