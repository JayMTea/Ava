"""The agent sandbox's model is the operative brain — surfaced truthfully.

Covers the two seams added for the "Ava's brain shows empty while Nemotron is
plainly answering" UX gap: NemoClawRuntime.sandbox_info() (parses `nemoclaw
list --json`) and agent.which_model()'s sandbox fallback when the router has
no answer.
"""
import json
import unittest
from unittest import mock

from ava_bridge import agent, config, runtime
from ava_bridge.runtime.nemoclaw import NemoClawRuntime

LIST_JSON = json.dumps({
    "schemaVersion": 1,
    "defaultSandbox": "my-assistant",
    "sandboxes": [
        {"name": "other-box", "model": "meta/llama-3.1-8b", "provider": "ollama"},
        {"name": "my-assistant",
         "model": "nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8",
         "provider": "compatible-endpoint", "isDefault": True},
    ],
})


def _fresh_runtime() -> NemoClawRuntime:
    rt = NemoClawRuntime()
    rt._info_cache["ts"] = 0.0  # never trust a previous test's cache
    return rt


class SandboxInfoTests(unittest.TestCase):
    def _rt(self, rc=0, out=LIST_JSON, cli_exists=True) -> NemoClawRuntime:
        rt = _fresh_runtime()
        rt._run = lambda argv, timeout=15: (rc, out)  # type: ignore[method-assign]
        self._exists = mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists",
                                  return_value=cli_exists)
        self._exists.start()
        self.addCleanup(self._exists.stop)
        return rt

    def test_parses_this_sandboxes_model(self):
        with mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            info = self._rt().sandbox_info()
        self.assertEqual(info["provider"], "compatible-endpoint")
        self.assertIn("Nemotron", info["model"])

    def test_other_sandbox_names_do_not_match(self):
        with mock.patch.object(config, "OC_SANDBOX", "nope"):
            self.assertIsNone(self._rt().sandbox_info())

    def test_cli_failure_and_garbage_are_none_not_raise(self):
        with mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            self.assertIsNone(self._rt(rc=1).sandbox_info())
            self.assertIsNone(self._rt(out="not json at all").sandbox_info())

    def test_result_is_cached(self):
        calls = []
        rt = _fresh_runtime()
        rt._run = lambda argv, timeout=15: (calls.append(1), (0, LIST_JSON))[1]  # type: ignore[method-assign]
        with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists", return_value=True), \
             mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            rt.sandbox_info()
            rt.sandbox_info()
        self.assertEqual(len(calls), 1)

    def test_status_carries_the_sandbox_model(self):
        rt = self._rt()
        # **_k so this stub survives signature changes — available() passes a
        # bounded timeout= now that it probes the sandbox on the turn path.
        rt._sandbox_exists = lambda **_k: True  # type: ignore[method-assign]
        with mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            st = rt.status()
        self.assertIn("Nemotron", st["sandbox_model"])
        self.assertEqual(st["sandbox_provider"], "compatible-endpoint")


class AvailabilityRequiresSandboxTests(unittest.TestCase):
    """`available()` gates runtime.active(): return True without a sandbox and
    every turn runs the full ~120s tool timeout instead of degrading to Direct.

    `ava agent provision --install` installs the CLI WITHOUT onboarding a sandbox,
    so CLI-present/sandbox-absent is the state a fresh fork actually lands in — it
    is the default outcome of following the docs, not an edge case.
    """

    def _rt(self, list_rc=0, list_out=LIST_JSON) -> NemoClawRuntime:
        rt = _fresh_runtime()
        rt._avail_cache.update(ts=0.0, ok=None)   # never trust a prior probe
        rt._run = lambda argv, timeout=30: (list_rc, list_out)  # type: ignore[method-assign]
        p = mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        return rt

    def test_cli_present_and_sandbox_present_is_available(self):
        with mock.patch.object(config, "AGENT_ENABLED", True), \
             mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            self.assertTrue(self._rt().available())

    def test_cli_present_but_sandbox_missing_is_unavailable(self):
        # The regression: onboarding never ran, so the sandbox is not in the list.
        with mock.patch.object(config, "AGENT_ENABLED", True), \
             mock.patch.object(config, "OC_SANDBOX", "never-onboarded"):
            self.assertFalse(self._rt().available())

    def test_indeterminate_probe_is_unavailable_not_assumed_healthy(self):
        # A timeout/CLI error must not read as healthy: Direct (tool-less but
        # working) is the better failure mode than 120s-per-turn timeouts.
        with mock.patch.object(config, "AGENT_ENABLED", True), \
             mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            self.assertFalse(self._rt(list_rc=1, list_out="boom").available())

    def test_active_falls_back_to_direct_without_a_sandbox(self):
        rt = self._rt()
        with mock.patch.object(config, "AGENT_ENABLED", True), \
             mock.patch.object(config, "OC_SANDBOX", "never-onboarded"), \
             mock.patch.object(runtime, "configured", return_value=rt):
            self.assertEqual(runtime.active().name, "direct")

    def test_probe_timeout_is_bounded_below_the_turn_timeout(self):
        seen = {}
        rt = _fresh_runtime()
        rt._avail_cache.update(ts=0.0, ok=None)
        rt._run = lambda argv, timeout=30: (seen.update(t=timeout), (0, LIST_JSON))[1]  # type: ignore[method-assign]
        with mock.patch("ava_bridge.runtime.nemoclaw.os.path.exists", return_value=True), \
             mock.patch.object(config, "AGENT_ENABLED", True), \
             mock.patch.object(config, "OC_SANDBOX", "my-assistant"):
            rt.available()
        self.assertLessEqual(seen.get("t", 999), 15,
                             "available() runs on the turn path; its probe must not stall a reply")


class WhichModelFallbackTests(unittest.TestCase):
    def test_router_miss_falls_back_to_sandbox_model(self):
        rt = _fresh_runtime()
        rt._info_cache.update(ts=9e12, info={"model": "nvidia/Nemotron-Omni",
                                             "provider": "compatible-endpoint"})
        with mock.patch.object(agent.requests, "get",
                               side_effect=OSError("router down")), \
             mock.patch.object(runtime, "active", return_value=rt):
            m = agent.which_model()
        self.assertEqual(m["id"], "agent-sandbox")
        self.assertEqual(m["label"], "Nemotron-Omni")
        self.assertEqual(m["model"], "nvidia/Nemotron-Omni")

    def test_router_answer_still_wins(self):
        resp = mock.Mock()
        resp.json.return_value = {"id": "local", "label": "Router Model",
                                  "model": "m", "prompt_tokens": 1, "total_tokens": 2}
        with mock.patch.object(agent.requests, "get", return_value=resp):
            m = agent.which_model()
        self.assertEqual(m["label"], "Router Model")

    def test_direct_floor_router_miss_is_none(self):
        with mock.patch.object(agent.requests, "get",
                               side_effect=OSError("router down")), \
             mock.patch.object(runtime, "active", return_value=runtime.direct()):
            self.assertIsNone(agent.which_model())


if __name__ == "__main__":
    unittest.main(verbosity=2)
