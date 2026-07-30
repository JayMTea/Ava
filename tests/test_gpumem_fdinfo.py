"""The DRM per-process memory reader, over a constructed /proc tree.

⚠️ **These fdinfo bodies are CONSTRUCTED from the documented drm-usage-stats ABI,
not recorded from real hardware.** NVIDIA's proprietary driver does not export
drm-usage-stats, so there is nothing on the maintainer's box to record — the same
constraint as `tests/test_hwinfo_amd.py`. What is proven here is the parsing and
the arithmetic; the numbers a real Radeon reports are unverified.

The case that earns its keep is `test_multiple_fds_on_one_client_count_once`. A
process holding several fds against the same DRM client reports the same memory on
each, so a naive sum multiplies residency by the fd count. For this module that is
not a cosmetic error: the allocator would see an engine apparently holding several
times the entire pool and evict far more than necessary to reach a target it had
already met — the exact failure the module docstring opens by warning about, from
the opposite direction.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from ava_bridge.alloc import gpumem

_AMD = """\
pos:\t0
flags:\t02100002
drm-driver:\tamdgpu
drm-client-id:\t{client}
drm-memory-vram:\t{vram_kib} KiB
drm-memory-gtt:\t131072 KiB
"""

_INTEL = """\
pos:\t0
drm-driver:\ti915
drm-client-id:\t{client}
drm-total-local0:\t{vram_kib} KiB
"""

_NON_DRM = "pos:\t0\nflags:\t0100002\nmnt_id:\t24\n"


def _write(root: str, pid: int, fd: int, body: str) -> None:
    d = os.path.join(root, str(pid), "fdinfo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, str(fd)), "w", encoding="utf-8") as f:
        f.write(body)


class DrmFdinfoTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ava-proc-")
        self._env = mock.patch.dict(os.environ,
                                    {gpumem._PROC_ROOT_ENV: self.root})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_amd_vram_is_summed_per_pid(self):
        _write(self.root, 111, 3, _AMD.format(client=1, vram_kib=2 * 1024 * 1024))
        _write(self.root, 222, 3, _AMD.format(client=2, vram_kib=1024 * 1024))
        table = gpumem._read_drm_fdinfo()
        self.assertEqual(table, {111: 2.0, 222: 1.0})

    def test_intel_local_memory_is_read(self):
        _write(self.root, 333, 4, _INTEL.format(client=9, vram_kib=4 * 1024 * 1024))
        self.assertEqual(gpumem._read_drm_fdinfo(), {333: 4.0})

    def test_multiple_fds_on_one_client_count_once(self):
        """The dedup that keeps the allocator from over-evicting."""
        body = _AMD.format(client=7, vram_kib=8 * 1024 * 1024)
        for fd in (3, 4, 5, 6):
            _write(self.root, 444, fd, body)
        table = gpumem._read_drm_fdinfo()
        self.assertEqual(table, {444: 8.0},
                         "four fds on one DRM client were counted four times — the "
                         "allocator would think this process held 32 GiB and evict "
                         "everything trying to free memory that was never held")

    def test_distinct_clients_in_one_pid_are_both_counted(self):
        """The other half: two real contexts in one process is 2x, legitimately."""
        _write(self.root, 555, 3, _AMD.format(client=1, vram_kib=1024 * 1024))
        _write(self.root, 555, 4, _AMD.format(client=2, vram_kib=1024 * 1024))
        self.assertEqual(gpumem._read_drm_fdinfo(), {555: 2.0})

    def test_non_drm_fds_are_ignored(self):
        _write(self.root, 666, 3, _NON_DRM)
        self.assertIsNone(
            gpumem._read_drm_fdinfo(),
            "a tree with no DRM fds at all must be UNKNOWN (None), not an empty "
            "reading — 'nothing holds memory' and 'this kernel cannot tell me' "
            "lead to different allocator decisions")

    def test_a_drm_fd_with_no_memory_keys_is_a_real_empty_reading(self):
        """DRM present but this client holds nothing: {} , not None."""
        _write(self.root, 777, 3, "drm-driver:\tamdgpu\ndrm-client-id:\t1\n")
        self.assertEqual(gpumem._read_drm_fdinfo(), {})

    def test_unparsable_values_contribute_nothing_rather_than_crashing(self):
        _write(self.root, 888, 3,
               "drm-driver:\tamdgpu\ndrm-client-id:\t1\ndrm-memory-vram:\tN/A\n")
        self.assertEqual(gpumem._read_drm_fdinfo(), {})

    def test_units_are_honoured(self):
        for unit, value, want in (("KiB", 1024 * 1024, 1.0),
                                  ("MiB", 2048, 2.0),
                                  ("GiB", 3, 3.0)):
            with self.subTest(unit=unit):
                shutil.rmtree(self.root, ignore_errors=True)
                _write(self.root, 999, 3,
                       f"drm-driver:\tamdgpu\ndrm-client-id:\t1\n"
                       f"drm-memory-vram:\t{value} {unit}\n")
                table = gpumem._read_drm_fdinfo()
                self.assertAlmostEqual(table[999], want, places=3)

    def test_a_vanishing_process_does_not_break_the_scan(self):
        """/proc races constantly; one unreadable pid must not lose the others."""
        _write(self.root, 1010, 3, _AMD.format(client=1, vram_kib=1024 * 1024))
        os.makedirs(os.path.join(self.root, "1011"), exist_ok=True)  # no fdinfo dir
        self.assertEqual(gpumem._read_drm_fdinfo(), {1010: 1.0})

    def test_read_falls_through_to_fdinfo_when_nvidia_is_absent(self):
        """The whole point: a non-NVIDIA box must stop reporting None."""
        _write(self.root, 1212, 3, _AMD.format(client=1, vram_kib=5 * 1024 * 1024))
        with mock.patch.object(gpumem.shutil, "which", return_value=None):
            self.assertEqual(gpumem._read(), {1212: 5.0})

    def test_measured_residency_reaches_for_tree(self):
        """for_tree is what drivers call; prove the new source flows through it."""
        _write(self.root, os.getpid(), 3,
               _AMD.format(client=1, vram_kib=6 * 1024 * 1024))
        with mock.patch.object(gpumem.shutil, "which", return_value=None):
            gpumem.by_pid(force=True)
            self.assertAlmostEqual(gpumem.for_tree(os.getpid()), 6.0, places=2)
        gpumem.reset_cache()


if __name__ == "__main__":
    unittest.main()
