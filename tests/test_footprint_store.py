"""Measurements are remembered; guesses are not.

Ollama evicts a model after ~5 minutes idle, so the one rung of the trust ladder
that is a MEASUREMENT is only reachable in a narrow window. Without a store, an
owner browsing models half an hour later gets a derived estimate for a model this
machine has already run and measured — the single case where Ava knows the true
answer and reports a guess instead.

The rule that keeps this from rotting into a cache of stale guesses: only
observations are ever written. A derived figure persisted for a month outlives
its inputs (a raised WSL2 ceiling, a different GPU, a re-quantised tag) and a
stale guess is harder to distrust than a fresh one, because it looks settled.

Run: .venv/bin/python -m pytest tests/test_footprint_store.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

from ava_bridge import footprint_store as store
from ava_bridge.model_footprint import DERIVED, OBSERVED, Footprint


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        p = mock.patch("ava_bridge.settings.data_dir", lambda: self.dir.name)
        p.start()
        self.addCleanup(p.stop)

    def _obs(self, model="m", gb=6.0, spilled=False):
        return Footprint(model=model, source=OBSERVED, weight_gb=gb,
                         spilled=spilled, detail="measured")


class OnlyMeasurements(_Tmp):
    def test_an_observation_round_trips(self):
        self.assertTrue(store.remember(self._obs(gb=6.25, spilled=True)))
        got = store.get("m")
        self.assertEqual(got.source, OBSERVED)
        self.assertEqual(got.weight_gb, 6.25)
        self.assertTrue(got.spilled)

    def test_a_derived_figure_is_refused(self):
        """The rule the whole file turns on."""
        fp = Footprint(model="m", source=DERIVED, weight_gb=9.0, detail="computed")
        self.assertFalse(store.remember(fp))
        self.assertIsNone(store.get("m"))

    def test_a_zero_weight_is_refused(self):
        self.assertFalse(store.remember(self._obs(gb=0)))


class DegradesQuietly(_Tmp):
    def test_an_unknown_model_is_none(self):
        self.assertIsNone(store.get("never-seen"))

    def test_a_corrupt_file_is_not_fatal(self):
        with open(os.path.join(self.dir.name, "model_footprints.json"), "w") as f:
            f.write("{not json at all")
        self.assertIsNone(store.get("m"))
        self.assertTrue(store.remember(self._obs()), "a bad file must not block writes")

    def test_a_stale_record_is_ignored_rather_than_trusted(self):
        """The box may have changed underneath it — a raised ceiling, a new GPU,
        a re-quantised tag. Re-observing costs one call; a stale number costs a
        wrong recommendation that looks authoritative."""
        store.remember(self._obs(), now=1000.0)
        self.assertIsNotNone(store.get("m", now=1000.0))
        self.assertIsNone(store.get("m", now=1000.0 + store._MAX_AGE_S + 1))

    def test_a_failed_write_is_reported_not_raised(self):
        with mock.patch("ava_bridge.settings.data_dir", lambda: "/nonexistent/nope"):
            self.assertFalse(store.remember(self._obs()))


class StaysBounded(_Tmp):
    def test_the_oldest_entries_are_dropped(self):
        """An unbounded file on a box someone is genuinely exploring models on
        is a slow leak nobody would ever go looking for."""
        for i in range(store._MAX_ENTRIES + 20):
            store.remember(self._obs(model=f"m{i:03d}"), now=1000.0 + i)
        import json
        with open(os.path.join(self.dir.name, "model_footprints.json")) as f:
            data = json.load(f)
        self.assertLessEqual(len(data), store._MAX_ENTRIES)
        self.assertNotIn("m000", data, "the oldest should have been evicted")
        self.assertIn(f"m{store._MAX_ENTRIES + 19:03d}", data, "the newest kept")


class TheLoop(_Tmp):
    def test_observing_records_everything_resident(self):
        GIB = 1024 ** 3
        rows = [{"id": "a", "size_bytes": 6 * GIB, "vram_bytes": 6 * GIB},
                {"id": "b", "size_bytes": 49 * GIB, "vram_bytes": 20 * GIB}]
        with mock.patch("ava_bridge.models.probe_resident", lambda *a, **k: rows):
            self.assertEqual(store.observe_and_remember("http://x/v1", "ollama"), 2)
        self.assertFalse(store.get("a").spilled)
        self.assertTrue(store.get("b").spilled, "the spill must survive the round trip")

    def test_an_engine_that_cannot_be_asked_records_nothing(self):
        with mock.patch("ava_bridge.models.probe_resident", lambda *a, **k: None):
            self.assertEqual(store.observe_and_remember("http://x/v1", "vllm"), 0)

    def test_a_broken_engine_read_is_not_fatal(self):
        with mock.patch("ava_bridge.models.probe_resident",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(store.observe_and_remember("http://x/v1", "ollama"), 0)


if __name__ == "__main__":
    unittest.main()
