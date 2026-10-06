"""Built-in tools that call back into the platform (skills, memory, delegation).

They are registered in tools/registry.yaml like any other tool, so they pass through the
same policy, approval and tracing path - there is no side door.
"""

from __future__ import annotations

from typing import Any

from .types import ToolContext, ToolError


def skill_load(args: dict[str, Any], ctx: ToolContext) -> str:
    name = args["name"]
    if name not in ctx.services["agent_skills"]:
        raise ToolError(f"skill {name!r} is not available to {ctx.agent}")
    return ctx.services["skills"].load(name)


def memory_recall(args: dict[str, Any], ctx: ToolContext) -> Any:
    allowed = ctx.services["agent_memory_read"]
    namespaces = args.get("namespaces") or allowed
    denied = sorted(set(namespaces) - set(allowed))
    if denied:
        raise ToolError(f"{ctx.agent} may not read: {', '.join(denied)}")
    hits = ctx.services["memory"].recall(ctx.agent, args["query"], namespaces, args.get("k"))
    return [{"namespace": h["namespace"], "text": h["text"], "score": h["score"]} for h in hits] or "No matching memories."


def memory_remember(args: dict[str, Any], ctx: ToolContext) -> str:
    if args["namespace"] not in ctx.services["agent_memory_write"]:
        raise ToolError(f"{ctx.agent} may not write {args['namespace']!r}")
    entry = ctx.services["memory"].remember(ctx.agent, args["namespace"], args["text"], args.get("tags"), ctx.task_id)
    return f"Stored memory {entry['id']} in {entry['namespace']}."


def task_delegate(args: dict[str, Any], ctx: ToolContext) -> str:
    target = args["agent"]
    if target not in ctx.services["agent_delegates"]:
        raise ToolError(f"{ctx.agent} may not delegate to {target!r}")
    result = ctx.services["delegate"](target, args["task"], ctx)
    if result.status != "completed":
        raise ToolError(f"{target} did not complete the task ({result.status}): {result.error}")
    return result.output
