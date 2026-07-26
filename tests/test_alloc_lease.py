"""Leases: the planner's decision table, cross-process ownership, and advisory mode.

Three properties matter most here, and each has a specific failure it prevents:

  * **Advisory mode actuates nothing.** With `alloc.lease.enforce` false the full
    decision is computed and recorded but no driver is touched. This is what makes
    the layer safe to ship before its sequencing has earned trust.
  * **A live lease at the requester's own rank or better is never preempted.** Every
    other rule in the planner is an efficiency question; this one is correctness —
    breaking it corrupts work already in flight.
  * **A dead holder is reclaimed by the kernel, with no timeout involved.** Tested by
    forking a real child that takes a lease and exits without releasing it, which is
    the scenario in-memory refcounts cannot survive.

The planner is a pure function, so its tests pass data and assert on data — no
hardware, no mocks, no clock.

Run: .venv/bin/python -m pytest tests/test_alloc_lease.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-lease-test-")

from ava_bridge import settings  # noqa: E402
from ava_bridge.alloc import broker, ledger, policy  # noqa: E402
from ava_bridge.alloc.base import ReleaseMode, ReleaseOption  # noqa: E402

INTERACTIVE, BATCH, BACKGROUND = 0, 1, 2


def _opt(mode=ReleaseMode.STOP, frees=20.0, release_s=30.0, restore_s=300.0,
         reversible=True, costs_known=True):
    return ReleaseOption(mode=mode, frees_gib=frees, release_s=release_s,
                         restore_s=restore_s, reversible=reversible,
                         costs_known=costs_known)


def _cand(mid, rank=BATCH, gib=20.0, **over):
    kw = dict(model_id=mid, rank=rank, resident=True, gib=gib, measured=True,
              options=(_opt(frees=gib),))
    kw.update(over)
    return policy.Candidate(**kw)


class AdmissionTests(unittest.TestCase):
    """Release only when the request does not already fit."""

    def test_a_request_that_fits_plans_nothing(self):
        # Coordination schemes that pause a heavy model for every request pay a
        # reload even when the box had room. The plan must be empty here.
        pl = policy.plan("m", need_gib=20.0, rank=INTERACTIVE, free_gib=50.0,
                         candidates=[_cand("other")])
        self.assertTrue(pl.admit)
        self.assertEqual(pl.steps, ())
        self.assertIn("nothing to release", pl.reason)

    def test_unknown_memory_admits_and_is_not_gated(self):
        pl = policy.plan("m", need_gib=999.0, rank=INTERACTIVE, free_gib=None,
                         candidates=[_cand("other")])
        self.assertTrue(pl.admit)
        self.assertFalse(pl.gated)
        self.assertEqual(pl.steps, ())

    def test_unknown_weight_admits_and_is_not_gated(self):
        pl = policy.plan("m", need_gib=None, rank=INTERACTIVE, free_gib=1.0,
                         candidates=[_cand("other")])
        self.assertTrue(pl.admit)
        self.assertFalse(pl.gated)

    def test_shortfall_is_reported_when_nothing_can_be_released(self):
        pl = policy.plan("m", need_gib=80.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[])
        self.assertFalse(pl.admit)
        self.assertEqual(pl.shortfall_gib, 70.0)
        self.assertIn("short", pl.reason)


class PriorityTests(unittest.TestCase):
    """Rule 4 is the correctness rule; the rest are efficiency."""

    def test_a_live_lease_at_equal_rank_is_never_preempted(self):
        pl = policy.plan("m", need_gib=60.0, rank=BATCH, free_gib=10.0,
                         candidates=[_cand("busy", rank=BATCH, held_live=True)])
        self.assertFalse(pl.admit)
        self.assertEqual(pl.evicts, ())
        self.assertIn("in use", pl.reason)

    def test_a_live_lease_at_better_rank_is_never_preempted(self):
        pl = policy.plan("m", need_gib=60.0, rank=BATCH, free_gib=10.0,
                         candidates=[_cand("vip", rank=INTERACTIVE, held_live=True)])
        self.assertFalse(pl.admit)
        self.assertEqual(pl.evicts, ())

    def test_an_idle_equal_rank_model_may_be_released(self):
        pl = policy.plan("m", need_gib=25.0, rank=BATCH, free_gib=10.0,
                         candidates=[_cand("idle", rank=BATCH, held_live=False)])
        self.assertTrue(pl.admit)
        self.assertEqual(pl.evicts, ("idle",))

    def test_lower_priority_is_released_even_while_in_use(self):
        # A background job holding memory yields to interactive work; that is what
        # the priority declaration is for.
        pl = policy.plan("m", need_gib=25.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[_cand("bg", rank=BACKGROUND, held_live=True)])
        self.assertTrue(pl.admit)
        self.assertEqual(pl.evicts, ("bg",))

    def test_a_higher_priority_model_is_not_touched_for_lower_priority_work(self):
        pl = policy.plan("m", need_gib=25.0, rank=BACKGROUND, free_gib=10.0,
                         candidates=[_cand("vip", rank=INTERACTIVE)])
        self.assertFalse(pl.admit)
        self.assertEqual(pl.evicts, ())

    def test_pinned_is_never_a_candidate(self):
        pl = policy.plan("m", need_gib=25.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[_cand("safe", rank=BACKGROUND, pinned=True)])
        self.assertFalse(pl.admit)
        self.assertIn("pinned", pl.reason)

    def test_lowest_priority_is_released_first(self):
        pl = policy.plan("m", need_gib=25.0, rank=INTERACTIVE, free_gib=0.0,
                         candidates=[_cand("b", rank=BATCH, gib=30.0),
                                     _cand("c", rank=BACKGROUND, gib=30.0)])
        self.assertEqual(pl.evicts[0], "c")


class UnreleasableTests(unittest.TestCase):
    def test_a_blocked_driver_is_not_planned_and_is_explained(self):
        pl = policy.plan("m", need_gib=60.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[_cand("obs", blocked="observe-only")])
        self.assertFalse(pl.admit)
        self.assertEqual(pl.evicts, ())
        self.assertIn("cannot release", pl.reason)

    def test_a_model_that_is_not_resident_yields_nothing(self):
        pl = policy.plan("m", need_gib=60.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[_cand("down", resident=False, gib=0.0)])
        self.assertFalse(pl.admit)
        self.assertEqual(pl.evicts, ())


class SpeculativeTests(unittest.TestCase):
    """Cheap reversible levers are worth pulling even on an unknown yield."""

    def _cheap(self, mid, frees=None):
        return _cand(mid, options=(_opt(mode=ReleaseMode.UNLOAD, frees=frees,
                                        release_s=8.0, restore_s=0.0),))

    def test_cheap_lever_is_tried_before_any_eviction(self):
        pl = policy.plan("m", need_gib=30.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[self._cheap("engine", frees=25.0),
                                     _cand("heavy", rank=BACKGROUND, gib=40.0)])
        self.assertTrue(pl.admit)
        self.assertTrue(pl.steps[0].speculative)
        self.assertEqual(pl.steps[0].model_id, "engine")
        # It was enough, so nothing was evicted.
        self.assertEqual(pl.evicts, ())

    def test_unknown_yield_is_still_attempted_but_not_counted(self):
        pl = policy.plan("m", need_gib=30.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[self._cheap("engine", frees=None)])
        # Attempted...
        self.assertTrue(any(s.speculative for s in pl.steps))
        # ...but it cannot be relied on in the arithmetic, so the verdict is honest.
        self.assertFalse(pl.admit)
        self.assertEqual(pl.projected_gib, 10.0)

    def test_an_undeclared_cost_is_never_treated_as_free(self):
        # Undeclared costs arrive as 0.0. If that counted as "cheap", the planner
        # would speculatively stop a service whose real reload cost is minutes.
        undeclared = _cand("engine", rank=BACKGROUND, options=(
            _opt(mode=ReleaseMode.UNLOAD, frees=25.0, release_s=0.0, restore_s=0.0,
                 costs_known=False),))
        pl = policy.plan("m", need_gib=30.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[undeclared])
        self.assertTrue(pl.admit)                     # still released...
        self.assertFalse(pl.steps[0].speculative)     # ...but as a real eviction
        self.assertEqual(pl.evicts, ("engine",))

    def test_stop_is_never_speculative_even_when_declared_cheap(self):
        # Stopping a process is not the cheap point on the axis, whatever the numbers
        # say — only UNLOAD keeps the service up.
        cheap_stop = _cand("svc", rank=BACKGROUND, options=(
            _opt(mode=ReleaseMode.STOP, frees=25.0, release_s=1.0, restore_s=1.0),))
        pl = policy.plan("m", need_gib=30.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[cheap_stop])
        self.assertFalse(pl.steps[0].speculative)

    def test_an_expensive_lever_is_not_speculative(self):
        pl = policy.plan("m", need_gib=30.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[_cand("slow", rank=BACKGROUND, gib=40.0,
                                           options=(_opt(release_s=120.0,
                                                         restore_s=600.0),))])
        self.assertTrue(pl.admit)
        self.assertFalse(pl.steps[0].speculative)

    def test_unload_is_preferred_over_stop_for_the_same_model(self):
        both = _cand("engine", rank=BACKGROUND, gib=20.0, options=(
            _opt(mode=ReleaseMode.STOP, frees=20.0, release_s=30.0, restore_s=300.0),
            _opt(mode=ReleaseMode.UNLOAD, frees=20.0, release_s=8.0, restore_s=20.0),
        ))
        pl = policy.plan("m", need_gib=25.0, rank=INTERACTIVE, free_gib=10.0,
                         candidates=[both])
        self.assertEqual(pl.steps[0].mode, ReleaseMode.UNLOAD)


class LedgerTests(unittest.TestCase):
    """Ownership is derived from held locks, never from a maintained count."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-")
        p = mock.patch.object(ledger, "_dir", lambda: self.dir)
        p.start()
        self.addCleanup(p.stop)

    def test_flock_is_reliable_on_local_storage(self):
        ok, why = ledger.flock_is_reliable()
        self.assertTrue(ok, why)

    def test_an_unheld_lock_reads_as_not_held(self):
        self.assertFalse(ledger.is_held("models/x.lock"))

    def test_a_held_lock_reads_as_held_even_within_one_process(self):
        # flock (not lockf) conflicts across separate descriptors in one process.
        # lockf would report "not held" here and give false exclusivity, which is
        # the bug this choice avoids.
        with ledger.flock("models/x.lock"):
            self.assertTrue(ledger.is_held("models/x.lock"))
        self.assertFalse(ledger.is_held("models/x.lock"))

    def test_lock_busy_raises_rather_than_blocking(self):
        with ledger.flock("models/y.lock"):
            with self.assertRaises(ledger.LockBusy):
                with ledger.flock("models/y.lock", wait_s=0.05):
                    pass

    def test_a_dead_holders_lease_is_reclaimed_with_no_timeout(self):
        """The scenario an in-memory refcount cannot survive.

        A real child process takes a lease and exits without releasing it. The
        kernel drops its flock, so the lease is detectably dead immediately — no
        heartbeat, no expiry, no clock.
        """
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"], "reason": "child"})
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            try:
                import fcntl
                fd = os.open(os.path.join(self.dir, ledger.lease_lock_name(lid)),
                             os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(w, b"held")
                os._exit(0)          # exit WITHOUT releasing
            except Exception:
                os._exit(1)
        os.close(w)
        self.assertEqual(os.read(r, 4), b"held")
        os.close(r)
        os.waitpid(pid, 0)
        # The child is gone: its lock is free, so the lease is not live and is reaped.
        self.assertEqual(ledger.live_leases(), [])
        self.assertIsNone(ledger.read_lease(lid))

    def test_a_lease_from_a_previous_boot_is_discarded(self):
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"]})
        rec = ledger.read_lease(lid)
        rec["boot_id"] = "some-other-boot"
        ledger._write_json(os.path.join("leases", f"{lid}.json"), rec)
        self.assertEqual(ledger.live_leases(), [])

    def test_corrupt_lease_record_is_quarantined_not_fatal(self):
        path = ledger._path(os.path.join("leases", "bad.json"))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(ledger.live_leases(), [])
        self.assertFalse(os.path.exists(path))

    def test_owed_lists_only_what_we_released_and_nobody_holds(self):
        ledger.set_model_state("a", released_by_us=True)
        ledger.set_model_state("b", released_by_us=False)
        self.assertEqual([r["model_id"] for r in ledger.owed()], ["a"])

    def test_released_by_us_gates_restoring(self):
        # The allocator must never start what it did not itself stop — that would
        # fight an operator's deliberate shutdown or another supervisor.
        self.assertFalse(ledger.released_by_us("never-touched"))
        ledger.set_model_state("ours", released_by_us=True)
        self.assertTrue(ledger.released_by_us("ours"))


class AdvisoryModeTests(unittest.TestCase):
    """Nothing is actuated until an operator turns enforcement on."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-adv-")
        for p in (mock.patch.object(ledger, "_dir", lambda: self.dir),
                  mock.patch.object(settings, "_CFG", {
                      "alloc": {"models": {
                          "want": {"weight_gb": 40, "min_free_gb": 4},
                          "victim": {"weight_gb": 40,
                                     "driver_config": {"container": "c"}}}}})):
            p.start()
            self.addCleanup(p.stop)
        for target, value in (("_backend_profiles", {}), ("_connector_services", [])):
            p = mock.patch(f"ava_bridge.alloc.spec.{target}",
                           lambda v=value: v)   # bind now, not at call time
            p.start()
            self.addCleanup(p.stop)
        # These tests exercise lease MECHANICS. Assembling candidates constructs real
        # drivers, which shell out to probe a container runtime — slow, and against
        # this suite's no-subprocess rule. Candidate assembly is covered by the
        # planner tests above, which pass hand-built candidates.
        p = mock.patch("ava_bridge.alloc.broker._candidates", lambda *a, **k: [])
        p.start()
        self.addCleanup(p.stop)
        # The decision log lives under logs/, not the ledger dir, so redirecting the
        # ledger does not cover it. Without this, fixture decisions land in the real
        # logs/alloc.jsonl — the record an operator reads to decide whether to enable
        # enforcement — and whether they do depends on module import order.
        p = mock.patch.object(broker, "_log_path",
                              lambda: os.path.join(self.dir, "alloc.jsonl"))
        p.start()
        self.addCleanup(p.stop)

    def test_enforce_defaults_off(self):
        from ava_bridge.alloc import broker as L
        self.assertFalse(L.enforcing())

    def test_advisory_lease_is_granted_and_touches_no_driver(self):
        from ava_bridge.alloc import broker as L
        with mock.patch.object(L, "_execute") as ex, \
             mock.patch.object(L.capacity, "free_gib", return_value=5.0):
            with L.lease("want", reason="test") as lz:
                self.assertTrue(lz)              # advisory never blocks the caller
                self.assertFalse(lz.enforced)
        ex.assert_not_called()

    def test_a_nested_lease_is_a_refcount_bump_not_a_second_decision(self):
        from ava_bridge.alloc import broker as L
        with mock.patch.object(L.capacity, "free_gib", return_value=100.0):
            with L.lease("want") as outer:
                self.assertFalse(outer.nested)
                with L.lease("want") as inner:
                    self.assertTrue(inner.nested)
                    self.assertTrue(inner)

    def test_the_body_still_runs_when_the_allocator_errors(self):
        from ava_bridge.alloc import broker as L
        ran = []
        with mock.patch.object(L, "_candidates", side_effect=RuntimeError("boom")):
            with L.lease("want") as lz:
                ran.append(True)
                self.assertTrue(lz)              # degraded, but never blocking
        self.assertEqual(ran, [True])

    def test_release_runs_even_when_the_body_raises(self):
        from ava_bridge.alloc import broker as L
        with mock.patch.object(L.capacity, "free_gib", return_value=100.0):
            with self.assertRaises(ValueError):
                with L.lease("want"):
                    raise ValueError("body failed")
            # A leaked hold would wedge later requests, so the thread-local must clear.
            self.assertFalse(getattr(L._local, "held", {}))

    def test_restore_now_is_a_noop_while_advisory(self):
        from ava_bridge.alloc import broker as L
        ledger.set_model_state("victim", released_by_us=True)
        self.assertEqual(L.restore_now(), [])

    def test_an_undeclared_model_is_not_governed(self):
        from ava_bridge.alloc import broker as L
        with L.lease("not-declared") as lz:
            self.assertTrue(lz)
            self.assertIn("not declared", lz.reason)


class EnforcementGateTests(unittest.TestCase):
    """`enforce: true` with eviction off must be genuinely safe.

    The two halves carry opposite risk: waiting is at worst slower, releasing is at
    worst taking memory from work in progress. So they are separate switches, and
    turning on only the safe half must actuate nothing.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-enf-")
        p = mock.patch.object(ledger, "_dir", lambda: self.dir)
        p.start()
        self.addCleanup(p.stop)
        for target, value in (("_backend_profiles", {}), ("_connector_services", [])):
            q = mock.patch(f"ava_bridge.alloc.spec.{target}", lambda v=value: v)
            q.start()
            self.addCleanup(q.stop)
        # The decision log lives under logs/, not the ledger dir, so redirecting the
        # ledger does not cover it. Without this, fixture decisions land in the real
        # logs/alloc.jsonl — the record an operator reads to decide whether to enable
        # enforcement — and whether they do depends on module import order.
        p = mock.patch.object(broker, "_log_path",
                              lambda: os.path.join(self.dir, "alloc.jsonl"))
        p.start()
        self.addCleanup(p.stop)

    def _cfg_with(self, **lease):
        return mock.patch.object(settings, "_CFG", {"alloc": {
            "lease": lease,
            "models": {"want": {"weight_gb": 40, "min_free_gb": 4},
                       "victim": {"weight_gb": 40, "priority": "background",
                                  "driver_config": {"container": "c"},
                                  "release": {"stop_s": 5}, "restore": {"warm_s": 5}}}}})

    def test_eviction_is_off_by_default_even_when_enforcing(self):
        from ava_bridge.alloc import broker as B
        with self._cfg_with(enforce=True):
            self.assertTrue(B.enforcing())
            self.assertFalse(B.evicting(), "eviction must need its own opt-in")

    def test_enforcing_without_evicting_releases_nothing(self):
        from ava_bridge.alloc import broker as B
        with self._cfg_with(enforce=True), \
             mock.patch.object(B, "_execute") as ex, \
             mock.patch.object(B, "_candidates", lambda *a, **k: []), \
             mock.patch.object(B.capacity, "free_gib", return_value=5.0):
            with B.lease("want") as lz:
                self.assertTrue(lz.enforced)
        ex.assert_not_called()

    def test_both_switches_on_permits_execution(self):
        from ava_bridge.alloc import broker as B
        with self._cfg_with(enforce=True, evict=True), \
             mock.patch.object(B, "_execute", return_value=("victim",)) as ex, \
             mock.patch.object(B, "_candidates", lambda *a, **k: [
                 policy.Candidate(model_id="victim", rank=2, resident=True, gib=40.0,
                                  measured=True, options=(_opt(frees=40.0),))]), \
             mock.patch.object(B.capacity, "free_gib", return_value=5.0):
            with B.lease("want") as lz:
                self.assertEqual(lz.released, ("victim",))
        ex.assert_called_once()

    def test_a_quiesced_allocator_skips_every_action(self):
        """The allocator's failure mode is a no-op, not a loop."""
        from ava_bridge.alloc import breaker, broker as B
        for i in range(breaker.BUDGET_ACTIONS + 2):
            breaker.record_attempt(f"m{i}")
        drv = mock.Mock(observe_only=False)
        with self._cfg_with(enforce=True, evict=True), \
             mock.patch.object(B, "_driver", return_value=drv):
            pl = policy.Plan("want", admit=True, need_gib=40.0, steps=(
                policy.Step(model_id="victim", mode=ReleaseMode.STOP, expect_gib=40.0),))
            released = B._execute(pl, lambda _m: None)
        self.assertEqual(released, ())
        drv.release.assert_not_called()

    def test_restore_refuses_a_start_that_cannot_fit(self):
        """The actual cure for a retry storm: do not attempt the impossible.

        And it must not count as a failure, or a model waiting for room would
        eventually be given up on as though it were broken.
        """
        from ava_bridge.alloc import breaker, broker as B
        ledger.set_model_state("victim", released_by_us=True)
        drv = mock.Mock(observe_only=False)
        with self._cfg_with(enforce=True, evict=True), \
             mock.patch.object(B, "_driver", return_value=drv), \
             mock.patch.object(B.capacity, "free_gib", return_value=5.0):
            ok = B._restore("victim", lambda _m: None)
        self.assertFalse(ok)
        drv.acquire.assert_not_called()
        self.assertEqual(breaker.state("victim").get("fails", 0), 0)
        self.assertIn("free", breaker.state("victim")["deferred"])

    def test_restore_never_starts_what_we_did_not_stop(self):
        from ava_bridge.alloc import broker as B
        drv = mock.Mock(observe_only=False)
        with self._cfg_with(enforce=True, evict=True), \
             mock.patch.object(B, "_driver", return_value=drv), \
             mock.patch.object(B.capacity, "free_gib", return_value=999.0):
            self.assertFalse(B._restore("victim", lambda _m: None))
        drv.acquire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
