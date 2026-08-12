"""Platform-aware onboarding on non-CUDA hardware (Apple Silicon, CPU-only).

The core regression these guard against: a high-RAM Mac (tier 'large') must never
be steered into the vLLM-only Nemotron default it cannot serve. Like test_hwinfo,
we can't run on real macOS here, so the platform is injected via
`hwinfo.platform_id` and the decision logic is what's asserted.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ava_cli
from ava_bridge import models, settings, setup_wizard


def _manifest():
    return dict(ava_cli._DEFAULT_MODELS)


class EngineServableTests(unittest.TestCase):
    def test_vllm_only_servable_on_cuda_platforms(self):
        for plat, want in [("linux-nvidia", True), ("windows-nvidia", True),
                           ("darwin-apple", False), ("linux-cpu", False),
                           ("generic", False)]:
            with mock.patch("ava_bridge.hwinfo.platform_id", return_value=plat):
                self.assertEqual(ava_cli._engine_servable_here("vllm"), want, plat)

    def test_local_engines_servable_anywhere(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            for eng in ("ollama", "llamacpp", "mlx", "lmstudio", "openai"):
                self.assertTrue(ava_cli._engine_servable_here(eng), eng)


class ResolveAutoTests(unittest.TestCase):
    def test_nvidia_keeps_the_vllm_brain(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-nvidia"):
            role, spec, note = ava_cli._resolve_auto("large", _manifest())
        self.assertEqual(role, "chat")
        self.assertEqual(spec["engine"], "vllm")
        self.assertIsNone(note)

    def test_apple_large_substitutes_ollama_never_vllm(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, note = ava_cli._resolve_auto("large", _manifest())
        self.assertEqual(spec["engine"], "ollama")
        self.assertNotEqual(spec["engine"], "vllm")
        self.assertEqual(spec["id"], "llama3.1:70b")
        self.assertIn("can't be served", note)

    def test_apple_medium_substitutes_smaller_ollama(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, note = ava_cli._resolve_auto("medium", _manifest())
        self.assertEqual(spec["engine"], "ollama")
        self.assertEqual(spec["id"], "llama3.1:8b")

    def test_apple_small_uses_fast_role_no_note(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, note = ava_cli._resolve_auto("small", _manifest())
        self.assertEqual(role, "fast")
        self.assertEqual(spec["engine"], "ollama")
        self.assertIsNone(note)

    def test_cpu_only_linux_also_protected(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-cpu"):
            _role, spec, _note = ava_cli._resolve_auto("large", _manifest())
        self.assertEqual(spec["engine"], "ollama")

    def test_cloud_tier_yields_no_local_role(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            role, spec, _note = ava_cli._resolve_auto("cloud", _manifest())
        self.assertIsNone(role)
        self.assertIsNone(spec)


class InferenceReseedTests(unittest.TestCase):
    """Which boxes get their inference block rewritten away from the vLLM default.

    Gating this on the platform (the original rule) silently missed small NVIDIA
    GPUs: `_resolve_auto` downshifts them to the Ollama 'fast' model, so `models
    pull --auto` fetched that while ava.yaml still named the full-size vLLM
    default — one model on disk, a different dead endpoint configured.
    """

    def test_big_nvidia_keeps_the_shipped_default(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-nvidia"):
            spec, why = ava_cli._inference_reseed(_manifest(), "large")
        self.assertIsNone(spec)
        self.assertIsNone(why)

    def test_small_nvidia_is_reseeded_to_what_it_downloads(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="linux-nvidia"):
            spec, why = ava_cli._inference_reseed(_manifest(), "small")
        self.assertIsNotNone(spec)
        self.assertEqual(spec["engine"], "ollama")
        # the reason must name size, not servability — vLLM does run on this box
        self.assertIn("too large", why)

    def test_apple_reseeded_for_servability_not_size(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            spec, why = ava_cli._inference_reseed(_manifest(), "large")
        self.assertEqual(spec["engine"], "ollama")
        self.assertIn("won't run here", why)

    def test_cloud_tier_warns_instead_of_seeding(self):
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"):
            spec, why = ava_cli._inference_reseed(_manifest(), "cloud")
        self.assertIsNone(spec)
        self.assertIn("cloud backend", why)

    def test_undetectable_hardware_changes_nothing(self):
        spec, why = ava_cli._inference_reseed(_manifest(), None)
        self.assertIsNone(spec)
        self.assertIsNone(why)


class SeedInferenceBackendTests(unittest.TestCase):
    def test_rewrites_vllm_default_to_clean_ollama_block(self):
        seed = ("inference:\n  primary: local\n  backends:\n"
                "    local:\n      engine: vllm\n      model: nvidia/whatever\n")
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "ava.yaml"
            cfg.write_text(seed, encoding="utf-8")
            with mock.patch.object(settings, "CONFIG_PATH", cfg):
                ok = ava_cli._seed_inference_backend(
                    {"engine": "ollama", "id": "llama3.1:70b", "tier": "large"})
            self.assertTrue(ok)
            import yaml
            out = yaml.safe_load(cfg.read_text())
        self.assertEqual(out["inference"]["primary"], "local-ollama")
        # the dead vLLM backend must be gone, not just deprioritised. Asserted as
        # an exact key set: `local` is a prefix of `local-ollama`, so a bare
        # assertNotIn would read as a substring check it isn't.
        self.assertEqual(set(out["inference"]["backends"]), {"local-ollama"})
        b = out["inference"]["backends"]["local-ollama"]
        self.assertEqual(b["engine"], "ollama")
        self.assertEqual(b["model"], "llama3.1:70b")
        self.assertEqual(b["base_url"], "http://127.0.0.1:11434/v1")


class WizardHardwareNoteTests(unittest.TestCase):
    def test_apple_hardware_probe_flags_engine_and_telemetry(self):
        import types
        mem = types.SimpleNamespace(total_gb=256.0, free_gb=210.0,
                                    source="system-psutil", readable=True)
        with mock.patch("ava_bridge.hwinfo.platform_id", return_value="darwin-apple"), \
             mock.patch("ava_bridge.hwinfo.fit_memory", return_value=mem):
            out = setup_wizard.api_hardware()
        self.assertEqual(out["platform"], "darwin-apple")
        self.assertEqual(out["tier"], "large")
        self.assertIn("30B", out["hint"])         # a large-tier model is offered
        # The route reports the SITUATION; the sentence about unified memory and
        # why not vLLM is the frontend's, and differs between the first-run
        # wizard and Setup → Hardware on purpose (CLAUDE.md: backend returns
        # facts, owner-facing copy lives in the frontend).
        self.assertEqual(out["note_code"], "apple-silicon")


class DiskPrecheckTests(unittest.TestCase):
    """A full disk must fail BEFORE a multi-GB pull, not inside a vendor CLI.

    The GGUF path has read content-length and prechecked since it was written.
    HF and Ollama had no check at all, so a laptop with no room left ran
    `huggingface-cli download` and found out several minutes and one partial
    download later, in somebody else's error message.
    """

    def _free(self, nbytes):
        return mock.patch.object(ava_cli.shutil, "disk_usage",
                                 return_value=mock.Mock(free=nbytes))

    def test_a_full_disk_stops_the_pull(self):
        with self._free(100 << 20):                      # 100 MiB
            self.assertEqual(ava_cli._disk_gate("/models", "Ollama"), 1)

    def test_room_to_work_proceeds(self):
        with self._free(80 << 30):
            self.assertEqual(ava_cli._disk_gate("/models", "Ollama"), 0)

    def test_an_unmeasurable_path_never_blocks(self):
        """Refusing on a measurement we could not take would be the same
        inference error the drift ladder exists to avoid: not knowing is not the
        same as knowing there is no room."""
        with mock.patch.object(ava_cli.shutil, "disk_usage",
                               side_effect=OSError("no such device")):
            self.assertEqual(ava_cli._disk_gate("/models", "HF"), 0)

    def test_both_vendor_paths_are_gated(self):
        with self._free(100 << 20):
            self.assertEqual(ava_cli._pull_hf("org/model", "/models/hf"), 1)
            with mock.patch.object(ava_cli.shutil, "which", return_value="/usr/bin/ollama"), \
                 mock.patch.object(ava_cli.subprocess, "call") as call:
                self.assertEqual(ava_cli._pull_ollama("llama3.1:8b", "/models/ollama"), 1)
            call.assert_not_called()


class _Stream:
    """A `requests.get(stream=True)` context manager over fixed bytes.

    `declared` exists to model the case that matters: a server that announces a
    full-size body and then closes early. Deriving the header from the body it
    actually delivers would make every truncation look complete.
    """

    def __init__(self, body: bytes, *, content_length: bool = True,
                 declared: int | None = None):
        self.body = body
        self.headers = ({"content-length": str(declared if declared is not None
                                              else len(body))}
                        if content_length else {})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]


class DownloadIntegrityTests(unittest.TestCase):
    """The direct-URL GGUF path is the ONLY download with no registry, no
    manifest and no content-addressed store behind it. Ollama verifies every
    layer's digest against the registry manifest and deletes a blob that does not
    match; Hugging Face checks each file's size against the repo metadata and
    fetches a whole repo, so a per-file hash has nothing to name there. Here the
    strongest check was `done == total` against a Content-Length the same server
    supplied.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        self.target = str(self.dir / "model.gguf")
        self.body = b"weights" * 1000
        self.digest = hashlib.sha256(self.body).hexdigest()

    def _get(self, body=None, content_length=True, declared=None):
        return mock.patch("requests.get",
                          return_value=_Stream(body if body is not None else self.body,
                                               content_length=content_length,
                                               declared=declared))

    def test_a_matching_checksum_installs_the_file(self):
        with self._get():
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256=self.digest)
        self.assertEqual(rc, 0)
        self.assertEqual(Path(self.target).read_bytes(), self.body)

    def test_a_mismatch_installs_nothing_and_fails(self):
        """Substituted weights are the threat, and the file must never be
        promoted: after `os.replace` every later check is `os.path.isfile` and it
        reads 'present' forever."""
        wrong = "0" * 64
        with self._get():
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256=wrong)
        self.assertEqual(rc, 1)
        self.assertFalse(Path(self.target).exists(), "the bad file was installed")
        self.assertFalse(Path(self.target + ".part").exists(), "the .part was left behind")

    def test_a_malformed_digest_is_an_error_not_a_pass(self):
        """A truncated or mistyped hash must never compare equal to nothing."""
        with self._get() as get:
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256="deadbeef")
        self.assertEqual(rc, 1)
        get.assert_not_called()

    def test_a_hashless_spec_still_downloads(self):
        """No shipped default carries a hash; refusing would break every working
        install to enforce a field nobody has written yet."""
        with self._get():
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat")
        self.assertEqual(rc, 0)
        self.assertTrue(Path(self.target).exists())

    def test_a_chunked_response_with_a_hash_is_still_verified(self):
        """No Content-Length means the length check never ran — that was the one
        path that promoted a silently truncated file. A hash does not care."""
        with self._get(content_length=False):
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256=self.digest)
        self.assertEqual(rc, 0)
        with self._get(body=self.body[:-5], content_length=False):
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256=self.digest)
        self.assertEqual(rc, 1, "a truncated chunked download was installed")

    def test_a_truncated_download_is_diagnosed_as_truncation(self):
        """Both checks fail on a short stream and only one names the cause.
        Telling an owner their weights were substituted sends them hunting for a
        compromised mirror when the fix is to retry."""
        import contextlib
        import io
        buf = io.StringIO()
        with self._get(body=self.body[:20], declared=len(self.body)), \
             contextlib.redirect_stdout(buf):
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256=self.digest)
        self.assertEqual(rc, 1)
        out = buf.getvalue()
        self.assertIn("truncated", out.lower())
        self.assertNotIn("These are not the weights", out)

    def test_a_substituted_body_is_still_reported_as_a_mismatch(self):
        """The other half: a COMPLETE response whose bytes are wrong is exactly
        the case the checksum exists for, and must not be softened."""
        import contextlib
        import io
        other = b"substituted" * 700
        buf = io.StringIO()
        with self._get(body=other), contextlib.redirect_stdout(buf):
            rc = ava_cli._download_url("https://x/m.gguf", self.target, "chat",
                                       sha256=self.digest)
        self.assertEqual(rc, 1)
        self.assertIn("checksum mismatch", buf.getvalue())

    def test_the_error_names_a_config_key_that_exists(self):
        """`label` used to be the model id, so the message read
        `models.my-model.Q4_K_M.gguf.sha256` — a path an owner can grep their
        ava.yaml for and never find."""
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ava_cli._pull_gguf({"id": "my-model.Q4_K_M.gguf",
                                "url": "https://x/m.gguf", "sha256": "nope"},
                               str(self.dir), role="chat")
        self.assertIn("models.chat.sha256", buf.getvalue())

    def test_the_spec_field_reaches_the_downloader(self):
        seen = {}

        def fake(url, target, label, sha256=""):
            seen["sha256"] = sha256
            return 0

        with mock.patch.object(ava_cli, "_download_url", side_effect=fake):
            ava_cli._pull_gguf({"id": "m.gguf", "url": "https://x/m.gguf",
                                "sha256": "AB" * 32}, str(self.dir))
        self.assertEqual(seen["sha256"], "ab" * 32, "the declared hash was dropped "
                                                    "on the way, or not normalised")


