"""A deferred acquire: answer now, make room in the background.

Making room can take minutes — stopping a container, then waiting for the kernel to
actually hand the memory back. Holding an HTTP request open across that hands policy to
the client's socket timeout, a number chosen with no knowledge of what it is waiting
for. When it fires mid-release the client concludes nothing is coordinating, falls back
to coordinating for itself, and two components are now acting on one container. That is
the failure this whole layer exists to remove, and it is what these tests pin down:

  * **The verdict comes back before the release finishes**, and is the same verdict the
    release will end with. `admit` is computed from the plan, not from executing it, so
    there is nothing to wait for except the memory.
  * **`pending` is never mistaken for done.** The poll separates "still working" from
    "finished" from "gone", and only `gone` means nobody is acting on the caller's
    behalf — the one case where falling back locally is safe.
  * **Progress writes and heartbeats do not clobber each other.** Two writers now touch
    one lease record, and a lost update would strand a client polling forever.
  * **One actor per model.** Answering immediately makes concurrent releases of the same
    model likely rather than rare, and a duplicate `docker stop` mid-start is how a
    model comes back up in the middle of the render that displaced it.
  * **The synchronous path is unchanged**, so a client written against the older
    contract is never told it may start before the memory is free.

Run: .venv/bin/python -m pytest tests/test_alloc_deferred.py -q
"""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-deferred-test-")

from ava_bridge.alloc import broker, capacity, ledger, spec  # noqa: E402
from ava_bridge.alloc.base import (ActionResult, ReleaseMode,  # noqa: E402
                                  ReleaseOption, ReleasePlan, Residency)

RELEASE_GIB = 48.0
NEED_GIB = 70.0
FREE_SHORT = 30.0           # 30 + 48 = 78 >= 70: the plan admits *after* one release
FREE_AFTER = 78.0


class _Spec:
    """Only the attributes the broker reads. A real ModelSpec needs a config file."""

    def __init__(self, mid, *, need, priority="interactive", rank=0):
        self.id, self.need_gib, self.priority, self.rank = mid, need, priority, rank
        self.local, self.pinned, self.weight_gb = True, False, RELEASE_GIB
        self.driver, self.driver_config, self.readiness = "fake", {}, {}
        self.release, self.restore = {}, {}


class _Driver:
    """A releasable model whose release blocks until the test lets it finish."""

    observe_only = False

    def __init__(self, mid, pool):
        self.model_id = mid
        self.pool = pool                    # mutated on release, as a real one would
        self.gate = threading.Event()       # set -> release() may return
        self.calls: list[str] = []
        self.entered = threading.Event()

    def residency(self):
        return Residency(resident=True, gib=RELEASE_GIB, measured=True, ready=True)

    def plan_release(self, need_gib=None):
        return ReleasePlan(model_id=self.model_id, options=(
            ReleaseOption(mode=ReleaseMode.STOP, frees_gib=RELEASE_GIB,
                          release_s=30.0, restore_s=300.0, costs_known=True),))

    def release(self, mode, *, need_gib=None, abort=None, timeout=180.0):
        self.calls.append(mode.value)
        self.entered.set()
        self.gate.wait(timeout=10.0)
        self.pool["free"] = FREE_AFTER      # the memory is only back once this returns
        return ActionResult(ok=True, freed_gib=RELEASE_GIB, detail="stopped")

    def acquire(self, *, abort=None, timeout=600.0):
        return ActionResult(ok=True, detail="started")


