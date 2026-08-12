"""Ava starting and stopping a vLLM server it owns.

The only driver that can bring an engine up from nothing, so it is the only one
that can leave a box in a state nobody asked for: a half-started engine holding
40 GB, a port serving the wrong model, or a shell killed while its vLLM workers
keep the pool. The tests are weighted toward those, not the happy path.

Nothing here launches a real engine. `deploy/local-serve.sh` is exercised only
through its dry-run contract, and every process interaction is faked, because a
test that actually loaded weights would take minutes and could not run in CI.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from ava_bridge.alloc import base
from ava_bridge.alloc.drivers import local_serve as ls


def _ctx(model_id="org/Some-7B", **kw):
    return base.DriverContext(model_id=model_id, weight_gb=14.0,
                              config={"port": 8002}, **kw)


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        patch = mock.patch.object(ls.settings, "logs_dir", lambda: self._td.name)
        patch.start()
        self.addCleanup(patch.stop)


class ResidencyNeverOverclaims(_Tmp):
    def test_an_unknown_port_is_unknown_not_free(self):
        # None, never False. Memory we cannot see is memory the planner must not
        # promise to free.
        r = ls.LocalServeDriver(_ctx()).residency()
        self.assertIsNone(r.resident)

    def test_another_model_on_our_port_is_not_our_residency(self):
        # Reporting it as ours would offer someone else's memory as reclaimable.
        ls._write_state({"8002": {"pid": os.getpid(), "model": "org/Different"}})
        r = ls.LocalServeDriver(_ctx()).residency()
        self.assertIsNone(r.resident)

    def test_a_dead_pid_for_our_model_is_genuinely_free(self):
        ls._write_state({"8002": {"pid": 2 ** 30, "model": "org/Some-7B"}})
        r = ls.LocalServeDriver(_ctx()).residency()
        self.assertIs(r.resident, False)
        self.assertEqual(r.gib, 0.0)

    def test_alive_but_not_answering_is_unknown(self):
        ls._write_state({"8002": {"pid": os.getpid(), "model": "org/Some-7B"}})
        with mock.patch.object(ls, "_serving", lambda *a, **k: None):
            r = ls.LocalServeDriver(_ctx()).residency()
        self.assertIsNone(r.resident)

    def test_alive_and_serving_this_model_is_resident(self):
        ls._write_state({"8002": {"pid": os.getpid(), "model": "org/Some-7B"}})
        with mock.patch.object(ls, "_serving", lambda *a, **k: True):
            r = ls.LocalServeDriver(_ctx()).residency()
        self.assertIs(r.resident, True)


class NothingIsSpawnedUntilItCanWork(_Tmp):
    def test_a_failed_preflight_starts_no_process(self):
        """The dry run is the whole point: a model whose family has no parser, or
        a misspelled id, must cost a second — not a 40 GB load and a crash loop."""
        class _CP:
            returncode, stdout, stderr = 1, "", "unknown model family"
        spawned = []
        with mock.patch.object(ls.subprocess, "run", lambda *a, **k: _CP()), \
             mock.patch.object(ls.subprocess, "Popen", lambda *a, **k: spawned.append(1)):
            out = ls.LocalServeDriver(_ctx()).acquire(timeout=1)
        self.assertFalse(out.ok)
        self.assertFalse(out.acted, "acted=True would make the caller owe a restore")
        self.assertEqual(spawned, [], "a process was started despite a failed preflight")
        self.assertIn("unknown model family", out.detail)

    def test_the_preflight_runs_in_dry_run_mode(self):
        seen = {}

        class _CP:
            returncode, stdout, stderr = 1, "", "nope"

        def fake_run(cmd, **kw):
            seen.update((kw.get("env") or {}))
            return _CP()
        with mock.patch.object(ls.subprocess, "run", fake_run):
            ls.LocalServeDriver(_ctx()).acquire(timeout=1)
        self.assertEqual(seen.get("AVA_SERVE_DRY_RUN"), "1")
        self.assertEqual(seen.get("AVA_MODEL"), "org/Some-7B")

    def test_a_port_serving_a_different_model_is_refused(self):
        # Starting a second engine on a busy port is how you get two models
        # fighting for the pool and neither answering.
        ls._write_state({"8002": {"pid": os.getpid(), "model": "org/Different"}})
        out = ls.LocalServeDriver(_ctx()).acquire(timeout=1)
        self.assertFalse(out.ok)
        self.assertIn("stop it first", out.detail)

    def test_the_same_model_already_running_is_a_no_op_success(self):
        ls._write_state({"8002": {"pid": os.getpid(), "model": "org/Some-7B"}})
        out = ls.LocalServeDriver(_ctx()).acquire(timeout=1)
        self.assertTrue(out.ok)
        self.assertFalse(out.acted)


class StartedIsNotServing(_Tmp):
    """A vLLM boot takes minutes and can still fail on OOM or a bad parser. Ava
    must not report a brain swap until the engine says it holds the model."""

    def _ok_preflight(self):
        class _CP:
            returncode, stdout, stderr = 0, "", ""
        return mock.patch.object(ls.subprocess, "run", lambda *a, **k: _CP())

    def test_an_engine_that_exits_during_start_is_a_failure_with_the_log(self):
        class _Proc:
            pid = 4242

            def poll(self):
                return 1
        log = os.path.join(self._td.name, "vllm-8002.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("CUDA out of memory\n")
        with self._ok_preflight(), \
             mock.patch.object(ls.subprocess, "Popen", lambda *a, **k: _Proc()):
            out = ls.LocalServeDriver(_ctx()).acquire(timeout=5)
        self.assertFalse(out.ok)
        self.assertIn("CUDA out of memory", out.detail)
        self.assertNotIn("8002", ls._read_state(),
                         "a dead engine was left in the state file")

    def test_a_start_that_never_serves_times_out_rather_than_claiming_success(self):
        class _Proc:
            pid = 4242

            def poll(self):
                return None
        with self._ok_preflight(), \
             mock.patch.object(ls.subprocess, "Popen", lambda *a, **k: _Proc()), \
             mock.patch.object(ls, "_serving", lambda *a, **k: False):
            out = ls.LocalServeDriver(_ctx()).acquire(timeout=0.1)
        self.assertFalse(out.ok)
        self.assertIn("not serving", out.detail)

    def test_success_is_only_reported_once_the_engine_holds_the_model(self):
        class _Proc:
            pid = 4242

            def poll(self):
                return None
        with self._ok_preflight(), \
             mock.patch.object(ls.subprocess, "Popen", lambda *a, **k: _Proc()), \
             mock.patch.object(ls, "_serving", lambda *a, **k: True):
            out = ls.LocalServeDriver(_ctx()).acquire(timeout=5)
        self.assertTrue(out.ok)
        self.assertTrue(out.acted)
        self.assertEqual(ls._read_state()["8002"]["model"], "org/Some-7B")


class StoppingTakesTheWholeGroup(_Tmp):
    def test_nothing_running_is_ok_and_did_not_act(self):
        out = ls.LocalServeDriver(_ctx()).release("stop")
        self.assertTrue(out.ok)
        self.assertFalse(out.acted, "acted=True would make the caller owe a restore")

    def test_it_signals_the_process_group_not_just_the_shell(self):
        """The launcher forks vLLM workers. Killing only the shell leaves them
        holding every byte of the pool."""
        killed = []
        ls._write_state({"8002": {"pid": 4242, "model": "org/Some-7B"}})
        alive = iter([True, False, False, False])
        with mock.patch.object(ls, "_alive", lambda p: next(alive, False)), \
             mock.patch.object(ls.os, "getpgid", lambda p: 4242), \
             mock.patch.object(ls.os, "killpg", lambda pg, sig: killed.append((pg, sig))):
            out = ls.LocalServeDriver(_ctx()).release("stop")
        self.assertTrue(killed, "no signal was sent to the process group")
        self.assertEqual(killed[0][0], 4242)
        self.assertTrue(out.acted)
        self.assertNotIn("8002", ls._read_state())


class TheStateFileSurvivesUs(_Tmp):
    def test_a_corrupt_state_file_reads_as_nothing_known(self):
        # It must not crash the driver: an unreadable state file means we do not
        # know what is running, which is exactly `residency() -> None`.
        with open(ls._state_path(), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ls._read_state(), {})

    def test_writes_are_atomic(self):
        # A torn write leaves a state file that cannot be read, and the engine it
        # described can then never be stopped by Ava.
        ls._write_state({"8002": {"pid": 1, "model": "a"}})
        with open(ls._state_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["8002"]["model"], "a")
        self.assertFalse(os.path.exists(ls._state_path() + ".tmp"))


if __name__ == "__main__":
    unittest.main()
