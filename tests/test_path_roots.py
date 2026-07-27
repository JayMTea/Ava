"""There is exactly ONE resolver for where runtime state lives: `ava_bridge.settings`.

`settings` layers AVA_HOME -> ava.yaml `paths.*` -> a per-path env override. Any
module that re-derives a root from `os.environ["AVA_HOME"]` or from `__file__`
gets only the first of those three layers, so `paths.data` in ava.yaml silently
moves state for the modules that ask settings and not for the ones that don't.

That is not hypothetical. It shipped three times:
  * agent/install.sh wrote the internal token to the CODE root while the bridge
    read $AVA_HOME/data — so every /internal/* callback 401'd on Docker.
  * perf_log.py wrote the perf history into the container's ephemeral layer while
    the Data page read the mounted volume — Performance showed 0 bytes forever.
  * ava_bridge/config.py built its own `AVA_HOME or ROOT` root and joined onto it,
    ignoring every `paths.*` key ava.yaml documents.

The class only misbehaves when AVA_HOME differs from the code root — which is
exactly Docker, and never the author's bare-metal box. Nobody notices by running
it; a static guard is the only thing that catches it.

Style matches tests/test_no_eval_data.py: a `git ls-files` scan, no bridge, no
AVA_HOME, no network.
"""
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Reading AVA_HOME directly is allowed only where it is justified in the file.
_ENV_ALLOW = {
    # The definition itself.
    "ava_bridge/settings.py",
    # Documented stdlib-only: shipped per-app, must not import ava_bridge. It
    # mirrors the env half of settings.logs_dir() and says so.
    "perf_log.py",
    # Tries settings.models_dir() first; the env read is a guarded fallback for
    # running the script outside the package.
    "speaker.py",
    # This file names the pattern it bans.
    "tests/test_path_roots.py",
}

_ENV_AVA_HOME = re.compile(r"environ(?:\.get\(|\[)\s*['\"]AVA_HOME['\"]")

# Joining a runtime-state directory onto a CODE root. `config.ROOT` itself is
# fine and widely correct — it locates code artifacts (arch.py, alerts.yaml, the
# persona template, piper binaries). It is only wrong when a *state* directory is
# hung off it, which is what these three did.
_STATE_ON_CODE_ROOT = re.compile(
    r"join\(\s*(?:config\.ROOT|ROOT|_HERE|HERE)\s*,\s*['\"](?:data|logs|media|uploads|secrets)['\"]")


def _tracked(pattern: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", pattern],
                         capture_output=True, text=True, check=True).stdout
    return [ln for ln in out.splitlines() if ln]


def _read(rel: str) -> str:
    try:
        return (ROOT / rel).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def test_only_settings_resolves_ava_home() -> None:
    offenders = [
        rel for rel in _tracked("*.py")
        if rel not in _ENV_ALLOW
        and not rel.startswith(("tests/", "qa/"))          # fixtures legitimately set it
        and _ENV_AVA_HOME.search(_read(rel))
    ]
    assert not offenders, (
        "these modules resolve AVA_HOME themselves and so ignore ava.yaml's "
        "`paths.*` and the per-path env overrides — call ava_bridge.settings "
        "(data_dir/logs_dir/media_dir/upload_dir/secrets_dir) instead, or add a "
        f"justified entry to _ENV_ALLOW in this file: {offenders}"
    )


def test_no_runtime_state_hangs_off_the_code_root() -> None:
    offenders: list[str] = []
    for rel in _tracked("*.py"):
        if rel.startswith(("tests/", "qa/")) or rel == "tests/test_path_roots.py":
            continue
        text = _read(rel)
        # A file that asks settings first may keep an indented fallback for
        # standalone use (`try: settings.media_dir() / except ImportError: …`).
        # gpu_service.py and speaker.py both do this deliberately. What stays
        # banned is an UNCONDITIONAL module-level root, which is the actual bug —
        # so the fallback allowance requires both the settings import and an
        # indented line.
        guarded = "from ava_bridge import settings" in text
        for i, line in enumerate(text.splitlines(), 1):
            if not _STATE_ON_CODE_ROOT.search(line):
                continue
            if guarded and line[:1].isspace():
                continue
            offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "runtime state must not be joined onto the CODE root — on Docker that is "
        "the ephemeral container layer while the bridge reads the mounted volume. "
        "Use ava_bridge.settings:\n  " + "\n  ".join(offenders)
    )


def test_config_paths_track_settings() -> None:
    """config.* and settings.* must resolve to the same directories.

    They are two names for one thing; the bug was that they were two things.
    """
    from ava_bridge import config, settings
    assert config.CHATS_DIR == settings.data_dir()
    assert config.LOGS_DIR == settings.logs_dir()
    assert config.MEDIA_DIR == settings.media_dir()
    assert config.UPLOAD_DIR == settings.upload_dir()
