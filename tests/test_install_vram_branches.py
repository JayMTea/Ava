"""Run deploy/install.sh for real against a fake GPU, and check which profile it picks.

The defect this file exists for: the VRAM gate had two tiers, both derived from
what vLLM needs to serve FP16 (14.2 GiB of weights plus a 32k KV cache), and
anything under the lower one fell to `cpu`. But `cpu` runs OLLAMA, which serves
quantized GGUF — llama3.2 at Q4_K_M is ~2 GB of weights. So a 6 GB card that
holds the model comfortably was declared "too little to serve a useful model"
and handed CPU-only inference several times slower, by a rule that described a
different engine. The owner's HP ZBook (RTX A1000 6 GB, NVIDIA runtime present
and working) was the report.

Static scans cannot catch this: every string was correct, the arithmetic in the
comment was correct, and the wrong answer came from applying it to the wrong
engine. So this asserts on the DECISION, by running the script.

Nothing here touches the real deploy/.env. install.sh is copied into a throwaway
git repo — `git rev-parse --show-toplevel` is how it decides it is inside a
clone — and given stub `nvidia-smi` and `docker` on PATH. The stub nvidia-smi is
the same trick .github/workflows/ci.yml uses to test the gpu path on a runner
with no GPU: inject the one fact the rule consumes, and test the rule off the
machine it describes.

Run: .venv/bin/python -m pytest tests/test_install_vram_branches.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Enough nvidia-smi for install.sh: the name and total memory of each card, in
# the two-column CSV the installer asks for. Emitting BOTH columns matters — the
# installer parses one query for both values so they describe the same card, and
# a stub that answered only the number would leave that parsing untested.
_SMI = """#!/bin/sh
case "$*" in
  *memory.total*)
    printf '%s, %s%s\\n' "${FAKE_GPU_NAME:-Fake GPU}" "${FAKE_GPU_MIB:-0}" \\
           "${FAKE_GPU_CRLF:+$(printf '\\r')}" ;;
  *) echo "stub nvidia-smi" ;;
esac
exit 0
"""

# Enough docker for install.sh's pre-flight: a compose v2 that validates, and an
# `info` whose runtime list is switchable, because "is an nvidia runtime
# registered" is now what picks between the cuda profile and cpu.
_DOCKER = """#!/bin/sh
case "$1" in
  compose) exit 0 ;;
  info)
    if [ "${FAKE_DOCKER_NVIDIA:-0}" = "1" ]; then
      printf '{"nvidia":{"path":"nvidia-container-runtime"},"runc":{"path":"runc"}}\\n'
    else
      printf '{"runc":{"path":"runc"}}\\n'
    fi
    exit 0 ;;