class GgufPathAgreementTests(unittest.TestCase):
    """The downloader and the presence check must build the SAME path.

    Two rules existed: `gguf_present` joined `spec["id"]`, the downloader wrote
    `basename(id or url)`. A URL-only spec therefore checked `isfile(<dir>/)` —
    the directory — which is never a file, so the model read "missing" forever
    and every pull re-downloaded gigabytes it already had; and a slash-qualified
    id was written flat but looked for in a subdirectory, so a checksum re-check
    would hash a path nothing had written.
    """

    CASES = [
        {"engine": "llamacpp", "url": "https://h/Q5_K_M.gguf"},          # no id
        {"engine": "llamacpp", "id": "bartowski/Q4.gguf"},               # slashes
        {"engine": "llamacpp", "id": "plain.gguf"},
        {"engine": "llamacpp", "id": "plain.gguf", "url": "https://h/other.gguf"},
    ]

    def test_the_two_agree_on_every_spec_shape(self):
        for spec in self.CASES:
            self.assertEqual(ava_cli._gguf_target(spec, "/g"),
                             models.gguf_path(spec, "/g"), spec)

    def test_a_url_only_spec_can_actually_read_as_present(self):
        d = Path(tempfile.mkdtemp())
        spec = {"engine": "llamacpp", "url": "https://h/Q5_K_M.gguf"}
        self.assertFalse(models.gguf_present(spec, str(d)))
        (d / "Q5_K_M.gguf").write_bytes(b"x")
        self.assertTrue(models.gguf_present(spec, str(d)),
                        "a downloaded URL-only GGUF still reads as missing, so "
                        "every pull re-downloads it")


