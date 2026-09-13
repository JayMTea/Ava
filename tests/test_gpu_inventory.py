"""Host inventory crosses the exporter boundary without secrets or false emptiness."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ava_bridge import gpu_inventory, hardware, hwexporters


class CollectorTests(unittest.TestCase):
    def test_workers_group_and_components_export_without_commands_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            for pid, parent, args in (
                (100, 1, ["vllm", "serve", "acme/Reasoner", "--api-key", "NEVER_EXPORT"]),
                (101, 100, ["VLLM::EngineCore"]),
                (200, 1, ["python", "/private/ComfyUI/main.py"]),
            ):
                folder = proc / str(pid)
                folder.mkdir()
                (folder / "cmdline").write_bytes("\0".join(args).encode())
                (folder / "stat").write_text(f"{pid} (worker) S {parent} 0 0")
                (folder / "maps").write_text(
                    "0-1 r--p 0 0 0 /private/models/text_encoders/encoder.safetensors\n"
                    if pid == 200 else "")
            run = mock.Mock(return_value=SimpleNamespace(
                stdout="100, python, 170\n101, VLLM::EngineCore, 44000\n200, python, 66000\n"))
            rows = gpu_inventory.collect(proc, run)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["model_id"], "acme/Reasoner")
            self.assertEqual(rows[0]["memory_bytes"], 44170 * 1024 ** 2)
            text = gpu_inventory.render(rows, time.time())
            self.assertNotIn("NEVER_EXPORT", text)
            self.assertNotIn("/private", text)
            self.assertIn('name="encoder.safetensors"', text)
            reading = hwexporters.Reading(node=hwexporters.parse_metrics(text))
            inventory = hwexporters.model_inventory(reading)
            self.assertEqual(inventory["state"], "ok")
            self.assertEqual(inventory["rows"][1]["name"], "ComfyUI")
            self.assertIsNone(inventory["rows"][1]["components"][0]["in_memory"])

    def test_failed_collection_replaces_old_snapshot_with_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gpu.prom"
            output.write_text("old data")
            with mock.patch.object(gpu_inventory, "collect", side_effect=OSError):
                gpu_inventory.write_snapshot(output)
            reading = hwexporters.Reading(node=hwexporters.parse_metrics(output.read_text()))
            self.assertEqual(hwexporters.model_inventory(reading)["state"], "unavailable")
            self.assertNotIn("old data", output.read_text())

    def test_stale_missing_failed_and_empty_are_distinct(self):
        for timestamp, success, expected in (
            (time.time() - 60, True, "stale"), (time.time() + 60, True, "stale"),
            (time.time(), False, "unavailable"), (time.time(), True, "ok"),
        ):
            reading = hwexporters.Reading(node=hwexporters.parse_metrics(
                gpu_inventory.render([], timestamp, success)))
            self.assertEqual(hwexporters.model_inventory(reading)["state"], expected)
        self.assertEqual(hwexporters.model_inventory(hwexporters.Reading())["state"], "unavailable")


class RemoteJoinTests(unittest.TestCase):
    def run_inventory(self, agent_host="gpu.example", model="acme/Reasoner", stale=False):
        text = gpu_inventory.render([
            {"pid": 100, "runtime": "vLLM", "model_id": "acme/Reasoner",
             "memory_bytes": 44 * 1024 ** 3, "components": []},
            {"pid": 200, "runtime": "ComfyUI", "model_id": "",
             "memory_bytes": 65 * 1024 ** 3, "components": [
                 {"name": "image-model.safetensors", "kind": "unet"}]},
        ], time.time() - (60 if stale else 0))
        reading = hwexporters.Reading(node=hwexporters.parse_metrics(text))
        with (
            mock.patch("ava_bridge.hardware.hwinfo.remote_source", return_value=(hwexporters, reading)),
            mock.patch.object(hwexporters, "config", return_value={"node_url": "http://gpu.example/metrics"}),
            mock.patch("ava_bridge.config.AGENT_URL", f"http://{agent_host}/agent"),
            mock.patch("ava_bridge.models.effective_brain", return_value={
                "source": "agent", "model_id": model, "local": False}),
            mock.patch.object(hardware, "_configured_backends", return_value=[]),
            mock.patch.object(hardware, "_owning_app", return_value=""),
            mock.patch.object(hardware, "_gpu_model_processes", side_effect=AssertionError("Wrong host")),
            mock.patch.object(hardware, "_read_mapped_model_components", side_effect=AssertionError("Wrong PID namespace")),
        ):
            return hardware._loaded_models()

    def test_brain_joins_only_matching_host_and_model_without_duplicate(self):
        rows = self.run_inventory()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["role_key"], "brain")
        self.assertEqual(rows[0]["state"], "resident")
        self.assertEqual(rows[0]["memory_gb"], 44)
        self.assertTrue(rows[0]["local"])
        self.assertEqual(rows[1]["relation"], "foreign")
        self.assertEqual(rows[1]["components"][0]["name"], "image-model.safetensors")

    def test_same_model_on_other_host_is_not_claimed_as_the_brain(self):
        rows = self.run_inventory(agent_host="cloud.example")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["state"], "remote")
        self.assertIsNone(rows[0]["memory_gb"])
        self.assertEqual(rows[1]["relation"], "foreign")

    def test_stale_inventory_never_keeps_claiming_residency(self):
        rows = self.run_inventory(stale=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIsNone(rows[0]["memory_gb"])

    def test_wrong_model_on_the_same_host_stays_unattributed(self):
        rows = self.run_inventory(model="acme/Other")
        self.assertEqual(rows[0]["state"], "unknown")
        self.assertIsNone(rows[0]["memory_gb"])
        self.assertTrue(all(r["relation"] == "foreign" for r in rows[1:]))
