"""Deterministic, offline provider for tests, evals and demos. Never touches a network.

Without a script it answers every turn with a short echo of the task. With a script it
plays back the given turns in order, which is how tests and eval scenarios drive the
runtime through tool calls, policy denials and approvals without a real model.

Script items are dicts: {"text": str, "tool_calls": [{"name": "<tool id>", "arguments": {...}}],
"stop_reason": "end_turn" | "tool_use" | "max_tokens" | "refusal"}.
"""

from __future__ import annotations

import re
from typing import Any

from ..types import Message, ModelResponse, ModelSpec, ToolCall, ToolSpec, Usage, wire_name


class MockProvider:
    def __init__(self, name: str = "mock", cfg: dict[str, Any] | None = None, script: list[Any] | None = None):
        self.name = name
        self.cfg = cfg or {}
        self.script: list[Any] = list(script or [])
        self.calls: list[dict[str, Any]] = []  # every request, for assertions
        self._counter = 0

    def push(self, *items: Any) -> None:
        self.script.extend(items)

    def complete(
        self,
        *,
        model: ModelSpec,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        effort: str | None,
        max_output_tokens: int,
    ) -> ModelResponse:
        self.calls.append(
            {"model": model.key, "system": system, "messages": list(messages), "tools": [t.id for t in tools]}
        )
        usage = Usage(input_tokens=sum(len(m.content) for m in messages) // 4, output_tokens=8)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, ModelResponse):
                return item
            calls = []
            for call in item.get("tool_calls", []):
                self._counter += 1
                calls.append(ToolCall(id=f"mock_call_{self._counter}", name=wire_name(call["name"]), arguments=call.get("arguments", {})))
            stop = item.get("stop_reason") or ("tool_use" if calls else "end_turn")
            return ModelResponse(
                text=item.get("text", ""),
                tool_calls=calls,
                stop_reason=stop,
                usage=usage,
                model=model.model,
                provider=self.name,
                refusal_category=item.get("refusal_category"),
            )
        task = next((m.content for m in reversed(messages) if m.role == "user"), "")
        summary = " ".join(re.sub(r"<[^>]+>", " ", task).split())[:160]
        return ModelResponse(
            text=f"[mock:{model.key}] {summary}",
            tool_calls=[],
            stop_reason="end_turn",
            usage=usage,
            model=model.model,
            provider=self.name,
        )
