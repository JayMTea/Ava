"""The agent loop: model turn -> tool calls -> results -> repeat, under hard limits.

One `run()` is one task for one agent. The loop:
  1. builds the system prompt and first message (ContextEngine),
  2. asks the router for a model turn on the agent's route,
  3. stops on a final answer, a refusal, or any limit in policies/safety.yaml,
  4. otherwise executes each tool call through the ToolExecutor (the only path to tools),
  5. checkpoints after every turn so `resume()` can pick up after a crash.

Errors from the model or a limit end the task as `failed` with a reason - they never
crash the caller. History is append-only (see context_engine.py for why).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .checkpoint_manager import CheckpointManager
from .config import PlatformConfig
from .context_engine import ContextEngine
from .memory_manager import MemoryManager
from .model_router import ModelRouter
from .policy_engine import PolicyEngine
from .sandbox import Sandbox
from .skill_registry import SkillRegistry
from .task_manager import TERMINAL, TaskManager
from .telemetry import Telemetry
from .tool_executor import ToolExecutor
from .tool_registry import ToolRegistry
from .types import AgentSpec, Message, ProviderError, RunResult, ToolCall, ToolContext, Usage, wire_name

TRUNCATED_CALL = (
    "Not executed: your response hit max_tokens, so this call's input may be truncated. "
    "Re-issue it, splitting large inputs if needed."
)
INTERRUPTED_CALL = "Not executed: the run was interrupted before this call ran. Re-issue it if it is still needed."


class AgentRuntime:
    def __init__(
        self,
        *,
        config: PlatformConfig,
        policy: PolicyEngine,
        router: ModelRouter,
        registry: ToolRegistry,
        executor: ToolExecutor,
        context: ContextEngine,
        tasks: TaskManager,
        checkpoints: CheckpointManager,
        telemetry: Telemetry,
        memory: MemoryManager,
        skills: SkillRegistry,
        sandbox: Sandbox,
        session_id: str,
        delegate: Callable[..., RunResult],
    ):
        self.config = config
        self.policy = policy
        self.router = router
        self.registry = registry
        self.executor = executor
        self.context = context
        self.tasks = tasks
        self.checkpoints = checkpoints
        self.telemetry = telemetry
        self.memory = memory
        self.skills = skills
        self.sandbox = sandbox
        self.session_id = session_id
        self.delegate = delegate

    # -- public ----------------------------------------------------------------------------

    def run(
        self,
        agent_name: str,
        task_input: str,
        *,
        inputs: dict[str, Any] | None = None,
        parent_task_id: str | None = None,
        depth: int = 0,
        workflow: str | None = None,
    ) -> RunResult:
        agent = self.config.agents[agent_name]
        task = self.tasks.create(agent.name, task_input, session_id=self.session_id, parent_id=parent_task_id, workflow=workflow)
        messages = [Message("user", self.context.first_message(agent, task_input, inputs))]
        return self._loop(agent, task["id"], messages, Usage(), turn=0, tool_calls=0, depth=depth)

    def resume(self, task_id: str) -> RunResult:
        task = self.tasks.get(task_id)
        agent = self.config.agents[task["agent"]]
        if task["status"] in TERMINAL:
            usage = Usage(**task["usage"])
            return RunResult(task_id, agent.name, task["status"], task["result"] or "", usage, 0, 0, task["error"])
        checkpoint = self.checkpoints.latest(task_id)
        if checkpoint is None:
            messages = [Message("user", self.context.first_message(agent, task["input"]))]
            return self._loop(agent, task_id, messages, Usage(), turn=0, tool_calls=0, depth=0)
        messages: list[Message] = checkpoint["messages"]
        last = messages[-1]
        if last.role == "assistant" and last.tool_calls:
            # Never silently re-run side effects: tell the model the calls did not happen.
            messages += [Message("tool", INTERRUPTED_CALL, tool_call_id=c.id, is_error=True) for c in last.tool_calls]
        return self._loop(agent, task_id, messages, checkpoint["usage"], checkpoint["turn"], checkpoint["tool_calls"], depth=0)

    # -- loop ------------------------------------------------------------------------------

    def _limits(self, agent: AgentSpec) -> tuple[int, int, float]:
        hard = self.policy.limits

        def lowest(agent_value: int | None, key: str, default: int) -> int:
            values = [v for v in (agent_value, hard.get(key)) if v]
            return min(values) if values else default

        return (
            lowest(agent.max_turns, "max_turns", 25),
            lowest(agent.max_tool_calls, "max_tool_calls", 100),
            float(hard.get("max_wall_clock_s", 900)),
        )

    def _tool_context(self, agent: AgentSpec, task_id: str, depth: int) -> ToolContext:
        return ToolContext(
            workspace=self.config.root,
            agent=agent.name,
            task_id=task_id,
            session_id=self.session_id,
            policy=self.policy,
            services={
                "skills": self.skills,
                "agent_skills": set(agent.skills),
                "memory": self.memory,
                "agent_memory_read": list(agent.memory_read),
                "agent_memory_write": list(agent.memory_write),
                "agent_delegates": set(agent.delegates_to),
                "delegate": lambda target, text, _ctx: self.delegate(target, text, parent_task_id=task_id, depth=depth + 1),
                "sandbox": self.sandbox,
                "tasks": self.tasks,
                "artifact_type": agent.output.get("artifact_type", "report"),
            },
        )

    def _loop(self, agent: AgentSpec, task_id: str, messages: list[Message], usage: Usage, turn: int, tool_calls: int, depth: int) -> RunResult:
        max_turns, max_tool_calls, max_wall = self._limits(agent)
        allowed = self.registry.for_agent(agent, self.policy)
        # Map every registered tool, not just allowed ones, so a refusal names the real tool id.
        by_wire = {wire_name(t): t for t in self.registry.ids()}
        system = self.context.system_prompt(agent)
        ctx = self._tool_context(agent, task_id, depth)
        started = time.monotonic()
        status, output, error = "failed", "", None
        self.tasks.update(task_id, status="running")

        try:
            with self.telemetry.span(
                f"invoke_agent {agent.name}",
                **{"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": agent.name, "agent.task_id": task_id, "agent.depth": depth},
            ) as span:
                while True:
                    if turn >= max_turns:
                        error = f"stopped: reached max_turns={max_turns}"
                        break
                    if time.monotonic() - started > max_wall:
                        error = f"stopped: exceeded max_wall_clock_s={max_wall:.0f}"
                        break
                    try:
                        response = self.router.complete(
                            route=agent.route,
                            system=system,
                            messages=messages,
                            tools=allowed,
                            effort=agent.effort,
                            max_output_tokens=agent.max_output_tokens,
                            data_class=agent.data_classification,
                            agent=agent.name,
                        )
                    except ProviderError as exc:
                        error = str(exc)
                        break
                    turn += 1
                    usage.add(response.usage)
                    calls = [ToolCall(c.id, by_wire.get(c.name, c.name), c.arguments, c.parse_error) for c in response.tool_calls]
                    messages.append(Message("assistant", response.text, calls, raw=response.raw, raw_provider=response.provider))

                    if response.stop_reason == "refusal":
                        # A refusal can cut a tool call off mid-input: never run that turn's tools.
                        error = f"model declined the request (category: {response.refusal_category or 'unspecified'})"
                        self.telemetry.audit("refusal", agent=agent.name, task_id=task_id, category=response.refusal_category)
                        break
                    if not calls:
                        output = response.text
                        if response.stop_reason == "max_tokens":
                            error = "final answer truncated at max_tokens; raise model.max_output_tokens"
                        else:
                            status = "completed"
                        self.checkpoints.save(task_id, turn, messages, usage, tool_calls)
                        break
                    if response.stop_reason == "max_tokens":
                        messages += [Message("tool", TRUNCATED_CALL, tool_call_id=c.id, is_error=True) for c in calls]
                    elif tool_calls + len(calls) > max_tool_calls:
                        error = f"stopped: tool-call budget max_tool_calls={max_tool_calls} exhausted"
                        break
                    else:
                        for call in calls:
                            outcome = self.executor.execute(call, allowed, ctx)
                            tool_calls += 1
                            messages.append(Message("tool", outcome.content, tool_call_id=call.id, is_error=outcome.is_error))
                    self.checkpoints.save(task_id, turn, messages, usage, tool_calls)
                span.update(
                    {
                        "agent.status": status,
                        "agent.turns": turn,
                        "agent.tool_calls": tool_calls,
                        "gen_ai.usage.input_tokens": usage.input_tokens,
                        "gen_ai.usage.output_tokens": usage.output_tokens,
                    }
                )
        except Exception as exc:
            self.tasks.finish(task_id, "failed", result=None, error=f"{type(exc).__name__}: {exc}", usage=usage)
            raise
        self.tasks.finish(task_id, status, result=output or None, error=error, usage=usage)
        return RunResult(task_id, agent.name, status, output, usage, turn, tool_calls, error, messages)
