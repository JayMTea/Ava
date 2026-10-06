"""The platform facade: wires every component together and coordinates agents.

    from runtime import Platform
    platform = Platform.load(".")                       # validates all config first
    result = platform.run("Summarize knowledge/architecture")
    wf = platform.run_workflow("research", {"question": "..."})

Coordination happens two ways, and they are deliberately different:
- Delegation (model-driven): an agent with `delegates_to` calls the task.delegate tool;
  the sub-agent starts with a fresh context and only the task text it was given.
- Workflows (code-driven): workflows/*.yaml run fixed steps in order. Prefer a workflow
  whenever the steps are known in advance - it is cheaper, testable and auditable.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_runtime import AgentRuntime
from .approval_manager import ApprovalManager, ApprovalRequest, Approver
from .checkpoint_manager import CheckpointManager
from .config import TEMPLATE_REF, PlatformConfig, load_platform
from .context_engine import ContextEngine
from .mcp_bridge import register_mcp_tools
from .memory_manager import MemoryManager
from .model_router import ModelRouter
from .policy_engine import PolicyEngine
from .providers import ModelProvider
from .sandbox import Sandbox
from .skill_registry import SkillRegistry
from .task_manager import TaskManager
from .telemetry import Telemetry
from .tool_executor import ToolExecutor
from .tool_registry import ToolRegistry
from .types import RunResult, ToolCall, ToolContext, ToolError


@dataclass
class WorkflowResult:
    name: str
    status: str  # completed | failed | rejected
    output: str
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_step: str | None = None


def render(template: Any, scope: dict[str, Any]) -> Any:
    """Fill {{ inputs.x }} / {{ steps.id.output }} placeholders, recursively."""
    if isinstance(template, dict):
        return {k: render(v, scope) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, scope) for v in template]
    if not isinstance(template, str):
        return template

    def lookup(match: re.Match[str]) -> str:
        value: Any = scope
        for part in match.group(1).split("."):
            value = value.get(part, "") if isinstance(value, dict) else ""
        return str(value)

    return TEMPLATE_REF.sub(lookup, template)


class Platform:
    def __init__(
        self,
        config: PlatformConfig,
        *,
        state_dir: str | Path | None = None,
        approver: Approver | None = None,
        providers: dict[str, ModelProvider] | None = None,
        route_override: str | None = None,
        session_id: str | None = None,
    ):
        self.config = config
        self.root = config.root
        self.state_dir = Path(state_dir) if state_dir else self.root / "state"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.session_id = session_id or f"sess_{stamp}_{uuid.uuid4().hex[:6]}"

        self.policy = PolicyEngine(config.policies, self.root)
        self.telemetry = Telemetry(config.observability, self.state_dir / "sessions" / self.session_id, self.policy.redact)
        self.registry = ToolRegistry(config.tools)
        wanted = {s for agent in config.agents.values() for s in agent.mcp_servers}
        if wanted:
            register_mcp_tools(self.registry, config.mcp, wanted)
        self.approvals = ApprovalManager(self.policy, self.telemetry, approver)
        self.sandbox = Sandbox(config.sandbox, self.root)
        self.executor = ToolExecutor(self.registry, self.policy, self.approvals, self.telemetry, self.sandbox.mode)
        self.router = ModelRouter(config, self.telemetry, providers, route_override)
        self.memory = MemoryManager(config.memory, self.state_dir, self.policy, self.session_id)
        self.skills = SkillRegistry(config.skills, self.root)
        self.context = ContextEngine(
            config.instructions,
            self.skills,
            self.memory,
            config.agents,
            auto_recall=config.memory.get("retrieval", {}).get("auto_recall", True),
        )
        self.tasks = TaskManager(self.state_dir, config.schemas_dir, self.root)
        self.checkpoints = CheckpointManager(self.state_dir)
        self.runtime = AgentRuntime(
            config=config,
            policy=self.policy,
            router=self.router,
            registry=self.registry,
            executor=self.executor,
            context=self.context,
            tasks=self.tasks,
            checkpoints=self.checkpoints,
            telemetry=self.telemetry,
            memory=self.memory,
            skills=self.skills,
            sandbox=self.sandbox,
            session_id=self.session_id,
            delegate=self._delegate,
        )

    @classmethod
    def load(cls, root: str | Path = ".", **kwargs: Any) -> Platform:
        return cls(load_platform(root), **kwargs)

    # -- agents ----------------------------------------------------------------------------

    def run(self, task: str, agent: str | None = None, inputs: dict[str, Any] | None = None) -> RunResult:
        return self.runtime.run(agent or self.config.manifest["default_agent"], task, inputs=inputs)

    def resume(self, task_id: str) -> RunResult:
        return self.runtime.resume(task_id)

    def _delegate(self, target: str, task: str, *, parent_task_id: str, depth: int) -> RunResult:
        max_depth = self.policy.limits.get("max_delegation_depth", 2)
        if depth > max_depth:
            raise ToolError(f"delegation depth {depth} exceeds max_delegation_depth={max_depth}")
        with self.telemetry.span(f"delegate {target}", **{"agent.delegate.target": target, "agent.depth": depth}):
            return self.runtime.run(target, task, parent_task_id=parent_task_id, depth=depth)

    # -- workflows -------------------------------------------------------------------------

    def run_workflow(self, name: str, inputs: dict[str, Any] | None = None) -> WorkflowResult:
        wf = self.config.workflows[name]
        inputs = dict(inputs or {})
        values: dict[str, Any] = {}
        for key, spec in (wf.get("inputs") or {}).items():
            if key in inputs:
                values[key] = inputs[key]
            elif "default" in spec:
                values[key] = spec["default"]
            elif spec.get("required", True):
                raise ValueError(f"workflow {name!r} needs input {key!r}")
        scope: dict[str, Any] = {"inputs": values, "steps": {}}

        with self.telemetry.span(f"workflow {name}", **{"agent.workflow": name}):
            for step in wf["steps"]:
                sid = step["id"]
                with self.telemetry.span(f"workflow_step {sid}", **{"agent.workflow.step": sid}):
                    result = self._run_step(name, step, scope)
                scope["steps"][sid] = result
                if result["status"] == "rejected":
                    return WorkflowResult(name, "rejected", result["output"], scope["steps"], sid)
                if result["status"] != "completed" and not step.get("continue_on_error", False):
                    return WorkflowResult(name, "failed", result["output"], scope["steps"], sid)
        last = wf["steps"][-1]["id"]
        output = render(wf.get("output", f"{{{{ steps.{last}.output }}}}"), scope)
        return WorkflowResult(name, "completed", output, scope["steps"])

    def _run_step(self, workflow: str, step: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
        if "agent" in step:
            r = self.runtime.run(step["agent"], render(step["input"], scope), workflow=workflow)
            return {"status": r.status, "output": r.output if r.status == "completed" else (r.error or ""), "task_id": r.task_id}
        if "tool" in step:
            spec = self.registry.get(step["tool"])
            ctx = ToolContext(self.root, step["as_agent"], f"workflow:{workflow}", self.session_id, self.policy, {"tasks": self.tasks})
            call = ToolCall(id=f"wf_{step['id']}", name=step["tool"], arguments=render(step.get("arguments", {}), scope))
            outcome = self.executor.execute(call, [spec] if spec else [], ctx)
            return {"status": "failed" if outcome.is_error else "completed", "output": outcome.content}
        gate = step["approval"]
        request = ApprovalRequest(
            agent=f"workflow:{workflow}",
            tool_id=f"workflow.{workflow}.{step['id']}",
            side_effects=gate.get("side_effects", "external_write"),
            arguments={"message": render(gate["message"], scope)},
            task_id="",
        )
        approved = self.approvals.request(request)
        return {"status": "completed" if approved else "rejected", "output": "approved" if approved else "rejected by approver"}
