"""Allocation watchdog: a degraded model must be impossible to miss.

The failure this guards is quiet by construction. A model whose warm-up hit an
out-of-memory error, caught it, and let its HTTP server bind anyway is healthy to
every liveness check there is — the port answers, the service manager says active,
the process is alive. Observed on the development box: a voice sidecar served nothing
for six days while its own `/health` said so the whole time, because nothing polled
it. `test_degraded_model_raises_a_critical_alert` is that incident.

Also guards the properties that keep the watchdog from being the thing that gets
ignored: it does not start at all until models are declared, "cannot fit right now"
is not alarming until it persists, undeclared memory is only mentioned when it is
actually blocking something, and the audit ledger records state CHANGES rather than
one row per model per cycle.

`run_cycle()` takes an injected report, so every branch runs with no hardware, no
subprocess and no network.

Run: .venv/bin/python -m pytest tests/test_alloc_watch.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-allocwatch-test-")

from ava_bridge import alerts
from ava_bridge.alloc import ledger, watch


def _unfit(mid="big", **over):
    """A declared model that is NOT running and could not be started right now.

    `resident=False` matters: `cold_load_ok` is naturally False for a model that is
    already resident (its own weights are part of the memory in use), so only a
    stopped model can meaningfully be "unable to start".
    """
    return _row(mid, resident=False, resident_gib=0.0, ready=False,
                cold_load_ok=False, cold_load_reason="20GiB free < 60GiB needed",
                detail="unit inactive", **over)


def _row(mid="m", **over):
    """A report row for a healthy, resident, declared model."""
    row = {
        "id": mid, "label": mid, "driver": "systemd", "source": "alloc",
        "implicit": False, "priority": "batch", "rank": 1, "pinned": False,
        "via": None, "host": None, "local": True, "observe_only": False,
        "weight_gb": 10.0, "min_free_gb": 4.0, "tier": "large", "workloads": [],
        "resident": True, "resident_gib": 10.0, "measured": True, "ready": True,
        "detail": "active, 10.0 GiB cgroup", "cold_load_ok": True,
        "cold_load_reason": "ok", "problems": [], "notes": [],
    }
    row.update(over)
    return row


def _report(rows, unknown_gib=0.0):
    return {"enabled": True, "gating": "enabled", "actuating": False,
            "platform": "linux-nvidia",
            "pool": {"free_gib": 50.0, "total_gib": 120.0, "source": "system-psutil",
                     "baseline_gib": 20.0, "declared_gib": 10.0,
                     "unknown_gib": unknown_gib},
            "models": rows, "declared_count": len(rows), "ts": 0.0}


class WatchTestCase(unittest.TestCase):
    """Isolate the module globals and the alert store between tests."""

    def setUp(self):
        # Redirect the ledger, not just AVA_HOME. `settings` freezes AVA_HOME at first
        # import, so if another test file imported it earlier in this pytest process the
        # env var alone is a silent no-op and run_cycle() -> breaker.note_ready() writes
        # fixture names into the REAL ledger. Observed: `voice`, `ok`, `x`, `gone` and
        # friends turned up in production breaker state. Patch the path seam.
        self._ledger = tempfile.mkdtemp(prefix="watch-ledger-")
        p = mock.patch.object(ledger, "_dir", lambda: self._ledger)
        p.start()
        self.addCleanup(p.stop)
        watch._last.clear()
        watch._unfit_since.clear()
        with alerts._lock:
            alerts._external.clear()
        self.addCleanup(watch._last.clear)
        self.addCleanup(watch._unfit_since.clear)
        self.recorded = []
        p = mock.patch.object(watch.audit, "record",
                              lambda **kw: self.recorded.append(kw))
        p.start()
        self.addCleanup(p.stop)

    def _alert_ids(self):
        return {a["id"] for a in alerts.active()}


class DegradedTests(WatchTestCase):
    """The six-day-outage regression."""

    def test_degraded_model_raises_a_critical_alert(self):
        rows = [_row("voice", resident=True, ready=False, resident_gib=0.03,
                     detail="active, 0.0 GiB cgroup, probe says NOT ready")]
        states = watch.run_cycle(_report(rows))
        self.assertEqual(states["voice"], "degraded")
        got = [a for a in alerts.active() if a["id"] == "alloc_degraded_voice"]
        self.assertTrue(got, f"expected a degraded alert, got {self._alert_ids()}")
        self.assertEqual(got[0]["severity"], "critical")
        # The message has to be actionable on its own: an operator reading only the
        # alert must understand that the thing is serving nothing.
        self.assertIn("NOT loaded", got[0]["message"])

    def test_healthy_model_raises_nothing(self):
        states = watch.run_cycle(_report([_row("ok")]))
        self.assertEqual(states["ok"], "ready")
        self.assertEqual(self._alert_ids(), set())

    def test_ready_unknown_is_not_treated_as_degraded(self):
        # An unreachable probe is UNKNOWN. Alerting on it would cry wolf every time
        # a service is briefly restarting.
        states = watch.run_cycle(_report([_row("x", ready=None)]))
        self.assertEqual(states["x"], "ready")
        self.assertEqual(self._alert_ids(), set())

    def test_alert_self_clears_when_the_model_recovers(self):
        rows = [_row("voice", ready=False)]
        watch.run_cycle(_report(rows))
        self.assertIn("alloc_degraded_voice", self._alert_ids())
        # A recovered model simply stops being pushed; the TTL retires the alert.
        with alerts._lock:
            alerts._external["alloc_degraded_voice"]["expires"] = 0.0
        watch.run_cycle(_report([_row("voice", ready=True)]))
        self.assertNotIn("alloc_degraded_voice", self._alert_ids())


class AbsentAndConfigTests(WatchTestCase):
    def test_declared_but_not_installed_is_flagged(self):
        rows = [_row("gone", resident=None, measured=False,
                     detail="container 'gone' absent")]
        states = watch.run_cycle(_report(rows))
        self.assertEqual(states["gone"], "absent")
        self.assertIn("alloc_absent_gone", self._alert_ids())

    def test_driver_problems_are_surfaced(self):
        rows = [_row("c", problems=["restart policy 'always' will fight the allocator",
                                    "no readiness.url"])]
        watch.run_cycle(_report(rows))
        got = [a for a in alerts.active() if a["id"] == "alloc_config_c"]
        self.assertTrue(got)
        self.assertIn("+1 more", got[0]["message"])

    def test_observe_only_model_is_not_alerted_on(self):
        # An observed model is not a failure — it is the documented state for a
        # remote model or a checkpoint hosted inside another one.
        rows = [_row("obs", observe_only=True, resident=None, ready=None,
                     detail="observe-only: no actuation configured")]
        states = watch.run_cycle(_report(rows))
        self.assertEqual(states["obs"], "observed")
        self.assertEqual(self._alert_ids(), set())


class UnfitTests(WatchTestCase):
    def test_unfit_is_silent_inside_the_grace_window(self):
        # Not fitting while something else holds the pool is the system working.
        rows = [_unfit("big")]
        watch.run_cycle(_report(rows))
        self.assertEqual(self._alert_ids(), set())

    def test_persistent_unfit_raises(self):
        rows = [_unfit("big")]
        watch.run_cycle(_report(rows))
        # Backdate the first sighting rather than sleeping.
        watch._unfit_since["big"] -= watch.UNFIT_GRACE_S + 1
        watch.run_cycle(_report(rows))
        self.assertIn("alloc_unfit_big", self._alert_ids())

    def test_recovery_resets_the_grace_clock(self):
        rows_bad = [_unfit("big")]
        watch.run_cycle(_report(rows_bad))
        watch.run_cycle(_report([_row("big")]))          # fits again
        self.assertNotIn("big", watch._unfit_since)
        watch.run_cycle(_report(rows_bad))               # clock starts over
        self.assertIn("big", watch._unfit_since)
        self.assertNotIn("alloc_unfit_big", self._alert_ids())


class UnknownHogTests(WatchTestCase):
    def test_hog_alert_needs_both_size_and_a_blocked_model(self):
        # Large but blocking nothing: silent. Reporting an idle desktop session as a
        # hog would train an operator to ignore the alert.
        watch.run_cycle(_report([_row("ok")], unknown_gib=60.0))
        self.assertNotIn("alloc_unknown_hog", self._alert_ids())

        # Large AND a declared model cannot start: worth saying.
        watch.run_cycle(_report([_unfit("big")], unknown_gib=60.0))
        got = [a for a in alerts.active() if a["id"] == "alloc_unknown_hog"]
        self.assertTrue(got)
        self.assertEqual(got[0]["value"], 60.0)

    def test_small_unknown_hold_is_ignored(self):
        watch.run_cycle(_report([_unfit("big")], unknown_gib=1.0))
        self.assertNotIn("alloc_unknown_hog", self._alert_ids())


class PinnedTests(WatchTestCase):
    """A pinned model is one Ava may not touch — so it must not be nagged about.

    Pinning exists for a model that belongs to ANOTHER app on this box: declared so
    its memory is counted, never actuated. Ava genuinely cannot start one (`_restore`
    needs provenance flags that only a release sets, and a pinned model is never
    released), so "has not been able to start for 60 min" is a complaint about
    somebody else's decision — and it drags the model into the undeclared-memory
    alert's blocked list, where it makes unrelated processes look like the cause.

    The line these draw: protection from the ALLOCATOR, never silence about lying.
    """

    def test_a_pinned_model_that_is_down_is_not_reported_as_unable_to_start(self):
        rows = [_unfit("vip", pinned=True)]
        watch.run_cycle(_report(rows))
        watch._unfit_since["vip"] = 0.0          # backdate past any grace window
        watch.run_cycle(_report(rows))
        self.assertNotIn("alloc_unfit_vip", self._alert_ids())
        self.assertFalse(watch._wants_start(rows[0]))

    def test_a_pinned_model_down_is_recorded_as_stopped(self):
        """Silent is not the same as unobserved: the ledger still gets the change."""
        self.assertEqual(watch._state_of(_unfit("vip", pinned=True)), "stopped")

    def test_a_pinned_model_does_not_appear_in_the_undeclared_memory_alert(self):
        # 60 GiB unexplained AND the only down model is one Ava may not start. There
        # is nothing being blocked, so blaming the unknown memory would be a lie.
        watch.run_cycle(_report([_unfit("vip", pinned=True)], unknown_gib=60.0))
        self.assertNotIn("alloc_unknown_hog", self._alert_ids())

    def test_a_pinned_model_that_is_running_but_not_ready_is_still_degraded(self):
        """The guard the other way — this is the failure the watchdog exists for.

        A pinned engine whose warm-up failed and served its port anyway is exactly
        the six-day silent fallback. Pinning must buy it no quiet at all.
        """
        watch.run_cycle(_report([_row("vip", pinned=True, ready=False)]))
        got = [a for a in alerts.active() if a["id"] == "alloc_degraded_vip"]
        self.assertTrue(got)
        self.assertEqual(got[0]["severity"], "critical")

    def test_metrics_do_not_count_a_pinned_model_as_unfit(self):
        rows = [_unfit("vip", pinned=True), _unfit("ordinary")]
        with mock.patch("ava_bridge.alloc.spec.load_models", return_value=[object()]), \
             mock.patch("ava_bridge.alloc.report", return_value=_report(rows)):
            m = watch.metrics()
        self.assertEqual(m["alloc_unfit_count"], 1)


class AuditTests(WatchTestCase):
    def test_only_transitions_are_recorded(self):
        rows = [_row("m")]
        watch.run_cycle(_report(rows))
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["kind"], "alloc.ready")
        self.assertIsNone(self.recorded[0]["previous"])
        # Same state again: no new row. A row per model per cycle would bury the
        # signal the ledger exists to keep.
        watch.run_cycle(_report(rows))
        self.assertEqual(len(self.recorded), 1)

    def test_state_change_is_recorded_with_its_predecessor(self):
        watch.run_cycle(_report([_row("m")]))
        watch.run_cycle(_report([_row("m", ready=False)]))
        self.assertEqual(len(self.recorded), 2)
        self.assertEqual(self.recorded[1]["kind"], "alloc.degraded")
        self.assertEqual(self.recorded[1]["previous"], "ready")

    def test_removed_model_is_forgotten(self):
        watch.run_cycle(_report([_row("m")]))
        watch.run_cycle(_report([]))
        self.assertNotIn("m", watch._last)


class DormantByDefaultTests(unittest.TestCase):
    """An install that has not opted in must pay nothing and see nothing."""

    def test_scheduler_does_not_start_without_declared_models(self):
        watch._started = False
        self.addCleanup(setattr, watch, "_started", False)
        with mock.patch("ava_bridge.alloc.spec.load_models", return_value=[]), \
             mock.patch("threading.Thread") as thread:
            watch.start_scheduler()
        thread.assert_not_called()
        self.assertFalse(watch._started)

    def test_metrics_are_zero_with_nothing_declared(self):
        with mock.patch("ava_bridge.alloc.spec.load_models", return_value=[]):
            m = watch.metrics()
        self.assertEqual(m, {"alloc_degraded_count": 0, "alloc_unfit_count": 0,
                             "alloc_unknown_hold_gb": 0})

    def test_metrics_never_raise(self):
        # The SSE producer calls this every few seconds per client; an exception here
        # would break the dashboard feed for an optional subsystem.
        with mock.patch("ava_bridge.alloc.spec.load_models",
                        side_effect=RuntimeError("boom")):
            m = watch.metrics()
        self.assertEqual(m["alloc_degraded_count"], 0)

    def test_metrics_count_declared_problems(self):
        rows = [_row("a", ready=False), _unfit("b"), _row("c")]
        with mock.patch("ava_bridge.alloc.spec.load_models", return_value=[object()]), \
             mock.patch("ava_bridge.alloc.report",
                        return_value=_report(rows, unknown_gib=30.0)):
            m = watch.metrics()
        self.assertEqual(m["alloc_degraded_count"], 1)
        self.assertEqual(m["alloc_unfit_count"], 1)
        self.assertEqual(m["alloc_unknown_hold_gb"], 30.0)


if __name__ == "__main__":
    unittest.main()
