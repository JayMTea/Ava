"""`agent.required` has to mean something, and `agent.runtime` has to be real.

Two silent config failures, both of which let the box run in a state the owner
believed they had ruled out:

  * **`agent.required: true` was void on the Direct runtime.** `gate()` tested
    `rt is not _direct and not rt.available()`, so an explicit
    `agent.runtime: direct` skipped the enforcement branch entirely and served
    tool-less chat with no error. `docs/AGENT_RUNTIME_REFERENCE.md` promises "a
    hard, actionable error at startup, in `ava doctor`, and on every chat turn".
    The two settings contradict each other, and the honest answer is to say so
    rather than pick a side quietly.
  * **An unrecognised `agent.runtime` fell back to NemoClaw with no signal.**
    The fallback is correct — a typo must not brick the box — but
    `/api/hub/agent/status` echoed the CONFIGURED string back, so the status
    screen agreed with the typo while a different runtime served every turn.

House style: stdlib unittest, no bridge, no sandbox, no network.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ava_bridge import runtime


class RequiredGateTests(unittest.TestCase):

    def _gate(self, *, runtime_name: str, required: bool, available: bool = True):
        with mock.patch("ava_bridge.config.AGENT_RUNTIME", runtime_name), \
             mock.patch("ava_bridge.config.AGENT_REQUIRED", required), \
             mock.patch.object(runtime.nemoclaw(), "available",
                               return_value=available):
            return runtime.gate()

    def test_explicit_direct_with_required_is_an_error(self):
        rt, err = self._gate(runtime_name="direct", required=True)
        self.assertIs(rt, runtime.direct())
        self.assertTrue(err, "agent.required was silently void on the Direct floor")
        self.assertIn("contradict", err)
        self.assertIn("agent.runtime", err)
        self.assertIn("agent.required", err)

    def test_the_none_alias_is_the_same_case(self):
        _rt, err = self._gate(runtime_name="none", required=True)
        self.assertTrue(err)

    def test_explicit_direct_without_required_is_fine(self):
        rt, err = self._gate(runtime_name="direct", required=False)
        self.assertIs(rt, runtime.direct())
        self.assertIsNone(err)

    def test_a_missing_nemoclaw_still_errors_when_required(self):
        """The case that always worked — pinned so the rewrite kept it."""
        rt, err = self._gate(runtime_name="nemoclaw", required=True, available=False)
        self.assertIs(rt, runtime.direct())
        self.assertIn("required", err)

    def test_a_missing_nemoclaw_falls_to_direct_when_not_required(self):
        rt, err = self._gate(runtime_name="nemoclaw", required=False, available=False)
        self.assertIs(rt, runtime.direct())
        self.assertIsNone(err)

    def test_a_working_nemoclaw_is_returned_unchanged(self):
        rt, err = self._gate(runtime_name="nemoclaw", required=True, available=True)
        self.assertIs(rt, runtime.nemoclaw())
        self.assertIsNone(err)

    def test_the_two_errors_are_distinguishable(self):
        """They have different fixes: one provisions, the other edits ava.yaml."""
        _rt, missing = self._gate(runtime_name="nemoclaw", required=True,
                                  available=False)
        _rt, contradiction = self._gate(runtime_name="direct", required=True)
        self.assertNotEqual(missing, contradiction)
        self.assertIn("provision", missing)
        self.assertNotIn("provision", contradiction)


class RuntimeNameTests(unittest.TestCase):

    def test_a_known_name_reports_no_error(self):
        for name in ("nemoclaw", "openclaw", "remote", "direct", "none"):
            with self.subTest(name=name), \
                 mock.patch("ava_bridge.config.AGENT_RUNTIME", name):
                self.assertIsNone(runtime.name_error())

    def test_case_and_whitespace_are_tolerated(self):
        with mock.patch("ava_bridge.config.AGENT_RUNTIME", "  NemoClaw \n"):
            self.assertIsNone(runtime.name_error())
            self.assertIs(runtime.configured(), runtime.nemoclaw())

    def test_a_typo_is_reported_and_names_the_alternatives(self):
        with mock.patch("ava_bridge.config.AGENT_RUNTIME", "nemocalw"):
            err = runtime.name_error()
            self.assertTrue(err)
            self.assertIn("nemocalw", err)
            self.assertIn("nemoclaw", err)

    def test_a_typo_still_falls_back_rather_than_bricking(self):
        """Loud, not fatal: the box must still answer."""
        with mock.patch("ava_bridge.config.AGENT_RUNTIME", "podman"):
            self.assertIs(runtime.configured(), runtime.nemoclaw())

    def test_the_status_payload_carries_both_errors(self):
        from ava_bridge.hub import agent as hub_agent

        with mock.patch("ava_bridge.config.AGENT_RUNTIME", "direct"), \
             mock.patch("ava_bridge.config.AGENT_REQUIRED", True):
            st = hub_agent.agent_status()
        self.assertIsNone(st["config_error"], "direct is a real runtime name")
        self.assertTrue(st["gate_error"],
                        "the panel echoes the configured runtime, so it has to "
                        "carry the reason that configuration cannot be honoured")


class BrainDerivationTests(unittest.TestCase):
    """"Which model does Ava think with" had four independent answers.

    `models.effective_brain()` is the resolver. `agent.which_model()` re-read
    `sandbox_info` itself, `setup_wizard.recommend_brain()` never asked at all,
    and `BrainPanel` reconstructed the decision client-side from
    `available && sandbox_model` across three endpoints. Four answers, nothing
    keeping them in step, and the symptom is two screens naming different models.
    """

    def test_the_status_payload_carries_the_resolvers_answer(self):
        from ava_bridge import models
        from ava_bridge.hub import agent as hub_agent

        with mock.patch.object(models, "effective_brain",
                               return_value={"source": "agent", "model_id": "m",
                                             "label": "M", "engine": "e",
                                             "implicit": False}):
            st = hub_agent.agent_status()
        self.assertEqual(st["brain"]["source"], "agent")
        self.assertEqual(st["brain"]["model"], "m")

    def test_a_broken_resolver_leaves_the_panel_a_fallback(self):
        from ava_bridge import models
        from ava_bridge.hub import agent as hub_agent

        with mock.patch.object(models, "effective_brain",
                               side_effect=RuntimeError("boom")):
            st = hub_agent.agent_status()
        self.assertIsNone(st["brain"],
                          "null lets the panel fall back to its own fields; a "
                          "missing key would read as 'no brain'")

    def test_which_model_reports_whether_it_observed_the_answer(self):
        """The router reports what it ACTUALLY proxied; the sandbox fallback is
        configuration. A caller must be able to tell them apart."""
        from ava_bridge import agent as agent_mod
        from ava_bridge import models

        with mock.patch.object(agent_mod.requests, "get",
                               side_effect=OSError("no router")), \
             mock.patch.object(models, "effective_brain",
                               return_value={"source": "agent", "model_id": "m",
                                             "label": "M"}):
            got = agent_mod.which_model()
        self.assertEqual(got["model"], "m")
        self.assertFalse(got["observed"],
                         "nothing measured this — it is what the sandbox is "
                         "configured with")

    def test_which_model_is_none_when_neither_source_knows(self):
        from ava_bridge import agent as agent_mod
        from ava_bridge import models

        with mock.patch.object(agent_mod.requests, "get",
                               side_effect=OSError("no router")), \
             mock.patch.object(models, "effective_brain",
                               return_value={"source": "configured", "model_id": "m"}):
            self.assertIsNone(agent_mod.which_model())

if __name__ == "__main__":
    unittest.main()
