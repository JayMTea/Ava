"""Deleting a model from the store removes the weights and reclaims the space.

The operation is irreversible and measured in tens of GB, so the tests are
weighted toward what must NEVER happen rather than toward the happy path:

  * nothing outside the store root is ever removed, however the id is spelled;
  * an Ollama tag is deleted through `ollama rm`, never by unlinking files —
    its store is content-addressed blobs SHARED between tags, so removing a
    manifest's blobs corrupts every other model that shares a layer;
  * a model something is serving is refused, with the reason, because deleting
    weights out from under a running engine does not fail loudly — the process
    keeps its file handles and the failure surfaces on the next restart with
    nothing connecting it to the deletion.
"""
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from ava_bridge import models


def _store(tmp: str) -> dict:
    d = {"root": tmp, "hf": os.path.join(tmp, "hf"),
         "ollama": os.path.join(tmp, "ollama"), "gguf": os.path.join(tmp, "gguf")}
    for k in ("hf", "ollama", "gguf"):
        os.makedirs(d[k], exist_ok=True)
    return d


def _hf(d: dict, repo_id: str, mb: int = 2) -> str:
    """Plant a HuggingFace-cache-shaped model, blobs and all."""
    safe = "models--" + repo_id.replace("/", "--")
    root = os.path.join(d["hf"], "hub", safe)
    blobs, snap = os.path.join(root, "blobs"), os.path.join(root, "snapshots", "abc")
    os.makedirs(blobs, exist_ok=True)
    os.makedirs(snap, exist_ok=True)
    blob = os.path.join(blobs, "deadbeef")
    with open(blob, "wb") as f:
        f.write(b"\0" * (mb * 1024 * 1024))
    # Real caches symlink the snapshot at the blob; the size must not double.
    os.symlink(blob, os.path.join(snap, "model.safetensors"))
    os.makedirs(os.path.join(d["hf"], "hub", ".locks", safe), exist_ok=True)
    return root


