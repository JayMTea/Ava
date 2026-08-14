"""The watchdog that looks when nobody is looking.

Between 2026-08-01 and 2026-08-13, Ava's router served a model id its engine did
not have. Every chat turn 404'd for twelve days. Three separate surfaces could
each have noticed and none did, and the last gap was the simplest: the only thing
that could have seen it was a panel, and a panel only reports to someone who has
it open.

Two properties carry the whole design, and the second is the one that decides
whether this survives contact with a real box:

  * a genuine contradiction — the engine ANSWERED, and does not hold the
    configured model — raises a **critical** alert naming both ids and both ways
    out;
  * every other way of not knowing raises **nothing**. An engine restarting, a
    model still loading, a cloud endpoint, the agent sandbox holding its own
    endpoint: none of those is a disagreement, and a watchdog that pages on
    silence is one that gets switched off before the day it would have mattered.

Run: .venv/bin/python -m pytest tests/test_brain_watch.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-brainwatch-test-")

from ava_bridge import alerts, brain_watch


def _truth(verdict, **over):
    t = {"verdict": verdict, "want": "acme/Cool-LLM-7B-FP8",
         "serving": [], "matched": "", "engine": "vllm",
         "base_url": "http://127.0.0.1:8002/v1", "backend_id": "omni",
         "source": "configured", "observed": True, "detail": ""}
    t.update(over)
    return t


class BrainWatchTestCase(unittest.TestCase):
    def setUp(self):
        # The alert store is process-global and alerts outlive a test by their
        # TTL, so a drift raised in one case would still be active in the next
        # and the "raises nothing" assertions would pass or fail by test order.
        # Same seam tests/test_alloc_watch.py uses.
        with alerts._lock:
            alerts._external.clear()
        brain_watch._last = ""
        self.addCleanup(setattr, brain_watch, "_last", "")

    def _ids(self):
        return {a["id"] for a in alerts.active()}


class DriftTests(BrainWatchTestCase):
    def test_drift_raises_a_critical_alert(self):
        brain_watch.run_cycle(_truth("drifted", serving=["nvidia/Other-LLM-30B"]))
        got = [a for a in alerts.active() if a["id"] == brain_watch.DRIFT_ALERT]
        self.assertTrue(got)
        self.assertEqual(got[0]["severity"], "critical")

    def test_the_message_names_both_models_and_both_ways_out(self):
        """Either id alone leaves the owner nothing to do."""
        brain_watch.run_cycle(_truth("drifted", serving=["nvidia/Other-LLM-30B"]))
        msg = next(a for a in alerts.active()
                   if a["id"] == brain_watch.DRIFT_ALERT)["message"]
        self.assertIn("acme/Cool-LLM-7B-FP8", msg)      # what Ava asked for
        self.assertIn("nvidia/Other-LLM-30B", msg)      # what it actually got
        self.assertIn("Setup", msg)                     # change the config…
        self.assertIn("load", msg)                      # …or load the model

    def test_agreement_raises_nothing(self):
        brain_watch.run_cycle(_truth("agrees", matched="acme/Cool-LLM-7B-FP8",
                                     serving=["acme/Cool-LLM-7B-FP8"]))
        self.assertEqual(self._ids() & {brain_watch.DRIFT_ALERT,
                                        brain_watch.UNREACHABLE_ALERT}, set())


class MismatchTests(BrainWatchTestCase):
    """A stale name is announced, but never as an outage."""

    def test_mismatch_raises_a_warning_not_a_critical(self):
        brain_watch.run_cycle(_truth("mismatched", want="acme/Old-FP8",
                                     serving=["acme/New-NVFP4"],
                                     detail="the agent sandbox is onboarded with "
                                            "acme/Old-FP8, but the model Ava serves "
                                            "is acme/New-NVFP4"))
        got = [a for a in alerts.active() if a["id"] == brain_watch.MISMATCH_ALERT]
        self.assertTrue(got)
        # Turns still succeed — the router rewrites the id — so critical here
        # would be crying wolf, and the drift alert would stop being believed.
        self.assertEqual(got[0]["severity"], "warn")
        self.assertNotIn(brain_watch.DRIFT_ALERT, self._ids())

    def test_the_message_names_both_models_and_the_fix(self):
        brain_watch.run_cycle(_truth("mismatched", want="acme/Old-FP8",
                                     serving=["acme/New-NVFP4"],
                                     detail="the agent sandbox is onboarded with "
                                            "acme/Old-FP8, but the model Ava serves "
                                            "is acme/New-NVFP4"))
        msg = next(a for a in alerts.active()
                   if a["id"] == brain_watch.MISMATCH_ALERT)["message"]
        self.assertIn("acme/Old-FP8", msg)
        self.assertIn("acme/New-NVFP4", msg)
        self.assertIn("onboard", msg)          # the actual remedy
        self.assertIn("works", msg)            # says plainly that chat is fine


class SilenceIsNotDriftTests(BrainWatchTestCase):
    """The verdicts that must never page anyone."""

    def test_unobservable_says_nothing(self):
        brain_watch.run_cycle(_truth("unobservable", observed=True))
        self.assertNotIn(brain_watch.DRIFT_ALERT, self._ids())
        self.assertNotIn(brain_watch.UNREACHABLE_ALERT, self._ids())

    def test_a_remote_brain_says_nothing(self):
        brain_watch.run_cycle(_truth("elsewhere"))
        self.assertEqual(self._ids() & {brain_watch.DRIFT_ALERT,
                                        brain_watch.UNREACHABLE_ALERT}, set())

    def test_an_unconfigured_install_is_a_setup_step_not_a_fault(self):
        brain_watch.run_cycle(_truth("unconfigured", want="", base_url=""))
        self.assertEqual(self._ids() & {brain_watch.DRIFT_ALERT,
                                        brain_watch.UNREACHABLE_ALERT}, set())


class UnreachableGraceTests(BrainWatchTestCase):
    def test_one_unreachable_cycle_is_silent(self):
        """An engine restart, a cold load, or Ava booting before its engine."""
        brain_watch.run_cycle(_truth("unreachable", observed=False))
        self.assertNotIn(brain_watch.UNREACHABLE_ALERT, self._ids())

    def test_two_consecutive_unreachable_cycles_warn(self):
        brain_watch.run_cycle(_truth("unreachable", observed=False))
        brain_watch.run_cycle(_truth("unreachable", observed=False))
        got = [a for a in alerts.active()
               if a["id"] == brain_watch.UNREACHABLE_ALERT]
        self.assertTrue(got)
        self.assertEqual(got[0]["severity"], "warn")

    def test_recovery_between_cycles_resets_the_grace(self):
        brain_watch.run_cycle(_truth("unreachable", observed=False))
        brain_watch.run_cycle(_truth("agrees", matched="acme/Cool-LLM-7B-FP8"))
        brain_watch.run_cycle(_truth("unreachable", observed=False))
        self.assertNotIn(brain_watch.UNREACHABLE_ALERT, self._ids())


class MetricsTests(unittest.TestCase):
    def test_metrics_are_zero_with_no_brain(self):
        with mock.patch("ava_bridge.models.serving_truth",
                        return_value=_truth("unconfigured")):
            self.assertEqual(brain_watch.metrics(),
                             {"brain_drift_count": 0, "brain_mismatch_count": 0})

    def test_metrics_count_a_drift(self):
        with mock.patch("ava_bridge.models.serving_truth",
                        return_value=_truth("drifted")):
            m = brain_watch.metrics()
        self.assertEqual(m["brain_drift_count"], 1)
        self.assertEqual(m["brain_mismatch_count"], 0)

    def test_metrics_count_a_mismatch_separately(self):
        """Two gauges, because they drive rules of different severity."""
        with mock.patch("ava_bridge.models.serving_truth",
                        return_value=_truth("mismatched")):
            m = brain_watch.metrics()
        self.assertEqual(m["brain_mismatch_count"], 1)
        self.assertEqual(m["brain_drift_count"], 0)

    def test_metrics_never_raise(self):
        with mock.patch("ava_bridge.models.serving_truth",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(brain_watch.metrics(),
                             {"brain_drift_count": 0, "brain_mismatch_count": 0})

    def test_a_metrics_scrape_does_not_push_alerts(self):
        """Otherwise an alert's lifetime depends on how often a dashboard is open."""
        with mock.patch("ava_bridge.models.serving_truth",
                        return_value=_truth("drifted")), \
             mock.patch.object(alerts, "push_external") as push:
            brain_watch.metrics()
        push.assert_not_called()


if __name__ == "__main__":
    unittest.main()
