"""Tests for the productized perf-log cold store (ava_bridge/perf_store).

Covers the properties that make cold storage safe for a self-hosted product:
no silent data loss, bounded/pruned raw, idempotent rollups, retention, the tok/s
garbage clamp, mergeable-histogram percentiles, and the hot/cold stitched readers.

Runs against a throwaway temp dir by rebinding the module's globals (they're looked
up at call time), so it never touches the real logs. Standalone: `python
tests/test_perf_store.py` or via pytest/unittest.
"""
import json
import os
import shutil
import tempfile
import unittest

from ava_bridge import perf_store as ps
from ava_bridge import perf_mgmt

NOW = 1_800_000_000.0  # fixed epoch so buckets are deterministic
HOUR = 3600
DAY = 86400


def _write_jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _llm(ts, tps=30.0, ttft=200.0, ct=100, model="Nano", failover=False, status=None):
    r = {"ts": ts, "category": "llm", "served_label": model, "served_model": model,
         "tokens_per_sec": tps, "ttft_ms": ttft, "completion_tokens": ct,
         "prompt_tokens": 50, "gen_seconds": 3.0}
    if failover:
        r["failover"] = True
    if status is not None:
        r["status"] = status
    return r


class PerfStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="perfstore-")
        self.live = os.path.join(self.tmp, "performance.jsonl")
        # Rebind module globals to the sandbox (looked up at call time).
        self._saved = {
            "ROLLUP_DIR": ps.ROLLUP_DIR, "_WATERMARK": ps._WATERMARK,
            "HOT_WINDOW_S": ps.HOT_WINDOW_S, "HOURLY_RETENTION_S": ps.HOURLY_RETENTION_S,
            "DAILY_RETENTION_S": ps.DAILY_RETENTION_S, "SOURCES": perf_mgmt.SOURCES,
        }
        ps.ROLLUP_DIR = os.path.join(self.tmp, "rollups")
        ps._WATERMARK = os.path.join(ps.ROLLUP_DIR, ".watermark")
        ps.HOT_WINDOW_S = 2 * HOUR       # anything >2h old is cold
        ps.HOURLY_RETENTION_S = 90 * DAY
        ps.DAILY_RETENTION_S = 0         # forever
        perf_mgmt.SOURCES = {"ava": self.live}

    def tearDown(self):
        for k, v in self._saved.items():
            if k == "SOURCES":
                perf_mgmt.SOURCES = v
            else:
                setattr(ps, k, v)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def _finalized_count(self, records, cutoff):
        return sum(1 for r in records if r["ts"] < cutoff)

    # -- tests --------------------------------------------------------------
    def test_no_data_loss(self):
        """Every finalized raw record lands in exactly one hourly + one daily bucket."""
        recs = [_llm(NOW - 5 * DAY + i * 900) for i in range(200)]  # spread over days
        _write_jsonl(self.live, recs)
        cutoff = NOW - ps.HOT_WINDOW_S
        expected = self._finalized_count(recs, cutoff)

        res = ps.run_rollup(now=NOW)
        self.assertEqual(res["processed"], expected)

        hourly = list(ps._load(ps.HOURLY_FILE).values())
        daily = list(ps._load(ps.DAILY_FILE).values())
        self.assertEqual(sum(b["n"] for b in hourly), expected)
        self.assertEqual(sum(b["n"] for b in daily), expected)

    def test_idempotent(self):
        """A second run with no new records changes nothing (watermark guard)."""
        _write_jsonl(self.live, [_llm(NOW - 3 * DAY + i * 600) for i in range(100)])
        hourly_path = os.path.join(ps.ROLLUP_DIR, ps.HOURLY_FILE)
        ps.run_rollup(now=NOW)
        with open(hourly_path, "rb") as f:
            before = f.read()
        res2 = ps.run_rollup(now=NOW)
        with open(hourly_path, "rb") as f:
            after = f.read()
        self.assertEqual(res2["processed"], 0)
        self.assertEqual(before, after)

    def test_rotated_segment_read_and_pruned(self):
        """Rotated .1 segments are rolled up then deleted; the live file is untouched."""
        old = [_llm(NOW - 6 * DAY + i * 600) for i in range(30)]      # all cold
        hot = [_llm(NOW - 600 + i * 60) for i in range(5)]           # recent, stays hot
        _write_jsonl(self.live + ".1", old)
        _write_jsonl(self.live, hot)

        res = ps.run_rollup(now=NOW)
        self.assertEqual(res["processed"], len(old))       # only cold records finalized
        self.assertEqual(res["pruned_segments"], 1)
        self.assertFalse(os.path.exists(self.live + ".1"))  # absorbed + pruned
        self.assertTrue(os.path.exists(self.live))          # live never pruned

    def test_tok_s_clamp(self):
        """Garbage tokens/sec (> MAX_TOK_S) is excluded from the histogram + averages."""
        good = [_llm(NOW - 3 * DAY + i * 600, tps=40.0) for i in range(10)]
        garbage = [_llm(NOW - 3 * DAY + 100 + i, tps=999999.0) for i in range(3)]
        _write_jsonl(self.live, good + garbage)
        ps.run_rollup(now=NOW)

        tok_count = sum((b["tok_hist"]["count"]) for b in ps._load(ps.HOURLY_FILE).values())
        self.assertEqual(tok_count, len(good))  # garbage rows contributed 0 tok samples
        # merged average stays sane (≈40), not dragged toward a million
        merged = None
        for b in ps._load(ps.HOURLY_FILE).values():
            merged = ps._hist_merge(merged, b["tok_hist"])
        self.assertLess(merged["sum"] / merged["count"], 100)

    def test_histogram_percentiles_track_truth(self):
        """Rollup histogram p50/p90/p95 land within a bucket-width of the real values."""
        base = NOW - 3 * DAY
        # 100 llm records in one hour with ttft = 1..100 ms
        recs = [_llm(base + i, ttft=float(i + 1)) for i in range(100)]
        _write_jsonl(self.live, recs)
        ps.run_rollup(now=NOW)

        merged = None
        for b in ps._load(ps.HOURLY_FILE).values():
            merged = ps._hist_merge(merged, b["ttft_hist"])
        B = ps.BUCKETS["ttft_ms"]
        truth = {50: 50, 90: 90, 95: 95}
        for pct, want in truth.items():
            got = ps.quantile(merged, B, pct / 100.0)
            self.assertIsNotNone(got)
            self.assertLessEqual(abs(got - want), 30, f"p{pct}: got {got}, want ~{want}")

    def test_retention_drops_old_hourly(self):
        """Hourly buckets older than the retention floor are dropped on write."""
        ps.HOURLY_RETENTION_S = 2 * DAY
        recs = ([_llm(NOW - 10 * DAY + i * 600) for i in range(20)] +   # older than 2d -> dropped
                [_llm(NOW - 1 * DAY - 3 * HOUR + i * 600) for i in range(20)])  # within 2d, cold
        _write_jsonl(self.live, recs)
        ps.run_rollup(now=NOW)
        floor = NOW - ps.HOURLY_RETENTION_S
        for b in ps._load(ps.HOURLY_FILE).values():
            self.assertGreaterEqual(b["bucket_ts"], floor)

    def test_cold_series_and_cost(self):
        """Cold readers return stitched-range points + summed cost/energy."""
        recs = [_llm(NOW - 5 * DAY + i * HOUR, tps=25.0 + (i % 5)) for i in range(48)]
        _write_jsonl(self.live, recs)
        ps.run_rollup(now=NOW)
        cutoff = ps.cold_boundary(NOW)

        series = ps.cold_series("tokens_per_sec", DAY, NOW - 7 * DAY, cutoff)
        self.assertTrue(series["points"])
        self.assertIn("Nano", series["series"])
        for pt in series["points"]:
            self.assertGreater(pt["Nano"], 0)

        cost = ps.cold_cost(NOW - 7 * DAY, cutoff, "model")
        self.assertGreaterEqual(cost["energy_wh"], 0)
        self.assertIn("Nano", cost["by"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
