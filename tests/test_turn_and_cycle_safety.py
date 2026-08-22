"""A turn must always reach a terminal status, and a distill cycle must be single-flight.

Both defects here were invisible: nothing polled a stuck turn in a test, and
nothing started two background cycles at once. The symptom in production is a
spinner that never stops and a second thread quietly polling a record forever.

The cycle half of this file used to guard `learning.run_all_cycles`. Learning was
removed; memory distillation, which rode that same lock, did not — so the guard
moved to `distill.run_distill_cycle` with it rather than being deleted alongside
the feature that happened to host it.
"""
import asyncio
import threading
import time
import unittest
from unittest import mock

from ava_bridge import distill, state, turns


class TurnAlwaysTerminatesTests(unittest.TestCase):
    def setUp(self):
        with state.turns_lock:
            state.turns.clear()

    def tearDown(self):
        with state.turns_lock:
            state.turns.clear()

    def _seed(self, tid: str, **over) -> None:
        rec = {"id": tid, "status": "running", "created": time.time(), "steps": []}
        rec.update(over)
        with state.turns_lock:
            state.turns[tid] = rec

    def test_an_unexpected_raise_marks_the_turn_error_not_running(self):
        """The whole point: _run_turn is a daemon-thread target, so anything it
        raises outside its own handlers used to vanish with the thread and strand
        the turn at status='running' for the lifetime of the process."""
        self._seed("t1")
        boom = KeyError("state.turns went away")
        with mock.patch.object(turns, "_run_turn", side_effect=boom):
            with self.assertRaises(KeyError):
                turns._run_turn_guarded("t1", "hi", "sid", "")
        with state.turns_lock:
            self.assertEqual(state.turns["t1"]["status"], "error")
            self.assertIn("KeyError", state.turns["t1"]["error"])

    def test_a_baseexception_also_terminates_the_turn(self):
        self._seed("t2")
        with mock.patch.object(turns, "_run_turn", side_effect=MemoryError()):
            with self.assertRaises(MemoryError):
                turns._run_turn_guarded("t2", "hi", "sid", "")
        with state.turns_lock:
            self.assertEqual(state.turns["t2"]["status"], "error")

    def test_set_turn_on_an_evicted_turn_is_a_no_op_not_a_keyerror(self):
        turns._set_turn("never-existed", status="done")  # must not raise

    def test_prune_never_evicts_a_running_turn(self):
        """_prune_turns runs at the top of every start_turn and used to test age
        only — so an unrelated new message could delete a turn that was still
        working (an agent turn can legitimately outlive the hour: OC_TIMEOUT is up
        to 600s and an image pickup waits 120s)."""
        old = time.time() - 7200
        self._seed("running-old", created=old, status="running")
        self._seed("done-old", created=old, status="done")
        self._seed("error-old", created=old, status="error")
        turns._prune_turns(max_age=3600.0)
        with state.turns_lock:
            self.assertIn("running-old", state.turns, "a live turn was evicted")
            self.assertNotIn("done-old", state.turns)
            self.assertNotIn("error-old", state.turns)

    def test_prune_still_collects_finished_turns(self):
        self._seed("fresh", created=time.time(), status="done")
        self._seed("stale", created=time.time() - 7200, status="done")
        turns._prune_turns(max_age=3600.0)
        with state.turns_lock:
            self.assertIn("fresh", state.turns)
            self.assertNotIn("stale", state.turns)


class DistillCycleIsSingleFlightTests(unittest.TestCase):
    """The distiller carries the same claim, for the same reason.

    Distillation used to ride the learning cycle's lock. It has its own module
    and its own scheduler now (ava_bridge/distill.py), so the single-flight
    property had to move with it or it would have been silently dropped: the
    cursor read at the top of run_cycle and the write at the bottom straddle a
    45s LLM call, and two overlapping runs both process the same messages and
    both advance it."""

    def test_a_second_concurrent_cycle_is_refused_not_queued(self):
        started = threading.Event()
        release = threading.Event()

        async def slow_run_cycle():
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait)
            return 3

        with mock.patch.object(distill.memory_distiller, "run_cycle", slow_run_cycle):
            out: dict = {}

            def first():
                out["a"] = asyncio.run(distill.run_distill_cycle())

            t = threading.Thread(target=first, daemon=True)
            t.start()
            self.assertTrue(started.wait(5), "the first cycle never started")

            second = asyncio.run(distill.run_distill_cycle())
            self.assertFalse(second["ran"])
            self.assertIn("already running", second["reason"])

            release.set()
            t.join(10)
            self.assertEqual(out["a"]["memory_facts"], 3)

        # The claim is released, so a later cycle runs normally.
        async def quick():
            return 0

        with mock.patch.object(distill.memory_distiller, "run_cycle", quick):
            self.assertTrue(asyncio.run(distill.run_distill_cycle())["ran"])

    def test_a_failing_distiller_never_raises_into_its_caller(self):
        """The scheduler thread and an API handler both call this; an exception
        escaping here takes out the thread and leaves memory silently dead."""
        async def boom():
            raise RuntimeError("router down")

        with mock.patch.object(distill.memory_distiller, "run_cycle", boom):
            out = asyncio.run(distill.run_distill_cycle())
        self.assertTrue(out["ran"])
        self.assertTrue(out["memory_error"])


if __name__ == "__main__":
    unittest.main()
