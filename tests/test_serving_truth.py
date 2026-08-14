"""What the engine is actually serving, against what the config asked for.

This is the fact that did not exist during the failure it was written for. From
2026-08-01 to 2026-08-13 a router process served the model id
`Qwen/Qwen2.5-7B-Instruct` while its engine held a Nemotron; every completion
404'd; and the hardware panel showed a green, correctly-named brain the whole
time, because the panel reads the ENGINE and the router read the CONFIG and
nothing in the tree could hold the two answers side by side. `hardware.py`
computed the comparison inline and discarded it.

The tests split into two halves, and the second matters more than the first:

  * `drifted` is reported when — and only when — the engine ANSWERED with a list
    that does not contain the configured model. That is a contradiction.
  * every other way of not knowing (down, still loading, someone else's box, the
    agent sandbox holding its own endpoint) gets its own verdict and must NEVER
    come back as `drifted`. A watchdog that pages on silence is one people turn
    off, and silence is not evidence of a mismatch.

Run: .venv/bin/python -m pytest tests/test_serving_truth.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-truth-test-")

from ava_bridge import models


def _brain(**over):
    """A configured, local vLLM brain — the ordinary shape."""
    b = {"source": "configured", "backend_id": "omni",
         "model_id": "acme/Cool-LLM-7B-FP8", "label": "Cool LLM",
         "engine": "vllm", "base_url": "http://127.0.0.1:8002/v1",
         "api_key": "", "local": True, "implicit": False}
    b.update(over)
    return b


class AgreementTests(unittest.TestCase):
    def test_the_engine_holding_the_configured_model_agrees(self):
        t = models.serving_truth(_brain(), served=["acme/Cool-LLM-7B-FP8"],
                                 reachable=True)
        self.assertEqual(t["verdict"], "agrees")
        self.assertEqual(t["matched"], "acme/Cool-LLM-7B-FP8")
        self.assertTrue(t["observed"])

    def test_a_bare_ollama_tag_agrees_with_its_latest_form(self):
        """The one forgiving direction `match_served` allows, kept here.

        `ollama pull llama3.2` stores `llama3.2:latest`. Calling that drift would
        raise a critical alert nightly on a correct install.
        """
        t = models.serving_truth(_brain(model_id="llama3.2", engine="ollama"),
                                 served=["llama3.2:latest"], reachable=True)
        self.assertEqual(t["verdict"], "agrees")
        # The ENGINE's spelling comes back, so a caller displays what it will accept.
        self.assertEqual(t["matched"], "llama3.2:latest")


class DriftTests(unittest.TestCase):
    def test_an_engine_serving_something_else_is_drift(self):
        """The twelve-day failure, as one assertion."""
        t = models.serving_truth(_brain(), served=["nvidia/Other-LLM-30B"],
                                 reachable=True)
        self.assertEqual(t["verdict"], "drifted")
        self.assertEqual(t["want"], "acme/Cool-LLM-7B-FP8")
        self.assertEqual(t["serving"], ["nvidia/Other-LLM-30B"])
        self.assertEqual(t["matched"], "")

    def test_the_detail_names_both_sides(self):
        """Neither id alone is actionable: the owner has to see the pair."""
        t = models.serving_truth(_brain(), served=["nvidia/Other-LLM-30B"],
                                 reachable=True)
        self.assertIn("acme/Cool-LLM-7B-FP8", t["detail"])
        self.assertIn("nvidia/Other-LLM-30B", t["detail"])


class AgentMismatchTests(unittest.TestCase):
    """The sandbox names one model; Ava's backend serves another.

    Real, 2026-08-13: the brain moved from the FP8 build to NVFP4, but the agent
    sandbox bakes its model into the container at `nemoclaw onboard` time
    (NEMOCLAW_MODEL), so it went on naming the old id. Nothing failed — the
    sandbox's upstream runs through Ava's router, which rewrites the model —
    but the hardware panel named a model that was not being served and filed the
    engine that WAS serving under "Ava's other engines".

    Both ids are configuration Ava can read, so this is observable even though
    the engine behind the sandbox is not. It is `mismatched`, never `drifted`:
    turns succeed, so calling it critical would be crying wolf.
    """

    def test_a_stale_sandbox_model_is_mismatched(self):
        with mock.patch.object(models, "_configured_backend_model",
                               return_value="acme/New-LLM-NVFP4"):
            t = models.serving_truth(_brain(source="agent", base_url="",
                                            model_id="acme/Old-LLM-FP8"))
        self.assertEqual(t["verdict"], "mismatched")
        self.assertEqual(t["want"], "acme/Old-LLM-FP8")
        self.assertEqual(t["serving"], ["acme/New-LLM-NVFP4"])

    def test_the_detail_names_both_sides(self):
        with mock.patch.object(models, "_configured_backend_model",
                               return_value="acme/New-LLM-NVFP4"):
            t = models.serving_truth(_brain(source="agent", base_url="",
                                            model_id="acme/Old-LLM-FP8"))
        self.assertIn("acme/Old-LLM-FP8", t["detail"])
        self.assertIn("acme/New-LLM-NVFP4", t["detail"])

    def test_an_agreeing_sandbox_is_unobservable_not_agrees(self):
        """Matching names are not an observation of the engine.

        We still have not looked inside the sandbox, and claiming `agrees` would
        manufacture exactly the reassurance this module exists to stop.
        """
        with mock.patch.object(models, "_configured_backend_model",
                               return_value="acme/Same-LLM"):
            t = models.serving_truth(_brain(source="agent", base_url="",
                                            model_id="acme/Same-LLM"))
        self.assertEqual(t["verdict"], "unobservable")

    def test_an_unknown_sandbox_model_is_not_a_mismatch(self):
        """A sandbox that is down reports no model. Absence is not disagreement."""
        with mock.patch.object(models, "_configured_backend_model",
                               return_value="acme/New-LLM-NVFP4"):
            t = models.serving_truth(_brain(source="agent", base_url="",
                                            model_id=""))
        self.assertEqual(t["verdict"], "unobservable")

    def test_no_configured_backend_is_not_a_mismatch(self):
        """Nothing to disagree WITH on an install that declares no backend."""
        with mock.patch.object(models, "_configured_backend_model", return_value=""):
            t = models.serving_truth(_brain(source="agent", base_url="",
                                            model_id="acme/Old-LLM-FP8"))
        self.assertEqual(t["verdict"], "unobservable")

    def test_the_sandbox_is_never_probed(self):
        """Ava does not hold the sandbox's endpoint; this is a config comparison."""
        with mock.patch.object(models, "_configured_backend_model",
                               return_value="acme/New-LLM-NVFP4"), \
             mock.patch.object(models, "probe_serving") as probe:
            models.serving_truth(_brain(source="agent", base_url="",
                                        model_id="acme/Old-LLM-FP8"))
        probe.assert_not_called()


