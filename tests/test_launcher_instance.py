"""Shell launchers must preserve instance and exported-environment boundaries."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="requires a POSIX shell")
@pytest.mark.parametrize("compatibility", [False, True])
def test_launcher_does_not_adopt_checkout_secrets(tmp_path, compatibility):
    code, home = tmp_path / "code", tmp_path / "instance"
    code.mkdir()
    home.mkdir()
    (code / ".env").write_text("CHECKOUT_ONLY=private\nCHOICE=checkout\n")
    (home / ".env").write_text("CHOICE=instance\nEXPLICIT=file\n")
    env = {k: v for k, v in os.environ.items() if k not in {
        "CHECKOUT_ONLY", "CHOICE", "EXPLICIT", "AVA_LOAD_REPO_ENV"}}
    env.update(AVA_HOME=str(home), EXPLICIT="process",
               AVA_LOAD_REPO_ENV="1" if compatibility else "0")
    result = subprocess.run([
        "bash", "-c",
        '. "$1"; ava_load_instance_env "$2"; printf "%s\\n" "${CHECKOUT_ONLY-unset}" "$CHOICE" "$EXPLICIT"',
        "audit", str(ROOT / "deploy/load-instance-env.sh"), str(code),
    ], env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["private" if compatibility else "unset", "instance", "process"]


def test_voice_uses_instance_models_and_persona(tmp_path, monkeypatch):
    import importlib
    from ava_bridge import settings
    monkeypatch.delenv("VOICE", raising=False)
    monkeypatch.setattr(settings, "models_dir", lambda: str(tmp_path))
    monkeypatch.setattr(settings, "persona_style", lambda: "Use a neutral tone.")
    import voice_ava
    importlib.reload(voice_ava)
    try:
        assert Path(voice_ava.VOICE).parent == tmp_path
        assert "Use a neutral tone." in voice_ava.PERSONA
        assert "locally and privately" not in voice_ava.PERSONA
    finally:
        monkeypatch.undo()
        importlib.reload(voice_ava)
