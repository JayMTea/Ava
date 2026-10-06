"""Model provider adapters. A provider `type` in models/providers/*.yaml selects the class."""

from __future__ import annotations

from typing import Any

from .anthropic import AnthropicProvider
from .base import ModelProvider, provider_enabled
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider

PROVIDER_TYPES: dict[str, type] = {
    "anthropic": AnthropicProvider,
    "openai_compatible": OpenAICompatibleProvider,
    "mock": MockProvider,
}


def build_provider(name: str, cfg: dict[str, Any]) -> ModelProvider:
    return PROVIDER_TYPES[cfg["type"]](name=name, cfg=cfg)


__all__ = [
    "PROVIDER_TYPES",
    "AnthropicProvider",
    "MockProvider",
    "ModelProvider",
    "OpenAICompatibleProvider",
    "build_provider",
    "provider_enabled",
]
