"""A fresh clone names observed models without Home Lab or any live service."""
import json
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from ava_bridge import gpu_inventory, hardware, hwexporters


@pytest.fixture
def process(tmp_path):
    proc = tmp_path / "proc"
    folder = proc / "42"
    (folder / "root").mkdir(parents=True)
    (folder / "cmdline").write_bytes(b"python3\0worker.py\0")
    (folder / "stat").write_text("42 (python3) S 1 0 0")
    return proc, folder


def observe(process, args=None, mapped=None, local=False):
    proc, folder = process
    if args is not None:
        (folder / "cmdline").write_bytes("\0".join(args).encode())
    if mapped is not None:
        (folder / "maps").write_text("".join(f"0-1 r--p 0 0 0 {p}\n" for p in mapped))
    output = "42, python3, 2048\n"
    # No optional registry is accessed, even when matching newly added models.
    with mock.patch.object(gpu_inventory.sqlite3, "connect", side_effect=AssertionError("No Home Lab")):
        if local:
            command = shlex.join((folder / "cmdline").read_text().split("\0"))
            with (
                mock.patch.object(hardware.shutil, "which", return_value="nvidia-smi"),
                mock.patch.object(hardware, "_run", return_value=output),
                mock.patch.object(hardware, "_gpu_process_util", return_value={}),
                mock.patch.object(hardware, "_proc_cmdline", return_value=command),
                mock.patch.object(hardware, "_proc_ppid", return_value=1),
            ):
                return hardware._gpu_model_processes(proc)[0]
        rows = gpu_inventory.collect(proc, lambda *a, **kw: SimpleNamespace(stdout=output))
        metrics = gpu_inventory.render(rows, time.time())
        assert "PRIVATE" not in metrics and str(proc) not in metrics
        return hwexporters.model_inventory(hwexporters.Reading(
            node=hwexporters.parse_metrics(metrics)))["rows"][0]


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("args,mid,name", [
    (["vllm", "serve", "publisher/Future-Reasoner"], "publisher/Future-Reasoner", "Future-Reasoner"),
    (["vllm", "serve", "publisher/Model"], "publisher/Model", "Model"),
    (["python3", "server.py", "--model-id=publisher/Future-Embedder"],
     "publisher/Future-Embedder", "Future-Embedder"),
    (["llama-server", "-m", "/weights/My Reasoner-Q4.gguf"], "My Reasoner-Q4.gguf", "My Reasoner-Q4"),
    (["vllm", "serve", "/cache/models--publisher--Future-Chat/snapshots/" + "ab" * 20],
     "publisher/Future-Chat", "Future-Chat"),
])
def test_launch_identity_on_fresh_local_and_remote_installs(process, local, args, mid, name):
    row = observe(process, args, local=local)
    assert (row["model_id"], row["model"]) == (mid, name)
    assert row["memory_gb"] == 2


@pytest.mark.parametrize("local", [False, True])
def test_bare_python_mapped_cache_shards_get_one_name_without_a_registry(process, local):
    root = "/cache/models--publisher--Image-Next/snapshots/" + "ab" * 20
    row = observe(process, mapped=[f"{root}/model-00001-of-00002.safetensors",
                                   f"{root}/model-00002-of-00002.safetensors"], local=local)
    assert row["model_id"] == "publisher/Image-Next"
    assert row["model"] == "Image-Next"
    assert len(row["components"]) == 1
    if local:
        assert row["components"][0]["path"].startswith(root)
    row = observe(process, mapped=[f"{root}/model.safetensors", "/weights/encoder.onnx"], local=local)
    assert not row["model_id"]  # Another unidentified component prevents attributing all memory.
    row = observe(process, mapped=[f"{root}/model.safetensors", "/weights/" + "cd" * 20 + ".safetensors"],
                  local=local)
    assert not row["model_id"]
    assert all("cd" * 20 not in c["name"] for c in row["components"])


def write_manifest(folder, name, digest, media="application/vnd.ollama.image.model"):
    path = folder / "root/store/manifests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"layers": [{"mediaType": media, "digest": digest}]}))
    return path


@pytest.mark.parametrize("local", [False, True])
def test_ollama_blob_resolves_new_pulls_and_renames_without_a_daemon(process, local):
    _, folder = process
    digest = "sha256:" + "ab" * 32
    args = ["ollama", "runner", "--model", "/store/blobs/" + digest.replace(":", "-")]
    assert not observe(process, args, local=local)["model_id"]
    manifest = write_manifest(folder, "registry.ollama.ai/library/new-chat/8b", digest)
    assert observe(process, local=local)["model"] == "new-chat:8b"
    renamed = write_manifest(folder, "registry.ollama.ai/team/custom-chat/q4", digest)
    assert not observe(process, local=local)["model_id"]  # Shared weights cannot identify a tag.
    manifest.unlink()
    assert observe(process, local=local)["model_id"] == "team/custom-chat:q4"
    renamed.unlink()
    write_manifest(folder, "registry.ollama.ai/library/other/latest", digest,
                   media="application/vnd.ollama.image.adapter")
    assert not observe(process, local=local)["model_id"]


