import unittest
from unittest import mock

from ava_bridge import model_fit

# Ava's real prod inference config is a SINGLE always-on backend: the open-model
# 30B brain (OMNI below). The fit engine itself is generic, though — a fork can
# declare more than one local backend (e.g. the Mac two-model example in
# config.example.yaml). SMALL is a synthetic second local backend used here only
# to exercise the engine's tier / workload / memory-shedding logic.
OMNI = {"id": "omni", "label": "open-model 30B",
        "model": "nvidia/Nemotron-Open-30B-A3B-Reasoning-NVFP4",
        "engine": "vllm", "url": "http://127.0.0.1:8002/v1",
        "fit": {"weight_gb": 35, "tier": "large", "min_free_gb": 10,
                "workloads": ["chat", "reasoning", "code", "vision", "audio",
                              "fast", "cheap", "utility", "classify"]}}
SMALL = {"id": "small", "label": "Small local", "model": "small-local",
         "engine": "vllm", "url": "http://127.0.0.1:8001/v1",
         "fit": {"weight_gb": 9, "tier": "small", "min_free_gb": 3,
                 "workloads": ["fast", "cheap", "utility", "classify", "chat"]}}
BACKENDS = [OMNI, SMALL]


class ProfileTests(unittest.TestCase):
    def test_missing_fit_block_yields_permissive_default(self):
        prof = model_fit.profile_for({"id": "x"})
        self.assertEqual(prof.tier, "large")
        self.assertTrue(prof.handles("anything"))  # no tags => handles all
        self.assertIsNone(prof.weight_gb)

    def test_bad_tier_falls_back_to_large(self):
        self.assertEqual(model_fit.profile_for({"id": "x", "fit": {"tier": "huge"}}).tier, "large")

    def test_omni_handles_every_workload(self):
        prof = model_fit.profile_for(OMNI)
        for wl in ("chat", "reasoning", "code", "vision", "audio", "fast"):
            self.assertTrue(prof.handles(wl))


class FitTests(unittest.TestCase):
    def test_unknown_memory_never_gates(self):
        ok, _ = model_fit.fits_now(model_fit.profile_for(OMNI), free_gb=None)
        self.assertTrue(ok)

    def test_serving_checks_only_headroom_not_full_weight(self):
        prof = model_fit.profile_for(OMNI)  # min_free_gb=10, weight_gb=35
        # 10 GiB free: enough headroom to keep serving a pre-loaded engine...
        self.assertTrue(model_fit.fits_now(prof, 10, assume_loaded=True)[0])
        # ...but NOT enough to cold-load its 35 GiB weight.
        self.assertFalse(model_fit.fits_now(prof, 10, assume_loaded=False)[0])

    def test_pressure_below_headroom_sheds(self):
        prof = model_fit.profile_for(OMNI)  # needs 10 GiB headroom
        self.assertFalse(model_fit.fits_now(prof, 5, assume_loaded=True)[0])


class SelectTests(unittest.TestCase):
    def test_code_workload_prefers_large_brain(self):
        order = model_fit.select(BACKENDS, workload="code", free_gb=60)
        self.assertEqual(order[0].backend["id"], "omni")

    def test_fast_workload_prefers_small_brain(self):
        order = model_fit.select(BACKENDS, workload="fast", free_gb=60)
        self.assertEqual(order[0].backend["id"], "small")

    def test_memory_pressure_sheds_off_big_brain(self):
        # Only 5 GiB free: Omni needs 10 headroom (sheds), Small needs 3 (fits).
        # Even for a chat workload Omni would normally win, fit dominates.
        order = model_fit.select(BACKENDS, workload="chat", free_gb=5)
        self.assertEqual(order[0].backend["id"], "small")
        self.assertTrue(order[0].fits)  # small fits
        shed = [c for c in order if c.backend["id"] == "omni"][0]
        self.assertFalse(shed.fits)

    def test_off_workload_model_still_ordered_last_not_dropped(self):
        # 'vision' is only handled by omni; small still appears as a fallback.
        order = model_fit.select(BACKENDS, workload="vision", free_gb=60)
        self.assertEqual([c.backend["id"] for c in order], ["omni", "small"])

    def test_single_backend_config_just_serves(self):
        # Ava's real prod shape: one backend, no selection to do.
        order = model_fit.select([OMNI], workload="chat", free_gb=60)
        self.assertEqual(len(order), 1)
        self.assertEqual(order[0].backend["id"], "omni")
        self.assertTrue(order[0].fits)

    def test_manual_primary_breaks_ties(self):
        # No workload, both fit: tier still leads, but the call must not error and
        # returns every backend in a stable order.
        order = model_fit.select(BACKENDS, primary_id="small", free_gb=60)
        self.assertEqual(len(order), 2)


class PortabilityTests(unittest.TestCase):
    """A second user's hardware differs from the GB10 dev box — verify it adapts."""

    def test_cloud_backend_is_never_memory_gated(self):
        cloud = {"id": "gpt", "engine": "openai", "api_key": "sk-x",
                 "url": "https://api.openai.com/v1",
                 "fit": {"tier": "large", "min_free_gb": 999}}
        prof = model_fit.profile_for(cloud)
        self.assertFalse(prof.local)
        # Even with 0 GiB free locally and an absurd min_free_gb, a cloud model fits.
        self.assertTrue(model_fit.fits_now(prof, free_gb=0)[0])

    def test_openrouter_via_bearer_key_detected_remote(self):
        b = {"id": "or", "engine": "openai", "api_key": "sk-or",
             "url": "https://openrouter.ai/api/v1"}
        self.assertFalse(model_fit.profile_for(b).local)

    def test_local_vllm_detected_local_and_gated(self):
        b = {"id": "v", "engine": "vllm", "url": "http://127.0.0.1:8002/v1",
             "fit": {"min_free_gb": 8}}
        prof = model_fit.profile_for(b)
        self.assertTrue(prof.local)
        self.assertFalse(model_fit.fits_now(prof, free_gb=5)[0])  # under headroom

    def test_lan_host_without_key_is_local(self):
        b = {"id": "lan", "engine": "vllm", "url": "http://127.0.0.1:9000/v1"}
        self.assertTrue(model_fit.profile_for(b).local)

    def test_explicit_fit_local_flag_overrides(self):
        b = {"id": "x", "engine": "vllm", "url": "http://127.0.0.1/v1",
             "fit": {"local": False}}
        self.assertFalse(model_fit.profile_for(b).local)

    def test_unknown_memory_degrades_to_no_gating(self):
        # When the HAL can read no memory (no GPU + non-Linux), local backends
        # still fit — ordering falls back to workload/tier only, never blocks.
        prof = model_fit.profile_for({"id": "v", "engine": "vllm",
                                      "url": "http://127.0.0.1/v1"})
        self.assertTrue(model_fit.fits_now(prof, free_gb=None)[0])
        # Simulate "memory unknown": free_mem_gb() itself returns None.
        with mock.patch.object(model_fit, "free_mem_gb", return_value=None):
            order = model_fit.select(BACKENDS, workload="code")
        self.assertEqual(order[0].backend["id"], "omni")  # workload/tier decides


class ReportTests(unittest.TestCase):
    def test_fit_report_shape(self):
        rep = model_fit.fit_report(BACKENDS)
        self.assertIn("backends", rep)
        self.assertEqual(len(rep["backends"]), 2)
        ids = {r["id"] for r in rep["backends"]}
        self.assertEqual(ids, {"omni", "small"})
        for row in rep["backends"]:
            self.assertIn("serve_ok", row)
            self.assertIn("cold_load_ok", row)


if __name__ == "__main__":
    unittest.main()
