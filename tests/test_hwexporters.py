"""Another machine's hardware, read through its exporters (ava_bridge/hwexporters.py).

The bridge on one box and the models on another: every HAL reader described the
box the process was on, so the monitor showed the wrong machine and the fit
layer sized the tier from its RAM. These tests pin the mapping from exporter
text onto the HAL's own shapes, and the two rules that matter more than the
numbers: the HAL follows the remote box everywhere once the switch is on, and
an unreachable remote box yields NOTHING - never this host's own figures.

No network: `_http_get` is the one call and is replaced per test.
Run: python -m pytest tests/test_hwexporters.py -q
"""
import unittest
from unittest import mock

import requests

from ava_bridge import features, hardware, hwexporters, hwinfo

GIB = 1024 ** 3

NODE_TEXT = f"""\
# HELP node_memory_MemTotal_bytes Memory information field MemTotal_bytes.
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes {128 * GIB:.7e}
node_memory_MemAvailable_bytes {96 * GIB:.7e}
node_memory_MemFree_bytes {40 * GIB:.7e}
node_cpu_seconds_total{{cpu="0",mode="idle"}} 1000
node_cpu_seconds_total{{cpu="0",mode="iowait"}} 10
node_cpu_seconds_total{{cpu="0",mode="user"}} 200
node_cpu_seconds_total{{cpu="0",mode="system"}} 50
node_cpu_seconds_total{{cpu="1",mode="idle"}} 1000
node_cpu_seconds_total{{cpu="1",mode="user"}} 300
node_filesystem_size_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="/"}} {1000 * GIB:.7e}
node_filesystem_free_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="/"}} {600 * GIB:.7e}
node_filesystem_avail_bytes{{device="/dev/nvme0n1p2",fstype="ext4",mountpoint="/"}} {550 * GIB:.7e}
node_filesystem_size_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/data"}} {4000 * GIB:.7e}
node_filesystem_free_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/data"}} {1000 * GIB:.7e}
node_filesystem_avail_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/data"}} {900 * GIB:.7e}
node_uname_info{{domainname="(none)",machine="aarch64",nodename="gpu-box",release="6.8.0",sysname="Linux",version="#1"}} 1
node_scrape_collector_duration_seconds{{collector="cpu"}} 0.001
"""

# Second scrape, 10 s of counter time later: cpu0 idle +8, user +2; cpu1 idle +5,
# user +5. Idle 13 of 20 -> 35 % busy.
NODE_TEXT_LATER = NODE_TEXT.replace(
    'node_cpu_seconds_total{cpu="0",mode="idle"} 1000',
    'node_cpu_seconds_total{cpu="0",mode="idle"} 1008',
).replace(
    'node_cpu_seconds_total{cpu="0",mode="user"} 200',
    'node_cpu_seconds_total{cpu="0",mode="user"} 202',
).replace(
    'node_cpu_seconds_total{cpu="1",mode="idle"} 1000',
    'node_cpu_seconds_total{cpu="1",mode="idle"} 1005',
).replace(
    'node_cpu_seconds_total{cpu="1",mode="user"} 300',
    'node_cpu_seconds_total{cpu="1",mode="user"} 305',
)

# DCGM on a unified-memory part: no framebuffer families at all.
DCGM_UNIFIED = """\
# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization (in %).
# TYPE DCGM_FI_DEV_GPU_UTIL gauge
DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="GPU-abc",pci_bus_id="00000000:01:00.0",device="nvidia0",modelName="NVIDIA GB10",Hostname="gpu-box"} 37
DCGM_FI_DEV_GPU_TEMP{gpu="0",UUID="GPU-abc",pci_bus_id="00000000:01:00.0",device="nvidia0",modelName="NVIDIA GB10",Hostname="gpu-box"} 61
DCGM_FI_DEV_POWER_USAGE{gpu="0",UUID="GPU-abc",pci_bus_id="00000000:01:00.0",device="nvidia0",modelName="NVIDIA GB10",Hostname="gpu-box"} 88.5
DCGM_FI_DEV_SM_CLOCK{gpu="0",UUID="GPU-abc",modelName="NVIDIA GB10"} 1500
"""

