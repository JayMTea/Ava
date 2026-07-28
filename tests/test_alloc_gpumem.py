"""Residency oracle: a cgroup figure must never be mistaken for device memory.

Measured on the development box for one language-model container:

    docker stats MemUsage ............  3.6 GiB
    nvidia-smi --query-compute-apps .. 43.7 GiB

A container runtime and a service manager report what the *cgroup* is charged for, and
an inference engine's weights are device allocations that are not charged there. Acting
on the smaller number is worse than having none: the planner projects a few GiB from
releasing a model that holds tens, refuses work that would have fit, and over-releases
chasing a target it already passed. This guards the split — and that "unreadable" stays
distinct from "holds nothing", because conflating them is how a model whose weights
failed to load passes for healthy.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ["AVA_HOME"] = tempfile.mkdtemp(prefix="ava-gpumem-test-")

from ava_bridge.alloc import gpumem

# pid, then MiB — the shape `--query-compute-apps` emits with noheader,nounits.
SMI_ROWS = "3625815, 44775\n3625099, 170\n2262036, 51557\n"


def _fake_smi(rows=SMI_ROWS, rc=0):
    return mock.patch.object(
        gpumem.subprocess, "run",
        return_value=mock.Mock(returncode=rc, stdout=rows, stderr=""))


class OracleTests(unittest.TestCase):
    def setUp(self):
        gpumem.reset_cache()
        self.addCleanup(gpumem.reset_cache)
        p = mock.patch.object(gpumem.shutil, "which", lambda _n: "/usr/bin/nvidia-smi")
        p.start()
        self.addCleanup(p.stop)

    def test_rows_are_parsed_into_gib_per_pid(self):
        with _fake_smi():
            table = gpumem.by_pid()
        self.assertAlmostEqual(table[3625815], 43.72, places=1)
        self.assertAlmostEqual(table[2262036], 50.35, places=1)

    def test_a_pid_holding_nothing_reads_zero_not_unknown(self):
        with _fake_smi():
            self.assertEqual(gpumem.for_pids([999999]), 0.0)

    def test_missing_tooling_is_unknown_not_zero(self):
        with mock.patch.object(gpumem.shutil, "which", lambda _n: None):
            self.assertIsNone(gpumem.by_pid())
            self.assertIsNone(gpumem.for_pids([1]))
            self.assertIsNone(gpumem.for_tree(1))

    def test_a_failing_query_is_unknown(self):
        with _fake_smi(rc=9):
            self.assertIsNone(gpumem.by_pid())

    def test_an_na_row_is_skipped_rather_than_read_as_zero(self):
        with _fake_smi(rows="123, [N/A]\n456, 1024\n"):
            table = gpumem.by_pid()
        self.assertNotIn(123, table)
        self.assertEqual(table[456], 1.0)

    def test_a_tree_sums_the_worker_not_just_the_launcher(self):
        # Engines split a launcher from the worker that owns the weights; asking only
        # about the pid a supervisor reports would miss nearly all of it.
        with _fake_smi(), \
             mock.patch.object(gpumem, "_descendants",
                               lambda root: {root, 3625815}):
            self.assertAlmostEqual(gpumem.for_tree(3625099), 43.89, places=1)

    def test_the_table_is_cached_so_one_plan_makes_one_query(self):
        with _fake_smi() as run:
            gpumem.by_pid()
            gpumem.by_pid()
            gpumem.by_pid()
        self.assertEqual(run.call_count, 1)

    def test_an_empty_table_is_a_real_answer(self):
        # "Nothing holds accelerator memory" differs from "cannot tell".
        with _fake_smi(rows=""):
            self.assertEqual(gpumem.by_pid(), {})
            self.assertEqual(gpumem.for_pids([1]), 0.0)


class DriverPreferenceTests(unittest.TestCase):
    """Both drivers must prefer the oracle and keep their own reading as fallback."""

    def setUp(self):
        gpumem.reset_cache()
        self.addCleanup(gpumem.reset_cache)

    def _ctx(self, **cfg):
        from ava_bridge.alloc.base import DriverContext
        return DriverContext(model_id="m", weight_gb=48.7, config=cfg,
                             free_gib=lambda: 10.0, wait_free=lambda *a, **k: True)

    def test_docker_prefers_the_oracle_over_docker_stats(self):
        from ava_bridge.alloc.drivers.docker import DockerDriver
        d = DockerDriver(self._ctx(container="c"))
        with mock.patch.object(d, "_main_pid", return_value=42), \
             mock.patch("ava_bridge.alloc.drivers._gpu.for_tree", return_value=43.7), \
             mock.patch.object(d, "_docker") as dk:
            self.assertEqual(d._stats_gib(), 43.7)
        dk.assert_not_called()      # docker stats never consulted when the oracle answers

    def test_docker_falls_back_when_the_oracle_is_unavailable(self):
        from ava_bridge.alloc.drivers.docker import DockerDriver
        d = DockerDriver(self._ctx(container="c"))
        with mock.patch.object(d, "_main_pid", return_value=42), \
             mock.patch("ava_bridge.alloc.drivers._gpu.for_tree", return_value=None), \
             mock.patch.object(d, "_docker", return_value=(0, "3.6GiB / 121.7GiB")):
            self.assertAlmostEqual(d._stats_gib(), 3.6, places=1)

    def test_systemd_prefers_the_oracle_over_the_cgroup(self):
        from ava_bridge.alloc.drivers.systemd import SystemdDriver
        d = SystemdDriver(self._ctx(unit="u.service"))
        with mock.patch.object(d, "_main_pid", return_value=42), \
             mock.patch("ava_bridge.alloc.drivers._gpu.for_tree", return_value=50.1), \
             mock.patch.object(d, "_systemctl") as sc:
            self.assertEqual(d._cgroup_gib(), 50.1)
        sc.assert_not_called()

    def test_systemd_falls_back_to_the_cgroup(self):
        from ava_bridge.alloc.drivers.systemd import SystemdDriver
        d = SystemdDriver(self._ctx(unit="u.service"))
        with mock.patch.object(d, "_main_pid", return_value=42), \
             mock.patch("ava_bridge.alloc.drivers._gpu.for_tree", return_value=None), \
             mock.patch.object(d, "_systemctl", return_value=str(8 * 1024**3)):
            self.assertEqual(d._cgroup_gib(), 8.0)


if __name__ == "__main__":
    unittest.main()
