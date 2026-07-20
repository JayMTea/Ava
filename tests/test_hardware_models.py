"""hardware.py model inventory: engine worker processes merge into one row and
model identity is READ (process cmdline / backend config / live API) — never
assumed from the runtime kind — so any self-hosted model is labeled honestly.

Guards the regression where every vLLM-ish GPU process was hardcoded to
"open-model 30B", which (a) showed the model twice in the hardware monitor's
picker (launcher + EngineCore worker = two PIDs, same name) and (b) would
mislabel whatever model a fork actually serves.

Run: .venv/bin/python -m pytest tests/test_hardware_models.py -q
"""
import unittest
from unittest import mock

from ava_bridge import hardware

# A vLLM-style engine: launcher (declares --model) + bare-cmdline worker,
# plus an unrelated the GPU service process. Deliberately NOT an NVIDIA/Ava model —
# the inventory must label it from the cmdline, not from a built-in name.
SMI_COMPUTE_APPS = (
    "100, python3, 170\n"
    "101, VLLM::EngineCore, 64000\n"
    "200, /opt/gpusvc/.venv/bin/python, 512\n"
)
CMDLINES = {
    100: "/usr/bin/python3 /usr/local/bin/vllm serve --model acme/Cool-LLM-7B-FP8 --port 9999",
    101: "VLLM::EngineCore",
    200: "/opt/gpusvc/.venv/bin/python /opt/the GPU service/main.py",
}
PPIDS = {101: 100, 100: 50, 200: 60}  # 101's parent is the launcher; 50/60 = shells


def _fake_run(cmd, timeout=4):
    if any("--query-compute-apps" in c for c in cmd):
        return SMI_COMPUTE_APPS
    return ""  # pmon (per-process util): unavailable on this fake box


class GpuProcessGrouping(unittest.TestCase):
    def setUp(self):
        for p in (
            mock.patch.object(hardware.shutil, "which", lambda name: f"/usr/bin/{name}"),
            mock.patch.object(hardware, "_run", _fake_run),
            mock.patch.object(hardware, "_proc_cmdline", lambda pid: CMDLINES.get(pid, "")),
            mock.patch.object(hardware, "_proc_ppid", PPIDS.get),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_one_engine_many_workers_is_one_row(self):
        rows = hardware._gpu_model_processes()
        vllm = [r for r in rows if r["name"] == "vLLM"]
        self.assertEqual(len(vllm), 1, f"launcher+worker must merge, got {vllm}")
        row = vllm[0]
        self.assertEqual(row["id"], "pid:100")          # stable id = owning launcher
        self.assertEqual(row["pid"], 101)               # surfaced pid = weight-holding worker
        self.assertEqual(row["memory_mb"], 64170)       # memory = family total

    def test_model_name_read_from_cmdline_not_assumed(self):
        row = [r for r in hardware._gpu_model_processes() if r["name"] == "vLLM"][0]
        self.assertEqual(row["model"], "Cool-LLM-7B-FP8")
        self.assertEqual(row["model_id"], "acme/Cool-LLM-7B-FP8")
        self.assertNotIn("Omni", row["model"])

    def test_unrelated_process_stays_its_own_row(self):
        rows = hardware._gpu_model_processes()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r for r in rows if r["name"] == "the GPU service"][0]["pid"], 200)


class _Resp:
    def __init__(self, ids):
        self.ok = True
        self._ids = ids

    def json(self):
        return {"data": [{"id": i} for i in self._ids]}


BACKEND = {"id": "brain", "url": "http://127.0.0.1:9999/v1",
           "model": "acme/Cool-LLM-7B-FP8", "label": "Cool LLM", "engine": "vllm"}


