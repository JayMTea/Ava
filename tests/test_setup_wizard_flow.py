"""The first-run wizard must not lie, and must not lock you out.

Three defects, all on the same screen:

  * `setup_completed()` returned True whenever `inference.backends` was
    non-empty. `ava setup` copied config.example.yaml, which ships a live
    `inference.backends.local` — so onboarding counted as finished the moment
    setup ran, and every CLI-installed user had the wizard silently skipped
    without ever seeing it.
  * `api_save` set `{"setup": {"completed": True}}` as its FIRST statement, and
    the local branch was `if base and model:` with no else. A blank model field
    therefore returned {"ok": true}, marked onboarding complete, and wrote no
    backend. Reproduced before the fix.
  * `/setup/wizard` then 303'd away because setup was "complete" — so the only
    screen that could fix it was unreachable. That combination is what turned an
    empty field into a dead install.
  * `/api/setup/backends` probed hardcoded 127.0.0.1:8002 and :11434. Inside the
    Docker image that is the CONTAINER's loopback; the compose engines are at
    vllm:8002 / ollama:11434 on the compose network. Every Docker first-run
    therefore reported "no local engine" and preselected cloud.
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("AVA_HOME", tempfile.mkdtemp(prefix="ava-wizard-test-"))

from ava_bridge import settings, setup_wizard  # noqa: E402


class _Body:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _save(payload):
    return asyncio.run(setup_wizard.api_save(_Body(payload)))


def _status(resp):
    return getattr(resp, "status_code", 200)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ava-wiz-")
        self._p = mock.patch.object(settings, "CONFIG_PATH",
                                    settings.Path(os.path.join(self.dir, "ava.yaml")))
        self._p.start()
        self._cfg, self._err = settings._CFG, settings._CFG_ERROR
        settings._CFG, settings._CFG_ERROR = {}, ""
        self.addCleanup(self._restore)

    def _restore(self):
        self._p.stop()
        settings._CFG, settings._CFG_ERROR = self._cfg, self._err


class CompletionFlagTests(_Tmp):
    def test_a_declared_backend_alone_does_not_mean_onboarded(self):
        settings._CFG = {"inference": {"backends": {"local": {"model": "x"}}}}
        self.assertFalse(setup_wizard.setup_completed(), (
            "a backend in ava.yaml used to count as 'onboarded', which is why "
            "the template's own inference block skipped the wizard for everyone"))

    def test_the_flag_is_what_counts(self):
        settings._CFG = {"setup": {"completed": True}}
        self.assertTrue(setup_wizard.setup_completed())

    def test_a_fresh_config_is_not_completed(self):
        settings._CFG = {}
        self.assertFalse(setup_wizard.setup_completed())


class SaveValidationTests(_Tmp):
    def test_a_blank_local_model_is_refused_and_nothing_is_claimed(self):
        r = _save({"inference": {"mode": "local", "engine": "ollama",
                                 "base_url": "http://127.0.0.1:11434/v1",
                                 "model": ""}})
        self.assertEqual(_status(r), 400)
        self.assertIsNone(settings.get("setup.completed"))
        self.assertIsNone(settings.get("inference"))

    def test_a_blank_local_base_url_is_refused(self):
        r = _save({"inference": {"mode": "local", "model": "llama3.2",
                                 "base_url": ""}})
        self.assertEqual(_status(r), 400)

    def test_a_blank_cloud_field_is_refused(self):
        for payload in ({"base_url": "", "model": "gpt-4o-mini"},
                        {"base_url": "https://api.openai.com/v1", "model": ""}):
            r = _save({"inference": {"mode": "cloud", **payload}})
            self.assertEqual(_status(r), 400, payload)

    def test_the_error_names_the_field_so_the_ui_can_focus_it(self):
        r = _save({"inference": {"mode": "local",
                                 "base_url": "http://x/v1", "model": ""}})
        self.assertIn(b'"field":"model"', bytes(r.body))

    def test_a_complete_local_save_writes_the_backend_and_completes(self):
        r = _save({"inference": {"mode": "local", "engine": "ollama",
                                 "base_url": "http://127.0.0.1:11434/v1",
                                 "model": "llama3.2"}})
        self.assertEqual(_status(r), 200)
        self.assertTrue(settings.get("setup.completed"))
        be = settings.get("inference.backends")["local"]
        self.assertEqual(be["model"], "llama3.2")
        self.assertEqual(be["engine"], "ollama")

    def test_skip_saves_preferences_without_claiming_completion(self):
        """Without this a user with no model yet had to either lie or abandon
        the page — and abandoning left setup half-applied."""
        r = _save({"skip_inference": True, "features": {"voice": True}})
        self.assertEqual(_status(r), 200)
        self.assertIsNone(settings.get("setup.completed"))
        self.assertTrue(settings.get("features.voice"))

    def test_only_registered_features_are_written(self):
        _save({"skip_inference": True,
               "features": {"voice": True, "not_a_feature": True}})
        self.assertNotIn("not_a_feature", settings.get("features") or {})

    def test_a_broken_config_is_a_409_not_a_500(self):
        with open(settings.CONFIG_PATH, "w", encoding="utf-8") as fh:
            fh.write("a:\n  b: 1\n c: 2\n")
        settings._CFG, settings._CFG_ERROR = settings._load_config()
        r = _save({"inference": {"mode": "local", "base_url": "http://x/v1",
                                 "model": "m"}})
        self.assertEqual(_status(r), 409)


class BackendProbeTests(_Tmp):
    def test_candidates_include_the_compose_service_names(self):
        """The old probe only knew container loopback, where compose engines
        never are."""
        bases = [c["base_url"] for c in setup_wizard._candidates()]
        self.assertIn("http://vllm:8002/v1", bases)
        self.assertIn("http://ollama:11434/v1", bases)

    def test_a_configured_backend_is_probed_first(self):
        settings._CFG = {"inference": {"backends": {
            "mine": {"base_url": "http://elsewhere:9000/v1", "engine": "vllm"}}}}
        cands = setup_wizard._candidates()
        self.assertEqual(cands[0]["base_url"], "http://elsewhere:9000/v1")

    def test_the_environment_backend_is_a_candidate(self):
        with mock.patch.dict(os.environ, {"AVA_BACKEND_URL": "http://env-host:1234/v1"}):
            bases = [c["base_url"] for c in setup_wizard._candidates()]
            self.assertIn("http://env-host:1234/v1", bases)

    def test_ollama_health_is_not_probed_under_v1(self):
        """Ollama's OpenAI-compatible base ends in /v1; its health endpoint does
        not live there, so probing base + /models reported it down."""
        self.assertEqual(
            setup_wizard._health_url("http://ollama:11434/v1", "ollama"),
            "http://ollama:11434/api/tags")

    def test_vllm_health_is_the_models_list(self):
        self.assertEqual(
            setup_wizard._health_url("http://vllm:8002/v1", "vllm"),
            "http://vllm:8002/v1/models")

    def test_candidates_are_deduplicated(self):
        bases = [c["base_url"] for c in setup_wizard._candidates()]
        self.assertEqual(len(bases), len(set(bases)))


if __name__ == "__main__":
    unittest.main()
