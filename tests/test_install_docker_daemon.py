"""A stopped Docker must be reported as a stopped Docker, and stop the install.

The report, verbatim, from a fresh `git clone` on a Windows laptop with a
working RTX A1000 6GB — the exact card deploy/profiles/cuda.env was written for:

    Warning: Detected the profile with shell probes (the HAL was unavailable): gpu
    ==> Detected GPU memory: 6144 MiB (NVIDIA RTX A1000 6GB Laptop GPU)
    Warning: 6144 MiB would serve a quantized model on the GPU (the cuda profile),
    Warning: but Docker has no 'nvidia' runtime registered, so no container here can
    Warning: reach the card. That is the NVIDIA Container Toolkit, installed separately
    ...
    ==> Hardware profile: cpu
    ==> Wrote deploy/.env (profile: cpu, model: <yours to set>)
    ==> Building & starting Ava (profile: cpu) — first run downloads images/models
    unable to get image 'ava/bridge:latest': failed to connect to the docker API
    at npipe:////./pipe/dockerDesktopLinuxEngine ... the daemon is running

Docker Desktop was not running. Nothing else was wrong with that machine.

Three separate failures, one cause. `command -v docker` and `docker compose
version` are both answered by the CLIENT, so the preflight passed with no engine
at all. The runtime probe then ran `docker info ... | grep -q nvidia`, in which
an unreachable daemon and a daemon without the NVIDIA runtime produce the same
empty output — so the owner was told to go install the NVIDIA Container Toolkit,
from a Linux package-manager URL, on Windows. That false "no" downgraded the
profile from cuda to cpu and wrote it to disk. Only then, a minute in, did the
install die on the actual problem, in Docker's own words rather than Ava's.

So this asserts on the DECISION, by running the script with a docker stub whose
`info` fails the way a stopped Docker Desktop fails. Same harness idea as
tests/test_install_vram_branches.py: a throwaway git repo (install.sh installs
"in place" when it is inside a checkout, which keeps this off the real
deploy/.env) plus stub binaries on PATH.

Run: .venv/bin/python -m pytest tests/test_install_docker_daemon.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The card from the report. install.sh must get far enough to want it before the
# daemon check matters — a stub that reported no GPU would pass this file while
# the misdiagnosis it exists for went on happening.
_SMI = """#!/bin/sh
case "$*" in
  *memory.total*) echo "NVIDIA RTX A1000 6GB Laptop GPU, 6144" ;;
  *) echo "stub nvidia-smi" ;;
esac
exit 0
"""

# A docker whose CLIENT works and whose DAEMON does not — the state the whole
# file is about, and the one the old preflight could not see. `compose version`
# and `compose config` answer from the client, exactly as the real thing does.
_DOCKER = """#!/bin/sh
case "$1" in
  compose) exit 0 ;;
  info)
    case "${FAKE_DOCKER_STATE:-down}" in
      up)
        printf '{"runc":{"path":"runc"}}\\n' ; exit 0 ;;
      denied)
        echo "permission denied while trying to connect to the Docker daemon" \\
             "socket at unix:///var/run/docker.sock" >&2 ; exit 1 ;;
      *)
        echo "ERROR: error during connect: Get" \\
             "\\"http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.51/info\\":" \\
             "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the" \\
             "file specified." >&2
        echo "errors pretty printing info" >&2
        exit 1 ;;
    esac ;;
esac
exit 0
"""


# The report came from Git Bash on Windows, where `sudo systemctl start docker`
# is not merely unhelpful but unrunnable — so the platform branch is worth a stub
# of its own. `-m` is answered too: install.sh pairs it with `-s` to recognise
# Apple Silicon, and a stub that dropped it would send this down that path.
_UNAME = """#!/bin/sh
case "$1" in
  -m) echo "x86_64" ;;
  *)  echo "${FAKE_UNAME_S:-Linux}" ;;