class BackendCrossCheck(unittest.TestCase):
    """_loaded_models ties rows to configured backends instead of a name map."""

    def setUp(self):
        self.proc_row = {
            "id": "pid:100", "name": "vLLM", "model": "Cool-LLM-7B-FP8",
            "model_id": "acme/Cool-LLM-7B-FP8", "memory_mb": 64170.0,
            "memory_gb": 62.67, "gpu_util": 12.0, "pid": 101,
            "status": "loaded", "source": "nvidia-smi",
            "cmd": CMDLINES[100],
        }
        for p in (
            mock.patch.object(hardware, "_docker_model_containers", lambda: []),
            mock.patch.object(hardware, "_configured_backends", lambda: [dict(BACKEND)]),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_running_engine_tags_existing_row_no_duplicate(self):
        with mock.patch.object(hardware, "_gpu_model_processes",
                               lambda: [dict(self.proc_row)]), \
             mock.patch.object(hardware.requests, "get",
                               lambda url, timeout=0: _Resp(["acme/Cool-LLM-7B-FP8"])):
            rows = hardware._loaded_models()
        self.assertEqual(len(rows), 1, "API cross-check must not add a second row")
        self.assertEqual(rows[0]["backend"], "brain")
        self.assertEqual(rows[0]["model"], "Cool LLM")  # display label from config
        served = [c for c in rows[0]["components"] if c["kind"] == "served-model"]
        self.assertEqual(served[0]["name"], "acme/Cool-LLM-7B-FP8")  # real id still shown

    def test_configured_engine_down_shows_offline_row_labeled_from_config(self):
        def boom(url, timeout=0):
            raise OSError("connection refused")
        with mock.patch.object(hardware, "_gpu_model_processes", lambda: []), \
             mock.patch.object(hardware.requests, "get", boom):
            rows = hardware._loaded_models()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "offline")
        self.assertEqual(rows[0]["model"], "Cool LLM")

    def test_unnamed_engine_row_claimed_by_api_not_duplicated(self):
        bare = dict(self.proc_row, model="vLLM model", model_id=None, cmd="VLLM::EngineCore")
        with mock.patch.object(hardware, "_gpu_model_processes", lambda: [bare]), \
             mock.patch.object(hardware.requests, "get",
                               lambda url, timeout=0: _Resp(["acme/Cool-LLM-7B-FP8"])):
            rows = hardware._loaded_models()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "Cool LLM")
        self.assertEqual(rows[0]["model_id"], "acme/Cool-LLM-7B-FP8")


class gpusvcComponentFallback(unittest.TestCase):
    """The configured-gpusvc-stack fallback must never claim residency it didn't
    observe: readable-but-empty maps = "not in memory"; unreadable = unknown."""

    def setUp(self):
        self.row = {"id": "pid:200", "name": "the GPU service", "model": "the GPU service", "model_id": None,
                    "memory_mb": 170.0, "memory_gb": 0.17, "gpu_util": None, "pid": 200,
                    "status": "loaded", "source": "nvidia-smi", "cmd": ""}
        for p in (
            mock.patch.object(hardware, "_read_mapped_model_components", lambda pid: []),
            mock.patch.object(hardware, "_read_open_model_components", lambda pid: []),
        ):
            p.start()
            self.addCleanup(p.stop)

    def test_observed_empty_scan_means_not_in_memory(self):
        with mock.patch.object(hardware, "_proc_maps_readable", lambda pid: True):
            item = hardware._attach_components([dict(self.row)])[0]
        self.assertTrue(item["components"])
        self.assertTrue(all(c["in_memory"] is False for c in item["components"]))
        self.assertEqual(item["component_count"], 0)

    def test_unobservable_process_means_unknown_not_loaded_claim(self):
        with mock.patch.object(hardware, "_proc_maps_readable", lambda pid: False):
            item = hardware._attach_components([dict(self.row)])[0]
        self.assertTrue(all(c["in_memory"] is None for c in item["components"]))
        self.assertEqual(item["component_count"], 0)

    def test_actually_mapped_files_still_report_resident(self):
        mapped = [{"name": "gpu_model_base", "kind": "checkpoints",
                   "kind_label": "Base checkpoint", "path": "/m/gpu_model_base",
                   "in_memory": True}]
        with mock.patch.object(hardware, "_read_mapped_model_components", lambda pid: mapped):
            item = hardware._attach_components([dict(self.row)])[0]
        self.assertEqual(item["component_count"], 1)
        self.assertTrue(item["components"][0]["in_memory"])


class HonestNaming(unittest.TestCase):
    def test_placeholders_stay_generic_per_runtime(self):
        self.assertEqual(hardware._short_model_name(None, runtime="vllm serve"), "vLLM model")
        self.assertEqual(hardware._short_model_name("VLLM::EngineCore"), "vLLM model")
        self.assertEqual(hardware._short_model_name("python3", runtime="/x/the GPU service/main.py"),
                         "the GPU service")
        self.assertEqual(hardware._short_model_name(None), "Model")

    def test_real_ids_keep_their_own_name(self):
        self.assertEqual(hardware._short_model_name("acme/Cool-LLM-7B-FP8"), "Cool-LLM-7B-FP8")
        self.assertEqual(hardware._short_model_name("gpu_model_base"),
                         "gpu_model_base")

    def test_brain_role_comes_from_backend_tag_not_name_guess(self):
        self.assertIn("brain", hardware._model_role("anything", backend_id="brain"))
        self.assertNotIn("brain", hardware._model_role("Some-Nemotron-30B-Omni"))


if __name__ == "__main__":
    unittest.main()
