"""Run a command. Registered with `sandbox: required` and `side_effects: destructive`, and
denied to every agent in policies/tool-permissions.yaml until a project opts in. The
argv is never passed through a shell, so there is no shell-injection surface."""

from __future__ import annotations

import subprocess
from typing import Any

from runtime.types import ToolContext, ToolError


def run(args: dict[str, Any], ctx: ToolContext) -> str:
    argv = args["command"]
    sandbox = ctx.services.get("sandbox")
    if sandbox is None:
        raise ToolError("no sandbox available")
    wrapped = sandbox.wrap(argv, "shell.run")
    profile = sandbox.profile_for("shell.run")
    try:
        proc = subprocess.run(
            wrapped,
            capture_output=True,
            text=True,
            timeout=min(args.get("timeout_s", 60), profile.get("timeout_s", 60)),
            cwd=ctx.workspace,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {exc.timeout}s") from exc
    return f"exit_code: {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
