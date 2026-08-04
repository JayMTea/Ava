"""The provision job must never strand its single slot, and never lie in the log.

`ava_bridge/provision_job.py` holds one run at a time. Two failure modes matter
more than the happy path:

  * A runner that RAISES must still reach a terminal status. If it does not, the
    slot stays "running" forever and every later Apply 409s — the button is dead
    until the bridge restarts, with nothing on screen explaining why.
  * Steps must come from install.sh's `[ava::step]` channel and stay OUT of the
    log. Parsing prose instead is how a progress view silently stops updating the
    day somebody rewords an `echo`.

Every test injects a fake runner, so nothing here touches a runtime, a sandbox, or
the real `nemoclaw` binary — which matters: `nemoclaw <sandbox> status --json` was
observed restarting the host's OpenShell gateway and killing a host process.
"""
from __future__ import annotations

import time
import unittest

from unittest import mock

from ava_bridge import provision, provision_job


def _wait_idle(timeout: float = 5.0) -> dict:
    """Block until the worker thread reaches a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = provision_job.snapshot()
        if snap["status"] in ("done", "error"):
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job never finished: {provision_job.snapshot()}")


class ProvisionJobTests(unittest.TestCase):

    def setUp(self) -> None:
        provision_job.reset_for_tests()
        self.addCleanup(provision_job.reset_for_tests)
        # These tests are about JOB MECHANICS. Without this the post-deploy
        # assertion runs against the developer's real box — and on the machine
        # this was written on it correctly found an unapplied egress policy and
        # turned every "successful" fake run red. The veto itself is pinned in
        # VerifyVetoTests below.
        clean = {"checked": True, "live": True, "items": [], "failed": []}
        p = mock.patch.object(provision, "verify", return_value=clean)
        p.start()
        self.addCleanup(p.stop)

    # ---- lifecycle ---------------------------------------------------------
    def test_a_successful_run_reaches_done(self):
        def runner(scope, auto_install, on_line):
            on_line("[ava] applying policy: ava-weather.yaml…")
            return {"ok": True, "detail": "provisioned", "steps": []}

        started, snap = provision_job.start(scope="all", runner=runner)
        self.assertTrue(started)
        # Not asserted as "running": the worker is a real thread, and a fake
        # runner can finish before start() returns. Only the terminal state is
        # deterministic.
        self.assertIn(snap["status"], ("running", "done"))
        self.assertIsNotNone(snap["id"])
        final = _wait_idle()
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["rc"], 0)
        self.assertIsNotNone(final["ended_at"])

    def test_a_failing_run_reaches_error_with_a_nonzero_rc(self):
        def runner(scope, auto_install, on_line):
            return {"ok": False, "detail": "sandbox not found", "steps": []}

        provision_job.start(scope="all", runner=runner)
        final = _wait_idle()
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["rc"], 1)
        self.assertIn("sandbox not found", final["detail"])

    def test_a_runner_that_raises_still_reaches_a_terminal_status(self):
        """THE test. Without the `finally`, the slot stays 'running' forever and
        every later Apply 409s with nothing explaining why."""
        def runner(scope, auto_install, on_line):
            raise RuntimeError("boom")

        provision_job.start(scope="all", runner=runner)
        final = _wait_idle()
        self.assertEqual(final["status"], "error")
        self.assertIn("crashed", final["detail"])
        # And the slot is genuinely free again.
        started, _ = provision_job.start(scope="all",
                                         runner=lambda **k: {"ok": True, "steps": []})
        self.assertTrue(started, "the slot was never released after a crash")
        _wait_idle()

    def test_a_second_start_while_running_is_refused(self):
        release = __import__("threading").Event()

        def slow(scope, auto_install, on_line):
            release.wait(timeout=5)
            return {"ok": True, "steps": []}

        provision_job.start(scope="all", runner=slow)
        try:
            started, snap = provision_job.start(scope="persona",
                                                runner=lambda **k: {"ok": True})
            self.assertFalse(started, "two deploys into one sandbox would interleave "
                                      "writes to openclaw.json")
            self.assertEqual(snap["status"], "running")
            self.assertEqual(snap["scope"], "all", "the 409 reports the wrong run")
        finally:
            release.set()
            _wait_idle()

    def test_an_unknown_scope_never_starts_a_run(self):
        started, snap = provision_job.start(scope="everything",
                                            runner=lambda **k: {"ok": True})
        self.assertFalse(started)
        self.assertEqual(snap["error_code"], "unknown_scope")
        self.assertEqual(snap["status"], "idle")

    # ---- the step channel --------------------------------------------------
    def test_step_lines_become_steps_and_stay_out_of_the_log(self):
        def runner(scope, auto_install, on_line):
            on_line("[ava] applying policy: ava-weather.yaml…")
            on_line("[ava::step]\tpolicies\tava-weather\tok\t")
            on_line("[ava::step]\tpolicies\tava-email\tfail\tpolicy-add rejected the file")
            return {"ok": True, "steps": []}

        provision_job.start(scope="policies", runner=runner)
        final = _wait_idle()
        ids = [(s["scope"], s["id"], s["ok"]) for s in final["steps"]
               if s["scope"] == "policies"]
        self.assertEqual(ids, [("policies", "ava-weather", True),
                               ("policies", "ava-email", False)])
        self.assertEqual([s["detail"] for s in final["steps"]
                          if s["id"] == "ava-email"],
                         ["policy-add rejected the file"])
        for line in final["log"]:
            self.assertNotIn("[ava::step]", line,
                             "the machine channel is leaking into the human log")

    def test_a_malformed_step_line_is_kept_as_log_rather_than_dropped(self):
        def runner(scope, auto_install, on_line):
            on_line("[ava::step]\tbroken")
            return {"ok": True, "steps": []}

        provision_job.start(scope="all", runner=runner)
        final = _wait_idle()
        # Ignore the `runtime`-scoped rows folded in at the end (the deploy step
        # and the verify verdict); this is about the install.sh step channel.
        self.assertEqual([s for s in final["steps"] if s["scope"] != "runtime"], [])
        self.assertTrue(any("broken" in ln for ln in final["log"]),
                        "an unparseable step line vanished entirely")

    def test_runtime_steps_are_folded_in_at_the_end(self):
        def runner(scope, auto_install, on_line):
            return {"ok": True, "steps": [{"step": "deploy", "ok": True,
                                           "detail": "agent/install.sh"}]}

        provision_job.start(scope="all", runner=runner)
        final = _wait_idle()
        self.assertIn(("runtime", "deploy", True),
                      [(s["scope"], s["id"], s["ok"]) for s in final["steps"]])

    # ---- the log -----------------------------------------------------------
    def test_the_log_is_bounded(self):
        def runner(scope, auto_install, on_line):
            for i in range(2000):
                on_line(f"line {i}")
            return {"ok": True, "steps": []}

        provision_job.start(scope="all", runner=runner)
        final = _wait_idle()
        self.assertLessEqual(len(final["log"]), 400,
                             "an unbounded log lets a chatty deploy eat memory")
        self.assertIn("line 1999", final["log"][-1])

    def test_since_ships_only_new_lines_and_seq_is_monotonic(self):
        def runner(scope, auto_install, on_line):
            for i in range(10):
                on_line(f"line {i}")
            return {"ok": True, "steps": []}

        provision_job.start(scope="all", runner=runner)
        final = _wait_idle()
        self.assertEqual(final["seq"], 10)
        tail = provision_job.snapshot(since=7)
        self.assertEqual(tail["log"], ["line 7", "line 8", "line 9"])
        self.assertEqual(provision_job.snapshot(since=10)["log"], [])
        # A cursor past the end must not explode or return the whole log.
        self.assertEqual(provision_job.snapshot(since=999)["log"], [])

    def test_a_late_line_from_a_previous_run_is_dropped(self):
        """A worker whose slot was taken must not write into the new run."""
        captured = {}

        def runner(scope, auto_install, on_line):
            captured["on_line"] = on_line
            return {"ok": True, "steps": []}

        provision_job.start(scope="all", runner=runner)
        _wait_idle()
        stale_sink = captured["on_line"]
        provision_job.start(scope="skills", runner=lambda **k: {"ok": True, "steps": []})
        _wait_idle()
        before = provision_job.snapshot()["seq"]
        stale_sink("a line from the old run")
        self.assertEqual(provision_job.snapshot()["seq"], before,
                         "a superseded run wrote into the current job's log")


class VerifyVetoTests(unittest.TestCase):
    """`install.sh` exiting 0 means "I ran", not "it worked".

    A rejected egress policy is reported and skipped by design, so a deploy can
    complete cleanly having silently dropped one. The post-deploy assertion is
    what turns that from a green run into a red one — and on the box this was
    written on it fires for real, because `ava-email-read-only` has never been
    accepted by the sandbox.
    """

    def setUp(self) -> None:
        provision_job.reset_for_tests()
        self.addCleanup(provision_job.reset_for_tests)

    def test_a_clean_run_that_did_not_land_is_reported_as_an_error(self):
        verdict = {"checked": True, "live": True, "items": [{}],
                   "failed": ["policies/ava-email-read-only"]}
        with mock.patch.object(provision, "verify", return_value=verdict):
            provision_job.start(scope="all",
                                runner=lambda **k: {"ok": True, "steps": []})
            final = _wait_idle()
        self.assertEqual(final["status"], "error",
                         "a deploy that dropped a policy still reported success")
        self.assertIn("ava-email-read-only", final["detail"])
        self.assertIn(("runtime", "verify", False),
                      [(s["scope"], s["id"], s["ok"]) for s in final["steps"]])

    def test_an_unverifiable_run_is_not_downgraded(self):
        """Sandbox down ⇒ `checked: false` ⇒ no verdict either way. A deploy must
        not be marked failed just because we could not look."""
        verdict = {"checked": False, "live": False, "items": [], "failed": []}
        with mock.patch.object(provision, "verify", return_value=verdict):
            provision_job.start(scope="all",
                                runner=lambda **k: {"ok": True, "steps": []})
            final = _wait_idle()
        self.assertEqual(final["status"], "done")
        self.assertNotIn("verify", [s["id"] for s in final["steps"]])

    def test_a_broken_check_never_fails_an_otherwise_good_deploy(self):
        with mock.patch.object(provision, "verify", side_effect=RuntimeError("boom")):
            provision_job.start(scope="all",
                                runner=lambda **k: {"ok": True, "steps": []})
            final = _wait_idle()
        self.assertEqual(final["status"], "done")


class SseSurfaceTests(unittest.TestCase):
    def test_there_is_still_exactly_one_sse_endpoint(self):
        """/api/stream/ops stays the single streaming surface. The provision job
        is polled instead — deliberately, because that channel carries the turn,
        hardware, device and alert deltas a browser subscribes to for Operations,
        and a run that belongs to whoever started it is none of those."""
        import pathlib
        import subprocess
        root = pathlib.Path(__file__).resolve().parents[1]
        tracked = subprocess.run(["git", "ls-files", "ava_bridge/*.py"], cwd=root,
                                 capture_output=True, text=True).stdout.split()
        # SERVES an SSE stream, not merely mentions the media type: mcp_client.py
        # consumes one, and the qa/tests tiers name it in assertions.
        hits = []
        for p in tracked:
            src = (root / p).read_text(encoding="utf-8", errors="ignore")
            if "StreamingResponse" in src and "text/event-stream" in src:
                hits.append(p)
        self.assertEqual(hits, ["ava_bridge/ops_api.py"],
                         f"the set of SSE endpoints changed: {hits}")


if __name__ == "__main__":
    unittest.main()
