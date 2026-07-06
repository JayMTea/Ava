"""Cross-platform tests for the hardware abstraction layer.

We can only run on this host's real OS, so each foreign platform (Apple Silicon,
CPU-only Linux, no-psutil) is exercised by monkeypatching the HAL's low-level
readers. This proves the *decision logic* is correct everywhere; the numbers on a
real Mac still need on-device validation (see docs/HWINFO_VALIDATION.md).
"""
import unittest
from unittest import mock

from ava_bridge import hwinfo


def _reset_cache():
    hwinfo._fit_cache.update(ts=0.0, info=None)
    hwinfo._platform_id = None


class FitMemorySourceTests(unittest.TestCase):
    def setUp(self):
        _reset_cache()

    def tearDown(self):
        _reset_cache()

    def test_discrete_gpu_uses_vram_not_system(self):
        # A discrete GPU: 24 GiB VRAM, 64 GiB system RAM. Fit must gate on VRAM.
        with mock.patch.object(hwinfo, "vram_mem",
                               return_value=hwinfo.MemInfo(10.0, 24.0, "vram-nvml")), \
             mock.patch.object(hwinfo, "system_mem",
                               return_value=hwinfo.MemInfo(50.0, 64.0, "system-psutil")):
            m = hwinfo.fit_memory()
        self.assertEqual(m.source, "vram-nvml")
        self.assertEqual(m.total_gb, 24.0)

    def test_unified_memory_reported_as_vram_prefers_system_label(self):
        # Spark/edge case: VRAM total ~= system total => it's unified, use system.
        with mock.patch.object(hwinfo, "vram_mem",
                               return_value=hwinfo.MemInfo(80.0, 120.0, "vram-smi")), \
             mock.patch.object(hwinfo, "system_mem",
                               return_value=hwinfo.MemInfo(85.0, 121.0, "system-psutil")):
            m = hwinfo.fit_memory()
        self.assertEqual(m.source, "system-psutil")

    def test_apple_silicon_uses_system_memory(self):
        # Mac: no VRAM reading at all -> system memory is the fit resource.
        with mock.patch.object(hwinfo, "vram_mem", return_value=hwinfo.MemInfo()), \
             mock.patch.object(hwinfo, "system_mem",
                               return_value=hwinfo.MemInfo(12.0, 24.0, "system-psutil")):
            m = hwinfo.fit_memory()
        self.assertEqual(m.source, "system-psutil")
        self.assertTrue(m.readable)

    def test_no_readable_source_returns_unknown(self):
        # Non-Linux without psutil and no GPU -> nothing readable -> don't gate.
        with mock.patch.object(hwinfo, "vram_mem", return_value=hwinfo.MemInfo()), \
             mock.patch.object(hwinfo, "system_mem", return_value=hwinfo.MemInfo()):
            m = hwinfo.fit_memory()
        self.assertFalse(m.readable)
        self.assertIsNone(m.source)


class SystemMemFallbackTests(unittest.TestCase):
    def test_no_psutil_falls_back_to_proc(self):
        # Force the psutil-less path; on this Linux box /proc/meminfo must resolve.
        with mock.patch.object(hwinfo, "_psutil", None):
            m = hwinfo.system_mem()
        self.assertEqual(m.source, "system-proc")
        self.assertTrue(m.readable)


class PlatformDetectionTests(unittest.TestCase):
    def setUp(self):
        hwinfo._platform_id = None

    def tearDown(self):
        hwinfo._platform_id = None

    def test_apple_silicon_detected(self):
        with mock.patch.object(hwinfo, "sys") as s, \
             mock.patch.object(hwinfo, "_platform") as p, \
             mock.patch.object(hwinfo, "_nvml", return_value=None):
            s.platform = "darwin"
            p.machine.return_value = "arm64"
            self.assertEqual(hwinfo.platform_id(), "darwin-apple")

    def test_cpu_only_linux_detected(self):
        with mock.patch.object(hwinfo, "sys") as s, \
             mock.patch.object(hwinfo, "_nvml", return_value=None), \
             mock.patch.object(hwinfo, "_which", return_value=None):
            s.platform = "linux"
            self.assertEqual(hwinfo.platform_id(), "linux-cpu")


class AppleGpuTests(unittest.TestCase):
    def setUp(self):
        hwinfo._platform_id = None

    def tearDown(self):
        hwinfo._platform_id = None

    def test_apple_gpu_reports_memory_but_not_util(self):
        # On Apple Silicon we surface unified memory but honestly leave util/temp/
        # power as None (no unprivileged API) rather than fabricating them.
        with mock.patch.object(hwinfo, "platform_id", return_value="darwin-apple"), \
             mock.patch.object(hwinfo, "_run", return_value="Apple M4 Pro"), \
             mock.patch.object(hwinfo, "system_mem",
                               return_value=hwinfo.MemInfo(12.0, 24.0, "system-psutil")):
            g = hwinfo.gpu()
        self.assertIn("Apple M4 Pro", g.name)
        self.assertEqual(g.mem_total_gb, 24.0)
        self.assertIsNone(g.util)
        self.assertIsNone(g.temp_c)


if __name__ == "__main__":
    unittest.main()
