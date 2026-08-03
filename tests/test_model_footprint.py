"""What a model costs here, resolved honestly, and the asymmetry it feeds.

`recommend_tier` grades the BOX. It cannot tell a 7B at Q4 (~5 GB) from the same
7B at FP16 (three times that before any KV cache), so it was never able to answer
"will this model run well here" — and the fit machinery that could
(`model_fit.fits_now`) was fed a weight the owner had to hand-declare, which
nothing derived and the model picker never called.

Two rules are load-bearing enough to be tested rather than commented:

  * **The ladder.** observed > declared > derived > unknown, and `observed` is
    the only rung that is a measurement.
  * **The asymmetry.** "Will not fit" is arithmetic and may be stated. "Will
    fit" is a projection and must stay hedged — a confident "fits!" that then
    thrashes is worse than the vague tier it supplements.

Architecture numbers below are real (Llama 3.1 8B, Qwen2.5 72B), so the KV
arithmetic is checked against models that exist rather than round ones.

Run: .venv/bin/python -m pytest tests/test_model_footprint.py -q
"""
import unittest
from unittest import mock

from ava_bridge import model_fit, model_footprint as mf

GIB = 1024 ** 3
# Llama 3.1 8B: 32 layers, 4096 wide, 32 heads, 8 KV heads (GQA).
LLAMA8B = {"layers": 32, "heads": 32, "kv_heads": 8, "embedding": 4096,
           "train_context": 131072}


class KvArithmetic(unittest.TestCase):
    def test_kv_at_32k_matches_hand_arithmetic(self):
        """2 x 32 layers x 8 kv_heads x 128 head_dim x 32768 x 2 bytes = 4 GiB."""
        self.assertAlmostEqual(mf.kv_cache_gb(LLAMA8B, 32768), 4.0, places=2)

    def test_grouped_query_attention_is_not_ignored(self):
        """Using head_count instead of head_count_kv overstates this model's
        cache 4x, which rejects a model that fits comfortably."""
        mha = dict(LLAMA8B, kv_heads=32)
        self.assertAlmostEqual(mf.kv_cache_gb(mha, 32768)
                               / mf.kv_cache_gb(LLAMA8B, 32768), 4.0, places=2)

    def test_it_scales_linearly_with_context(self):
        a = mf.kv_cache_gb(LLAMA8B, 8192)
        b = mf.kv_cache_gb(LLAMA8B, 32768)
        self.assertAlmostEqual(b / a, 4.0, places=2)

    def test_a_shape_we_cannot_read_is_none_not_zero(self):
        for bad in ({}, {"layers": 32}, {"layers": 0, "heads": 1, "embedding": 1},
                    dict(LLAMA8B, embedding="?")):
            self.assertIsNone(mf.kv_cache_gb(bad, 32768), bad)
        self.assertIsNone(mf.kv_cache_gb(LLAMA8B, 0))


class ObservationIsAMeasurement(unittest.TestCase):
    def _rows(self, rows):
        return mock.patch("ava_bridge.models.probe_resident", lambda *a, **k: rows)

    def test_a_fully_resident_model_has_not_spilled(self):
        with self._rows([{"id": "m", "size_bytes": 6 * GIB, "vram_bytes": 6 * GIB}]):
            fp = mf.observed("http://x/v1", "ollama")["m"]
        self.assertEqual(fp.source, mf.OBSERVED)
        self.assertTrue(fp.measured)
        self.assertFalse(fp.spilled)

    def test_a_partly_offloaded_model_has(self):
        """THE case. It runs, 5-30x slower, and nothing else in Ava sees it."""
        with self._rows([{"id": "m", "size_bytes": 49 * GIB, "vram_bytes": 20 * GIB}]):
            self.assertTrue(mf.observed("http://x/v1", "ollama")["m"].spilled)

    def test_an_absent_vram_field_is_unknown_not_false(self):
        """Some builds omit size_vram rather than zeroing it. "We could not
        tell" must not render as "it was fine"."""
        with self._rows([{"id": "m", "size_bytes": 6 * GIB, "vram_bytes": None}]):
            self.assertIsNone(mf.observed("http://x/v1", "ollama")["m"].spilled)

    def test_an_observation_never_double_counts_the_kv_cache(self):
        """`/api/ps` size is the WHOLE resident footprint, cache included.
        Adding a computed one on top would reject models that fit."""
        with self._rows([{"id": "m", "size_bytes": 6 * GIB, "vram_bytes": 6 * GIB}]):
            fp = mf.observed("http://x/v1", "ollama")["m"]
        self.assertIsNone(fp.kv_gb)
        self.assertEqual(fp.overhead_gb, 0.0)
        self.assertAlmostEqual(fp.total_gb, fp.weight_gb, places=2)

    def test_an_engine_that_cannot_be_asked_yields_nothing(self):
        with mock.patch("ava_bridge.models.probe_resident", lambda *a, **k: None):
            self.assertEqual(mf.observed("http://x/v1", "vllm"), {})


