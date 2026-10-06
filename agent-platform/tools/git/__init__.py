"""Read-only git inspection of the workspace repository.

Refs and paths are validated so a model cannot smuggle options: `git diff --output=FILE`
would otherwise turn a read-only tool into a file writer.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any

from runtime.types import ToolContext, ToolError

SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]*$")


def _git(ctx: ToolContext, *argv: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(ctx.workspace), "--no-pager", *argv],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError("git is not installed") from exc
    if proc.returncode != 0:
        raise ToolError(proc.stderr.strip() or f"git exited {proc.returncode}")
    return proc.stdout


def _ref(value: str) -> str:
    # Must start with an alphanumeric, so it can never be parsed as an option.
    if not SAFE_REF.match(value):
        raise ToolError(f"unsafe ref {value!r}")
    return value


def status(args: dict[str, Any], ctx: ToolContext) -> str:
    return _git(ctx, "status", "--short", "--branch") or "clean"


def diff(args: dict[str, Any], ctx: ToolContext) -> str:
    argv = ["diff", "--no-color", "--no-ext-diff"]
    if args.get("staged"):
        argv.append("--cached")
    if args.get("base"):
        argv.append(_ref(args["base"]))
    if args.get("path"):
        argv += ["--", str(ctx.policy.resolve_path(args["path"], "read").relative_to(ctx.workspace).as_posix())]
    return _git(ctx, *argv) or "no changes"


def log(args: dict[str, Any], ctx: ToolContext) -> str:
    n = max(1, min(int(args.get("limit", 20)), 200))
    return _git(ctx, "log", f"-{n}", "--date=short", "--pretty=format:%h %ad %an %s")
