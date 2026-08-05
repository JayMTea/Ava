"""Owner-initiated release: a person naming a model, which is not the allocator acting.

Three properties, each guarding a way the button strands a model or lies about it.

  * **An owner release works with enforcement off.** `alloc.lease.enforce` governs
    whether Ava may act on its own judgement; a click is not Ava's judgement. Gating
    the owner on it made the control invisible on every default install, and — worse —
    `_restore` gated on the same switch, so a model released while advisory could
    never come back by ANY route: not the timer, not the watchdog, not the CLI.
  * **Nothing autonomous undoes it.** The cooldown sweep in `_release` stamps a restore
    on every model in `owed()`, not just the ones a given lease released, so any
    unrelated lease on the box would have put the owner's model back ~90s later.
  * **A release the box cannot measure is not the model's fault.** Where a card exists
    and no reader can reach it, `free_gib()` is watching system RAM, so freeing VRAM
    moves nothing and a perfectly good release arrives as `ok=False`. Charging that to
    the breaker gave up on a working model after two clicks half an hour apart —
    curable only from a terminal, which is the one place this feature exists to avoid.

Run: .venv/bin/python -m pytest tests/test_alloc_owner.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-owner-test-")

from ava_bridge import settings
from ava_bridge.alloc import breaker, broker, ledger, policy
from ava_bridge.alloc.base import ActionResult, ReleaseMode


class OwnerTestCase(unittest.TestCase):
    """Redirect every destination this exercise writes to.

    Three seams, not one: the ledger dir (model state + breaker.json), the decision
    log under logs/, and the actuation threads. See tests/README.md — each has leaked
    into a live box at least once.
    """

    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="owner-ledger-")
        p = mock.patch.object(ledger, "_dir", lambda: self._dir)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(broker, "_log_path",
                              lambda: os.path.join(self._dir, "alloc.jsonl"))
        p.start()
        self.addCleanup(p.stop)
        # Registered AFTER the patches, so it runs BEFORE them on teardown — a release
        # thread that outlives `p.stop()` writes its next update into the real ledger.
        self.addCleanup(broker.wait_for_actuations)

    def _advisory(self):
        """The shipped default: the allocator may not act on its own initiative."""
        return mock.patch.object(settings, "get_bool", lambda *a, **k: False)

    def _driver(self, **over):
        kw = dict(observe_only=False, SELF_RESTORING=False)
        kw.update(over)
        drv = mock.Mock(**kw)
        drv.release.return_value = ActionResult(ok=True, acted=True, freed_gib=12.0,
                                                detail="released")
        drv.acquire.return_value = ActionResult(ok=True, acted=True, detail="started")
        return drv


class RestoreGateTests(OwnerTestCase):
    def test_an_owner_can_bring_back_what_they_freed_while_advisory(self):
        """The blocker: `_restore` returned False before doing anything at all."""
        ledger.set_model_state("victim", released_by_us=True, released_by_owner=True)
        drv = self._driver()
        with self._advisory(), \
             mock.patch.object(broker, "_driver", return_value=drv), \
             mock.patch.object(broker.spec, "by_id",
                               return_value={"victim": mock.Mock(need_gib=1.0,
                                                                 restore={})}):
            self.assertFalse(broker._restore("victim", lambda _m: None))
            self.assertTrue(broker._restore("victim", lambda _m: None, owner=True))
        drv.acquire.assert_called_once()

    def test_a_successful_owner_restore_clears_the_provenance(self):
        ledger.set_model_state("victim", released_by_us=True, released_by_owner=True)
        with self._advisory(), \
             mock.patch.object(broker, "_driver", return_value=self._driver()), \
             mock.patch.object(broker.spec, "by_id",
                               return_value={"victim": mock.Mock(need_gib=1.0,
                                                                 restore={})}):
            broker._restore("victim", lambda _m: None, owner=True)
        rec = ledger.model_state("victim")
        self.assertFalse(rec.get("released_by_owner"))
        self.assertFalse(rec.get("released_by_us"))

    def test_a_failed_owner_restore_keeps_the_provenance(self):
        """Clearing it on failure would let the next autonomous sweep undo the choice.

        `set_model_state` merges with a plain `dict.update`, so passing the field as
        None writes None — which is falsy, and silently reopens the model to the
        cooldown sweep while it is still down.
        """
        ledger.set_model_state("victim", released_by_us=True, released_by_owner=True)
        drv = self._driver()
        drv.acquire.return_value = ActionResult(ok=False, acted=False, detail="nope")
        with self._advisory(), \
             mock.patch.object(broker, "_driver", return_value=drv), \
             mock.patch.object(broker.spec, "by_id",
                               return_value={"victim": mock.Mock(need_gib=1.0,
                                                                 restore={})}):
            self.assertFalse(broker._restore("victim", lambda _m: None, owner=True))
        self.assertTrue(ledger.model_state("victim").get("released_by_owner"))


class AutonomyTests(OwnerTestCase):
    def test_the_watchdog_does_not_undo_a_deliberate_release(self):
        ledger.set_model_state("victim", released_by_us=True, released_by_owner=True,
                               restore_after=0.0)
        drv = self._driver()
        # Enforcing AND evicting: the allocator is fully switched on, and must still
        # decline. This is the state in which the bug would actually bite.
        with mock.patch.object(settings, "get_bool", lambda *a, **k: True), \
             mock.patch.object(broker, "_driver", return_value=drv), \
             mock.patch.object(broker.spec, "by_id",
                               return_value={"victim": mock.Mock(need_gib=1.0,
                                                                 restore={})}):
            self.assertEqual(broker.restore_due(), [])
        drv.acquire.assert_not_called()

    def test_an_unrelated_lease_does_not_arm_a_restore_on_it(self):
        """`_release`'s sweep is blanket by design — it must skip the owner's model."""
        ledger.set_model_state("victim", released_by_us=True, released_by_owner=True)
        with mock.patch.object(settings, "get_bool", lambda *a, **k: True):
            broker._release(None, "somebody-else", lambda _m: None)
        self.assertIsNone(ledger.model_state("victim").get("restore_after"))

    def test_the_allocators_own_release_is_still_restored(self):
        """The filter must key on provenance, not on being released at all."""
        ledger.set_model_state("victim", released_by_us=True)
        with mock.patch.object(settings, "get_bool", lambda *a, **k: True):
            broker._release(None, "somebody-else", lambda _m: None)
        self.assertIsNotNone(ledger.model_state("victim").get("restore_after"))


class BreakerTests(OwnerTestCase):
    def test_an_unmeasurable_pool_is_not_the_models_fault(self):
        """The WSL2 laptop: a card exists, no reader reaches it, RAM never moves."""
        drv = self._driver()
        drv.release.return_value = ActionResult(ok=False, acted=True, freed_gib=None,
                                                detail="the pool did not move")
        step = policy.Step(model_id="victim", mode=ReleaseMode.UNLOAD, expect_gib=8.0)
        with mock.patch.object(broker.capacity, "accel_unreadable", return_value=True):
            self.assertFalse(broker._release_one(step, drv, lambda _m: None))
        st = breaker.state("victim")
        self.assertEqual(st.get("fails", 0), 0)
        self.assertEqual(st.get("cause"), "pool_unmeasurable")

    def test_a_release_that_really_failed_still_counts(self):
        """The exemption must not swallow genuine failures on a readable box."""
        drv = self._driver()
        drv.release.return_value = ActionResult(ok=False, acted=True, freed_gib=None,
                                                detail="the pool did not move")
        step = policy.Step(model_id="victim", mode=ReleaseMode.UNLOAD, expect_gib=8.0)
        with mock.patch.object(broker.capacity, "accel_unreadable", return_value=False):
            self.assertFalse(broker._release_one(step, drv, lambda _m: None))
        self.assertEqual(breaker.state("victim").get("fails"), 1)

    def test_a_human_action_is_not_refused_by_the_thrash_guards(self):
        """`given_up` is a loop guard. A person clicking is bounded by the person."""
        breaker.record_failure("victim", "boom", cause="start_failed")
        for _ in range(8):
            breaker.record_failure("victim", "boom", cause="start_failed")
        self.assertTrue(breaker.state("victim").get("given_up"))
        self.assertFalse(breaker.allowed("victim")[0])
        self.assertTrue(breaker.allowed("victim", human=True)[0])

    def test_a_human_action_still_spends_budget(self):
        """Permissive is not unaccounted: the action stays visible in the same books."""
        before = len(breaker._read().get("actions") or [])
        breaker.record_attempt("victim")
        self.assertEqual(len(breaker._read().get("actions") or []), before + 1)


class ProvenanceTests(OwnerTestCase):
    def test_a_release_records_who_asked(self):
        step = policy.Step(model_id="victim", mode=ReleaseMode.UNLOAD, expect_gib=8.0)
        broker._release_one(step, self._driver(), lambda _m: None, owner=True)
        rec = ledger.model_state("victim")
        # BOTH flags: `released_by_us` is what `_owe_restore` reads and what makes a
        # crash mid-release recoverable; `released_by_owner` is provenance on top.
        self.assertTrue(rec.get("released_by_us"))
        self.assertTrue(rec.get("released_by_owner"))

    def test_an_allocator_release_clears_a_stale_owner_flag(self):
        """Written unconditionally, so a later autonomous release does not inherit it."""
        ledger.set_model_state("victim", released_by_owner=True)
        step = policy.Step(model_id="victim", mode=ReleaseMode.UNLOAD, expect_gib=8.0)
        broker._release_one(step, self._driver(), lambda _m: None, owner=False)
        self.assertFalse(ledger.model_state("victim").get("released_by_owner"))


class WatchdogTests(unittest.TestCase):
    """A deliberate release must not read as the six-day outage."""

    def test_released_outranks_degraded(self):
        from ava_bridge.alloc import watch
        # An engine that dropped its weights on request keeps serving its port and
        # answers readiness with "no model loaded" — byte for byte the silent-fallback
        # shape. Only provenance separates them.
        row = {"id": "gpu-voice", "local": True, "resident": True, "ready": False,
               "released_by_owner": True}
        self.assertEqual(watch._state_of(row), "released")
        self.assertEqual(watch._state_of({**row, "released_by_owner": False}),
                         "degraded")

    def test_a_freed_model_is_not_reported_as_unable_to_start(self):
        from ava_bridge.alloc import watch
        row = {"id": "coder", "local": True, "resident": False, "cold_load_ok": False,
               "released_by_owner": True}
        self.assertFalse(watch._wants_start(row))
        self.assertEqual(watch._state_of(row), "released")


if __name__ == "__main__":
    unittest.main()
