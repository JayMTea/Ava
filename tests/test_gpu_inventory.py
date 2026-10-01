"""Host inventory crosses the exporter boundary without secrets or false emptiness."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ava_bridge import gpu_inventory, hardware, hwexporters


class CollectorTests(unittest.TestCase):
    def test_snapshot_paths_export_repository_names_instead_of_revisions(self):
        revision = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
        for path in (
            f"/private/hub/models--acme--Embed-8B/snapshots/{revision}",
            f"C:\\private\\hub\\models--acme--Embed-8B\\snapshots\\{revision}",
        ):
            text = gpu_inventory.render([{
                "pid": 100, "runtime": "vLLM", "model_id": path,
                "memory_bytes": 18 * 1024 ** 3, "components": [],
            }], time.time())
            inventory = hwexporters.model_inventory(
                hwexporters.Reading(node=hwexporters.parse_metrics(text)))
            row = inventory["rows"][0]
            self.assertEqual(row["model_id"], "acme/Embed-8B")
            self.assertEqual(row["model"], "Embed-8B")
            self.assertNotIn("private", text)
            self.assertNotIn(revision, text)

    def test_legacy_exporters_do_not_display_hashes_as_model_names(self):
        for opaque in ("abcdef0123456789abcdef", "sha256-abcdef0123456789", "123456"):
            text = (
                f"ava_gpu_inventory_timestamp_seconds {time.time()}\n"
                "ava_gpu_inventory_success 1\n"
                f'ava_gpu_process_info{{pid="100",runtime="vLLM",model_id="{opaque}"}} 1\n'
                'ava_gpu_process_memory_bytes{pid="100"} 1024\n'
            )
            row = hwexporters.model_inventory(
                hwexporters.Reading(node=hwexporters.parse_metrics(text)))["rows"][0]
            self.assertIsNone(row["model_id"])
            self.assertEqual(row["model"], "vLLM")
            self.assertEqual(row["state"], "resident")

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


@unittest.skipUnless(os.name == "posix", "Linux /proc descriptor and mount semantics")
class ActiveGenerationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.proc = Path(directory.name) / "proc"
        self.process = self.proc / "100"
        (self.process / "fd").mkdir(parents=True)
        (self.process / "fdinfo").mkdir()
        (self.process / "cmdline").write_bytes(b"python3\0generate.py\0")
        (self.process / "stat").write_text("100 (python3) S 1 0 0")
        self.manifest = self.add_run("3", "batch", "acme/Image-Model")

    def add_run(self, fd, run, model):
        # The container's /runs lives under /proc/PID/root, not on this host.
        folder = self.process / "root" / "runs" / run
        folder.mkdir(parents=True)
        (folder / "generated.jsonl").write_text("PRIVATE_GENERATED_RECORD")
        (self.process / "fd" / fd).symlink_to(f"/runs/{run}/generated.jsonl")
        (self.process / "fdinfo" / fd).write_text("pos:\t123\nflags:\t02402001\n")
        manifest = folder / "run.json"
        manifest.write_text(json.dumps({
            "generator": {"name": "diffusers", "model_id": model},
            "images_started_at": "2026-09-30T10:00:00Z",
            "images_finished_at": None,
            "prompt": "PRIVATE_PROMPT", "api_key": "PRIVATE_KEY",
        }))
        return manifest

    def row(self):
        run = mock.Mock(return_value=SimpleNamespace(stdout="100, python3, 64265\n"))
        rows = gpu_inventory.collect(self.proc, run)
        text = gpu_inventory.render(rows, time.time())
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("/runs", text)
        row = hwexporters.model_inventory(
            hwexporters.Reading(node=hwexporters.parse_metrics(text)))["rows"][0]
        self.assertEqual(row["memory_mb"], 64265)
        return row

    def edit_run(self, **changes):
        document = json.loads(self.manifest.read_text())
        document.update(changes)
        self.manifest.write_text(json.dumps(document))

    def test_active_python_writer_resolves_the_observed_model(self):
        self.assertEqual(self.row()["model"], "Image-Model")
        self.assertEqual(self.row()["model_id"], "acme/Image-Model")
        self.assertEqual(self.row()["name"], "python3")  # runtime remains a separate fact

    def test_future_model_identities_are_read_on_each_collection(self):
        self.assertEqual(self.row()["model"], "Image-Model")
        self.edit_run(generator={"name": "diffusers", "model_id": "acme/Next-Model"})
        self.assertEqual(self.row()["model"], "Next-Model")
        self.edit_run(generator={"name": "diffusers", "model_id": "acme/Updated-Model"})
        self.assertEqual(self.row()["model"], "Updated-Model")

    def test_readers_non_appenders_and_closed_descriptors_cannot_claim_a_model(self):
        for flags in ("0", "02000", "01"):
            with self.subTest(flags=flags):
                (self.process / "fdinfo" / "3").write_text(f"flags:\t{flags}\n")
                self.assertIsNone(self.row()["model_id"])
        (self.process / "fd" / "3").unlink()
        self.assertIsNone(self.row()["model_id"])

    def test_finished_runs_are_rejected_but_a_resumed_run_is_recognized(self):
        self.edit_run(images_finished_at="2026-09-30T11:00:00Z")
        self.assertIsNone(self.row()["model_id"])
        self.edit_run(images_started_at="2026-09-30T12:00:00Z")
        self.assertEqual(self.row()["model"], "Image-Model")

    def test_remote_generators_do_not_name_local_gpu_memory(self):
        for engine in ("comfyui", "api", "fake"):
            with self.subTest(engine=engine):
                self.edit_run(generator={"name": engine, "model_id": "acme/Image-Model"})
                self.assertIsNone(self.row()["model_id"])

    def test_conflicting_active_models_are_not_arbitrarily_selected(self):
        other = self.add_run("4", "other", "acme/Other-Model")
        self.assertIsNone(self.row()["model_id"])
        other.unlink()
        self.assertEqual(self.row()["model"], "Image-Model")

    def test_unreadable_malformed_and_oversized_manifests_preserve_memory(self):
        for value in ("{broken", "[]", '{"generator": null}', "x" * (1024 * 1024 + 1)):
            with self.subTest(length=len(value)):
                self.manifest.write_text(value)
                self.assertIsNone(self.row()["model_id"])
        self.manifest.unlink()
        self.assertIsNone(self.row()["model_id"])

    def test_command_line_identity_takes_precedence(self):
        (self.process / "cmdline").write_bytes(b"python3\0server.py\0--model\0acme/Served\0")
        self.assertEqual(self.row()["model_id"], "acme/Served")


class RemoteJoinTests(unittest.TestCase):
    def run_inventory(self, agent_host="gpu.example", model="acme/Reasoner", stale=False):
        text = gpu_inventory.render([
            {"pid": 100, "runtime": "vLLM", "model_id": "acme/Reasoner",
             "model_name": "Custom reasoner",
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
        self.assertEqual(rows[0]["model"], "Custom reasoner")
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
