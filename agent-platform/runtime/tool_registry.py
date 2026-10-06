"""The catalog of every tool the platform can run: tools/registry.yaml plus any MCP tools.

The registry answers "what exists". Whether a given agent may *use* a tool is the
intersection of its agent.yaml `tools` list and policies/tool-permissions.yaml.
"""

from __future__ import annotations

import fnmatch
import importlib
from collections.abc import Callable
from typing import Any

from .policy_engine import PolicyEngine
from .types import AgentSpec, ToolSpec


class ToolRegistry:
    def __init__(self, specs: dict[str, ToolSpec]):
        self._specs = dict(specs)
        self._resolved: dict[str, Callable[..., Any]] = {}

    def __contains__(self, tool_id: str) -> bool:
        return tool_id in self._specs

    def get(self, tool_id: str) -> ToolSpec | None:
        return self._specs.get(tool_id)

    def ids(self) -> list[str]:
        return sorted(self._specs)

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.id] = spec

    def resolve(self, spec: ToolSpec) -> Callable[..., Any]:
        if callable(spec.implementation):
            return spec.implementation
        if spec.id not in self._resolved:
            module_name, _, attr = spec.implementation.partition(":")
            self._resolved[spec.id] = getattr(importlib.import_module(module_name), attr)
        return self._resolved[spec.id]

    def for_agent(self, agent: AgentSpec, policy: PolicyEngine) -> list[ToolSpec]:
        """Tools this agent asked for AND is granted, sorted by id.

        Sorted, stable output matters: the tool list is part of the prompt prefix, and any
        change in order invalidates the provider's prompt cache.
        """
        chosen = {
            tool_id
            for pattern in agent.tools
            for tool_id in self._specs
            if fnmatch.fnmatchcase(tool_id, pattern)
        }
        return [self._specs[t] for t in sorted(chosen) if policy.check_tool(agent.name, t).allowed]
