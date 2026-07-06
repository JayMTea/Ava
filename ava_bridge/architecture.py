"""Architecture capability — let Ava read and update her own system's SSOT.

The architecture manifest (`agent/docs/architecture.yaml`) + generator/validator
(`agent/docs/arch.py`) live on the host; Ava's MCP tools call back here (the same
token-gated host-callback pattern the document tools use) to read the manifest,
run a drift check, regenerate the diagrams, or apply an edit. `arch.py` does the
real work (and the auto-commit); this module is a thin subprocess wrapper so the
bridge never reimplements the pipeline.
"""
import json
import os
import subprocess
import sys

from . import config

ARCH_PY = os.path.join(config.ROOT, "agent", "docs", "arch.py")
MANIFEST = os.path.join(config.ROOT, "agent", "docs", "architecture.yaml")


def _env() -> dict:
    env = dict(os.environ)
    # systemd user services don't carry ~/.local/bin (where d2 lives) on PATH.
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")
    return env


def _run(args: list[str], stdin: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, ARCH_PY, *args],
        cwd=config.ROOT, env=_env(), input=stdin,
        capture_output=True, text=True, timeout=timeout,
    )


def _json(args: list[str], timeout: int = 120) -> dict:
    cp = _run([*args, "--json"], timeout=timeout)
    out = (cp.stdout or "").strip()
    try:
        return json.loads(out.splitlines()[-1]) if out else {"error": (cp.stderr or "no output").strip()}
    except (json.JSONDecodeError, IndexError):
        return {"error": (cp.stderr or cp.stdout or "arch.py failed").strip()[:500]}


def raw_manifest() -> str:
    with open(MANIFEST, encoding="utf-8") as f:
        return f.read()


def summary_payload() -> dict:
    """Full architecture snapshot + the exact YAML so Ava can read or edit it."""
    return {"summary": _json(["summary"]), "manifest_yaml": raw_manifest()}


def describe_payload(name: str) -> dict:
    return _json(["describe", name])


def check_payload() -> dict:
    return _json(["check"])


def sync_payload(commit: bool = True, message: str | None = None) -> dict:
    args = ["sync"]
    if commit:
        args.append("--commit")
    if message:
        args += ["--message", message]
    cp = _run(args, timeout=180)
    try:
        return json.loads((cp.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": cp.returncode == 0, "output": (cp.stdout + cp.stderr).strip()[-800:]}


def update_payload(new_yaml: str, message: str | None = None, commit: bool = True) -> dict:
    args = ["update"]
    if not commit:
        args.append("--no-commit")
    if message:
        args += ["--message", message]
    cp = _run(args, stdin=new_yaml, timeout=180)
    try:
        return json.loads((cp.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"ok": cp.returncode == 0, "output": (cp.stdout + cp.stderr).strip()[-800:]}
