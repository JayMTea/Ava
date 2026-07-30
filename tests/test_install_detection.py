"""Convention guard: deploy/install.sh detects hardware via the HAL, not by itself.

install.sh used to carry its own `nvidia-smi`-or-CPU probe. That meant the
installer's idea of the machine and the app's could disagree — and did, in the
direction that matters: the shell probe had no branch for ROCm, Level Zero or an
integrated APU, so a 128 GB Strix Halo box silently got the CPU-Ollama profile
with nothing said about why, while `hwinfo` would have classified it correctly.

One detector (`ava_bridge/hwinfo.py`), one mapping table
(`deploy/platforms.conf`). The installer asks and reports.

A single shell fallback survives on purpose, and this guard bounds it: with
`AVA_REPO` set, install.sh runs BEFORE the clone, so there may be no Python
package to import yet. That block is fenced by markers and is the only place a
vendor tool may be probed directly.

Style follows tests/test_alloc_measurement.py — a static scan that runs anywhere.
"""
import re

import pytest
from gitfiles import ROOT, require_git

_INSTALL = "deploy/install.sh"

# Probing a vendor tool to decide WHAT the hardware is. `docker info` is not here:
# asking whether a container runtime is registered is a different question from
# asking which GPU exists, and it legitimately belongs in the installer.
_VENDOR_PROBE = re.compile(
    r"""(?x)
    \bnvidia-smi\b
  | \brocm-smi\b
  | \brocminfo\b
  | \bxpu-smi\b
  | /sys/class/drm
  | \bsystem_profiler\b
    """)

# Explicit sentinels, not prose comments. The first version of this guard fenced
# on the wording of ordinary comments, which meant rewording one silently widened
# or broke the fence — and it immediately flagged the legitimate SIZING block as a
# violation. A sentinel says "this exemption is deliberate" out loud, and it takes
# an edit to that line to move it.
_FENCE_OPEN = "# HAL-EXEMPT-BEGIN"
_FENCE_SHUT = "# HAL-EXEMPT-END"


def _lines() -> list[str]:
    return (ROOT / _INSTALL).read_text(encoding="utf-8").splitlines()


def test_vendor_detection_is_confined_to_the_fenced_fallback() -> None:
    require_git()
    lines = _lines()
    assert any(_FENCE_OPEN in ln for ln in lines), (
        f"{_INSTALL} lost its {_FENCE_OPEN!r} marker, so this guard cannot tell "
        "the sanctioned fallback from a new ad-hoc probe. Restore the marker.")

    inside = False
    offenders: list[str] = []
    for n, ln in enumerate(lines, 1):
        if _FENCE_OPEN in ln:
            inside = True
        elif _FENCE_SHUT in ln:
            inside = False
        s = ln.strip()
        if s.startswith("#") or inside:
            continue
        if _VENDOR_PROBE.search(ln):
            offenders.append(f"{_INSTALL}:{n}: {s[:100]}")

    assert not offenders, (
        "these probe a vendor tool to decide what the hardware is, outside the "
        "one sanctioned fallback block:\n  " + "\n  ".join(offenders)
        + "\n\nAsk the HAL instead — it is the only thing that knows about APUs:\n"
          "  eval \"$(python3 -m ava_bridge.platforms --install-detect)\"\n"
          "  # -> AVA_DETECTED_PROFILE / _PLATFORM / _MEM_MODEL / _TIER / _LABEL\n"
          "\nSizing ('is there ENOUGH GPU') and runtime ('is the container "
          "toolkit registered') are different questions and may stay in the "
          "installer — but they must not be how the platform is identified.")


def test_the_installer_reports_the_verification_tier() -> None:
    """An owner should learn how well their hardware class is known, at install."""
    require_git()
    src = (ROOT / _INSTALL).read_text(encoding="utf-8")
    assert "AVA_DETECTED_TIER" in src, (
        f"{_INSTALL} no longer prints the platform's verification tier. That line "
        "is the point of deploy/platforms.conf: a support matrix nobody sees is "
        "the same as not having one. Restore the `say \"Platform: ...\"` line.")
    assert "ondevice_check" in src, (
        f"{_INSTALL} should tell a ci-simulated platform's owner how to promote "
        "it (tools/ondevice_check.py --record). That is the only route from "
        "simulated to verified, and they are the only person who can take it.")


def test_every_profile_the_table_can_emit_is_installable() -> None:
    """A `profile` in the matrix must be a shipped compose profile, or bare-metal.

    The one non-compose value, `bare-metal`, has to be mapped before it reaches
    `profiles/<name>.env` — otherwise a Mac user passing AVA_FORCE_DOCKER=1 dies
    on a missing file, which is what happened before the mapping existed.
    """
    require_git()
    from ava_bridge import platforms
    src = (ROOT / _INSTALL).read_text(encoding="utf-8")
    shipped = {p.stem for p in (ROOT / "deploy" / "profiles").glob("*.env")}
    if not shipped:
        pytest.skip("no profile files found")

    for row in platforms.load():
        prof = row.profile
        if prof == "bare-metal":
            assert '"bare-metal"' in src or "'bare-metal'" in src or \
                   "= \"bare-metal\"" in src or "bare-metal" in src, (
                "deploy/platforms.conf emits the profile 'bare-metal', which is "
                "not a compose profile — install.sh must map it before it is used "
                "to look up profiles/<name>.env.")
            continue
        assert prof in shipped, (
            f"row {row.key!r} names profile {prof!r}, but deploy/profiles/{prof}.env "
            f"does not exist (shipped: {sorted(shipped)}). The installer would die "
            "looking for it.")
