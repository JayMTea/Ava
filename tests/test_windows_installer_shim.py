"""Convention guard: deploy/install.cmd is a shim, and must stay one.

Windows opens Command Prompt, which cannot run a bash script. The documented
answer was "use Git Bash", and it kept losing people — four consecutive real
first-run attempts pasted `./install.sh` into cmd, by someone who knew the rule.
install.cmd removes the choice: it locates the bash that ships with Git for
Windows and hands it the SAME install.sh.

The value is entirely in it staying a shim. The moment it grows its own detection
or profile logic, Windows has a second installer that drifts from the first, and
the drift will be discovered by a Windows user rather than by CI — there is no
Windows runner here.

Static scan, no shell and no Windows required. It cannot prove install.cmd RUNS
(nothing in this repo can), only that it has not become a fork of the installer.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CMD = ROOT / "deploy" / "install.cmd"
ATTRS = ROOT / ".gitattributes"


def test_the_shim_exists_and_delegates_to_the_real_installer() -> None:
    assert CMD.is_file(), "deploy/install.cmd is gone — Windows is back to needing Git Bash"
    src = CMD.read_text(encoding="utf-8")
    assert "./install.sh" in src, (
        "install.cmd no longer runs install.sh. If Windows needs its own steps, "
        "they belong in install.sh behind a platform check, not in a second file "
        "that no CI runner executes.")
    assert "-lc" in src, (
        "install.cmd dropped the LOGIN shell. install.sh needs uname/sed/tr/seq "
        "from Git's usr\\bin, which only a login shell puts on PATH — without it "
        "the script dies partway through on a missing tool instead of starting.")


def test_the_shim_has_not_grown_an_installer_of_its_own() -> None:
    """Anything resembling profile selection or hardware detection here is the
    start of a second installer."""
    src = CMD.read_text(encoding="utf-8").lower()
    for leaked in ("nvidia-smi", "docker compose", "platforms.conf",
                   "profiles\\", "compose_profiles"):
        assert leaked not in src, (
            f"install.cmd mentions {leaked!r} — it is doing the installer's job "
            "rather than launching it, and nothing on this project's CI runs it")


def test_it_says_something_useful_when_git_is_missing() -> None:
    src = CMD.read_text(encoding="utf-8")
    assert "git-scm.com" in src, (
        "no pointer to Git for Windows. Without git there is no bash, and the "
        "failure would otherwise be a bare 'not recognized' with no next step.")


def test_batch_files_are_pinned_to_crlf() -> None:
    """cmd.exe parses a batch file line by line AS it executes it, so LF-only
    endings make it mis-read labels and `goto` targets. That failure looks like a
    broken installer rather than a broken checkout, which is the worst way for it
    to present."""
    attrs = ATTRS.read_text(encoding="utf-8")
    assert "*.cmd text eol=crlf" in attrs, (
        ".gitattributes no longer pins *.cmd to CRLF — install.cmd can check out "
        "LF-only on a fork and mis-parse its own labels")
