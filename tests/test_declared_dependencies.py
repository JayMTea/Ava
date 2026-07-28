"""Every third-party module shipped code imports must be a declared dependency.

`ava_bridge/gpu_jobs.py` imported PIL to build thumbnails while Pillow appeared
in NO requirements file. It worked here because Pillow had been installed
transitively; on a fresh `pip install -r requirements.txt` the import raised
ImportError, `ensure_thumbnail`'s bare `except Exception` swallowed it, and every
/thumb/<name> quietly served the FULL-size original. Nothing broke, so nothing
was noticed — the gallery just shipped megabytes where it meant to ship
kilobytes, for every forker, for as long as that went undeclared.

An undeclared dependency is invisible precisely on the machine that has it. This
is the same static-scan shape as tests/test_diagram_sync.py: read `git ls-files`,
collect offenders, one instructed assert.
"""
import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Directories whose imports are not part of what a forker installs.
# `sdk/` is a separate subtree a forker COPIES into their own device/MCP project
# rather than something `pip install -r requirements.txt` provisions; its deps
# (pyserial and friends) belong to that project, not to the bridge.
_SKIP_TOP = {"tests", "qa", "overlay", "demo", "frontend", "tools", "docs",
             "sdk", ".venv"}

# Import name -> distribution name, where they differ.
_ALIASES = {
    "PIL": "pillow", "yaml": "pyyaml", "dotenv": "python-dotenv",
    "multipart": "python-multipart", "websocket": "websocket-client",
    "pynvml": "nvidia-ml-py", "cv2": "opencv-python", "serial": "pyserial",
    "sounddevice": "sounddevice", "soundfile": "soundfile",
    "dateutil": "python-dateutil", "jwt": "pyjwt", "bs4": "beautifulsoup4",
}

# Third-party imports that are deliberately optional AND already guarded so
# their absence degrades instead of crashing. Each needs a reason.
_OPTIONAL = {
    # Voice tier — requirements-voice.txt, gated behind features.preflight.
    "faster_whisper", "torch", "torchaudio", "speechbrain", "piper",
    "kokoro", "onnxruntime", "librosa", "scipy", "sherpa_onnx",
    # The owner's private overlay tree — not a distribution at all. Imported
    # behind try/except in ava_bridge/access_policy.py; a fresh fork has none.
    "overlay",
}

# Direct imports guaranteed by a DECLARED dependency's own hard requirements.
# Not the same as optional: these are unguarded imports that cannot fail while
# the parent is installed. Pinning them here independently would let this file
# and the parent's supported range drift apart, which is a worse failure than
# the one this guard exists to catch.
_TRANSITIVE_OK = {
    "pydantic",   # a hard requirement of fastapi, which IS pinned here
}


def _tracked() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.py"],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _declared() -> set[str]:
    """Distribution names from every requirements*.txt, normalised."""
    names: set[str] = set()
    for req in ROOT.glob("requirements*.txt"):
        for line in req.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            # "uvicorn[standard]==0.51.0" -> "uvicorn"
            name = re.split(r"[<>=!\[;\s]", line, 1)[0].strip().lower()
            if name:
                names.add(name.replace("_", "-"))
    return names


def _first_party() -> set[str]:
    """Top-level modules and packages that live in this repo."""
    out = {p[:-3] for p in _tracked() if "/" not in p}
    out |= {p.split("/", 1)[0] for p in _tracked() if "/" in p}
    return out


def _imported_third_party() -> dict[str, str]:
    """Top-level import name -> the first shipped file that imports it."""
    first_party = _first_party()
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, str] = {}
    for rel in _tracked():
        if rel.split("/", 1)[0] in _SKIP_TOP:
            continue
        try:
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is a relative (first-party) import
                mods = [node.module] if node.module and node.level == 0 else []
            else:
                continue
            for m in mods:
                top = (m or "").split(".", 1)[0]
                if (not top or top in stdlib or top in first_party
                        or top in _OPTIONAL or top in _TRANSITIVE_OK):
                    continue
                found.setdefault(top, rel)
    return found


def test_every_imported_third_party_module_is_declared() -> None:
    declared = _declared()
    offenders = []
    for mod, where in sorted(_imported_third_party().items()):
        dist = _ALIASES.get(mod, mod).lower().replace("_", "-")
        if dist not in declared:
            offenders.append(f"{mod}  (imported by {where}; expected dist '{dist}')")
    assert not offenders, (
        "These third-party modules are imported by shipped code but are declared "
        "in no requirements*.txt:\n  - " + "\n  - ".join(offenders)
        + "\n\nThey resolve on any machine that happens to have them and raise "
        "ImportError on a fresh `pip install -r requirements.txt`. If the import "
        "is deliberately optional, guard it AND add it to _OPTIONAL here with the "
        "reason; otherwise pin it in the right requirements file.\n"
        "If the distribution is simply named differently from the import, add the "
        "mapping to _ALIASES."
    )
