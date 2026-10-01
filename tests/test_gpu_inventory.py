"""Host inventory crosses the exporter boundary without secrets or false emptiness."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
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


class RegistryNamesTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / "controller.sqlite3"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE record (kind TEXT, id TEXT, document TEXT)")
        self.proc = self.root / "proc"
        (self.proc / "100").mkdir(parents=True)
        self.revision = "abcdef0123456789abcdef0123456789abcdef0123"
        self.model_path = f"/private/models/hub/models--acme--Embed-8B/snapshots/{self.revision}"
        self.register("model:1", "My registered embedder", "acme/Embed-8B", self.model_path)

    def register(self, mid, name, repo, path):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO record VALUES ('model', ?, ?)", (mid, json.dumps({
                "id": mid, "name": name, "variants": [{
                    "source_repo": repo, "revision": self.revision,
                    "locations": [{"path": path, "relative_path": "local/" + Path(path).name,
                                   "hashes": {"secret": "NEVER_EXPORT"}}],
                }],
            })))

    def collect(self, model):
        (self.proc / "100" / "cmdline").write_bytes(
            "\0".join(["vllm", "serve", model, "--api-key", "NEVER_EXPORT"]).encode())
        run = mock.Mock(return_value=SimpleNamespace(stdout="100, python, 18974\n"))
        rows = gpu_inventory.collect(self.proc, run, model_registry=self.database)
        text = gpu_inventory.render(rows, time.time())
        self.assertNotIn("NEVER_EXPORT", text)
        self.assertNotIn("/private", text)
        return hwexporters.model_inventory(
            hwexporters.Reading(node=hwexporters.parse_metrics(text)))["rows"][0]

    def test_registered_name_survives_collection_and_exporter_boundary(self):
        for value in (self.model_path, "acme/Embed-8B", f"/models/{self.revision}", "model:1"):
            row = self.collect(value)
            self.assertEqual(row["model"], "My registered embedder")
            self.assertEqual(row["model_id"], "acme/Embed-8B")
            self.assertEqual(row["memory_mb"], 18974)

    def test_new_registrations_and_renames_are_read_on_the_next_collection(self):
        self.collect(self.model_path)
        self.register("model:2", "New local reasoner", None, "/private/new/reasoner.gguf")
        self.assertEqual(self.collect("/private/new/reasoner.gguf")["model"], "New local reasoner")
        self.assertEqual(self.collect("/models/local/reasoner.gguf")["model"], "New local reasoner")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE record SET document=json_set(document, '$.name', 'Renamed embedder') "
                       "WHERE id='model:1'")
        self.assertEqual(self.collect(self.model_path)["model"], "Renamed embedder")

    def test_same_revision_in_two_models_does_not_guess_the_name(self):
        self.register("model:2", "Other embedder", "other/Embed", "/private/other")
        row = self.collect(f"/models/{self.revision}")
        self.assertIsNone(row["model_id"])
        self.assertEqual(row["model"], "vLLM")
        self.assertEqual(self.collect(self.model_path)["model"], "My registered embedder")

    def test_missing_or_invalid_registry_preserves_inventory_and_repository_name(self):
        for filename in ("missing.sqlite3", "invalid.sqlite3"):
            self.database = self.root / filename
            if filename == "invalid.sqlite3":
                self.database.write_text("not a database")
            row = self.collect(self.model_path)
            self.assertEqual(row["model"], "Embed-8B")
            self.assertEqual(row["memory_mb"], 18974)
        self.assertFalse((self.root / "missing.sqlite3").exists())

    def test_model_families_with_multiple_quantizations_still_have_one_registered_name(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            model = json.loads(db.execute("SELECT document FROM record WHERE id='model:1'").fetchone()[0])
            model["variants"].append({"source_repo": "acme/Embed-8B-FP8", "locations": []})
            db.execute("UPDATE record SET document=? WHERE id='model:1'", (json.dumps(model),))
        self.assertEqual(self.collect("model:1")["model"], "My registered embedder")
        self.assertEqual(self.collect("acme/Embed-8B-FP8")["model"], "My registered embedder")
        self.assertTrue(gpu_inventory.audit_registry(self.database)["ok"])

    def test_shared_copy_uses_a_profile_name_and_preserves_each_registered_alias(self):
        self.register("model:2", "Folder alias", None, self.model_path)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO record VALUES ('profile', 'profile:embed', ?)",
                       (json.dumps({"model_id": "model:1"}),))
        for value in (self.model_path, "profile:embed"):
            self.assertEqual(self.collect(value)["model"], "My registered embedder")
        self.assertEqual(self.collect("model:2")["model"], "Folder alias")
        self.assertTrue(gpu_inventory.audit_registry(self.database)["ok"])

    def test_declared_name_beats_automatically_scanned_folder_alias(self):
        self.register("model:2", "Folder alias", None, self.model_path)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE record SET document=json_set(document, '$.source', 'registry') "
                       "WHERE id='model:1'")
        self.assertEqual(self.collect(self.model_path)["model"], "My registered embedder")

    def test_basename_alias_does_not_override_an_exact_registered_name(self):
        self.register("model:2", "image_folder", None, "/private/image_folder")
        self.register("model:3", "Registered image model", None, "/private/image_folder")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE record SET document=json_set(document, '$.source', 'registry') "
                       "WHERE id='model:3'")
        self.assertEqual(self.collect("image_folder")["model"], "image_folder")
        self.assertEqual(self.collect("/private/image_folder")["model"], "Registered image model")
        self.assertTrue(gpu_inventory.audit_registry(self.database)["ok"])

    def test_audit_detects_bad_stored_names_even_when_display_can_use_repository(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE record SET document=json_set(document, '$.name', '123456')")
        self.assertEqual(self.collect(self.model_path)["model"], "acme/Embed-8B")
        audit = gpu_inventory.audit_registry(self.database)
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["registered_models"], 1)

    def test_mapped_components_use_registered_names_including_onnx_and_binary_weights(self):
        self.register("model:2", "Registered voice", None, "/private/voice.onnx")
        (self.proc / "100" / "maps").write_text(
            "0-1 r--p 0 0 0 /private/voice.onnx\n"
            f"0-1 r--p 0 0 0 {self.model_path}/pytorch_model.bin\n"
            "0-1 r--p 0 0 0 /private/unrelated.bin\n")
        components = gpu_inventory._components(self.proc, 100, gpu_inventory._registry(self.database))
        self.assertEqual({c["name"] for c in components}, {"Registered voice", "My registered embedder"})

    def test_model_flags_cover_registered_models_without_confusing_python_modules(self):
        for flag in ("--model-id", "--model-path", "--ckpt_name"):
            self.assertEqual(gpu_inventory._model(["python3", "server.py", flag, "acme/New"]), "acme/New")
        self.assertEqual(gpu_inventory._model(["llama-server", "-m", "new.gguf"]), "new.gguf")
        self.assertEqual(gpu_inventory._model(["python3", "-m", "app.server"]), "")

    def test_unnamed_python_with_one_registered_mapped_model_gets_its_name(self):
        (self.proc / "100" / "cmdline").write_bytes(b"python3\0worker.py\0")
        (self.proc / "100" / "maps").write_text(
            f"0-1 r--p 0 0 0 {self.model_path}/model-00001.safetensors\n"
            f"0-1 r--p 0 0 0 {self.model_path}/model-00002.safetensors\n")
        run = mock.Mock(return_value=SimpleNamespace(stdout="100, python3, 20000\n"))
        rows = gpu_inventory.collect(self.proc, run, self.database)
        self.assertEqual(rows[0]["model_name"], "My registered embedder")
        self.assertEqual(rows[0]["model_id"], "acme/Embed-8B")
        self.assertEqual(len(rows[0]["components"]), 1)
        # A process holding two distinct models must not attribute all its memory to one.
        self.register("model:2", "Second model", None, "/private/other.onnx")
        with (self.proc / "100" / "maps").open("a") as stream:
            stream.write("0-1 r--p 0 0 0 /private/other.onnx\n")
        rows = gpu_inventory.collect(self.proc, run, self.database)
        self.assertEqual(rows[0]["model_id"], "")
        self.assertEqual({c["name"] for c in rows[0]["components"]},
                         {"My registered embedder", "Second model"})


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
        self.registry = {"acme/Image-Model": {("acme/Image-Model", "Registered image model")}}

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
        with mock.patch.object(gpu_inventory, "_registry", return_value=self.registry):
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

    def test_active_python_writer_resolves_the_registered_model(self):
        self.assertEqual(self.row()["model"], "Registered image model")
        self.assertEqual(self.row()["model_id"], "acme/Image-Model")
        self.assertEqual(self.row()["name"], "python3")  # runtime remains a separate fact

    def test_future_models_and_registry_renames_are_not_cached(self):
        self.assertEqual(self.row()["model"], "Registered image model")
        self.registry["acme/Next-Model"] = {("acme/Next-Model", "Newly registered model")}
        self.edit_run(generator={"name": "diffusers", "model_id": "acme/Next-Model"})
        self.assertEqual(self.row()["model"], "Newly registered model")
        self.registry["acme/Next-Model"] = {("acme/Next-Model", "Renamed model")}
        self.assertEqual(self.row()["model"], "Renamed model")

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
        self.assertEqual(self.row()["model"], "Registered image model")

    def test_remote_generators_do_not_name_local_gpu_memory(self):
        for engine in ("comfyui", "api", "fake"):
            with self.subTest(engine=engine):
                self.edit_run(generator={"name": engine, "model_id": "acme/Image-Model"})
                self.assertIsNone(self.row()["model_id"])

    def test_conflicting_active_models_are_not_arbitrarily_selected(self):
        other = self.add_run("4", "other", "acme/Other-Model")
        self.assertIsNone(self.row()["model_id"])
        other.unlink()
        self.assertEqual(self.row()["model"], "Registered image model")

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
             "model_name": "Registered reasoner",
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
        self.assertEqual(rows[0]["model"], "Registered reasoner")
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
