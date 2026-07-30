"""A turn must always reach a terminal status, and a learning cycle must be single-flight.

Both defects here were invisible: nothing polled a stuck turn in a test, and
nothing started two learning cycles at once. The symptom in production is a
spinner that never stops and a second thread quietly polling a record forever.
"""
import asyncio
import threading
import time
import unittest
from unittest import mock

from ava_bridge import learning, state, turns


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


class LearningCycleIsSingleFlightTests(unittest.TestCase):
    def test_a_second_concurrent_cycle_is_refused_not_queued(self):
        """Two entry points exist — POST /api/learning/run and the scheduler
        thread — and a cycle spends most of its life awaiting a 45s LLM call, so
        overlap needs no load at all. Both runs would read the same distiller
        cursor, process the same messages, and both advance it."""
        started = threading.Event()
        release = threading.Event()

        async def slow_inner():
            started.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait)
            return {"ran": True}

        with mock.patch.object(learning, "_run_all_cycles_locked", slow_inner):
            out: dict = {}

            def first():
                out["a"] = asyncio.run(learning.run_all_cycles())

            t = threading.Thread(target=first, daemon=True)
            t.start()
            self.assertTrue(started.wait(5), "the first cycle never started")

            # Second caller, while the first still holds the claim.
            second = asyncio.run(learning.run_all_cycles())
            self.assertFalse(second["ran"])
            self.assertIn("already running", second["reason"])

            release.set()
            t.join(10)
            self.assertTrue(out["a"]["ran"])

        # The claim is released, so a later cycle runs normally.
        with mock.patch.object(learning, "_run_all_cycles_locked",
                              lambda: asyncio.sleep(0, result={"ran": True})):
            self.assertTrue(asyncio.run(learning.run_all_cycles())["ran"])


if __name__ == "__main__":
    unittest.main()