# DCGM on a discrete card: framebuffer in MiB.
DCGM_DISCRETE = """\
DCGM_FI_DEV_GPU_UTIL{gpu="0",modelName="NVIDIA RTX 6000"} 12
DCGM_FI_DEV_GPU_TEMP{gpu="0",modelName="NVIDIA RTX 6000"} 45
DCGM_FI_DEV_POWER_USAGE{gpu="0",modelName="NVIDIA RTX 6000"} 70
DCGM_FI_DEV_FB_USED{gpu="0",modelName="NVIDIA RTX 6000"} 20480
DCGM_FI_DEV_FB_FREE{gpu="0",modelName="NVIDIA RTX 6000"} 28672
DCGM_FI_DEV_FB_TOTAL{gpu="0",modelName="NVIDIA RTX 6000"} 49152
"""

NVSMI_TEXT = """\
nvidia_smi_utilization_gpu_ratio{uuid="GPU-1",name="NVIDIA GeForce RTX 4090"} 0.42
nvidia_smi_temperature_gpu{uuid="GPU-1",name="NVIDIA GeForce RTX 4090"} 55
nvidia_smi_power_draw_watts{uuid="GPU-1",name="NVIDIA GeForce RTX 4090"} 210
nvidia_smi_memory_used_bytes{uuid="GPU-1",name="NVIDIA GeForce RTX 4090"} 8589934592
nvidia_smi_memory_total_bytes{uuid="GPU-1",name="NVIDIA GeForce RTX 4090"} 25769803776
"""

NODE_URL = "http://gpu-box:9100/metrics"
GPU_URL = "http://gpu-box:9400/metrics"