class NotKnowingIsNotDriftTests(unittest.TestCase):
    """Four ways of not knowing. None of them is a contradiction."""

    def test_an_unreachable_engine_is_unreachable(self):
        t = models.serving_truth(_brain(), served=[], reachable=False)
        self.assertEqual(t["verdict"], "unreachable")
        self.assertFalse(t["observed"])

    def test_an_engine_that_lists_nothing_is_unobservable(self):
        """A vLLM still loading its weights lists nothing, and so does an empty
        Ollama store. Neither has said the configured model is absent."""
        t = models.serving_truth(_brain(), served=[], reachable=True)
        self.assertEqual(t["verdict"], "unobservable")

    def test_a_remote_brain_is_elsewhere_and_is_never_probed(self):
        """An API key must not leave the box on a 2s panel timer."""
        with mock.patch.object(models, "probe_serving") as probe:
            t = models.serving_truth(_brain(local=False,
                                            base_url="https://api.example/v1"))
        self.assertEqual(t["verdict"], "elsewhere")
        probe.assert_not_called()

    def test_the_agent_sandbox_is_unobservable_not_drifted(self):
        """The sandbox answers turns itself and holds its own endpoint.

        Probing Ava's router here would compare the config against an engine that
        is not the one answering — a fabricated agreement, or a fabricated drift.
        """
        with mock.patch.object(models, "probe_serving") as probe:
            t = models.serving_truth(_brain(source="agent", base_url="",
                                            backend_id=""))
        self.assertEqual(t["verdict"], "unobservable")
        probe.assert_not_called()

    def test_no_brain_at_all_is_unconfigured(self):
        t = models.serving_truth({"source": "none", "model_id": "", "base_url": "",
                                  "local": False})
        self.assertEqual(t["verdict"], "unconfigured")

    def test_no_verdict_outside_the_declared_vocabulary(self):
        """The vocabulary is closed and mirrored in the frontend."""
        cases = [
            models.serving_truth(_brain(), served=["acme/Cool-LLM-7B-FP8"],
                                 reachable=True),
            models.serving_truth(_brain(), served=["x"], reachable=True),
            models.serving_truth(_brain(), served=[], reachable=False),
            models.serving_truth(_brain(), served=[], reachable=True),
            models.serving_truth(_brain(local=False)),
            models.serving_truth({"source": "none"}),
        ]
        for t in cases:
            self.assertIn(t["verdict"], models.TRUTHS)


class HotPathTests(unittest.TestCase):
    def test_supplied_observations_are_not_re_probed(self):
        """`/api/hardware` polls every 2s per client and probes already.

        A second probe in here would double that load for an answer it was just
        handed.
        """
        with mock.patch.object(models, "probe_serving") as probe:
            models.serving_truth(_brain(), served=["acme/Cool-LLM-7B-FP8"],
                                 reachable=True)
        probe.assert_not_called()

    def test_it_probes_when_nothing_was_supplied(self):
        with mock.patch.object(models, "probe_serving",
                               return_value=(True, ["acme/Cool-LLM-7B-FP8"])) as p:
            t = models.serving_truth(_brain())
        p.assert_called_once()
        self.assertEqual(t["verdict"], "agrees")


if __name__ == "__main__":
    unittest.main()
