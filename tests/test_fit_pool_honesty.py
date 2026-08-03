"""The fit pool says WHICH pool it is, and never passes RAM off as a GPU.

Reported from a fresh install on an HP ZBook (RTX A1000 6 GB, 32 GB RAM,
Docker Desktop on WSL2). Setup's hardware card read:

    GPU · 15.4 GB usable (system-psutil)
    Recommended tier: small — ~7-8B models

Three unrelated facts spliced into one sentence. "GPU" was the *name* of a DRM
card whose PCI vendor is unreadable under WSL2, so `_vendor_label(None)` fell
through to its default. 15.4 GB was the WSL2 VM's system RAM — half the host's
31.6 GB. And the 6 GB the card actually has appeared nowhere.

The cause was that an empty `vram_mem()` meant two opposite things, which
`hwinfo.py` had already written down as a KNOWN GAP: "a container without the
toolkit — where a 128 GB host with an 8 GB card advertises 128 GB of fit memory
… so Setup recommends a tier that OOMs on first load." It named the fix it
declined to guess at — "teaching vram_mem() to distinguish 'the probe answered
N/A' from 'the probe errored'" — because it needed a discrete NVIDIA box to
verify. This is that fix, and these are the cases.

Run: .venv/bin/python -m pytest tests/test_fit_pool_honesty.py -q
"""
import unittest
from unittest import mock

from ava_bridge import hwinfo


def _reset():
    hwinfo._fit_cache.update(ts=0.0, info=None)
    hwinfo._platform_id = None


class ProbeStatus(unittest.TestCase):
    """"No VRAM by design" and "could not read VRAM" are different answers."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)
        for p in (
            mock.patch.object(hwinfo, "_nvml", lambda: None),
            mock.patch.object(hwinfo, "_vram_amdgpu", hwinfo.MemInfo),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_a_card_we_cannot_read_is_unreadable_not_absent(self):
        """The ZBook in a container: a DRM card exists, no reader can run."""
        with mock.patch.object(hwinfo, "_which", lambda n: None), \
             mock.patch.object(hwinfo, "_drm_cards",
                               lambda: [("/sys/class/drm/card0", None)]):
            self.assertEqual(hwinfo.vram_probe()[1], "unreadable")

    def test_a_box_with_no_card_at_all_is_none(self):
        with mock.patch.object(hwinfo, "_which", lambda n: None), \
             mock.patch.object(hwinfo, "_drm_cards", list):
            self.assertEqual(hwinfo.vram_probe()[1], "none")

    def test_an_all_na_smi_answer_is_na_not_a_failure(self):
        """GB10/Grace: nvidia-smi RUNS and reports N/A because the pool is
        unified. Treating that as a failed probe would break the box this
        project is developed on."""
        class _Out:
            returncode = 0
            stdout = "N/A, N/A\n"
        with mock.patch.object(hwinfo, "_which", lambda n: "/usr/bin/" + n), \
             mock.patch.object(hwinfo.subprocess, "run", lambda *a, **k: _Out()):
            self.assertEqual(hwinfo.vram_probe()[1], "na")


class PoolKind(unittest.TestCase):
    """What the card is allowed to claim, per pool."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def _pool(self, status, sysm, vram=hwinfo.MemInfo()):
        with mock.patch.object(hwinfo, "vram_probe", lambda: (vram, status)), \
             mock.patch.object(hwinfo, "vram_mem", lambda: vram), \
             mock.patch.object(hwinfo, "system_mem", lambda: sysm), \
             mock.patch.object(hwinfo, "gpu",
                               lambda: hwinfo.GpuInfo(name="GPU")), \
             mock.patch.object(hwinfo, "_pool_cap", lambda t: (False, None)):
            return hwinfo.fit_pool()

    def test_an_unreadable_card_never_makes_system_ram_accelerated(self):
        """THE BUG. 15.4 GB of VM RAM must not be reported as an accelerator
        pool just because a card we cannot measure exists."""
        p = self._pool("unreadable", hwinfo.MemInfo(10.3, 15.4, "system-psutil"))
        self.assertEqual(p.kind, "system")
        self.assertFalse(p.accelerated,
                         "system RAM was presented as GPU memory again")

    def test_a_measured_card_is_a_vram_pool(self):
        p = self._pool("measured", hwinfo.MemInfo(50.0, 64.0, "system-psutil"),
                       vram=hwinfo.MemInfo(5.5, 6.0, "vram-nvml"))
        self.assertEqual(p.kind, "vram")
        self.assertTrue(p.accelerated)

    def test_unified_memory_stays_accelerated(self):
        """Grace/Apple: one pool, and the GPU really can use all of it. The fix
        must not cost the maintainer's own box its correct reading."""
        p = self._pool("na", hwinfo.MemInfo(90.0, 121.7, "system-psutil"))
        self.assertEqual(p.kind, "unified")
        self.assertTrue(p.accelerated)

    def test_a_cpu_only_box_is_a_system_pool(self):
        p = self._pool("none", hwinfo.MemInfo(6.0, 16.0, "system-psutil"))
        self.assertEqual(p.kind, "system")
        self.assertFalse(p.accelerated)


