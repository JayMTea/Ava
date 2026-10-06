"""The single choke point every tool call passes through.

Order of checks (each one fails closed and returns an error *result* to the model rather
than raising - the model can then recover or explain):

  1. the tool exists and is in this agent's granted tool set
  2. hard policy (policies/tool-permissions.yaml) allows it
  3. the provider returned parseable arguments that validate against the tool's schema
  4. sandbox requirements are met
  5. approval is granted when policies/approvals.yaml requires it
  6. run with a timeout; truncate; redact secrets; wrap as untrusted data

Every call is traced, and every denial is written to the audit log.
"""

from __future__ import annotations

import concurrent.futures
import json
import time
from typing import Any

from jsonschema import Draft202012Validator

from .approval_manager import ApprovalManager, ApprovalRequest
from .policy_engine import PolicyEngine
from .telemetry import Telemetry
from .tool_registry import ToolRegistry
from .types import PolicyViolation, ToolCall, ToolContext, ToolError, ToolOutcome, ToolSpec


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        approvals: ApprovalManager,
        telemetry: Telemetry,
        sandbox_mode: str = "none",
    ):
        self.registry = registry
        self.policy = policy
        self.approvals = approvals
        self.telemetry = telemetry
        self.sandbox_mode = sandbox_mode

    def execute(self, call: ToolCall, allowed: list[ToolSpec], ctx: ToolContext) -> ToolOutcome:
        started = time.perf_counter()
        with self.telemetry.span(
            f"execute_tool {call.name}",
            **{"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": call.name, "gen_ai.tool.call.id": call.id, "gen_ai.agent.name": ctx.agent},
        ) as span:
            content, is_error = self._run(call, allowed, ctx)
            span["tool.is_error"] = is_error
            span["tool.output_chars"] = len(content)
        self.telemetry.metric("agent.tool.calls", 1, tool=call.name, error=is_error)
        return ToolOutcome(call.name, content, is_error, int((time.perf_counter() - started) * 1000))

    def _deny(self, ctx: ToolContext, tool_id: str, reason: str) -> tuple[str, bool]:
        self.telemetry.audit("tool_denied", agent=ctx.agent, tool=tool_id, task_id=ctx.task_id, reason=reason)
        return f"Tool call refused: {reason}", True

    def _run(self, call: ToolCall, allowed: list[ToolSpec], ctx: ToolContext) -> tuple[str, bool]:
        spec = next((s for s in allowed if s.id == call.name), None)
        if spec is None:
            return self._deny(ctx, call.name, f"{call.name!r} is not one of your tools")
        decision = self.policy.check_tool(ctx.agent, spec.id)
        if not decision.allowed:
            return self._deny(ctx, spec.id, decision.reason)
        if call.parse_error:
            return f"Tool call not executed: {call.parse_error}. Re-send the call with valid JSON arguments.", True
        errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(call.arguments), key=str)
        if errors:
            details = "; ".join(f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in errors[:5])
            return f"Invalid arguments for {spec.id}: {details}", True
        if spec.sandbox == "required" and self.sandbox_mode == "none":
            return self._deny(ctx, spec.id, "this tool requires a sandbox and sandbox/permissions.yaml mode is 'none'")
        if self.policy.requires_approval(spec):
            request = ApprovalRequest(ctx.agent, spec.id, spec.side_effects, call.arguments, ctx.task_id)
            if not self.approvals.request(request):
                return self._deny(ctx, spec.id, "a human did not approve this action")
        try:
            result = self._call_with_timeout(spec, call.arguments, ctx)
        except (ToolError, PolicyViolation) as exc:
            if isinstance(exc, PolicyViolation):
                self.telemetry.audit("policy_violation", agent=ctx.agent, tool=spec.id, task_id=ctx.task_id, reason=str(exc))
            return self.policy.redact(f"{type(exc).__name__}: {exc}"), True
        except concurrent.futures.TimeoutError:
            return f"{spec.id} timed out after {spec.timeout_s}s", True
        except Exception as exc:  # a tool bug must not crash the agent loop
            return self.policy.redact(f"{spec.id} failed unexpectedly: {type(exc).__name__}: {exc}"), True
        return self._format(spec, result), False

    def _call_with_timeout(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> Any:
        fn = self.registry.resolve(spec)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{spec.id}")
        try:
            return pool.submit(fn, args, ctx).result(timeout=spec.timeout_s)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _format(self, spec: ToolSpec, result: Any) -> str:
        text = result if isinstance(result, str) else json.dumps(result, indent=2, default=str, ensure_ascii=False)
        if len(text) > spec.max_output_chars:
            text = text[: spec.max_output_chars] + f"\n... [truncated {len(text) - spec.max_output_chars} chars]"
        text = self.policy.redact(text)
        if self.policy.wrap_untrusted:
            # Tool output is data, never instructions - the label tells the model so.
            text = f'<tool_output tool="{spec.id}" trust="untrusted">\n{text}\n</tool_output>'
        return text