@pytest.mark.parametrize("local", [False, True])
def test_portable_sidecar_names_opaque_custom_weights_and_refreshes(process, local):
    _, folder = process
    path = folder / "root/weights" / ("ab" * 20 + ".gguf")
    path.parent.mkdir(parents=True)
    sidecar = path.with_name(path.name + ".ava-model.json")
    args = ["llama-server", "--model", "/weights/" + path.name]
    assert not observe(process, args, local=local)["model_id"]
    sidecar.write_text(json.dumps({"id": "team/Custom-Chat", "name": "Custom chat Q4", "key": "PRIVATE"}))
    row = observe(process, local=local)
    assert (row["model_id"], row["model"]) == ("team/Custom-Chat", "Custom chat Q4")
    sidecar.write_text(json.dumps({"id": "team/Custom-Chat", "name": "Renamed chat"}))
    assert observe(process, local=local)["model"] == "Renamed chat"
    sidecar.write_text("x" * 16385)
    assert not observe(process, local=local)["model_id"]


@pytest.mark.parametrize("local", [False, True])
def test_directory_sidecar_names_generic_mapped_files(process, local):
    _, folder = process
    directory = folder / "root/weights/custom"
    directory.mkdir(parents=True)
    (directory / "ava-model.json").write_text(json.dumps({"id": "team/Custom", "name": "My fine-tune"}))
    row = observe(process, mapped=["/weights/custom/model.safetensors"], local=local)
    assert row["model"] == "My fine-tune"


@pytest.mark.parametrize("name", ["python3", "12345", "ab" * 20, "/PRIVATE/model", "bad\nname"])
def test_bad_sidecar_does_not_turn_a_runtime_or_hash_into_a_model(process, name):
    _, folder = process
    directory = folder / "root/weights/12345"
    directory.mkdir(parents=True)
    (directory / "ava-model.json").write_text(json.dumps({"id": name, "name": name}))
    row = observe(process, ["vllm", "serve", "/weights/12345"])
    assert not row["model_id"]


def test_container_metadata_never_falls_back_to_another_hosts_path(process, tmp_path):
    host_model = tmp_path / "12345"
    host_model.mkdir()
    (host_model / "ava-model.json").write_text(json.dumps({"id": "wrong/Model", "name": "Wrong host"}))
    assert gpu_inventory.resolve_model(host_model.as_posix(), process[0], 42) == ("", "")


def test_proc_arguments_preserve_spaces_before_model_parsing():
    raw = b"llama-server\0--model\0/weights/My Model.gguf\0--port\08080\0"
    with mock.patch("builtins.open", mock.mock_open(read_data=raw)):
        command = hardware._proc_cmdline(42)
    assert hardware._extract_model_names(command) == ["/weights/My Model.gguf"]


def test_relative_model_path_uses_the_process_working_directory(process):
    proc, folder = process
    directory = folder / "cwd" / "12345"
    directory.mkdir(parents=True)
    (directory / "ava-model.json").write_text(json.dumps({"name": "My local model"}))
    assert gpu_inventory.resolve_model("12345", proc, 42) == ("My local model", "My local model")


def test_collector_is_standalone_without_ava_or_home_lab_imports(process):
    proc, folder = process
    (folder / "cmdline").write_bytes(b"vllm\0serve\0publisher/New-Model\0")
    script = (
        "import json, runpy, sys; from pathlib import Path; from types import SimpleNamespace; "
        "m = runpy.run_path(sys.argv[1]); "
        "rows = m['collect'](Path(sys.argv[2]), "
        "lambda *a, **kw: SimpleNamespace(stdout='42, python3, 2048\\n')); "
        "print(json.dumps(rows))"
    )
    result = subprocess.run([sys.executable, "-I", "-c", script, gpu_inventory.__file__, str(proc)],
                            check=True, capture_output=True, text=True, timeout=10)
    assert json.loads(result.stdout)[0]["model_name"] == "New-Model"


@pytest.mark.parametrize("value", ["python3", "12345", "ab" * 20, "model.safetensors",
                                    "model-00001-of-00003.safetensors"])
def test_local_short_names_cannot_regress_to_processes_hashes_or_shards(value):
    assert hardware._short_model_name(value) == "Model"
