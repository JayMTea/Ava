"""Breaker and action budget: a retry storm must be arithmetically impossible.

`test_a_permanently_failing_start_gives_up_quickly` is the regression for the incident
that motivated this whole layer: a container supervised with an unbounded restart
policy was asked to start a model that could not fit, and retried **7,997 times**
because nothing capped it and nothing had told it the attempt could not succeed.

The two answers are tested separately because they are independent, and the first one
matters more:

  * `InsufficientMemoryTests` — a start that provably cannot fit is **not attempted**,
    and does not count as a failure. Not attempting is the cure; capping is damage
    control.
  * `GiveUpTests` — a genuinely broken model costs a handful of attempts and then
    stops, rather than looping.

Also guarded: the classic breaker bug where one brief success resets the counter (so a
crash-loop that looks healthy for three seconds runs forever), and the allocator's own
thrash guard, whose whole job is to make its failure mode a **no-op rather than a
loop**.

State lives in the ledger, so these tests point it at a temp dir and use a fake clock
rather than sleeping.

Run: .venv/bin/python -m pytest tests/test_alloc_breaker.py -q
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-breaker-test-")

from ava_bridge.alloc import breaker, ledger


class BreakerTestCase(unittest.TestCase):
    """Isolated ledger + a clock we control, so nothing sleeps."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="breaker-")
        self.t = 1000.0
        for p in (mock.patch.object(ledger, "_dir", lambda: self.dir),
                  mock.patch.object(ledger, "now", lambda: self.t)):
            p.start()
            self.addCleanup(p.stop)

    def advance(self, seconds: float):
        self.t += seconds


class GiveUpTests(BreakerTestCase):
    def test_a_permanently_failing_start_gives_up_quickly(self):
        """The 7,997-restart regression, bounded.

        A driver that always fails must cost a handful of attempts over roughly half
        an hour, then stop — not an unbounded loop.
        """
        attempts = 0
        while True:
            ok, why = breaker.allowed("m")
            if not ok and "gave up" in why:
                break
            if not ok:
                # Backing off: jump to the retry time rather than sleeping.
                st = breaker.state("m")
                self.advance(max(1.0, float(st["next_attempt_at"]) - self.t))
                continue
            attempts += 1
            breaker.record_attempt("m")
            breaker.record_failure(
                "m",
                "Free memory on device cuda:0 (33.95/121.69 GiB) is less than desired",
                cause="start_failed")
            self.assertLess(attempts, 20, "breaker failed to bound the attempts")
        self.assertLessEqual(attempts, breaker.MAX_ATTEMPTS)
        self.assertGreaterEqual(attempts, 2, "should retry at least a couple of times")
        st = breaker.state("m")
        self.assertTrue(st["given_up"])
        self.assertIn("Free memory", st["reason"])

    def test_backoff_grows_between_attempts(self):
        delays = []
        for _ in range(3):
            breaker.record_failure("m", "nope", cause="start_failed")
            delays.append(float(breaker.state("m")["next_attempt_at"]) - self.t)
        # Jitter is +/-20%, so compare with a margin rather than exact doubling.
        self.assertGreater(delays[1], delays[0] * 1.3)
        self.assertGreater(delays[2], delays[1] * 1.3)

    def test_a_given_up_model_is_not_attempted_again(self):
        for _ in range(breaker.MAX_ATTEMPTS):
            breaker.record_failure("m", "nope", cause="start_failed")
        ok, why = breaker.allowed("m")
        self.assertFalse(ok)
        self.assertIn("gave up", why)
        # And the message must say how to clear it — a dead end with no exit is worse
        # than the loop it replaced.
        self.assertIn("ava alloc reset", why)

    def test_reset_reopens_a_given_up_model(self):
        for _ in range(breaker.MAX_ATTEMPTS):
            breaker.record_failure("m", "nope", cause="start_failed")
        breaker.reset("m")
        self.assertTrue(breaker.allowed("m")[0])

    def test_giving_up_also_triggers_on_a_long_failure_window(self):
        breaker.record_failure("m", "nope", cause="start_failed")
        self.advance(breaker.MAX_WINDOW_S + 1)
        breaker.record_failure("m", "nope", cause="start_failed")
        self.assertTrue(breaker.state("m")["given_up"])


class InsufficientMemoryTests(BreakerTestCase):
    """A model waiting for room is not a broken model."""

    def test_insufficient_memory_never_consumes_the_failure_budget(self):
        for _ in range(breaker.MAX_ATTEMPTS * 3):
            breaker.record_failure("m", "20GiB free < 60GiB needed",
                                   cause="insufficient_memory")
        st = breaker.state("m")
        self.assertEqual(st.get("fails", 0), 0)
        self.assertFalse(st.get("given_up"))
        self.assertTrue(breaker.allowed("m")[0],
                        "a model deferred for memory must stay retryable forever")

    def test_deferral_is_recorded_so_it_is_visible(self):
        breaker.record_failure("m", "20GiB free < 60GiB needed",
                               cause="insufficient_memory")
        self.assertIn("20GiB", breaker.state("m")["deferred"])
        self.assertEqual(
            breaker.snapshot()["models"]["m"]["deferred"],
            "20GiB free < 60GiB needed")

    def test_a_real_failure_clears_a_stale_deferral(self):
        breaker.record_failure("m", "no room", cause="insufficient_memory")
        breaker.record_failure("m", "actually broken", cause="start_failed")
        st = breaker.state("m")
        self.assertIsNone(st.get("deferred"))
        self.assertEqual(st["fails"], 1)


