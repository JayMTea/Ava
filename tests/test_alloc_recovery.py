"""What the allocator promises, and what survives a reboot or a crash.

Four defects, all of which only bite once `alloc.lease.enforce: true` — which is
the state every operator is eventually told to reach, and the state the
development box already runs in:

  * **Double-booked memory.** `_acquire` planned against the raw pool with
    nothing subtracting what live leases had already been promised, so two
    callers for two different COLD models inside the same window were both told
    the room existed. On the deferred HTTP path that window is minutes — exactly
    the scenario docs/ALLOCATION.md sells `/lease` for.
  * **A reboot wedged the allocator.** breaker.json and run/alloc/models/*.json
    persisted CLOCK_MONOTONIC values with no boot marker, so after a restart a
    backoff deadline sat arbitrarily far in the future and every action was
    refused for as long as the previous uptime — with no alert, because
    watch._breaker_alerts only fires on quiesced/given_up.
  * **A crash mid-release stranded the model.** The `released_by_us` record was
    written AFTER the driver returned, and stopping a container takes minutes.
    A crash inside that window left no evidence, so `_restore` (which gates on
    that flag) refused to bring it back.
  * **Drivers reported success on a release the kernel never honoured.** All
    three discarded `wait_free`'s boolean, violating base.py's release contract
    verbatim.

Isolation per tests/test_alloc_isolation.py: ledger._dir is redirected, and the
decision log with it.
"""
import os
import tempfile
import threading
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-allocrec-test-")

from ava_bridge.alloc import breaker, broker, ledger  # noqa: E402
from ava_bridge.alloc.base import (  # noqa: E402
    ActionResult, DriverContext, ModelDriver, ReleaseMode, Residency,
)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ava-alloc-")
        for p in (mock.patch.object(ledger, "_dir", lambda: self.tmp),
                  mock.patch.object(broker, "_log_path",
                                    lambda: os.path.join(self.tmp, "alloc.jsonl"))):
            p.start()
            self.addCleanup(p.stop)

    def _grant(self, lid, *, need, at_free, models=("m",)):
        ledger.write_lease(lid, {"models": list(models), "granted": True,
                                 "need_gib": need, "free_at_grant": at_free})
        keep = ledger.flock(ledger.lease_lock_name(lid), wait_s=0.5)
        keep.__enter__()
        self.addCleanup(lambda: keep.__exit__(None, None, None))
        return keep


class ReservationTests(_Base):
    def test_the_full_need_is_reserved_before_the_model_loads(self):
        self._grant(ledger.new_lease_id(), need=70.0, at_free=100.0)
        self.assertAlmostEqual(ledger.reserved_gib(100.0), 70.0)

    def test_the_reservation_cancels_exactly_as_the_pool_drops(self):
        """No double-counting. Effective free must stay CONSTANT while the model
        loads — over-counting would drive it negative and make the planner
        over-evict, which is the harm this layer exists to prevent."""
        self._grant(ledger.new_lease_id(), need=70.0, at_free=100.0)
        for free_now in (100.0, 80.0, 65.0, 30.0):
            effective = free_now - ledger.reserved_gib(free_now)
            self.assertAlmostEqual(effective, 30.0, places=5, msg=f"free={free_now}")

    def test_a_lease_never_reserves_against_itself(self):
        lid = ledger.new_lease_id()
        self._grant(lid, need=70.0, at_free=100.0)
        self.assertEqual(ledger.reserved_gib(100.0, exclude=lid), 0.0)

    def test_an_ungranted_lease_reserves_nothing(self):
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"], "granted": False,
                                 "need_gib": 70.0, "free_at_grant": 100.0})
        keep = ledger.flock(ledger.lease_lock_name(lid), wait_s=0.5)
        keep.__enter__()
        self.addCleanup(lambda: keep.__exit__(None, None, None))
        self.assertEqual(ledger.reserved_gib(100.0), 0.0)

    def test_a_dead_holder_reserves_nothing(self):
        """The reservation is only as live as the lease, and a lease is live only
        while its holder holds the lock."""
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"], "granted": True,
                                 "need_gib": 70.0, "free_at_grant": 100.0})
        self.assertEqual(ledger.reserved_gib(100.0), 0.0)

    def test_a_legacy_record_without_free_at_grant_reserves_it_all(self):
        """Conservative: a record written before this field existed must not be
        read as 'already consumed'."""
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"], "granted": True, "need_gib": 12.0})
        keep = ledger.flock(ledger.lease_lock_name(lid), wait_s=0.5)
        keep.__enter__()
        self.addCleanup(lambda: keep.__exit__(None, None, None))
        self.assertAlmostEqual(ledger.reserved_gib(100.0), 12.0)

    def test_two_cold_models_are_not_both_promised_the_same_pool(self):
        """The defect itself."""
        self._grant(ledger.new_lease_id(), need=70.0, at_free=100.0, models=("a",))
        # A second caller asking for 70 GiB sees 30 GiB effective, not 100.
        self.assertLess(100.0 - ledger.reserved_gib(100.0), 70.0)