class PoolCap(unittest.TestCase):
    """A slice of a machine must not be presented as the machine."""

    def setUp(self):
        _reset()
        self.addCleanup(_reset)

    def test_a_wsl2_vm_is_reported_as_capped(self):
        """Windows gives WSL2 half the host's RAM, which is why a 32 GB laptop
        reads 15.4 GB. The number is right for anything running inside; saying
        it is the machine's is what makes it a lie."""
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: None), \
             mock.patch.object(hwinfo, "_read_sysfs",
                               lambda p: "5.15.153.1-microsoft-standard-WSL2"
                               if "osrelease" in p else None):
            self.assertEqual(hwinfo._pool_cap(15.4), (True, "wsl2-vm"))

    def test_a_container_memory_limit_is_reported_as_capped(self):
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: 8.0), \
             mock.patch.object(hwinfo, "_read_sysfs", lambda p: None):
            self.assertEqual(hwinfo._pool_cap(64.0), (True, "cgroup"))

    def test_a_container_limit_CLAMPS_the_pool_not_just_flags_it(self):
        """THE SECOND BUG, found by running it rather than reasoning about it.

        `/proc/meminfo` is not namespaced and psutil reads it, so `system_mem()`
        inside a container reports the HOST's RAM. The cgroup limit was wired to
        `_pool_cap`, which sets a flag — and nothing corrected the number. Four
        real containers on a 121 GB box, at -m 6g/14g/48g, every one of them
        advertising the `large` tier beside `capped: true`.
        """
        m = hwinfo.MemInfo(free_gb=80.0, total_gb=121.7, source="system-psutil")
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: 6.0), \
             mock.patch.object(hwinfo, "_cgroup_usage_gb", lambda: 1.5):
            out = hwinfo._apply_cgroup_cap(m)
        self.assertEqual(out.total_gb, 6.0, "the pool still claimed the host's RAM")
        self.assertAlmostEqual(out.free_gb, 4.5, places=3)
        self.assertTrue(out.source.startswith("system"),
                        "platforms.py keys on this prefix to detect unified memory")
        self.assertIn("cgroup", out.source, "the clamp must be visible in provenance")

    def test_an_unmeasurable_usage_still_clamps_the_total(self):
        m = hwinfo.MemInfo(free_gb=80.0, total_gb=121.7, source="system-psutil")
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: 6.0), \
             mock.patch.object(hwinfo, "_cgroup_usage_gb", lambda: None):
            out = hwinfo._apply_cgroup_cap(m)
        self.assertEqual(out.total_gb, 6.0)
        self.assertEqual(out.free_gb, 6.0, "free is bounded by the ceiling")

    def test_a_vram_pool_is_never_clamped_by_a_RAM_limit(self):
        """A cgroup memory limit caps RAM. A discrete card's pool is not RAM, and
        clamping it would under-report a 24 GB card in an 8 GB container."""
        m = hwinfo.MemInfo(free_gb=23.0, total_gb=24.0, source="vram-nvml")
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: 8.0):
            self.assertEqual(hwinfo._apply_cgroup_cap(m).total_gb, 24.0)

    def test_a_limit_above_the_pool_changes_nothing(self):
        """`-m 200g` on a 121 GB box is not a cap."""
        m = hwinfo.MemInfo(free_gb=80.0, total_gb=121.7, source="system-psutil")
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: 200.0):
            self.assertEqual(hwinfo._apply_cgroup_cap(m).source, "system-psutil")

    def test_wsl2_needs_no_clamp(self):
        """A real VM's /proc/meminfo already reports the VM's share — which is
        why the laptop read 15.4 GB correctly while the container path did not."""
        m = hwinfo.MemInfo(free_gb=10.3, total_gb=15.4, source="system-psutil")
        with mock.patch.object(hwinfo, "_cgroup_limit_gb", lambda: None):
            self.assertEqual(hwinfo._apply_cgroup_cap(m), m)

    def test_an_unlimited_cgroup_is_not_a_cap(self):
        """`memory.max` is literally "max" when unlimited, and v1 uses a ~2^63
        sentinel. Reading either as a number reports every ordinary Linux box as
        capped."""
        with mock.patch.object(hwinfo, "_read_sysfs",
                               lambda p: "max" if "memory.max" in p else None):
            self.assertIsNone(hwinfo._cgroup_limit_gb())
        with mock.patch.object(hwinfo, "_read_sysfs",
                               lambda p: str(1 << 63) if "limit_in_bytes" in p
                               else None):
            self.assertIsNone(hwinfo._cgroup_limit_gb())


if __name__ == "__main__":
    unittest.main()
