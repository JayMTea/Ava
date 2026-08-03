"""Platform-aware onboarding on non-CUDA hardware (Apple Silicon, CPU-only).

The core regression these guard against: a high-RAM Mac (tier 'large') must never
be steered into the vLLM-only Nemotron default it cannot serve. Like test_hwinfo,
we can't run on real macOS here, so the platform is injected via
`hwinfo.platform_id` and the decision logic is what's asserted.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ava_cli
from ava_bridge import settings, setup_wizard


def _manifest():
    return dict(ava_cli._DEFAULT_MODELS)


class EngineServableTests(unittest.TestCase):
    def test_vllm_only_servable_on_cuda_platforms(self):
        for plat, want in [("linux-nvidia", True), ("windows-nvidia", True),
                           ("darwin-apple", False), ("linux-cpu", False),
                           ("generic", False)]:
            with mock.patch("ava_bridge.hwinfo.platform_id", return_value=plat):
                self.assertEqual(ava_cli._engine_servable_here("vllm"), want, plat)

    def test_local_engines_servable_anywhere(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            for eng in ("ollama", "llamacpp", "mlx", "lmstudio", "openai"):
                self.assertTrue(ava_cli._engine_servable_here(eng), eng)


class ResolveAutoTests(unittest.TestCase):
    def test_nvidia_keeps_the_vllm_brain(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-nvidia"):
            role, spec, note = ava_cli._resolve_auto("large", _manifest())
        self.assertEqual(role, "chat")
        self.assertEqual(spec["engine"], "vllm")
        self.assertIsNone(note)

    def test_apple_large_substitutes_ollama_never_vllm(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, note = ava_cli._resolve_auto("large", _manifest())
        self.assertEqual(spec["engine"], "ollama")
        self.assertNotEqual(spec["engine"], "vllm")
        self.assertEqual(spec["id"], "llama3.1:70b")
        self.assertIn("can't be served", note)

    def test_apple_medium_substitutes_smaller_ollama(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, note = ava_cli._resolve_auto("medium", _manifest())
        self.assertEqual(spec["engine"], "ollama")
        self.assertEqual(spec["id"], "llama3.1:8b")

    def test_apple_small_uses_fast_role_no_note(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, note = ava_cli._resolve_auto("small", _manifest())
        self.assertEqual(role, "fast")
        self.assertEqual(spec["engine"], "ollama")
        self.assertIsNone(note)

    def test_cpu_only_linux_also_protected(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-cpu"):
            _role, spec, _note = ava_cli._resolve_auto("large", _manifest())
        self.assertEqual(spec["engine"], "ollama")

    def test_cloud_tier_yields_no_local_role(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, _note = ava_cli._resolve_auto("cloud", _manifest())
        self.assertIsNone(role)
        self.assertIsNone(spec)


class InferenceReseedTests(unittest.TestCase):
    """Which boxes get their inference block rewritten away from the vLLM default.

    Gating this on the platform (the original rule) silently missed small NVIDIA
    GPUs: `_resolve_auto` downshifts them to the Ollama 'fast' model, so `models
    pull --auto` fetched that while ava.yaml still named the full-size vLLM
    default — one model on disk, a different dead endpoint configured.
    """

    def test_big_nvidia_keeps_the_shipped_default(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-nvidia"):
            spec, why = ava_cli._inference_reseed(_manifest(), "large")
        self.assertIsNone(spec)
        self.assertIsNone(why)

    def test_small_nvidia_is_reseeded_to_what_it_downloads(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-nvidia"):
            spec, why = ava_cli._inference_reseed(_manifest(), "small")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["engine"], "ollama")
        # the reason must name size, not servability — vLLM does run on this box
        self.assertIn("too large", why)

    def test_apple_reseeded_for_servability_not_size(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            spec, why = ava_cli._inference_reseed(_manifest(), "large")
        self.assertEqual(spec["engine"], "ollama")
        self.assertIn("won't run here", why)

    def test_cloud_tier_warns_instead_of_seeding(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            spec, why = ava_cli._inference_reseed(_manifest(), "cloud")
        self.assertIsNone(spec)
        self.assertIn("cloud backend", why)

    def test_undetectable_hardware_changes_nothing(self):
        spec, why = ava_cli._inference_reseed(_manifest(), None)
        self.assertIsNone(spec)
        self.assertIsNone(why)


class SeedInferenceBackendTests(unittest.TestCase):
    def test_rewrites_vllm_default_to_clean_ollama_block(self):
        seed = ("inference:\n  primary: local\n  backends:\n"
                "    local:\n      engine: vllm\n      model: nvidia/whatever\n")
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "ava.yaml"
            cfg.write_text(seed, encoding="utf-8")
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                ok = ava_cli._seed_inference_backend(
                    {"engine": "ollama", "id": "llama3.1:70b", "tier": "large"})
            self.assertTrue(ok)
            import yaml
            out = yaml.safe_load(cfg.read_text())
        self.assertEqual(out["inference"]["primary"], "local-ollama")
        # the dead vLLM backend must be gone, not just deprioritised. Asserted as
        # an exact key set: `local` is a prefix of `local-ollama`, so a bare
        # assertNotIn would read as a substring check it isn't.
        self.assertEqual(set(out["inference"]["backends"]), {"local-ollama"})
        b = out["inference"]["backends"]["local-ollama"]
        self.assertEqual(b["engine"], "ollama")
        self.assertEqual(b["model"], "llama3.1:70b")
        self.assertEqual(b["base_url"], "http://127.0.0.1:11434/v1")


class WizardHardwareNoteTests(unittest.TestCase):
    def test_apple_hardware_probe_flags_engine_and_telemetry(self):
        import types
        mem = types.SimpleNamespace(total_gb=256.0, free_gb=210.0,
                                    source="system-psutil", readable=True)
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"), \
             mock.patch("ava_bridge.hwinfo.fit_memory", return_value=mem):
            out = setup_wizard.api_hardware()
        self.assertEqual(out["platform"], "darwin-apple")
        self.assertEqual(out["tier"], "large")
        self.assertIn("30B", out["hint"])         # a large-tier model is offered
        # The route reports the SITUATION; the sentence about unified memory and
        # why not vLLM is the frontend's, and differs between the first-run
        # wizard and Setup → Hardware on purpose (CLAUDE.md: backend returns
        # facts, owner-facing copy lives in the frontend).
        self.assertEqual(out["note_code"], "apple-silicon")


if __name__ == "__main__":
    unittest.main()