class _Case(unittest.TestCase):
    """Exporters configured through `config()`, the switch through the registry,
    HTTP through `_http_get` - so nothing touches settings, env or the network."""

    def setUp(self):
        hwinfo.reset_cache()
        self.pages: dict[str, tuple[int, str] | Exception] = {}
        self.calls: list[str] = []
        self.cfg = {"label": "", "node_url": NODE_URL, "gpu_url": GPU_URL,
                    "disk_mount": "/", "timeout_s": 1.5}
        self.on = True

        def fake_get(url, timeout):
            self.calls.append(url)
            page = self.pages.get(url)
            if page is None:
                raise requests.exceptions.ConnectionError(url)
            if isinstance(page, Exception):
                raise page
            return page

        real_enabled = features.enabled

        def fake_enabled(key):
            if key == hwexporters.KEY:
                return self.on
            return real_enabled(key)

        for p in (mock.patch.object(hwexporters, "_http_get", side_effect=fake_get),
                  mock.patch.object(hwexporters, "config", side_effect=lambda: dict(self.cfg)),
                  mock.patch.object(features, "enabled", side_effect=fake_enabled)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(hwinfo.reset_cache)

    def serve(self, node=NODE_TEXT, gpu=DCGM_UNIFIED):
        self.pages = {}
        if node is not None:
            self.pages[NODE_URL] = (200, node)
        if gpu is not None:
            self.pages[GPU_URL] = (200, gpu)
        hwexporters.reset_cache()


# --------------------------------------------------------------------------- #
# The text format
# --------------------------------------------------------------------------- #

class ParseTests(unittest.TestCase):
    def test_labels_comments_exponents_and_nan(self):
        text = (
            "# HELP x\n"
            'x{a="1",b="with \\"quote\\", comma"} 1.5e+03 1700000000\n'
            "y NaN\n"
            "z +Inf\n"
            "plain 7\n"
            "garbage line here\n"
        )
        m = hwexporters.parse_metrics(text)
        self.assertEqual(m["x"][0].labels, {"a": "1", "b": 'with "quote", comma'})
        self.assertEqual(m["x"][0].value, 1500.0)
        self.assertNotIn("y", m)          # NaN is "no reading", never a number
        self.assertNotIn("z", m)
        self.assertEqual(m["plain"][0].value, 7.0)

    def test_want_filters_before_parsing(self):
        m = hwexporters.parse_metrics(NODE_TEXT, frozenset({"node_uname_info"}))
        self.assertEqual(set(m), {"node_uname_info"})
        self.assertEqual(m["node_uname_info"][0].labels["nodename"], "gpu-box")


# --------------------------------------------------------------------------- #
# node_exporter -> memory, CPU, disk, name
# --------------------------------------------------------------------------- #

class NodeExporterTests(_Case):
    def test_system_memory(self):
        self.serve()
        m = hwexporters.system_mem(hwexporters.reading())
        self.assertEqual(m.source, "system-node-exporter")
        self.assertAlmostEqual(m.total_gb, 128.0, places=3)
        self.assertAlmostEqual(m.free_gb, 96.0, places=3)

    def test_cpu_needs_two_samples_then_differences_them(self):
        self.serve()
        self.assertIsNone(hwexporters.reading().cpu_util)      # one sample: unknown
        self.pages[NODE_URL] = (200, NODE_TEXT_LATER)
        hwexporters._state["reading"] = None                   # expire the TTL
        self.assertEqual(hwexporters.reading().cpu_util, 35)   # 13 idle of 20

    def test_disk_is_the_mount_holding_the_configured_directory(self):
        self.cfg["disk_mount"] = "/data/models/weights"
        self.serve()
        d = hwexporters.disk(hwexporters.reading())
        self.assertEqual(d["path"], "/data")
        self.assertEqual(d["total_gb"], 4000.0)
        self.assertEqual(d["used_gb"], 3000.0)      # size - free (reserved counts as used)
        self.assertEqual(d["free_gb"], 900.0)       # what a user may actually fill
        self.assertEqual(d["used_pct"], 75)
        self.assertFalse(d["capped"])

    def test_disk_with_no_covering_mount_is_unknown_not_invented(self):
        # A directory the exporter reports no filesystem for (here: the root
        # filesystem is not exported, so nothing covers /mnt/elsewhere) is
        # unknown. It is NOT the nearest exported volume, and it is not zero.
        self.cfg["disk_mount"] = "/mnt/elsewhere"
        no_root = "\n".join(line for line in NODE_TEXT.splitlines()
                            if 'mountpoint="/"' not in line)
        self.serve(node=no_root)
        d = hwexporters.disk(hwexporters.reading())
        self.assertIsNone(d["total_gb"])
        self.assertIsNone(d["used_pct"])
        self.assertEqual(d["path"], "/mnt/elsewhere")

    def test_disk_for_a_directory_on_root_is_the_root_volume(self):
        # ...whereas a directory that simply is not its own mount lives on the
        # root filesystem, and saying so is the honest reading of the box.
        self.cfg["disk_mount"] = "/home/models"
        self.serve()
        d = hwexporters.disk(hwexporters.reading())
        self.assertEqual(d["path"], "/")
        self.assertEqual(d["total_gb"], 1000.0)

    def test_label_prefers_owner_then_hostname_then_address(self):
        self.serve()
        self.assertEqual(hwexporters.label(), "gpu-box")
        self.cfg["label"] = "Workstation"
        self.assertEqual(hwexporters.label(), "Workstation")
        self.cfg["label"] = ""
        self.serve(node=None)                       # no uname: fall back to the URL host
        self.assertEqual(hwexporters.label(), "gpu-box")


# --------------------------------------------------------------------------- #
# GPU exporters
# --------------------------------------------------------------------------- #

class GpuExporterTests(_Case):
    def test_dcgm_without_framebuffer_is_unified_memory(self):
        self.serve()
        r = hwexporters.reading()
        (g,) = hwexporters.gpus(r)
        self.assertEqual((g.name, g.util, g.temp_c, g.power_w, g.source),
                         ("NVIDIA GB10", 37.0, 61.0, 88.5, "dcgm"))
        self.assertIsNone(g.mem_total_gb)           # absent stays None, never 0
        mem, status = hwexporters.vram_probe(r)
        self.assertEqual(status, hwinfo._PROBE_NA)
        self.assertFalse(mem.readable)

    def test_dcgm_with_framebuffer_is_measured_vram(self):
        self.serve(gpu=DCGM_DISCRETE)
        r = hwexporters.reading()
        (g,) = hwexporters.gpus(r)
        self.assertAlmostEqual(g.mem_total_gb, 48.0)
        self.assertAlmostEqual(g.mem_used_gb, 20.0)
        mem, status = hwexporters.vram_probe(r)
        self.assertEqual(status, hwinfo._PROBE_MEASURED)
        self.assertEqual(mem.source, "vram-dcgm")
        self.assertAlmostEqual(mem.free_gb, 28.0)

    def test_nvidia_smi_exporter_schema(self):
        self.serve(gpu=NVSMI_TEXT)
        (g,) = hwexporters.gpus(hwexporters.reading())
        self.assertEqual(g.name, "NVIDIA GeForce RTX 4090")
        self.assertEqual(g.util, 42.0)              # ratio -> percent
        self.assertAlmostEqual(g.mem_total_gb, 24.0)
        self.assertAlmostEqual(g.mem_used_gb, 8.0)
        self.assertEqual(g.source, "nvidia-smi-exporter")

    def test_no_gpu_exporter_named_means_no_accelerator_not_unreadable(self):
        self.cfg["gpu_url"] = ""
        self.serve(gpu=None)
        r = hwexporters.reading()
        self.assertEqual(r.state, "ok")
        self.assertEqual(hwexporters.vram_probe(r)[1], hwinfo._PROBE_NONE)
        self.assertEqual(hwexporters.gpus(r), [])


# --------------------------------------------------------------------------- #
# The HAL follows the remote box - everywhere, with one switch
# --------------------------------------------------------------------------- #

class HalFollowsRemoteTests(_Case):
    def test_public_readers_report_the_remote_box(self):
        self.serve()
        self.assertEqual(hwinfo.system_mem().source, "system-node-exporter")
        self.assertEqual(hwinfo.gpus()[0].source, "dcgm")
        self.assertEqual(hwinfo.gpu().name, "NVIDIA GB10")
        self.assertEqual(hwinfo.vram_probe()[1], hwinfo._PROBE_NA)

    def test_fit_memory_is_the_remote_unified_pool_without_our_container_cap(self):
        self.serve()
        with mock.patch.object(hwinfo, "_apply_cgroup_cap",
                               side_effect=AssertionError("our cgroup is not that box's")):
            m = hwinfo.fit_memory()
        self.assertEqual(m.source, "system-node-exporter")
        self.assertAlmostEqual(m.total_gb, 128.0, places=3)

    def test_fit_pool_is_unified_accelerated_and_uncapped(self):
        self.serve()
        with mock.patch.object(hwinfo, "stated_fit_gb", return_value=None):
            p = hwinfo.fit_pool()
        self.assertEqual((p.kind, p.accelerated, p.accel_name, p.capped),
                         ("unified", True, "NVIDIA GB10", False))
        self.assertAlmostEqual(p.total_gb, 128.0, places=3)

    def test_fit_pool_is_vram_on_a_remote_discrete_card(self):
        self.serve(gpu=DCGM_DISCRETE)
        with mock.patch.object(hwinfo, "stated_fit_gb", return_value=None):
            p = hwinfo.fit_pool()
        self.assertEqual((p.kind, p.accelerated), ("vram", True))
        self.assertAlmostEqual(p.total_gb, 48.0)

    def test_monitor_readers_follow_too(self):
        self.serve()
        self.assertEqual(hardware._gpu()["name"], "NVIDIA GB10")
        self.assertEqual(hardware._mem()["total_gb"], 128.0)
        self.assertEqual(hardware._disk()["total_gb"], 1000.0)
        self.assertIsNone(hardware._cpu())          # first sample: unknown, not 0
        src = hardware._machine()
        self.assertEqual((src["kind"], src["label"], src["reachable"], src["error_code"]),
                         ("exporters", "gpu-box", True, ""))

    def test_local_process_inventory_is_skipped_for_a_remote_box(self):
        self.serve()
        procs = mock.Mock(return_value=[])
        docker = mock.Mock(return_value=[])
        with mock.patch.object(hardware, "_gpu_model_processes", procs), \
             mock.patch.object(hardware, "_docker_model_containers", docker), \
             mock.patch.object(hardware, "_configured_backends", return_value=[]), \
             mock.patch("ava_bridge.models.effective_brain",
                        return_value={"source": "configured", "backend_id": ""}):
            self.assertEqual(hardware._loaded_models(), [])
            procs.assert_not_called()
            docker.assert_not_called()
            # ...and it is the remote source doing that, not the mocks.
            self.on = False
            hwexporters.reset_cache()
            hardware._loaded_models()
            procs.assert_called_once()

    def test_snapshot_names_the_source(self):
        self.serve()
        self.assertEqual(hwinfo.snapshot()["machine"]["kind"], "exporters")


# --------------------------------------------------------------------------- #
# Off, unconfigured, unreachable
# --------------------------------------------------------------------------- #

class GatingTests(_Case):
    def test_off_reads_local_says_so_and_never_fetches(self):
        self.on = False
        self.serve()
        self.assertIsNone(hwinfo.remote_source())
        d = hwexporters.describe()
        self.assertEqual((d["kind"], d["error_code"]), ("local", "remote_hardware_off"))
        self.assertIn("Setup", d["error"])
        self.assertEqual(self.calls, [])
        # The local readers answer as themselves.
        self.assertNotEqual(hwinfo.system_mem().source, "system-node-exporter")

    def test_unconfigured_is_plain_local(self):
        self.cfg.update(node_url="", gpu_url="")
        self.serve(node=None, gpu=None)
        self.assertIsNone(hwinfo.remote_source())
        self.assertEqual(hwexporters.describe(),
                         {"kind": "local", "label": "", "reachable": True,
                          "error_code": "", "error": ""})
        self.assertEqual(self.calls, [])

    def test_unreachable_yields_nothing_never_this_box(self):
        self.pages = {}                              # both refuse
        hwexporters.reset_cache()
        self.assertIsNotNone(hwinfo.remote_source())  # still the configured source
        self.assertFalse(hwinfo.system_mem().readable)
        self.assertEqual(hwinfo.gpus(), [])
        self.assertEqual(hwinfo.vram_probe()[1], hwinfo._PROBE_UNREADABLE)
        self.assertFalse(hwinfo.fit_memory().readable)
        self.assertIsNone(hardware._cpu())
        self.assertIsNone(hardware._disk()["total_gb"])
        d = hardware._machine()
        self.assertEqual((d["kind"], d["reachable"], d["error_code"]),
                         ("exporters", False, "remote_hardware_down"))
        self.assertIn("could not be reached", d["error"])
        self.assertIn(NODE_URL, d["error"])

    def test_partial_failure_keeps_what_answered(self):
        self.serve(gpu=None)                         # node up, GPU exporter refuses
        r = hwexporters.reading()
        self.assertEqual((r.state, r.code), ("down", "remote_hardware_down"))
        self.assertEqual(hwexporters.system_mem(r).source, "system-node-exporter")
        self.assertEqual(hwexporters.gpus(r), [])
        self.assertIn("GPU exporter", r.error)
        self.assertNotIn("node_exporter at", r.error)

    def test_http_error_and_wrong_page_are_named(self):
        self.serve()
        self.pages[GPU_URL] = (503, "")
        hwexporters.reset_cache()
        self.assertIn("HTTP 503", hwexporters.reading().gpu_error)
        self.pages[GPU_URL] = (200, "<html>not an exporter</html>")
        hwexporters.reset_cache()
        self.assertIn("none of the metrics", hwexporters.reading().gpu_error)

    def test_one_fetch_serves_every_reader_within_the_ttl(self):
        self.serve()
        hwinfo.system_mem()
        hwinfo.gpus()
        hardware._cpu()
        hardware._disk()
        self.assertEqual(sorted(self.calls), sorted([NODE_URL, GPU_URL]))


class RegistryTests(unittest.TestCase):
    def test_registered_with_a_panel_checkbox_and_an_env_pin(self):
        spec = features.REGISTRY[hwexporters.KEY]
        self.assertEqual(spec["env"], "AVA_REMOTE_HARDWARE")
        self.assertFalse(spec["default"])
        self.assertIn(hwexporters.KEY, [f["key"] for f in features.snapshot()])

    def test_url_validation(self):
        self.assertEqual(hwexporters.validate_url(""), "")
        self.assertEqual(hwexporters.validate_url("http://gpu-box:9100/metrics"), "")
        self.assertIn("http", hwexporters.validate_url("gpu-box:9100"))
        self.assertIn("host", hwexporters.validate_url("http:///metrics"))


if __name__ == "__main__":
    unittest.main()
