"""AMD / Intel sysfs providers, and the APU carve-out trap.

⚠️ **These values are CONSTRUCTED from the documented amdgpu sysfs ABI, not
recorded from real hardware.** The maintainer owns no AMD machine, and Strix Halo
cannot be rented, so nothing here is evidence that the *numbers* on a real box
are right — only that the parsing and the unified/discrete decision behave as
designed given bytes of this shape. When someone runs the on-device recorder on
real hardware, replace these with the recorded fixture and delete this warning.
Until then the platform matrix must read `ci-simulated`, never `verified`.

The case that matters is `test_an_apu_carve_out_does_not_become_the_fit_pool`.
The naive version of the AMD VRAM reader turns one bug into its mirror image:
before, a 128 GB box with a discrete 24 GB card reported system RAM as the
fit-limiting pool and green-lit a 70B; after, an APU reporting a 512 MiB BIOS
carve-out would refuse a 7B on a 128 GB machine. Both are confidently wrong, and
the second is the one no amount of local testing would have caught.
"""
import os
import unittest
from unittest import mock

from ava_bridge import hwinfo

_AMD = "0x1002"
_INTEL = "0x8086"
_MIB = 1024 ** 2
_GIB = 1024 ** 3


def _card(root, n, vendor, **files):
    dev = os.path.join(root, f"card{n}", "device")
    os.makedirs(dev, exist_ok=True)
    if vendor is not None:
        _w(os.path.join(dev, "vendor"), vendor)
    hwmon = files.pop("_hwmon", None)
    for k, v in files.items():
        _w(os.path.join(dev, k), v)
    if hwmon:
        # The hwmon index is not stable across boots, hence a non-zero one here.
        hd = os.path.join(dev, "hwmon", "hwmon3")
        os.makedirs(hd, exist_ok=True)
        for k, v in hwmon.items():
            _w(os.path.join(hd, k), v)
    return dev


def _w(path, val):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{val}\n")


class AmdSysfsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").mkdtemp(prefix="ava-drm-")
        self._env = mock.patch.dict(os.environ, {hwinfo._DRM_ROOT_ENV: self._tmp})
        self._env.start()
        # No NVIDIA, so the AMD/Intel branches are reachable.
        self._nvml = mock.patch.object(hwinfo, "_nvml", return_value=None)
        self._nvml.start()
        self._which = mock.patch.object(hwinfo, "_which", return_value=None)
        self._which.start()
        hwinfo.reset_cache()

    def tearDown(self):
        self._which.stop()
        self._nvml.stop()
        self._env.stop()
        hwinfo.reset_cache()
        __import__("shutil").rmtree(self._tmp, ignore_errors=True)

    # -- enumeration ---------------------------------------------------------- #
    def test_connectors_and_render_nodes_are_not_cards(self):
        _card(self._tmp, 0, _AMD)
        for junk in ("card0-Unknown-1", "renderD128", "version"):
            os.makedirs(os.path.join(self._tmp, junk), exist_ok=True)
        self.assertEqual([v for _, v in hwinfo._drm_cards()], [_AMD],
                         "a bare card* glob counts connectors and render nodes; "
                         "this box really does have a card0-Unknown-1")

    # -- the trap ------------------------------------------------------------- #
    def test_an_apu_carve_out_does_not_become_the_fit_pool(self):
        """Strix-Halo-shaped: 512 MiB VRAM carve-out, 64 GiB shareable GTT."""
        _card(self._tmp, 0, _AMD,
              mem_info_vram_total=512 * _MIB, mem_info_vram_used=64 * _MIB,
              mem_info_gtt_total=64 * _GIB, product_name="Radeon 8060S Graphics")
        hwinfo.reset_cache()
        model, why = hwinfo._memory_model()
        self.assertEqual(model, "unified", why)

        sysm = hwinfo.MemInfo(free_gb=100.0, total_gb=128.0, source="test")
        with mock.patch.object(hwinfo, "system_mem", return_value=sysm):
            hwinfo.reset_cache()
            fit = hwinfo.fit_memory()
        self.assertEqual(fit.source, "test", (
            "an APU's BIOS carve-out was taken as the fit-limiting pool, so a "
            f"128 GB box would gate every model against {fit.total_gb} GB"))
        self.assertAlmostEqual(fit.total_gb, 128.0)

    def test_a_discrete_card_does_gate_on_its_own_vram(self):
        """The other half: a real 24 GiB card must NOT defer to system RAM."""
        _card(self._tmp, 0, _AMD,
              mem_info_vram_total=24 * _GIB, mem_info_vram_used=2 * _GIB,
              mem_info_gtt_total=8 * _GIB, product_name="Radeon RX 7900 XTX")
        hwinfo.reset_cache()
        self.assertEqual(hwinfo._memory_model()[0], "discrete")

        sysm = hwinfo.MemInfo(free_gb=100.0, total_gb=128.0, source="test")
        with mock.patch.object(hwinfo, "system_mem", return_value=sysm):
            hwinfo.reset_cache()
            fit = hwinfo.fit_memory()
        self.assertEqual(fit.source, "vram-amdgpu-sysfs")
        self.assertAlmostEqual(fit.total_gb, 24.0, places=1)

    def test_env_override_beats_every_inferred_signal(self):
        """A misclassified box must be a one-variable fix, not a patch release."""
        _card(self._tmp, 0, _AMD, mem_info_vram_total=24 * _GIB,
              mem_info_gtt_total=8 * _GIB)          # looks discrete
        with mock.patch.dict(os.environ,
                             {hwinfo._MEMORY_MODEL_ENV: "unified"}):
            hwinfo.reset_cache()
            model, why = hwinfo._memory_model()
        self.assertEqual(model, "unified")
        self.assertIn(hwinfo._MEMORY_MODEL_ENV, why,
                      "the reason string must name the override, so `ava doctor` "
                      "can explain which signal decided")

    # -- telemetry ------------------------------------------------------------ #
    def test_power_is_read_from_hwmon_in_microwatts(self):
        _card(self._tmp, 0, _AMD, mem_info_vram_total=24 * _GIB,
              gpu_busy_percent=42, product_name="Radeon RX 7900 XTX",
              _hwmon={"power1_average": 123_000_000, "temp1_input": 61_000})
        hwinfo.reset_cache()
        g = hwinfo.gpus()
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].source, "amdgpu-sysfs")
        self.assertAlmostEqual(g[0].power_w, 123.0)
        self.assertAlmostEqual(g[0].temp_c, 61.0)
        self.assertAlmostEqual(g[0].util, 42.0)

    def test_missing_hwmon_yields_none_not_zero(self):
        _card(self._tmp, 0, _AMD, mem_info_vram_total=24 * _GIB)
        hwinfo.reset_cache()
        g = hwinfo.gpus()[0]
        self.assertIsNone(g.power_w, "absent power must be unknown, never 0 W — "
                                     "0 W would be charged as free electricity")
        self.assertIsNone(g.util)

    def test_an_unnameable_card_still_reports_a_gpu(self):
        """`[]` used to mean both 'no GPU' and 'no provider for this GPU'."""
        _card(self._tmp, 0, None)          # vendor unreadable, as card0 is here
        hwinfo.reset_cache()
        g = hwinfo.gpus()
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].source, "drm-sysfs")
        self.assertIsNone(g[0].power_w)

    def test_intel_card_is_detected_and_read(self):
        _card(self._tmp, 0, _INTEL, product_name="Arc A770",
              _hwmon={"power1_average": 90_000_000})
        hwinfo.reset_cache()
        self.assertEqual(hwinfo.platform_id(), "linux-intel")
        g = hwinfo.gpus()[0]
        self.assertEqual(g.source, "intel-sysfs")
        self.assertAlmostEqual(g.power_w, 90.0)


class RaplTests(unittest.TestCase):
    def test_an_empty_powercap_dir_is_not_rapl(self):
        """Measured on this aarch64 box: /sys/class/powercap exists and is EMPTY,
        so `isdir` reported RAPL available with nothing to read."""
        tmp = __import__("tempfile").mkdtemp(prefix="ava-powercap-")
        try:
            with mock.patch.object(hwinfo, "_RAPL_ROOT", tmp):
                self.assertFalse(hwinfo._have_rapl())
                os.makedirs(os.path.join(tmp, "intel-rapl:0"))
                _w(os.path.join(tmp, "intel-rapl:0", "energy_uj"), 123456)
                self.assertTrue(hwinfo._have_rapl())
        finally:
            __import__("shutil").rmtree(tmp, ignore_errors=True)
