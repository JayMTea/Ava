"""Launch the adopted platform in its own Python environment.

The app and the reference platform both own a package named ``app``.
A subprocess with a separate interpreter and working directory
keeps their imports, dependencies and configuration independent.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from app.paths import INTEGRATION_ROOT, PLATFORM_ROOT, ROOT  # noqa: F401 -- public source locations


def command(args) -> int:
    interpreter = os.environ.get("AVA_PLATFORM_PYTHON")
    python = Path(interpreter).expanduser() if interpreter else (
        PLATFORM_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if not python.is_absolute():
        print("AVA_PLATFORM_PYTHON must be an absolute interpreter path.", file=sys.stderr)
        return 2
    if not python.is_file():
        print(
            "Install the agent platform environment first:\n"
            "  uv venv agent-platform/.venv\n"
            "  uv pip install --python agent-platform/.venv "
            "-e 'agent-platform[dev,openai]'\n"
            "See agent-platform/README.md.", file=sys.stderr,
        )
        return 2
    forwarded = list(args.platform_args) or ["agents"]
    if forwarded[0] == "check":
        entry = ["scripts/check.py", *forwarded[1:]]
    elif forwarded[0] == "evals":
        entry = ["-m", "evals.run", *forwarded[1:]]
    else:
        entry = ["-m", "app.cli", *forwarded]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    try:
        return subprocess.run([str(python), *entry], cwd=PLATFORM_ROOT, env=env).returncode
    except OSError as exc:
        print(f"Cannot start agent platform: {exc}", file=sys.stderr)
        return 2
