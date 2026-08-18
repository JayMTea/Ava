"""The first-run link must reach the owner's browser, on every ending.

A novice install is meant to be: run the installer, a browser opens, set a
password. The claim token is machinery that makes that safe under Docker — the
container cannot see that the caller is at the keyboard, because the request
arrives from the bridge gateway — and a human is never supposed to hold one.

On a real Windows install a human held one. The owner reached the "Claim this
Ava" page, was told by it to run `docker compose exec ava cat
/data/data/setup_claim`, and pasted the result into a box. Three separate paths
lead there, and every one of them ends in `deploy/install.sh`:

  * the ~3-minute health wait times out on a slow first boot, and that branch
    printed two debug commands and NO link at all;
  * a preset password means no gate, and that branch printed an address and
    opened nothing;
  * under WSL `uname -s` is `Linux`, so opening needed DISPLAY *and* xdg-open —
    a minimal WSL image has neither, and the `&&` failure is swallowed by the
    backgrounding, so the arm returned 0 having silently done nothing.

None of it was caught, because nothing had ever RUN it. tests/test_install_
opens_the_link.py is a pure string scan, and the two tests that execute the
script set AVA_INSTALL_DRY_RUN=1 (which exits well before this block) and
AVA_NO_BROWSER=1. Deleting open_browser's whole Windows arm left them green.

So this runs deploy/install.sh for real against stub binaries and asserts on what
was LAUNCHED. Same harness as tests/test_install_vram_branches.py and
tests/test_install_docker_daemon.py: a throwaway git repo, stubs on PATH,
nothing touching the real deploy/.env.

Run: .venv/bin/python -m pytest tests/test_install_first_run_exec.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOKEN = "tok_ABCdef0123456789"

# A docker whose token is reachable by exactly one route, so each of install.sh's
# three fallbacks can be exercised alone. FAKE_TOKEN_VIA: exec | logs | none.
_DOCKER = f"""#!/bin/sh
case "$1" in
  info) printf '{{"runc":{{"path":"runc"}}}}\\n'; exit 0 ;;
  compose)
    shift
    case "$*" in
      *"cat /data/data/setup_claim"*)
        [ "${{FAKE_TOKEN_VIA:-exec}}" = "exec" ] || exit 1
        echo "{TOKEN}" ; exit 0 ;;
      *"test -f"*)
        [ "${{FAKE_TOKEN_VIA:-exec}}" = "none" ] && exit 1
        exit 0 ;;
      logs*)
        [ "${{FAKE_TOKEN_VIA:-exec}}" = "none" ] && exit 0
        echo "ava | first run: http://127.0.0.1:8096/setup?claim={TOKEN}"
        exit 0 ;;
    esac
    exit 0 ;;
esac
exit 0
"""

# The published-port probe install.sh waits on. FAKE_PORT_UP=0 makes it never
# answer, which is the slow-first-boot branch.
_CURL = """#!/bin/sh
[ "${FAKE_PORT_UP:-1}" = "1" ] || exit 7
exit 0
"""

_UNAME = """#!/bin/sh
case "$1" in
  -m) echo "x86_64" ;;
  *)  echo "${FAKE_UNAME_S:-Linux}" ;;