class TheScanSeesWhatIsActuallyThere(unittest.TestCase):
    def test_it_finds_a_model_nothing_declared(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            _hf(d, "org/Some-7B")
            with mock.patch.object(models.shutil, "which", lambda _n: None):
                found = models.installed_models(d)
            self.assertEqual([(m["engine"], m["id"]) for m in found],
                             [("vllm", "org/Some-7B")])

    def test_a_symlinked_snapshot_is_not_counted_twice(self):
        # A HF snapshot is symlinks into blobs/. Following them reports a 14 GB
        # model as 28 GB, which would make every "reclaim" figure a lie.
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            _hf(d, "org/Some-7B", mb=8)
            with mock.patch.object(models.shutil, "which", lambda _n: None):
                size = models.installed_models(d)[0]["size_gb"]
            self.assertLess(size, 0.012, "the blob was counted through its symlink")

    def test_voice_assets_in_the_store_root_are_not_models(self):
        # models/ also holds ecapa/ and en_US-*.onnx. Listing them as models
        # makes them deletable BY a model manager.
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            os.makedirs(os.path.join(tmp, "ecapa"), exist_ok=True)
            pathlib.Path(tmp, "en_US-amy-medium.onnx").write_bytes(b"x" * 1024)
            with mock.patch.object(models.shutil, "which", lambda _n: None):
                found = models.installed_models(d)
            self.assertEqual(found, [])

    def test_a_gguf_sidecar_is_not_a_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            pathlib.Path(d["gguf"], "model.gguf").write_bytes(b"x" * 2048)
            pathlib.Path(d["gguf"], "model.gguf.sha256").write_text("abc")
            pathlib.Path(d["gguf"], "README.md").write_text("hi")
            with mock.patch.object(models.shutil, "which", lambda _n: None):
                found = models.installed_models(d)
            self.assertEqual([m["id"] for m in found], ["model.gguf"])


class DeletingReclaimsTheSpace(unittest.TestCase):
    def test_the_weights_and_the_lock_dir_both_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            root = _hf(d, "org/Some-7B")
            out = models.delete_model("vllm", "org/Some-7B", d)
            self.assertTrue(out["removed"])
            self.assertFalse(os.path.exists(root))
            self.assertFalse(os.path.exists(
                os.path.join(d["hf"], "hub", ".locks", "models--org--Some-7B")))

    def test_it_reports_what_it_freed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            _hf(d, "org/Some-7B", mb=16)
            out = models.delete_model("vllm", "org/Some-7B", d)
            self.assertGreater(out["freed_gb"], 0.01)

    def test_a_gguf_file_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            p = pathlib.Path(d["gguf"], "model.gguf")
            p.write_bytes(b"x" * (4 * 1024 * 1024))
            out = models.delete_model("gguf", "model.gguf", d)
            self.assertTrue(out["removed"])
            self.assertFalse(p.exists())

    def test_deleting_something_absent_is_not_an_error(self):
        # Idempotent: the caller may be retrying, or two tabs may both have hit
        # delete. "It is not there" is the desired state, not a failure.
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            out = models.delete_model("vllm", "org/Never-Existed", d)
            self.assertFalse(out["removed"])
            self.assertEqual(out["freed_gb"], 0.0)


class NothingOutsideTheStoreIsEverTouched(unittest.TestCase):
    def test_a_traversing_id_cannot_escape_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            outside = pathlib.Path(tmp, "PRECIOUS")
            outside.mkdir()
            (outside / "keep.txt").write_text("do not delete me")
            # `models--..--..--PRECIOUS` resolves out of the hf/ tree.
            for evil in ("../../PRECIOUS", "..%2f..%2fPRECIOUS", "/etc"):
                with self.subTest(id=evil):
                    try:
                        models.delete_model("vllm", evil, d)
                    except models.ModelDeleteError:
                        pass
                    self.assertTrue((outside / "keep.txt").exists(),
                                    f"'{evil}' reached outside the store")

    def test_a_symlinked_model_dir_pointing_out_is_refused(self):
        # The containment check resolves symlinks for exactly this: a name that
        # looks like a cache entry but IS a link to somewhere else.
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            outside = pathlib.Path(tmp, "PRECIOUS")
            outside.mkdir()
            (outside / "keep.txt").write_text("do not delete me")
            os.makedirs(os.path.join(d["hf"], "hub"), exist_ok=True)
            os.symlink(outside, os.path.join(d["hf"], "hub", "models--org--Evil"))
            with self.assertRaises(models.ModelDeleteError):
                models.delete_model("vllm", "org/Evil", d)
            self.assertTrue((outside / "keep.txt").exists())

    def test_an_unknown_engine_is_refused_rather_than_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(models.ModelDeleteError):
                models.delete_model("sorcery", "org/Some-7B", _store(tmp))


class OllamaIsDeletedThroughOllama(unittest.TestCase):
    """Never by unlinking files: its blobs are shared BETWEEN tags, so removing
    one manifest's blobs corrupts every other model sharing a layer."""

    def test_it_shells_out_to_ollama_rm(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = _store(tmp)
            seen = {}

            class _CP:
                returncode, stdout, stderr = 0, "deleted", ""

            def fake_run(cmd, **kw):
                seen["cmd"] = cmd
                seen["models_dir"] = (kw.get("env") or {}).get("OLLAMA_MODELS")
                return _CP()

            with mock.patch.object(models.shutil, "which", lambda _n: "/usr/bin/ollama"), \
                 mock.patch.object(models.subprocess, "run", fake_run):
                out = models.delete_model("ollama", "llama3.1:8b", d)
            self.assertTrue(out["removed"])
            self.assertEqual(seen["cmd"], ["ollama", "rm", "llama3.1:8b"])
            self.assertEqual(seen["models_dir"], d["ollama"],
                             "must delete from Ava's store, not the user's default")

    def test_a_missing_tag_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            class _CP:
                returncode, stdout, stderr = 1, "", "Error: model 'x' not found"
            with mock.patch.object(models.shutil, "which", lambda _n: "/usr/bin/ollama"), \
                 mock.patch.object(models.subprocess, "run", lambda *a, **k: _CP()):
                out = models.delete_model("ollama", "x", _store(tmp))
            self.assertFalse(out["removed"])

    def test_a_real_failure_is_raised_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            class _CP:
                returncode, stdout, stderr = 1, "", "Error: daemon unreachable"
            with mock.patch.object(models.shutil, "which", lambda _n: "/usr/bin/ollama"), \
                 mock.patch.object(models.subprocess, "run", lambda *a, **k: _CP()):
                with self.assertRaises(models.ModelDeleteError):
                    models.delete_model("ollama", "llama3.1:8b", _store(tmp))

    def test_no_ollama_binary_is_refused_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(models.shutil, "which", lambda _n: None):
                with self.assertRaises(models.ModelDeleteError):
                    models.delete_model("ollama", "llama3.1:8b", _store(tmp))


if __name__ == "__main__":
    unittest.main()
