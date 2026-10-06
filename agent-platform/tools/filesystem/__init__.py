"""Workspace file access. Every path goes through PolicyEngine.resolve_path, which confines
it to the workspace and applies policies/data-access.yaml (secrets and state/ are denied;
writes are allowed only under artifacts/ by default)."""

from __future__ import annotations

from typing import Any

from runtime.types import ToolContext, ToolError

MAX_READ_CHARS = 100_000


def read(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.policy.resolve_path(args["path"], "read")
    if not path.is_file():
        raise ToolError(f"{args['path']} is not a file")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{args['path']} is not UTF-8 text") from exc
    limit = min(args.get("max_chars", MAX_READ_CHARS), MAX_READ_CHARS)
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated; file has {len(text)} chars]"


def list_dir(args: dict[str, Any], ctx: ToolContext) -> list[str]:
    root = ctx.policy.resolve_path(args.get("path", "."), "read")
    if not root.is_dir():
        raise ToolError(f"{args.get('path', '.')} is not a directory")
    pattern = "**/*" if args.get("recursive", False) else "*"
    limit = args.get("max_entries", 200)
    out: list[str] = []
    for p in sorted(root.glob(pattern)):
        rel = p.relative_to(ctx.workspace).as_posix()
        try:
            ctx.policy.resolve_path(rel, "read")
        except Exception:
            continue  # hide what the agent may not read
        out.append(rel + ("/" if p.is_dir() else ""))
        if len(out) >= limit:
            out.append(f"... [stopped at {limit} entries]")
            break
    return out


def write(args: dict[str, Any], ctx: ToolContext) -> str:
    path = ctx.policy.resolve_path(args["path"], "write")
    if path.exists() and not args.get("overwrite", False):
        raise ToolError(f"{args['path']} exists; pass overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(args["content"], encoding="utf-8")
    rel = path.relative_to(ctx.workspace).as_posix()
    tasks = ctx.services.get("tasks")
    if tasks is not None and rel.startswith("artifacts/") and ctx.task_id.startswith("task_"):
        tasks.record_artifact(ctx.task_id, ctx.agent, path, ctx.services.get("artifact_type", "report"), args.get("description", ""))
    return f"Wrote {len(args['content'])} chars to {rel}"