class RebootTests(_Base):
    def test_a_stale_backoff_deadline_does_not_survive_a_reboot(self):
        breaker._write({"models": {"m": {"next_attempt_at": ledger.now() + 100_000,
                                         "fails": 2}}})
        self._forge_foreign_boot()
        rec = breaker._read()
        self.assertNotIn("next_attempt_at", rec["models"]["m"])
        self.assertTrue(breaker.allowed("m")[0])

    def test_a_stale_quiesce_does_not_survive_a_reboot(self):
        breaker._write({"quiesced_at": ledger.now() + 100_000})
        self._forge_foreign_boot()
        self.assertFalse(breaker.quiesced()[0])

    def test_a_judgement_about_a_model_DOES_survive(self):
        """A reboot does not repair a broken model."""
        breaker._write({"models": {"m": {"given_up": True, "fails": 6,
                                         "reason": "start failed"}}})
        self._forge_foreign_boot()
        rec = breaker._read()
        self.assertTrue(rec["models"]["m"]["given_up"])
        self.assertEqual(rec["models"]["m"]["fails"], 6)
        self.assertFalse(breaker.allowed("m")[0])

    def test_model_state_survives_but_its_clocks_do_not(self):
        """Unlike a lease, a model we stopped is STILL stopped after a reboot —
        a container with `--restart no` does not come back on its own."""
        ledger.set_model_state("m", released_by_us=True, mode="stop",
                               at=999_999.0, restore_after=999_999.0)
        self._forge_foreign_boot_model("m")
        rec = ledger.model_state("m")
        self.assertTrue(rec["released_by_us"])
        self.assertNotIn("at", rec)
        self.assertLessEqual(rec["restore_after"], ledger.now())
        self.assertTrue(any(x["model_id"] == "m" for x in ledger.owed()))

    def _forge_foreign_boot(self):
        import json
        p = os.path.join(self.tmp, "breaker.json")
        obj = json.load(open(p, encoding="utf-8"))
        obj["boot_id"] = "a-previous-boot"
        json.dump(obj, open(p, "w", encoding="utf-8"))

    def _forge_foreign_boot_model(self, mid):
        import json
        p = os.path.join(self.tmp, "models", f"{mid}.json")
        obj = json.load(open(p, encoding="utf-8"))
        obj["boot_id"] = "a-previous-boot"
        json.dump(obj, open(p, "w", encoding="utf-8"))


class ReleaseIntentTests(_Base):
    def test_intent_is_recorded_before_the_driver_is_called(self):
        """Asserted from INSIDE release(), which proves the ordering directly and
        needs no crash simulation."""
        seen = {}

        class _D(ModelDriver):
            name = "probe"
            RELEASE_MODES = (ReleaseMode.STOP,)

            def residency(self):
                return Residency(resident=True, gib=1.0)

            def release(self, mode, *, need_gib=None, abort=None, timeout=180.0):
                seen["releasing"] = ledger.model_state("m").get("releasing")
                return ActionResult(ok=True, acted=True, freed_gib=1.0)

        step = mock.Mock(model_id="m", mode=ReleaseMode.STOP, expect_gib=1.0,
                         release_s=1.0)
        broker._release_one(step, _D(DriverContext(model_id="m")), lambda _m: None)
        self.assertTrue(seen["releasing"],
                        "the release ran with no record that we intended it")

    def test_a_release_we_could_not_measure_still_owes_a_restore(self):
        """ok=False, acted=True: the container IS down. Believing only `ok` is
        what left models stopped forever."""
        class _D(ModelDriver):
            name = "unmeasured"
            RELEASE_MODES = (ReleaseMode.STOP,)

            def residency(self):
                return Residency(resident=True, gib=1.0)

            def release(self, mode, *, need_gib=None, abort=None, timeout=180.0):
                return ActionResult(ok=False, acted=True, detail="pool did not move")

        step = mock.Mock(model_id="m2", mode=ReleaseMode.STOP, expect_gib=1.0,
                         release_s=1.0)
        ok = broker._release_one(step, _D(DriverContext(model_id="m2")), lambda _m: None)
        self.assertFalse(ok)
        self.assertTrue(ledger.model_state("m2")["released_by_us"])

    def test_a_release_that_never_happened_owes_nothing(self):
        class _D(ModelDriver):
            name = "declined"
            RELEASE_MODES = (ReleaseMode.STOP,)

            def residency(self):
                return Residency(resident=True, gib=1.0)

            def release(self, mode, *, need_gib=None, abort=None, timeout=180.0):
                return ActionResult(ok=False, acted=False, detail="command failed")

        step = mock.Mock(model_id="m3", mode=ReleaseMode.STOP, expect_gib=1.0,
                         release_s=1.0)
        broker._release_one(step, _D(DriverContext(model_id="m3")), lambda _m: None)
        rec = ledger.model_state("m3")
        self.assertFalse(rec.get("released_by_us"))
        self.assertFalse(rec.get("releasing"))

    def test_a_crashed_release_is_still_owed(self):
        """Only the intent was written — the process died before the result."""
        ledger.set_model_state("m4", releasing=True, mode="stop",
                               intent_at=ledger.now())
        self.assertTrue(broker._owe_restore("m4"))
        self.assertTrue(any(x["model_id"] == "m4" for x in ledger.owed()))


class ModelStateConcurrencyTests(_Base):
    def test_concurrent_merges_do_not_lose_a_field(self):
        """An unlocked read-modify-write could drop `released_by_us`, after which
        every restore path refuses the model — permanently."""
        barrier = threading.Barrier(2)

        def writer(key, value):
            barrier.wait()
            ledger.set_model_state("cc", **{key: value})

        ts = [threading.Thread(target=writer, args=a)
              for a in (("released_by_us", True), ("mode", "stop"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        rec = ledger.model_state("cc")
        self.assertTrue(rec.get("released_by_us"))
        self.assertEqual(rec.get("mode"), "stop")

    def test_the_mutex_is_not_the_actuation_lock(self):
        """`_release_one` holds models/<id>.lock for the whole actuation, and
        flock conflicts across descriptors within one process — so reusing it
        here would self-deadlock on every release."""
        with ledger.flock(os.path.join("models", "dl.lock"), wait_s=0.5):
            rec = ledger.set_model_state("dl", released_by_us=True)
        self.assertTrue(rec["released_by_us"])


if __name__ == "__main__":
    unittest.main()
