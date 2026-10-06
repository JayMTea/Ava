"""The provider contract. One implementation per wire protocol, not per vendor."""

from __future__ import annotations

import os
from typing import Any, Protocol

from ..types import Message, ModelResponse, ModelSpec, ToolSpec


class ModelProvider(Protocol):
    name: str

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
        """Run one model turn. Raise ProviderError (with a `kind`) on failure.

        Returned tool calls carry *wire* names (see types.wire_name); the runtime maps
        them back to tool ids. Assistant `raw` blocks must be returned so they can be
        replayed verbatim on the next turn.
        """
        ...


def provider_enabled(cfg: dict[str, Any]) -> tuple[bool, str]:
    """A provider is usable when not disabled and every `enabled_when_env` var is set."""
    if cfg.get("enabled") is False:
        return False, "disabled in its provider file"
    missing = [v for v in cfg.get("enabled_when_env", []) if not os.environ.get(v)]
    if missing:
        return False, f"missing env: {', '.join(missing)}"
    return True, "enabled"
