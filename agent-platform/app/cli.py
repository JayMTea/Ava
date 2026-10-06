"""Command-line surface for the platform. Run from the platform root:

    python -m app.cli validate
    python -m app.cli agents
    python -m app.cli run "Summarize knowledge/README.md" --route test
    python -m app.cli workflow <name> --input key=value
    python -m app.cli resume <task_id>
    python -m app.cli tasks
    python -m app.cli memory consolidate

`--route test` (or AGENT_ROUTE_OVERRIDE=test) uses the offline mock model - no API keys needed.
`--root DIR` runs against another platform root (e.g. the template's examples/reference).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from runtime import InteractiveApprover, Platform, validate_platform
from runtime.telemetry import configure_logging

ROOT = Path(__file__).resolve().parent.parent


def _platform(args: argparse.Namespace) -> Platform:
    approver = InteractiveApprover() if sys.stdin.isatty() else None  # unattended -> policy decides (deny)
    platform = Platform.load(args.root, approver=approver, route_override=getattr(args, "route", None))
    configure_logging(platform.config.observability.get("logging", {}), platform.policy.redact)
    return platform


def _print_result(result) -> int:
    print(result.output or "(no output)")
    print(
        f"\n[{result.status}] task={result.task_id} agent={result.agent} turns={result.turns} "
        f"tool_calls={result.tool_calls} tokens_in={result.usage.input_tokens} tokens_out={result.usage.output_tokens}",
        file=sys.stderr,
    )
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    return 0 if result.status == "completed" else 1


def cmd_validate(args: argparse.Namespace) -> int:
    problems, warnings = validate_platform(args.root)
    for w in warnings:
        print(f"warning: {w}")
    for p in problems:
        print(f"error: {p}")
    print("platform config OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_agents(args: argparse.Namespace) -> int:
    platform = _platform(args)
    for agent in platform.config.agents.values():
        tools = [t.id for t in platform.registry.for_agent(agent, platform.policy)]
        print(f"{agent.name}  route={agent.route}  data={agent.data_classification}\n  {agent.description}\n  tools: {', '.join(tools)}\n")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    return _print_result(_platform(args).run(args.task, agent=args.agent))


def cmd_resume(args: argparse.Namespace) -> int:
    return _print_result(_platform(args).resume(args.task_id))


def cmd_workflow(args: argparse.Namespace) -> int:
    inputs = dict(item.split("=", 1) for item in args.input)
    result = _platform(args).run_workflow(args.name, inputs)
    print(result.output or "(no output)")
    print(f"\n[{result.status}] workflow={result.name} failed_step={result.failed_step}", file=sys.stderr)
    return 0 if result.status == "completed" else 1


def cmd_tasks(args: argparse.Namespace) -> int:
    for task in _platform(args).tasks.list(args.limit):
        print(f"{task['id']}  {task['status']:<10} {task['agent']:<14} {task['input'][:60]!r}")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    platform = _platform(args)
    stats = platform.memory.consolidate()
    days = platform.config.memory.get("consolidation", {}).get("session_retention_days", 14)
    stats["sessions_pruned"] = platform.memory.prune_sessions(days)
    print(json.dumps(stats))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(ROOT), help="platform root (directory with agent.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="validate every config file and cross-reference").set_defaults(fn=cmd_validate)

    p = sub.add_parser("agents", help="list agents and their effective tools")
    p.set_defaults(fn=cmd_agents)

    p = sub.add_parser("run", help="run one task")
    p.add_argument("task")
    p.add_argument("--agent", help="defaults to agent.yaml default_agent")
    p.add_argument("--route", help="force every model call onto this route (e.g. test)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="resume an interrupted task from its last checkpoint")
    p.add_argument("task_id")
    p.add_argument("--route")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("workflow", help="run a workflow from workflows/")
    p.add_argument("name")
    p.add_argument("--input", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--route")
    p.set_defaults(fn=cmd_workflow)

    p = sub.add_parser("tasks", help="list recent tasks")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("memory", help="memory housekeeping")
    p.add_argument("action", choices=["consolidate"])
    p.set_defaults(fn=cmd_memory)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