esac
exit 0
"""


def _requirements_met() -> bool:
    return all(shutil.which(b) for b in ("bash", "git", "awk", "grep"))


@unittest.skipUnless(_requirements_met(), "needs bash, git, awk, grep")
class VramBranchTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-install-vram-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # install.sh installs "in place" when it is inside a git checkout, which
        # is what keeps this off the real repo and out of the real deploy/.env.
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True,
                       capture_output=True)
        shutil.copytree(ROOT / "deploy", self.tmp / "deploy")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("nvidia-smi", _SMI), ("docker", _DOCKER)):
            p = self.bin / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

    def _install(self, vram_mib: int | str, *, nvidia_runtime: bool,
                 gpu_name: str = "NVIDIA RTX A1000 6GB Laptop GPU",
                 **extra: str) -> dict[str, str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_GPU_MIB": str(vram_mib),
            "FAKE_GPU_NAME": gpu_name,
            "FAKE_DOCKER_NVIDIA": "1" if nvidia_runtime else "0",
            "AVA_INSTALL_DRY_RUN": "1",
            # Pinned so the decision under test is the SIZING one, not detection:
            # the copied tree carries no ava_bridge, so the HAL cannot answer here
            # and the shell fallback would otherwise vary with the test machine.
            "AVA_PROFILE": "gpu",
            # Same reason CI sets it: makes the VRAM number actually get consumed
            # instead of skipped as it is on unified memory.
            "AVA_GPU_MEMORY_MODEL": "discrete",
            "AVA_NO_BROWSER": "1",
        }
        for k in ("AVA_MODEL", "AVA_SKIP_GPU_RUNTIME_CHECK", "AVA_HOME", "AVA_DIR"):
            env.pop(k, None)
        env.update(extra)
        p = subprocess.run(["bash", str(self.tmp / "deploy" / "install.sh")],
                           capture_output=True, text=True, timeout=180,
                           cwd=str(self.tmp / "deploy"), env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.out = p.stdout + p.stderr
        return self._env_file()

    def _env_file(self) -> dict[str, str]:
        """The MANAGED block only — hand-written lines are not this script's answer."""
        text = (self.tmp / "deploy" / ".env").read_text(encoding="utf-8")
        body = text.split(">>> ava managed", 1)[1].split("<<< ava managed", 1)[0]
        out: dict[str, str] = {}
        for line in body.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k] = v
        return out

    # --- the reported defect ------------------------------------------------ #

    def test_a_six_gig_card_gets_the_gpu_not_the_cpu_profile(self):
        env = self._install(6144, nvidia_runtime=True)
        self.assertEqual(env.get("COMPOSE_PROFILES"), "cuda", self.out)
        self.assertEqual(env.get("AVA_BACKEND_URL"), "http://ollama-cuda:11434/v1")
        self.assertEqual(env.get("AVA_BACKEND_ENGINE"), "ollama")
        self.assertNotIn("AVA_VLLM_MODEL_FLAGS", env,
                         "vLLM flags in a .env that serves on Ollama are dead "
                         "config that takes effect the moment someone flips "
                         "COMPOSE_PROFILES by hand")

    def test_the_card_is_only_used_if_docker_can_actually_reach_it(self):
        """Same card, no container toolkit: the cuda service could not start."""
        env = self._install(6144, nvidia_runtime=False)
        self.assertEqual(env.get("COMPOSE_PROFILES"), "cpu", self.out)
        self.assertIn("Container Toolkit", self.out,
                      "falling back is right, but silently is not — the owner "
                      "has a working GPU and one install away from using it")

    def test_the_runtime_override_is_honoured(self):
        env = self._install(6144, nvidia_runtime=False,
                            AVA_SKIP_GPU_RUNTIME_CHECK="1")
        self.assertEqual(env.get("COMPOSE_PROFILES"), "cuda", self.out)

    # --- the floors above and below ----------------------------------------- #

    def test_a_card_too_small_even_for_a_quantized_model_still_falls_to_cpu(self):
        env = self._install(2048, nvidia_runtime=True, gpu_name="NVIDIA GeForce GT 1030")
        self.assertEqual(env.get("COMPOSE_PROFILES"), "cpu", self.out)

    def test_a_big_card_still_gets_vllm_with_the_shipped_model(self):
        """vLLM's own thresholds are unchanged — this is the guard on that."""
        env = self._install(24564, nvidia_runtime=True, gpu_name="NVIDIA RTX A5000")
        self.assertEqual(env.get("COMPOSE_PROFILES"), "gpu", self.out)
        self.assertEqual(env.get("AVA_BACKEND_ENGINE"), "vllm")
        self.assertTrue(env.get("AVA_VLLM_MODEL_FLAGS"),
                        "a vLLM .env with no resolved flags falls back to "
                        "compose's hardcoded parser, which returns no tool calls "
                        "at all on a model that does not speak it")

    def test_a_mid_sized_card_keeps_vllm_and_downshifts_the_model(self):
        env = self._install(16376, nvidia_runtime=True, gpu_name="NVIDIA GeForce RTX 4080")
        self.assertEqual(env.get("COMPOSE_PROFILES"), "gpu", self.out)
        self.assertEqual(env.get("AVA_MODEL"), "Qwen/Qwen2.5-3B-Instruct", self.out)

    # --- the measurement must survive into the container -------------------- #

    def test_the_measured_card_is_declared_in_the_env_file(self):
        env = self._install(6144, nvidia_runtime=True)
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_NAME"),
                         "NVIDIA RTX A1000 6GB Laptop GPU", self.out)
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_VRAM_MIB"), "6144")

    def test_the_declaration_survives_the_fallback_that_ignores_it(self):
        """Falling back to cpu does not make the card stop existing.

        This is the case the reading was being dropped in: the profile decision
        goes one way, and the owner is left with a UI that reports no GPU at all
        on a machine that has one.
        """
        env = self._install(6144, nvidia_runtime=False)
        self.assertEqual(env.get("COMPOSE_PROFILES"), "cpu")
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_VRAM_MIB"), "6144", self.out)
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_NAME"),
                         "NVIDIA RTX A1000 6GB Laptop GPU")

    def test_windows_line_endings_do_not_leak_into_the_declaration(self):
        """install.cmd runs this under Git Bash, where nvidia-smi is a .exe.

        Every line it prints ends CRLF there, and the values below are written
        into deploy/.env verbatim — a stray \\r lands mid-file in a value compose
        hands straight to the container.
        """
        env = self._install(6144, nvidia_runtime=True, FAKE_GPU_CRLF="1")
        self.assertEqual(env.get("COMPOSE_PROFILES"), "cuda", self.out)
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_VRAM_MIB"), "6144")
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_NAME"),
                         "NVIDIA RTX A1000 6GB Laptop GPU")
        self.assertNotIn("\r", (self.tmp / "deploy" / ".env").read_text(
            encoding="utf-8"))

    def test_a_card_that_reports_no_total_declares_a_name_and_no_number(self):
        """Unified memory answers N/A by design; a 0 there would be a lie."""
        env = self._install("N/A", nvidia_runtime=True, gpu_name="NVIDIA GB10")
        self.assertEqual(env.get("AVA_DECLARED_HOST_GPU_NAME"), "NVIDIA GB10", self.out)
        self.assertNotIn("AVA_DECLARED_HOST_GPU_VRAM_MIB", env,
                         "an unreadable total must be absent, not zero — zero is "
                         "a claim that the card has no memory")


if __name__ == "__main__":
    unittest.main()