class _Base(unittest.TestCase):
    """Enforcing and evicting, one releasable victim, a pool that is short."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ledger-deferred-")
        self.pool = {"free": FREE_SHORT}    # short: the plan must release something
        self.victim = _Driver("victim", self.pool)
        self.specs = {"want": _Spec("want", need=NEED_GIB),
                      "victim": _Spec("victim", need=RELEASE_GIB, priority="batch",
                                      rank=1)}
        for p in (mock.patch.object(ledger, "_dir", lambda: self.dir),
                  # The decision log is the evidence an operator enables enforcement
                  # from. Fixture decisions must not appear in it.
                  mock.patch.object(broker, "_log_path",
                                    lambda: os.path.join(self.dir, "alloc.jsonl")),
                  mock.patch.object(spec, "by_id", lambda: self.specs),
                  mock.patch.object(spec, "pool_config", lambda: {}),
                  mock.patch.object(capacity, "free_gib", lambda: self.pool["free"]),
                  mock.patch.object(broker, "enforcing", lambda: True),
                  mock.patch.object(broker, "evicting", lambda: True),
                  mock.patch.object(broker, "_driver", lambda s: self.victim),
                  mock.patch.object(broker, "_arm_restore_timer", lambda say: None)):
            p.start()
            self.addCleanup(p.stop)
        # Registered AFTER the patches, so it runs BEFORE they are undone: a release
        # thread that outlives the redirection writes its next state into the real
        # ledger. That is how `victim` once turned up in a live box's run/alloc.
        self.addCleanup(self._drain)
        self.addCleanup(self._drop_keepers)

    def _drain(self):
        self.victim.gate.set()          # never leave a release blocked on the fixture
        self.assertTrue(broker.wait_for_actuations(timeout=15.0),
                        "a release thread outlived the test's ledger redirection")

    def _drop_keepers(self):
        for keeper in list(broker._keepers.values()):
            keeper.release()
        broker._keepers.clear()

    def _finish_release(self):
        self.victim.gate.set()

    def _settle(self, lease_id, tries=200):
        """Poll a lease out of `pending`, the way a client does."""
        got = broker.state_api(lease_id)
        for _ in range(tries):
            if got["state"] != broker.PENDING:
                break
            time.sleep(0.02)
            got = broker.state_api(lease_id)
        return got


class DeferredAcquireTests(_Base):
    def test_the_verdict_arrives_before_the_release_finishes(self):
        started = time.monotonic()
        out = broker.acquire_api("want", reason="render", wait=False)
        elapsed = time.monotonic() - started

        self.assertEqual(out["state"], broker.PENDING)
        self.assertTrue(out["granted"], "the plan admits; only the room is outstanding")
        self.assertFalse(out["ready"], "pending must never read as ready")
        self.assertTrue(self.victim.entered.wait(2.0), "release did not start")
        self.assertLess(elapsed, 2.0, "the acquire waited for the release")
        self._finish_release()

    def test_pending_becomes_active_once_the_room_exists(self):
        out = broker.acquire_api("want", wait=False)
        lid = out["lease_id"]
        self.assertEqual(broker.state_api(lid)["state"], broker.PENDING)

        self._finish_release()
        got = self._settle(lid)
        self.assertEqual(got["state"], broker.ACTIVE)
        self.assertTrue(got["ready"])
        self.assertEqual(got["released"], ["victim"])

    def test_the_verdict_does_not_change_when_the_release_completes(self):
        # A caller acts on the answer it got first. If polling could turn a grant into a
        # denial, that answer was a guess.
        out = broker.acquire_api("want", wait=False)
        self._finish_release()
        got = self._settle(out["lease_id"])
        self.assertEqual(got["granted"], out["granted"])
        self.assertEqual(got["reason"], out["reason"])

    def test_polling_tells_a_client_how_long_to_wait(self):
        out = broker.acquire_api("want", wait=False)
        self.assertEqual(out["poll_after_s"], broker.POLL_AFTER_S)
        self._finish_release()

    def test_a_lease_that_is_gone_is_terminal_not_pending(self):
        # The one case where a client may safely coordinate for itself: nothing exists,
        # so nothing is acting on its behalf.
        got = broker.state_api("never-existed")
        self.assertEqual(got["state"], "gone")
        self.assertFalse(got["granted"])

    def test_releasing_while_pending_stops_the_lease_reappearing(self):
        # The actuation thread writes progress after the holder has gone. A blind write
        # would recreate a released record, leaving a lease nobody holds in the ledger.
        out = broker.acquire_api("want", wait=False)
        lid = out["lease_id"]
        broker.release_api(lid)
        self._finish_release()
        deadline = time.monotonic() + 5.0
        while self.victim.gate.is_set() and time.monotonic() < deadline:
            if ledger.read_lease(lid) is None and not broker._keepers.get(lid):
                break
            time.sleep(0.05)
        self.assertIsNone(ledger.read_lease(lid), "a released lease was resurrected")
        self.assertEqual(broker.state_api(lid)["state"], "gone")


class SynchronousPathTests(_Base):
    """The default must still hold the call open until the room exists."""

    def test_wait_true_returns_only_after_the_release(self):
        self.victim.gate.set()              # release completes without blocking
        out = broker.acquire_api("want", wait=True)
        self.assertEqual(out["state"], broker.ACTIVE)
        self.assertTrue(out["ready"])
        self.assertEqual(out["released"], ["victim"],
                         "the synchronous path reports what it released, as before")
        self.assertEqual(self.pool["free"], FREE_AFTER,
                         "returned before the memory was actually back")

    def test_wait_defaults_to_true(self):
        self.victim.gate.set()
        out = broker.acquire_api("want")
        self.assertEqual(out["state"], broker.ACTIVE)
        self.assertEqual(out["released"], ["victim"])


class RecordWriterTests(_Base):
    """Two writers, one record. A lost update strands a polling client."""

    def test_a_heartbeat_does_not_erase_pending_state(self):
        out = broker.acquire_api("want", wait=False)
        lid = out["lease_id"]
        broker.heartbeat_api(lid, ttl_s=120.0)
        self.assertEqual(broker.state_api(lid)["state"], broker.PENDING)
        self._finish_release()

    def test_progress_does_not_erase_the_deadline(self):
        out = broker.acquire_api("want", wait=False)
        lid = out["lease_id"]
        broker.heartbeat_api(lid, ttl_s=999.0)
        expires = ledger.read_lease(lid)["expires_at"]
        self._finish_release()
        self._settle(lid)
        self.assertEqual(ledger.read_lease(lid)["expires_at"], expires)

    def test_renewing_does_not_reset_a_holds_age(self):
        # `started` is what a hold is judged overdue by. Stamping it on every write
        # means a lease renewed every 100s can never look old, however long it is held.
        lid = ledger.new_lease_id()
        ledger.write_lease(lid, {"models": ["m"]})
        started = ledger.read_lease(lid)["started"]
        time.sleep(0.01)
        ledger.update_lease(lid, expires_at=1.0)
        self.assertEqual(ledger.read_lease(lid)["started"], started)

    def test_updating_a_vanished_lease_creates_nothing(self):
        self.assertIsNone(ledger.update_lease("gone-already", state=broker.ACTIVE))
        self.assertIsNone(ledger.read_lease("gone-already"))


class OneActorPerModelTests(_Base):
    """A duplicate release is not merely wasteful — it is a second actor.

    Asserted on the lock rather than on a call count, because a count race would make
    the test pass or fail on scheduling. The lock is the mechanism: while it is held, no
    other actor can reach the driver, whatever order the threads happen to run in.
    """

    def test_the_models_lock_is_held_across_the_release(self):
        broker.acquire_api("want", reason="a", wait=False)
        self.assertTrue(self.victim.entered.wait(2.0), "release did not start")
        name = broker._model_lock("victim")
        self.assertTrue(ledger.is_held(name),
                        "the release ran without holding the model's lock")
        with self.assertRaises(ledger.LockBusy):
            with ledger.flock(name, wait_s=0.1):
                pass
        self._finish_release()

    def test_the_lock_is_released_when_the_action_finishes(self):
        broker.acquire_api("want", reason="a", wait=False)
        self.assertTrue(self.victim.entered.wait(2.0))
        self._finish_release()
        self.assertEqual(self._settle(broker.snapshot()["leases"][0]["lease_id"]
                                     )["state"], broker.ACTIVE)
        self.assertFalse(ledger.is_held(broker._model_lock("victim")),
                         "a finished action left the model locked")

    def test_a_second_requester_waits_for_the_memory_it_did_not_release(self):
        # The peer's release is exactly the work this requester needed. Repeating the
        # action risks a stop landing after the peer has moved on to a restore, so it
        # waits on the pool instead — and still ends up with the room.
        broker.acquire_api("want", reason="a", wait=False)
        self.assertTrue(self.victim.entered.wait(2.0))
        waited = threading.Event()

        def wait_free(target, **kw):
            waited.set()
            self._finish_release()          # the peer's release completes
            return self.pool["free"] >= (target or 0)

        with mock.patch.object(capacity, "wait_free", wait_free):
            b = broker.acquire_api("want", reason="b", wait=False)
            got = self._settle(b["lease_id"])

        self.assertTrue(waited.is_set(), "the second requester did not wait")
        self.assertEqual(self.victim.calls, ["stop"],
                         f"released more than once: {self.victim.calls}")
        self.assertEqual(got["state"], broker.ACTIVE)
        self.assertEqual(got["released"], [],
                         "it must not claim credit for a release it did not perform")


if __name__ == "__main__":
    unittest.main()
