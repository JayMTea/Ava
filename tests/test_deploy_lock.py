"""One deploy at a time on this machine, across PROCESSES.

`provision_job` is single-slot, and its lock is a `threading.Lock` — so it holds
for everything inside the bridge and for nothing outside it. Two other things run
the same `agent/install.sh` against the same sandbox: `ava agent provision` from
a terminal, and the shim inside the agent container. install.sh does
`rm -rf "$DEST"` before extracting each MCP server, so two runs interleaving
there leave a server half-written while still registered in openclaw.json —
which reads `stale` forever and is only fixed by another deploy.

Nothing here invokes the real `nemoclaw` binary: `nemoclaw <sandbox> status
--json` was observed restarting the host's OpenShell gateway and killing a host
process (see tests/test_install_scope_exec.py).
"""
from __future__ import annotations

import multiprocessing
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from ava_bridge import settings
from ava_bridge.runtime import deploy_lock


def _grab(path_root: str, q) -> None:
    """Child process: try to take the lock and report whether it got it."""
    import pathlib as _pathlib
    os.environ["AVA_HOME"] = path_root
    from ava_bridge import settings as child_settings
    from ava_bridge.runtime import deploy_lock as child_lock
    # settings caches AVA_HOME as a Path at import time.
    child_settings.AVA_HOME = _pathlib.Path(path_root)
    with child_lock.held() as mine:
        q.put(bool(mine))


class DeployLockTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.object(settings, "AVA_HOME", pathlib.Path(self.home))
        p.start()
        self.addCleanup(p.stop)

    def test_the_lock_file_lives_under_ava_home(self):
        """AVA_HOME is exactly what the racing parties share — the bridge, the
        CLI and the agent container all mount it, which is what lets them
        collide. A lock anywhere else would not be seen by all three."""
        self.assertTrue(deploy_lock.lock_path().startswith(self.home),
                        deploy_lock.lock_path())

    def test_one_holder_at_a_time_across_processes(self):
        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()
        with deploy_lock.held() as mine:
            self.assertTrue(mine)
            child = ctx.Process(target=_grab, args=(self.home, q))
            child.start()
            child.join(30)
            got = q.get(timeout=5)
        self.assertFalse(got, "a second process deployed while the first held "
                              "the lock — install.sh's `rm -rf $DEST` is what "
                              "those two would be racing on")

    def test_the_lock_is_released_when_the_holder_exits(self):
        """Deliberately not a lock FILE whose existence means held: a process
        killed mid-deploy would leave that behind forever, and "delete this
        file" is an instruction no owner should ever have to receive."""
        with deploy_lock.held() as mine:
            self.assertTrue(mine)
        with deploy_lock.held() as again:
            self.assertTrue(again, "the lock was not released")

    def test_a_platform_without_flock_still_deploys(self):
        """Serialising is a safety improvement; a runtime that cannot deploy at
        all is worse than the race it prevents."""
        with mock.patch.object(deploy_lock, "fcntl", None):
            with deploy_lock.held() as mine:
                self.assertTrue(mine)

    def test_an_unwritable_home_still_deploys(self):
        with mock.patch.object(deploy_lock.os, "makedirs",
                               side_effect=OSError("read-only")):
            with deploy_lock.held() as mine:
                self.assertTrue(mine)


class ProvisionRefusesWhenAnotherDeployHoldsIt(unittest.TestCase):
    def test_provision_reports_the_conflict_rather_than_racing(self):
        from ava_bridge.runtime.nemoclaw import NemoClawRuntime

        rt = NemoClawRuntime()
        with mock.patch.object(rt, "_sandbox_exists", return_value=True), \
             mock.patch.object(rt, "install_env", return_value={}), \
             mock.patch.object(rt, "_run") as run, \
             mock.patch.object(type(rt), "cli",
                               mock.PropertyMock(return_value="/usr/bin/nemoclaw")), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(deploy_lock, "held") as held:
            held.return_value.__enter__ = lambda *_: False
            held.return_value.__exit__ = lambda *_: False
            res = rt.provision(scope="all")

        run.assert_not_called()
        self.assertFalse(res["ok"])
        self.assertEqual(res["error_code"], "provision_running")
        self.assertTrue(any("already running" in s["detail"]
                            for s in res["steps"]), res["steps"])


if __name__ == "__main__":
    unittest.main()