class TheLadder(unittest.TestCase):
    """Highest available rung wins, and each rung says which it is."""

    def setUp(self):
        # Default: nothing remembered, nothing resident, nothing declared.
        for target, val in (("ava_bridge.footprint_store.get", None),
                            ("ava_bridge.model_footprint.declared", None)):
            p = mock.patch(target, lambda *a, **k: val)
            p.start()
            self.addCleanup(p.stop)

    def test_a_remembered_measurement_outranks_a_live_derivation(self):
        remembered = mf.Footprint(model="m", source=mf.OBSERVED, weight_gb=6.0,
                                  detail="measured earlier")
        with mock.patch("ava_bridge.footprint_store.get", lambda *a, **k: remembered), \
             mock.patch.object(mf, "derived", lambda *a, **k: 1 / 0):
            self.assertEqual(mf.resolve("m", "http://x/v1", "ollama").source, mf.OBSERVED)

    def test_declared_outranks_derived(self):
        d = mf.Footprint(model="m", source=mf.DECLARED, weight_gb=9.0, detail="yaml")
        with mock.patch.object(mf, "observed", lambda *a, **k: {}), \
             mock.patch.object(mf, "declared", lambda m: d), \
             mock.patch.object(mf, "derived", lambda *a, **k: 1 / 0):
            self.assertEqual(mf.resolve("m", "http://x/v1", "ollama").source, mf.DECLARED)

    def test_nothing_resolvable_is_unknown_not_a_guess(self):
        with mock.patch.object(mf, "observed", lambda *a, **k: {}), \
             mock.patch.object(mf, "derived", lambda *a, **k: None):
            fp = mf.resolve("m", "http://x/v1", "ollama")
        self.assertEqual(fp.source, mf.UNKNOWN)
        self.assertIsNone(fp.total_gb)

    def test_resolve_never_raises(self):
        """A footprint must not be able to take a model list down."""
        with mock.patch.object(mf, "observed", side_effect=RuntimeError("boom")), \
             mock.patch.object(mf, "derived", side_effect=RuntimeError("boom")):
            self.assertEqual(mf.resolve("m", "http://x/v1", "ollama").source, mf.UNKNOWN)

    def test_an_empty_model_name_is_answered_not_probed(self):
        self.assertEqual(mf.resolve("", "http://x/v1", "ollama").source, mf.UNKNOWN)


class TheAsymmetry(unittest.TestCase):
    """The contract every caller depends on."""

    def _verdict(self, fp, pool_gb):
        from ava_bridge import hwinfo
        pool = hwinfo.PoolInfo(total_gb=pool_gb, free_gb=pool_gb, kind="vram",
                               source="vram-nvml")
        with mock.patch.object(mf, "resolve", lambda *a, **k: fp), \
             mock.patch.object(hwinfo, "fit_pool", lambda: pool):
            return model_fit.verdict("m", "http://x/v1", "ollama")

    def test_weights_alone_over_the_pool_is_stated_plainly(self):
        fp = mf.Footprint(model="m", source=mf.DERIVED, weight_gb=47.0, detail="")
        self.assertEqual(self._verdict(fp, 16.0)["verdict"], model_fit.WONT_FIT)

    def test_an_observed_spill_beats_any_projection_of_one(self):
        fp = mf.Footprint(model="m", source=mf.OBSERVED, weight_gb=6.0,
                          spilled=True, detail="")
        v = self._verdict(fp, 16.0)
        self.assertEqual(v["verdict"], model_fit.WILL_SPILL)
        self.assertTrue(v["measured"])

    def test_weights_fit_but_the_working_set_does_not_is_a_spill(self):
        """Runs and is slow, rather than failing — which is why it needs saying."""
        fp = mf.Footprint(model="m", source=mf.DERIVED, weight_gb=12.0, kv_gb=6.0,
                          overhead_gb=0.8, detail="")
        self.assertEqual(self._verdict(fp, 16.0)["verdict"], model_fit.WILL_SPILL)

    def test_comfortable_is_should_fit_never_will_fit(self):
        fp = mf.Footprint(model="m", source=mf.DERIVED, weight_gb=4.9, kv_gb=4.0,
                          overhead_gb=0.8, detail="")
        v = self._verdict(fp, 24.0)
        self.assertEqual(v["verdict"], model_fit.SHOULD_FIT)
        self.assertGreater(v["headroom_gb"], 0)

    def test_no_footprint_is_unknown_not_should_fit(self):
        """The dangerous default. Absence of evidence must not read as fit."""
        fp = mf.Footprint(model="m", source=mf.UNKNOWN, detail="")
        self.assertEqual(self._verdict(fp, 24.0)["verdict"], model_fit.UNKNOWN_FIT)

    def test_no_pool_is_unknown_too(self):
        fp = mf.Footprint(model="m", source=mf.DERIVED, weight_gb=4.0, detail="")
        self.assertEqual(self._verdict(fp, None)["verdict"], model_fit.UNKNOWN_FIT)

    def test_verdict_never_raises(self):
        with mock.patch.object(mf, "resolve", side_effect=RuntimeError("boom")):
            self.assertEqual(model_fit.verdict("m")["verdict"], model_fit.UNKNOWN_FIT)


if __name__ == "__main__":
    unittest.main()