esac
exit 0
"""

# Every launcher open_browser can reach, each recording what it was handed. The
# test asserts on this file: "was a browser pointed at a usable link", which is
# the only question that matters and the one no scan of the source can answer.
_RECORDER = """#!/bin/sh
printf '%s\\n' "$*" >> "$AVA_TEST_RECORD"
exit 0
"""

# Nothing must actually wait: the timeout branch loops 90 times over `sleep 2`.
_SLEEP = "#!/bin/sh\nexit 0\n"


def _requirements_met() -> bool:
    return all(shutil.which(b) for b in ("bash", "git", "awk", "grep", "sed", "seq"))


@unittest.skipUnless(_requirements_met(), "needs bash, git and coreutils")
class FirstRunLinkTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ava-install-firstrun-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True,
                       capture_output=True)
        shutil.copytree(ROOT / "deploy", self.tmp / "deploy")
        (self.tmp / "deploy" / ".env").unlink(missing_ok=True)
        self.record = self.tmp / "launched.txt"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stubs = {"docker": _DOCKER, "curl": _CURL, "uname": _UNAME, "sleep": _SLEEP}
        # Every launcher arm of open_browser, so a branch that picks the "wrong"
        # one still records rather than silently doing nothing.
        for name in ("xdg-open", "wslview", "powershell.exe", "open", "cmd"):
            stubs[name] = _RECORDER
        for name, body in stubs.items():
            p = self.bin / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)

    def _install(self, **extra: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "AVA_TEST_RECORD": str(self.record),
            # A Linux desktop by default: DISPLAY set and xdg-open present, so
            # the plain-Linux arm is live unless a test asks for WSL.
            "DISPLAY": ":0",
            "AVA_PROFILE": "cpu",
            "AVA_GPU_MEMORY_MODEL": "discrete",
        }
        # The three things that make open_browser a deliberate no-op. Unset, or
        # this file would assert nothing on a CI runner or over SSH.
        for k in ("AVA_NO_BROWSER", "CI", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY",
                  "AVA_MODEL", "AVA_HOME", "AVA_DIR", "AVA_INSTALL_DRY_RUN",
                  "WSL_DISTRO_NAME", "WAYLAND_DISPLAY"):
            env.pop(k, None)
        env.update(extra)
        p = subprocess.run(["bash", str(self.tmp / "deploy" / "install.sh")],
                           capture_output=True, text=True, timeout=300,
                           cwd=str(self.tmp / "deploy"), env=env)
        self.out = p.stdout + p.stderr
        self.launched = (self.record.read_text(encoding="utf-8")
                         if self.record.exists() else "")
        return p

    # --- the silent exit that skipped all of it ----------------------------- #

    def test_the_installer_survives_an_env_with_no_model_line(self):
        """The defect this whole file found on its first run.

        Since "ship no default model" every profile ships AVA_MODEL empty, and
        install.sh writes the line ONLY when a model exists — so a stock .env has
        no AVA_MODEL line at all. The Ollama pull step then ran
        `grep -E '^AVA_MODEL=' .env | tail -1 | cut -d= -f2-`, grep exited 1
        matching nothing, `set -euo pipefail` carried that through the pipe into
        the assignment, and the installer died there: four lines after
        `docker compose up` and a hundred before the first-run link.

        The stack was up, so it looked like a successful install. Everything that
        makes a first run usable had been skipped.
        """
        p = self._install()
        env = (self.tmp / "deploy" / ".env").read_text(encoding="utf-8")
        self.assertNotIn("AVA_MODEL=", env,
                         "this test is only meaningful while the stock .env has "
                         "no model line — if that changed, so did the defect")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn("Logs:", self.out,
                      "the installer stopped before its own last line, so every "
                      "step after `docker compose up` was skipped\n" + self.out)

    # --- the link has to be handed over, whichever route found the token ---- #

    def test_a_browser_is_opened_at_the_claimed_link(self):
        p = self._install()
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn(f"/setup?claim={TOKEN}", self.launched,
                      "nothing was launched, so the owner meets the claim gate "
                      "with nothing in hand\n" + self.out)

    def test_the_token_read_from_the_log_is_handed_over_too(self):
        """The `docker compose exec` route fails on plenty of real boxes."""
        p = self._install(FAKE_TOKEN_VIA="logs")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn(f"/setup?claim={TOKEN}", self.launched, self.out)

    def test_an_instance_with_no_gate_still_gets_its_page_opened(self):
        """A preset password means nothing to prove — which is a reason to skip
        the token, not a reason to make someone type an address."""
        p = self._install(FAKE_TOKEN_VIA="none")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn("http://localhost:8096", self.launched, self.out)
        self.assertNotIn("claim=", self.launched,
                         "a claim link where no token exists is a 403")

    # --- the slow first boot, which is how the owner got stranded ----------- #

    def test_a_slow_first_boot_still_prints_a_working_link(self):
        p = self._install(FAKE_PORT_UP="0")
        self.assertEqual(p.returncode, 0,
                         "a slow boot is not a failed install\n" + self.out)
        self.assertIn(f"http://localhost:8096/setup?claim={TOKEN}", self.out,
                      "the owner is left to find :8096 themselves later, meet "
                      "the claim gate, and be told to paste a token\n" + self.out)

    def test_a_slow_first_boot_does_not_open_a_refused_tab(self):
        """ERR_CONNECTION_REFUSED reads as a failed install, not a slow one."""
        self._install(FAKE_PORT_UP="0")
        self.assertEqual(self.launched.strip(), "", self.launched)

    # --- WSL: a Windows install wearing a Linux uname ----------------------- #

    def test_wsl_opens_the_windows_browser_with_no_display(self):
        p = self._install(WSL_DISTRO_NAME="Ubuntu", DISPLAY="")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertIn(f"/setup?claim={TOKEN}", self.launched,
                      "WSL took the Linux arm, found no DISPLAY, and returned 0 "
                      "having done nothing\n" + self.out)

    def test_a_headless_linux_box_still_does_not_try_to_open_anything(self):
        """The guard that WSL must not break: no display, no browser, no error."""
        p = self._install(DISPLAY="")
        self.assertEqual(p.returncode, 0, self.out)
        self.assertEqual(self.launched.strip(), "", self.launched)
        self.assertIn(f"/setup?claim={TOKEN}", self.out,
                      "the link is ALWAYS printed — opening can only ever save a "
                      "step, never be the only way through")


if __name__ == "__main__":
    unittest.main()
