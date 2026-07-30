"""The one place a microphone or speaker command is constructed.

Ava's *browser* voice path was always portable — `audio.decode_to_pcm` shells to
ffmpeg, which exists everywhere. What was Linux-only is the **bare-metal terminal
loop**: `voice_ava.py`, `enroll_voice.py` and `devices.sh` each hardcoded ALSA
(`arecord`, `aplay`), so the headless-appliance story ran on exactly one platform
and `devices.sh` printed "no capture device found" on a Mac with a working mic.

Shaped like `alloc/drivers/__init__.py`: resolution by platform, with a floor that
raises a typed error rather than returning a command that cannot run.

**ffmpeg rather than a new dependency.** The obvious answer is PortAudio via
`sounddevice`, and it was the plan of record. Two things argued against it: it is
a new dependency whose Linux wheels need `libportaudio2` present, and it is an
*in-process* API — so adopting it means rewriting `voice_ava`'s capture loop
(which reads a subprocess's stdout) blind, on a box with no microphone and no
speaker to test against. ffmpeg is **already a hard dependency** of the browser
path, ships `avfoundation` on macOS, `alsa`/`pulse` on Linux and `dshow` on
Windows, and keeps the subprocess shape the callers already have. So portability
here costs zero new packages.

⚠️ **NOT VERIFIED on any platform but Linux.** These argv lists are built from
documented ffmpeg/afplay flags. Nothing here has produced sound on a Mac, because
the maintainer has no Mac on this network and CI runners have no audio devices at
all. What IS testable — and tested — is that the right command is *constructed*
per platform (`tests/test_audio_io.py`). Voice therefore stays
`verified-on-device` only where someone has actually spoken and listened; see
deploy/platforms.conf.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

# Raw capture format every caller expects: mono signed 16-bit little-endian.
_FMT_ALSA = "S16_LE"
_FMT_FFMPEG = "s16le"


class AudioUnavailable(RuntimeError):
    """No usable capture or playback path on this box.

    Raised rather than returning a broken command, so a caller reports
    `voice_down` through the feature registry instead of a confusing
    FileNotFoundError from deep inside subprocess.
    """


def _which(name: str) -> str | None:
    return shutil.which(name)


def platform_key() -> str:
    """`linux` | `darwin` | `windows` | `generic` — audio only, not the HAL's job.

    Deliberately NOT `hwinfo.platform_id()`: that answers "which accelerator",
    which has nothing to do with which audio subsystem exists. A CUDA box and a
    CPU box on the same distro have identical audio.
    """
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p.startswith("win"):
        return "windows"
    return "generic"


# ---- capture ---------------------------------------------------------------- #
def capture_argv(device: str = "", rate: int = 16000,
                 seconds: float | None = None) -> list[str]:
    """A command that writes raw mono s16le PCM at `rate` to stdout.

    `seconds=None` means stream until killed (the push-to-talk loop);
    a number means capture exactly that long and exit (voice enrollment).
    """
    plat = platform_key()

    if plat == "linux" and _which("arecord"):
        argv = ["arecord", "-D", device or "default", "-f", _FMT_ALSA,
                "-c", "1", "-r", str(rate), "-t", "raw", "-q"]
        if seconds:
            argv += ["-d", str(int(seconds))]
        return argv

    ff = _which("ffmpeg")
    if not ff:
        raise AudioUnavailable(
            "no capture tool: install ffmpeg (already required for browser audio), "
            "or on Linux install alsa-utils for arecord")

    # ffmpeg input driver by platform. The device syntax differs per driver, which
    # is why `device` is passed through rather than normalised — a user who knows
    # their machine can name ":1" on macOS or "plughw:1,0" on Linux.
    if plat == "darwin":
        src = device or ":0"          # avfoundation "[[video]:[audio]]"
        argv = [ff, "-hide_banner", "-loglevel", "error",
                "-f", "avfoundation", "-i", src]
    elif plat == "windows":
        src = device or "audio=default"
        argv = [ff, "-hide_banner", "-loglevel", "error", "-f", "dshow", "-i", src]
    else:
        src = device or "default"
        argv = [ff, "-hide_banner", "-loglevel", "error", "-f", "alsa", "-i", src]

    if seconds:
        argv += ["-t", str(seconds)]
    argv += ["-ac", "1", "-ar", str(rate), "-f", _FMT_FFMPEG, "-"]
    return argv


def open_capture(device: str = "", rate: int = 16000) -> subprocess.Popen:
    """Start a streaming capture. `.stdout.read(n)` yields raw PCM."""
    return subprocess.Popen(capture_argv(device, rate),
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def record(seconds: float, device: str = "", rate: int = 16000) -> bytes:
    """Capture exactly `seconds` of raw PCM and return it."""
    r = subprocess.run(capture_argv(device, rate, seconds),
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return r.stdout or b""


# ---- playback --------------------------------------------------------------- #
def play_argv(path: str, device: str = "", player: str = "") -> list[str]:
    """A command that plays a WAV file and exits.

    `player` honours the pre-existing PLAYER env seam (aplay | pw-play | paplay)
    so an operator who tuned it keeps their setting.
    """
    plat = platform_key()

    if player and _which(player):
        if player == "aplay":
            return ["aplay", "-q"] + (["-D", device] if device else []) + [path]
        if player == "pw-play":
            return ["pw-play"] + (["--target", device] if device else []) + [path]
        if player == "paplay":
            return ["paplay"] + (["--device", device] if device else []) + [path]
        return [player, path]

    if plat == "darwin" and _which("afplay"):
        # Built into macOS: no install, and it honours the system output device.
        return ["afplay", path]

    if plat == "linux":
        for cand in ("aplay", "pw-play", "paplay"):
            if _which(cand):
                return play_argv(path, device, cand)

    if _which("ffplay"):
        return ["ffplay", "-hide_banner", "-loglevel", "error",
                "-nodisp", "-autoexit", path]

    raise AudioUnavailable(
        "no playback tool: install alsa-utils (aplay) on Linux, or ffmpeg "
        "(ffplay) anywhere; macOS has afplay built in")


def play(path: str, device: str = "", player: str = "") -> None:
    subprocess.run(play_argv(path, device, player),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=False)


# ---- enumeration ------------------------------------------------------------ #
def list_devices() -> str:
    """Human-readable device list for `devices.sh` / `ava doctor`.

    One implementation instead of the per-platform shell that `devices.sh`
    reimplemented — which is how that script came to claim a Mac had no mic.
    """
    plat = platform_key()
    out: list[str] = [f"Audio backend: {describe()}"]

    if plat == "linux" and _which("arecord"):
        out.append("\nCAPTURE (MIC_DEVICE):")
        out.append(_run(["arecord", "-l"]) or "  (none found)")
        out.append("\nPLAYBACK (OUT_DEVICE):")
        out.append(_run(["aplay", "-l"]) or "  (none found)")
        return "\n".join(out)

    if plat == "darwin" and _which("ffmpeg"):
        # avfoundation prints its device list to stderr and exits non-zero; that
        # is the documented way to enumerate, not an error.
        out.append("\nDEVICES (use the index as MIC_DEVICE, e.g. \":1\"):")
        out.append(_run(["ffmpeg", "-hide_banner", "-f", "avfoundation",
                         "-list_devices", "true", "-i", ""], want_stderr=True)
                   or "  (none found)")
        return "\n".join(out)

    if plat == "windows" and _which("ffmpeg"):
        out.append("\nDEVICES:")
        out.append(_run(["ffmpeg", "-hide_banner", "-f", "dshow",
                         "-list_devices", "true", "-i", "dummy"],
                        want_stderr=True) or "  (none found)")
        return "\n".join(out)

    out.append("\n  (no enumeration available on this platform)")
    return "\n".join(out)


def _run(argv: list[str], want_stderr: bool = False) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        return ((r.stderr if want_stderr else r.stdout) or "").strip()
    except Exception:  # noqa: BLE001 — a missing tool is a normal answer
        return ""


def describe() -> str:
    """Which tools would actually be used, for diagnostics."""
    plat = platform_key()
    cap = "none"
    try:
        cap = capture_argv()[0]
    except AudioUnavailable:
        pass
    play_tool = "none"
    try:
        play_tool = play_argv("/dev/null")[0]
    except AudioUnavailable:
        pass
    return f"{plat}: capture={cap} playback={play_tool}"


def available() -> bool:
    try:
        capture_argv()
        play_argv("/dev/null")
        return True
    except AudioUnavailable:
        return False


if __name__ == "__main__":
    if "--list" in sys.argv:
        print(list_devices())
    else:
        print(describe())
    raise SystemExit(0 if available() else 1)
