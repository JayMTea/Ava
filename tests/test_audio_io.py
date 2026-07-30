"""Convention guard + behaviour: one audio seam, and no engine defaults to absent.

Two failures this covers, both of which shipped:

  1. **ALSA hardcoded in four places.** `voice_ava.py`, `enroll_voice.py` and
     `devices.sh` each named `arecord`/`aplay` directly, so the bare-metal voice
     loop was Linux-only and `devices.sh` told a Mac owner with a working built-in
     microphone that no capture device was found.
  2. **`AVA_TTS` defaulted to `piper`, and `bin/piper/` is gitignored.** The
     default TTS engine therefore pointed at a file present on the maintainer's
     box and in no fork at all.

The convention scan follows tests/test_alloc_measurement.py. The behavioural half
asserts the argv is right per platform, which is as far as automation can go:
CI runners have no audio devices, so no test anywhere can prove sound came out.
That is why voice stays `verified-on-device` in deploy/platforms.conf.
"""
import ast
import re
import sys
from unittest import mock

from gitfiles import ROOT, tracked_paths

from ava_bridge import audio, audio_io

# Tools that read a microphone or drive a speaker. `ffmpeg` is deliberately NOT
# here: it is also the browser-audio decoder (audio.decode_to_pcm), which is a
# format conversion rather than device access, and banning it repo-wide would
# flag the one path that was always portable.
_AUDIO_TOOL = re.compile(
    r"""(?x)
    \barecord\b | \baplay\b | \bpw-play\b | \bpaplay\b
  | \bafplay\b | \bffplay\b
  | \bavfoundation\b | \bdshow\b
    """)

_ALLOWED = {
    # The seam itself.
    "ava_bridge/audio_io.py",
    # Names the tools it bans, in prose.
    "tests/test_audio_io.py",
    # Sets the PLAYER env seam (`PLAYER="${PLAYER:-aplay}"`), which audio_io
    # deliberately honours so an operator who tuned their playback tool keeps it.
    # Choosing a default for a documented override is configuration, not command
    # construction — the thing this guard exists to centralise.
    "run.sh",
}


def _docstring_lines(text: str) -> set[int]:
    """Line numbers covered by DOCSTRINGS only — not every string literal.

    Prose that *describes* the pipeline ("USB mic --(arecord)--> VAD") is not a
    command, and voice_ava.py's module docstring says exactly that. Skipping
    comments was not enough.

    The first attempt at this excluded every string Constant, which silently broke
    the guard: an argv list IS a list of string literals, so `["arecord", "-D"]`
    became invisible and the negative control stopped failing. A docstring is a
    string in STATEMENT position (`ast.Expr`), which argv lists never are — so
    match that shape rather than the type.
    """
    covered: set[int] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return covered
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            end = getattr(node.value, "end_lineno", node.lineno) or node.lineno
            covered.update(range(node.lineno, end + 1))
    return covered