class StabilityWindowTests(BreakerTestCase):
    """One brief success must not clear a crash-loop's record."""

    def test_a_single_success_does_not_reset_the_counter(self):
        breaker.record_failure("m", "nope", cause="start_failed")
        breaker.record_failure("m", "nope", cause="start_failed")
        breaker.record_success("m", ready=True)
        self.assertEqual(breaker.state("m")["fails"], 2,
                         "a success alone must not clear the record — that is how a "
                         "crash-loop resets its own counter forever")

    def test_sustained_readiness_clears_the_counter(self):
        breaker.record_failure("m", "nope", cause="start_failed")
        breaker.note_ready("m", True)
        self.advance(breaker.STABILITY_WINDOW_S + 1)
        breaker.note_ready("m", True)
        self.assertEqual(breaker.state("m").get("fails", 0), 0)

    def test_readiness_lost_before_the_window_does_not_clear(self):
        breaker.record_failure("m", "nope", cause="start_failed")
        breaker.note_ready("m", True)
        self.advance(breaker.STABILITY_WINDOW_S / 2)
        breaker.note_ready("m", False)          # flapped
        self.advance(breaker.STABILITY_WINDOW_S)
        breaker.note_ready("m", True)           # clock restarts from here
        self.assertEqual(breaker.state("m")["fails"], 1)


class ActionBudgetTests(BreakerTestCase):
    """The allocator's failure mode must be a no-op, never a loop."""

    def test_exceeding_the_budget_quiesces_the_allocator(self):
        for i in range(breaker.BUDGET_ACTIONS + 2):
            breaker.record_attempt(f"m{i}")
        q, why = breaker.quiesced()
        self.assertTrue(q)
        self.assertIn("exceeded", why)
        # QUIESCED means nothing may act, for any model.
        ok, reason = breaker.allowed("anything")
        self.assertFalse(ok)
        self.assertIn("QUIESCED", reason)

    def test_the_budget_is_a_rolling_window(self):
        for i in range(breaker.BUDGET_ACTIONS - 1):
            breaker.record_attempt(f"m{i}")
        self.assertTrue(breaker.budget_ok()[0])
        self.advance(breaker.BUDGET_WINDOW_S + 1)   # the old actions age out
        self.assertTrue(breaker.budget_ok()[0])
        self.assertEqual(breaker.snapshot()["actions_in_window"], 0)

    def test_quiesce_self_clears_after_the_cooloff(self):
        for i in range(breaker.BUDGET_ACTIONS + 2):
            breaker.record_attempt(f"m{i}")
        self.assertTrue(breaker.quiesced()[0])
        self.advance(breaker.QUIESCE_COOLOFF_S + 1)
        self.assertFalse(breaker.quiesced()[0],
                         "quiesce must self-clear; a permanent one needs a human for "
                         "no reason")

    def test_resume_clears_quiesce_immediately(self):
        for i in range(breaker.BUDGET_ACTIONS + 2):
            breaker.record_attempt(f"m{i}")
        breaker.reset(None)
        self.assertFalse(breaker.quiesced()[0])

    def test_the_budget_is_shared_across_processes(self):
        """Counted in the ledger, not in memory.

        A per-process counter would let each process independently spend the whole
        budget, which is precisely the multiplication this is meant to prevent.
        """
        for i in range(breaker.BUDGET_ACTIONS + 2):
            breaker.record_attempt(f"m{i}")
        # A "second process" reads the same ledger: no in-memory state carried over.
        import importlib
        fresh = importlib.reload(breaker)
        with mock.patch.object(ledger, "_dir", lambda: self.dir), \
             mock.patch.object(ledger, "now", lambda: self.t):
            self.assertTrue(fresh.quiesced()[0])
        importlib.reload(breaker)


class ResilienceTests(BreakerTestCase):
    def test_a_corrupt_breaker_file_is_treated_as_empty(self):
        with open(os.path.join(self.dir, "breaker.json"), "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertTrue(breaker.allowed("m")[0])
        self.assertEqual(breaker.snapshot()["models"], {})

    def test_an_unwritable_ledger_does_not_raise(self):
        with mock.patch.object(ledger, "_dir", lambda: "/nonexistent/deep/path"):
            breaker.record_failure("m", "nope", cause="start_failed")
            self.assertTrue(breaker.allowed("m")[0])


if __name__ == "__main__":
    unittest.main()