class ModelsVerifyHonestyTests(unittest.TestCase):
    """`ava models verify` is where a declared field is checked or admitted to be
    inert. Every branch here was a green `present` that let an owner believe a
    guarantee Ava does not provide."""

    def _verify(self, manifest, dirs):
        args = mock.Mock(action="verify")
        with mock.patch.object(ava_cli, "_models_manifest", return_value=manifest), \
             mock.patch.object(ava_cli, "_model_dirs", return_value=dirs), \
             mock.patch.object(ava_cli, "_model_present", return_value=True):
            return ava_cli.cmd_models(args)

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.dirs = {"root": str(self.d), "hf": str(self.d / "hf"),
                     "ollama": str(self.d / "ollama"), "gguf": str(self.d / "gguf")}
        (self.d / "gguf").mkdir(parents=True)

    def test_a_malformed_digest_is_a_config_error_not_corruption(self):
        """The old branch went straight to CHECKSUM MISMATCH, whose advice is to
        delete a good multi-gigabyte file and download it again."""
        (self.d / "gguf" / "m.gguf").write_bytes(b"x")
        rc = self._verify({"chat": {"engine": "llamacpp", "id": "m.gguf",
                                    "sha256": "deadbeef"}}, self.dirs)
        self.assertEqual(rc, 1)

    def test_a_checksum_on_an_engine_that_ignores_it_is_reported(self):
        """Nothing hashes an Ollama blob here. A green `present` would let the
        owner believe a checksum guards a model Ava never checks."""
        rc = self._verify({"fast": {"engine": "ollama", "id": "llama3.1:8b",
                                    "sha256": "a" * 64}}, self.dirs)
        self.assertEqual(rc, 1)

    def test_a_matching_digest_passes(self):
        body = b"weights"
        (self.d / "gguf" / "m.gguf").write_bytes(body)
        rc = self._verify({"chat": {"engine": "llamacpp", "id": "m.gguf",
                                    "sha256": hashlib.sha256(body).hexdigest()}},
                          self.dirs)
        self.assertEqual(rc, 0)

    def test_a_swapped_file_is_caught(self):
        (self.d / "gguf" / "m.gguf").write_bytes(b"substituted")
        rc = self._verify({"chat": {"engine": "llamacpp", "id": "m.gguf",
                                    "sha256": hashlib.sha256(b"weights").hexdigest()}},
                          self.dirs)
        self.assertEqual(rc, 1)

    def test_a_revision_that_never_reached_the_store_is_reported(self):
        """`_pull_one` returns early when a model is already present, so a
        revision added AFTER the first pull never reaches `hf download` and the
        snapshot on disk stays whatever the branch tip was that day."""
        rc = self._verify({"chat": {"engine": "vllm", "id": "org/model",
                                    "revision": "b" * 40}}, self.dirs)
        self.assertEqual(rc, 1)

    def test_a_revision_that_is_in_the_store_passes(self):
        rev = "b" * 40
        snap = self.d / "hf" / "hub" / "models--org--model" / "snapshots" / rev
        snap.mkdir(parents=True)
        rc = self._verify({"chat": {"engine": "vllm", "id": "org/model",
                                    "revision": rev}}, self.dirs)
        self.assertEqual(rc, 0)

    def test_a_plain_spec_still_passes(self):
        rc = self._verify({"chat": {"engine": "vllm", "id": "org/model"}}, self.dirs)
        self.assertEqual(rc, 0)