def test_only_the_audio_seam_names_an_audio_device_tool() -> None:
    offenders: list[str] = []
    for path in tracked_paths("*.py") + tracked_paths("*.sh"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _ALLOWED or rel.startswith("tests/"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        skip = _docstring_lines(text) if rel.endswith(".py") else set()
        for n, line in enumerate(text.splitlines(), 1):
            s = line.lstrip()
            if s.startswith("#") or n in skip:
                continue          # a comment or docstring naming it is not the trap
            # Also drop a TRAILING comment: `PLAYER = ...  # aplay | pw-play` is
            # documentation of the seam's accepted values, not a command.
            code = s.split("  #", 1)[0] if "  #" in s else s
            if _AUDIO_TOOL.search(code):
                offenders.append(f"{rel}:{n}: {s[:100]}")
    assert not offenders, (
        "these construct an audio device command outside the one seam:\n  "
        + "\n  ".join(offenders)
        + "\n\nUse ava_bridge/audio_io.py instead:\n"
          "  audio_io.open_capture(device, rate)   # streaming mic -> Popen\n"
          "  audio_io.record(seconds, device, rate)\n"
          "  audio_io.play(path, device, player)\n"
          "  audio_io.list_devices()\n"
          "\nIt picks the tool per platform (arecord on Linux, ffmpeg's\n"
          "avfoundation/dshow elsewhere, afplay on macOS) and raises\n"
          "AudioUnavailable rather than returning a command that cannot run.")


def test_capture_and_playback_resolve_per_platform() -> None:
    """The argv must match the platform, since only one of them is testable live."""
    cases = {
        "linux": ("arecord", "aplay"),
        "darwin": ("ffmpeg", "afplay"),
        "windows": ("ffmpeg", "ffplay"),
    }
    for plat, (want_cap, want_play) in cases.items():
        with mock.patch.object(audio_io, "platform_key", return_value=plat), \
             mock.patch.object(audio_io, "_which",
                               side_effect=lambda n, p=plat: (
                                   None if (n == "arecord" and p != "linux")
                                   or (n == "afplay" and p != "darwin")
                                   or (n in ("aplay", "pw-play", "paplay")
                                       and p != "linux")
                                   else f"/usr/bin/{n}")):
            cap = audio_io.capture_argv("", 16000)
            play = audio_io.play_argv("/tmp/x.wav")
        assert cap[0].endswith(want_cap), f"{plat}: capture used {cap[0]}"
        assert play[0].endswith(want_play), f"{plat}: playback used {play[0]}"
        # Whatever the tool, the format contract is fixed: mono 16 kHz s16le.
        joined = " ".join(cap)
        assert "16000" in joined, f"{plat}: capture lost the sample rate"
        assert ("S16_LE" in joined or "s16le" in joined), (
            f"{plat}: capture lost the PCM format — callers parse int16")


def test_a_fixed_duration_capture_actually_bounds_itself() -> None:
    """Enrollment records N seconds; an unbounded command would hang it forever."""
    with mock.patch.object(audio_io, "platform_key", return_value="linux"), \
         mock.patch.object(audio_io, "_which", return_value="/usr/bin/arecord"):
        argv = audio_io.capture_argv("", 16000, seconds=3)
    assert "-d" in argv and "3" in argv, f"no duration bound in {argv}"


def test_no_audio_tooling_raises_rather_than_returning_a_broken_command() -> None:
    with mock.patch.object(audio_io, "platform_key", return_value="generic"), \
         mock.patch.object(audio_io, "_which", return_value=None):
        for fn in (lambda: audio_io.capture_argv(),
                   lambda: audio_io.play_argv("/tmp/x.wav")):
            try:
                fn()
            except audio_io.AudioUnavailable as e:
                assert "install" in str(e).lower(), (
                    "the error must say what to install, not just that it failed")
            else:
                raise AssertionError(
                    "returned a command with no tooling present — the caller would "
                    "get a confusing FileNotFoundError from inside subprocess")


# ---- TTS resolution --------------------------------------------------------- #
def test_the_default_tts_engine_is_not_a_file_that_may_not_exist() -> None:
    """`auto`, never a hardcoded engine name.

    The literal default was `piper` while `bin/piper/` is gitignored, so the
    shipped default was broken everywhere except the machine that wrote it.
    """
    src = (ROOT / "ava_bridge" / "audio.py").read_text(encoding="utf-8")
    assert 'os.environ.get("AVA_TTS", "piper")' not in src, (
        "AVA_TTS must not default to 'piper' — bin/piper/ is gitignored, so that "
        "default names a file no fork has. Default to 'auto' and resolve.")
    assert '"AVA_TTS"' in src and "auto" in src


def test_macos_gets_speech_with_nothing_installed() -> None:
    """`say` is built in, so a Mac must have working TTS before any pip install."""
    with mock.patch.object(audio, "_piper_bin", return_value=None), \
         mock.patch.object(audio, "_kokoro_reachable", return_value=False), \
         mock.patch.object(sys, "platform", "darwin"), \
         mock.patch("shutil.which",
                    side_effect=lambda n: "/usr/bin/say" if n == "say" else None):
        assert audio._resolve_tts() == "say"


def test_no_engine_degrades_to_silence_instead_of_losing_the_reply() -> None:
    """phone_bridge builds the /api/talk response inline, so a raise here would
    take the assistant's TEXT down with the audio."""
    with mock.patch.object(audio, "_resolve_tts", return_value="none"):
        out = audio.tts_wav_bytes("hello")
    assert out == b"", "no engine must yield an empty clip, not an exception"
