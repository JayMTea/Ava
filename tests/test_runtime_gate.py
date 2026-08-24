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

    def _gate(self, *, runtime_name: str, required: bool, available: bool = True,
              enabled: bool = True):
        """`enabled` is pinned, not inherited.

        `gate()` reads the agent's feature switch to tell `agent_off` from
        `agent_down`, and that switch is a real environment variable
        (AVA_AGENT_ENABLED) which the QA tier sets to 0 for its own reasons. A
        test whose verdict depends on the ambient environment passes alone and
        fails beside another suite, which is the least useful kind of failure.
        """
        from ava_bridge import features
        with mock.patch("ava_bridge.config.AGENT_RUNTIME", runtime_name), \
             mock.patch("ava_bridge.config.AGENT_REQUIRED", required), \
             mock.patch.object(features, "enabled",
                               side_effect=lambda k: enabled if k == "agent"
                               else True), \
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

    # ---- the codes ------------------------------------------------------- #
    # The agent runtime is the largest optional capability Ava has, and it was
    # the one outside the feature registry: its gate emitted prose with no code,
    # so a chat turn that failed because the agent was off gave the owner an
    # accurate sentence and nowhere to go — while a failed web search, a far
    # smaller thing, got a guided fix link.

    def test_a_missing_runtime_is_agent_down(self):
        _rt, err = self._gate(runtime_name="nemoclaw", required=True,
                              available=False)
        self.assertEqual(getattr(err, "code", ""), "agent_down")

    def test_the_contradiction_has_its_own_code(self):
        _rt, err = self._gate(runtime_name="direct", required=True)
        self.assertEqual(getattr(err, "code", ""), "agent_conflict")

    def test_a_deliberately_disabled_agent_reads_off_not_down(self):
        """Different problems, different fixes: a switch to flip vs a runtime to
        provision. Collapsing them would send the owner to install something they
        already have."""
        _rt, err = self._gate(runtime_name="nemoclaw", required=True,
                              available=False, enabled=False)
        self.assertEqual(getattr(err, "code", ""), "agent_off")
        self.assertIn("required", err, "the conflict with agent.required must "
                                       "still be named — being off is only half "
                                       "the story when it is also required")

    def test_the_error_is_still_a_plain_string(self):
        """Every existing caller does `rt, err = gate()` and formats `err`. The
        code rides along; it does not change what the value IS."""
        _rt, err = self._gate(runtime_name="direct", required=True)
        self.assertIsInstance(err, str)
        self.assertTrue(f"{err}".startswith("agent.required"))

    def test_the_code_reaches_the_turn(self):
        """`/api/turn/<id>` returns the turn dict verbatim, and fixes.ts resolves
        the link from `error_code` — so the code has to be ON the turn, not just
        in the gate's return value."""
        from ava_bridge import state, turns

        tid = "t-gate"
        state.turns[tid] = {"status": "running"}
        try:
            with mock.patch.object(runtime, "gate",
                                   return_value=(runtime.direct(),
                                                 runtime.GateError("nope", "agent_down"))):
                turns._run_turn(tid, "hi", "s1", "")
            self.assertEqual(state.turns[tid]["status"], "error")
            self.assertEqual(state.turns[tid]["error_code"], "agent_down")
        finally:
            state.turns.pop(tid, None)


class ServingRuntimeTests(unittest.TestCase):
    """The runtime `gate()` returned is the runtime that serves the turn.

    `agent.ask_openclaw()` hardcoded `runtime.nemoclaw().run_turn(...)`, so on
    `agent.runtime: remote` the gate admitted RemoteRuntime and the reply then
    came from the in-process CLI adapter — while `sbx_read`/`session_file`
    (live chain-of-thought) *did* route through `configured()`. The reasoning
    and the reply came from two different machines, and nothing said so.

    Two guards, because the bug had two halves: a facade that pinned a runtime,
    and a caller that used it instead of the one it had already resolved.
    """

    def test_the_turn_runs_on_the_gated_runtime(self):
        """A behavioural check, not a scan: whatever `gate()` hands back is what
        `_run_turn` must call, even when it is not the local NemoClaw adapter."""
        from ava_bridge import state, turns

        served: list[str] = []

        # A real AgentRuntime subclass, so it inherits the seam's actual
        # defaults (supports_push_turns() -> False) rather than a hand-written
        # approximation of them that could drift from the ABC.
        from ava_bridge.runtime.base import AgentRuntime

        class _Fake(AgentRuntime):
            name = "fake"
            supports_tools = True
            supports_cot = True

            def available(self):
                return True

            def run_turn(self, text, session_id=None, history=None):
                served.append(session_id or "")
                return "ok", []

        tid = "t-serve"
        state.turns[tid] = {"status": "running"}
        try:
            with mock.patch.object(runtime, "gate", return_value=(_Fake(), None)), \
                 mock.patch.object(turns, "_session_line_count", return_value=0), \
                 mock.patch.object(turns, "_poll_turn_steps", lambda *a, **k: None), \
                 mock.patch.object(turns, "_tools_from_session", return_value=[]), \
                 mock.patch.object(turns, "_read_session_steps", return_value=[]), \
                 mock.patch.object(turns, "which_model", return_value=None), \
                 mock.patch.object(turns, "build_turn_artifact", return_value=None):
                turns._run_turn(tid, "hi", "s1", "")
            self.assertEqual(state.turns[tid]["status"], "done")
            self.assertEqual(state.turns[tid]["reply"], "ok")
            self.assertEqual(served, ["s1"],
                             "the turn did not run on the runtime gate() returned")
        finally:
            state.turns.pop(tid, None)

    def test_the_agent_facade_pins_no_runtime(self):
        """`ava_bridge/agent.py` is a facade over the seam. It may resolve
        `gate()`, `active()`, `configured()` or `direct()` — each of which names
        a ROLE — but never a concrete adapter, which names a machine."""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "ava_bridge"
               / "agent.py").read_text(encoding="utf-8")
        calls = {c.func.attr for c in ast.walk(ast.parse(src))
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                 and isinstance(c.func.value, ast.Name)
                 and c.func.value.id == "runtime"}
        self.assertNotIn("nemoclaw", calls,
                         "agent.py resolves a concrete adapter. Use gate() / "
                         "active() / configured() so `agent.runtime` decides.")


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
