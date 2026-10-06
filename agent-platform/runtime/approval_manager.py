"""Human-in-the-loop approval for tool calls, driven by policies/approvals.yaml.

The policy decides *whether* a call needs approval (by side-effect class or tool id).
An Approver decides *how* it is answered: a terminal prompt, a chat UI, a ticket queue.
With no approver attached, `non_interactive` in approvals.yaml decides - and it must
stay `deny` outside tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .policy_engine import PolicyEngine
from .telemetry import Telemetry


@dataclass
class ApprovalRequest:
    agent: str
    tool_id: str
    side_effects: str
    arguments: dict[str, Any]
    task_id: str


class Approver(Protocol):
    def decide(self, request: ApprovalRequest) -> bool: ...


class DenyAllApprover:
    """Default for unattended runs: anything that needs approval is refused."""

    def decide(self, request: ApprovalRequest) -> bool:
        return False


class AllowListApprover:
    """Approves only the listed tool ids. For tests, evals and tightly scoped automation."""

    def __init__(self, tool_ids: set[str]):
        self.tool_ids = set(tool_ids)

    def decide(self, request: ApprovalRequest) -> bool:
        return request.tool_id in self.tool_ids


class InteractiveApprover:
    """Asks on the terminal. Swap for your UI's approval flow in app/."""

    def __init__(self, ask: Callable[[str], str] = input):
        self.ask = ask

    def decide(self, request: ApprovalRequest) -> bool:
        args = json.dumps(request.arguments, indent=2, default=str)
        prompt = (
            f"\n[approval] agent '{request.agent}' wants to run {request.tool_id} "
            f"(side effects: {request.side_effects})\n{args}\nApprove? [y/N] "
        )
        return self.ask(prompt).strip().lower() in ("y", "yes")


class ApprovalManager:
    def __init__(self, policy: PolicyEngine, telemetry: Telemetry, approver: Approver | None = None):
        self.policy = policy
        self.telemetry = telemetry
        self.approver = approver

    def request(self, request: ApprovalRequest) -> bool:
        if self.approver is not None:
            approved = bool(self.approver.decide(request))
            via = type(self.approver).__name__
        else:
            approved = self.policy.approvals.get("non_interactive", "deny") == "allow"
            via = "non_interactive policy"
        self.telemetry.audit(
            "approval",
            agent=request.agent,
            tool=request.tool_id,
            side_effects=request.side_effects,
            task_id=request.task_id,
            approved=approved,
            via=via,
        )
        return approved