class HuggingFaceRevisionTests(unittest.TestCase):
    """`hf download <repo>` with no --revision takes the default branch tip, so
    one config can fetch different weights on different days and nothing reports
    it. A file checksum does not fit a whole-repo download; a commit does."""

    def test_a_revision_is_passed_to_the_cli(self):
        with mock.patch.object(ava_cli, "_disk_gate", return_value=0), \
             mock.patch.object(ava_cli.shutil, "which", return_value="/usr/bin/hf"), \
             mock.patch.object(ava_cli.subprocess, "call", return_value=0) as call:
            rc = ava_cli._pull_hf("org/model", "/models/hf", revision="abc123")
        self.assertEqual(rc, 0)
        argv = call.call_args[0][0]
        self.assertIn("--revision", argv)
        self.assertEqual(argv[argv.index("--revision") + 1], "abc123")

    def test_no_revision_still_works_and_says_so(self):
        with mock.patch.object(ava_cli, "_disk_gate", return_value=0), \
             mock.patch.object(ava_cli.shutil, "which", return_value="/usr/bin/hf"), \
             mock.patch.object(ava_cli.subprocess, "call", return_value=0) as call:
            rc = ava_cli._pull_hf("org/model", "/models/hf")
        self.assertEqual(rc, 0)
        self.assertNotIn("--revision", call.call_args[0][0])

    def test_a_revision_that_could_carry_an_argument_is_refused(self):
        with mock.patch.object(ava_cli, "_disk_gate", return_value=0), \
             mock.patch.object(ava_cli.subprocess, "call") as call:
            rc = ava_cli._pull_hf("org/model", "/models/hf",
                                  revision="main --local-dir /etc")
        self.assertEqual(rc, 1)
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