esac
exit 0
"""


def _requirements_met() -> bool:
    return all(shutil.which(b) for b in ("bash", "git", "awk", "grep"))


@unittest.skipUnless(_requirements_met(), "needs bash, git, awk, grep")
class DockerDaemonPreflightTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-install-daemon-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True,
                       capture_output=True)
        shutil.copytree(ROOT / "deploy", self.tmp / "deploy")
        # The maintainer's own deploy/.env rides along in that copy. Remove it so
        # "did the installer write a profile?" is a question this test can ask,
        # and so a real AVA_PASSWORD never lands in a temp directory.
        self.env_file = self.tmp / "deploy" / ".env"
        self.env_file.unlink(missing_ok=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in (("nvidia-smi", _SMI), ("docker", _DOCKER),
                           ("uname", _UNAME)):
            p = self.bin / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

    def _install(self, state: str, uname_s: str = "Linux"
                 ) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_DOCKER_STATE": state,
            # Stubbed for EVERY case here, not just the Windows one. On an Apple
            # Silicon Mac install.sh takes the bare-metal path, which skips the
            # Docker preflight entirely — so on the maintainer's own laptop this
            # whole file would otherwise test nothing and say so by failing.
            "FAKE_UNAME_S": uname_s,
            "AVA_INSTALL_DRY_RUN": "1",
            "AVA_GPU_MEMORY_MODEL": "discrete",
            "AVA_NO_BROWSER": "1",
        }
        for k in ("AVA_MODEL", "AVA_PROFILE", "AVA_SKIP_GPU_RUNTIME_CHECK",
                  "AVA_HOME", "AVA_DIR"):
            env.pop(k, None)
        p = subprocess.run(["bash", str(self.tmp / "deploy" / "install.sh")],
                           capture_output=True, text=True, timeout=180,
                           cwd=str(self.tmp / "deploy"), env=env)
        self.out = p.stdout + p.stderr
        return p

    # --- the reported defect ------------------------------------------------ #

    def test_a_stopped_daemon_stops_the_install(self) -> None:
        p = self._install("down")
        self.assertNotEqual(p.returncode, 0,
                            "an install that cannot build or start anything must "
                            "not exit 0\n" + self.out)
        self.assertIn("daemon", self.out.lower(), self.out)

    def test_it_fails_before_it_starts_guessing_at_hardware(self) -> None:
        """The minute between the wrong diagnosis and the real error.

        Every line the old run printed after the preflight was produced by a
        machine it had already lost the ability to ask anything.
        """
        self.assertNotIn("Hardware profile:", self._install("down").stdout,
                         "the installer got as far as choosing a profile with no "
                         "daemon to run it")

    def test_a_stopped_daemon_is_never_reported_as_a_missing_toolkit(self) -> None:
        """The false diagnosis, which is the expensive half of this bug.

        A stopped Docker Desktop is a click to fix. The NVIDIA Container Toolkit
        is a package install the owner did not need, from a URL that does not
        apply to their platform, and following it correctly would have changed
        nothing at all.
        """
        out = self._install("down")
        self.assertNotIn("Container Toolkit", self.out, self.out)
        self.assertNotIn("nvidia", self.out.lower(), self.out)
        self.assertNotIn("AVA_SKIP_GPU_RUNTIME_CHECK", out.stdout + out.stderr,
                         "offering the GPU-probe override for a problem that is "
                         "not the GPU probe sends the owner further away still")

    def test_it_does_not_write_a_profile_it_could_not_choose(self) -> None:
        """The cpu profile in that transcript outlived the run that wrote it.

        install.sh backs up and rewrites deploy/.env each run, so a re-run does
        recover — but the owner has no reason to re-run, having been handed a
        different task entirely. Writing nothing leaves nothing to be wrong.
        """
        self._install("down")
        wrote = (self.env_file.read_text(encoding="utf-8")
                 if self.env_file.exists() else "")
        self.assertFalse(self.env_file.exists(),
                         "a profile chosen while blind to the daemon was still "
                         f"written to disk:\n{wrote}")

    # --- and it has to say the true, actionable thing ----------------------- #

    def test_the_owner_is_told_what_to_do_about_it(self) -> None:
        self._install("down")
        self.assertIn("systemctl start docker", self.out,
                      "naming the fault without naming the fix is half a "
                      "diagnosis\n" + self.out)

    def test_windows_is_told_to_start_docker_desktop_not_to_run_systemctl(self) -> None:
        """install.cmd hands this script to Git Bash, where systemd does not exist.

        The fix on the machine that reported this was one click in Docker
        Desktop. A shell command that cannot run is not a smaller version of
        that instruction — it is a dead end that reads like the install needing
        something the owner does not have.
        """
        self._install("down", uname_s="MINGW64_NT-10.0-26100")
        self.assertIn("Docker Desktop", self.out, self.out)
        self.assertNotIn("systemctl", self.out, self.out)

    def test_a_permissions_problem_is_not_reported_as_a_stopped_engine(self) -> None:
        """Linux's version of the same silence, with a different fix.

        `sudo systemctl start docker` on an already-running daemon appears to
        work and changes nothing, which is the worst possible response to a
        group-membership problem.
        """
        out = self._install("denied")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("usermod -aG docker", self.out, self.out)

    # --- the control -------------------------------------------------------- #

    def test_a_healthy_daemon_installs_exactly_as_before(self) -> None:
        """The check has to be invisible on every machine that was already fine.

        `info` answers here with a runtime list that has no nvidia in it, so this
        also pins the distinction the fix turns on: a daemon that ANSWERS "no
        NVIDIA runtime" still earns the Container Toolkit message and the cpu
        fallback. Only silence is treated as silence.
        """
        p = self._install("up")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn("Container Toolkit", self.out, self.out)
        self.assertTrue(self.env_file.exists(), self.out)
        self.assertIn("COMPOSE_PROFILES=cpu",
                      self.env_file.read_text(encoding="utf-8"), self.out)


if __name__ == "__main__":
    unittest.main()
