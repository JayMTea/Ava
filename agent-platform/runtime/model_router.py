"""Route model calls by *route name*, never by model id.

Agents ask for a route (`reasoning`, `balanced`, `fast`, ...). models/routing.yaml maps a
route to an ordered fallback chain of registry keys; models/registry.yaml maps a key to a
provider + model id. Swapping models is a config change, never a code change.

A candidate is skipped when its provider is disabled (missing credentials) or when the
agent's data classification exceeds the provider's `max_data_classification` - so
`restricted` work can only ever reach a provider you have declared safe for it.
"""

from __future__ import annotations

import os
from typing import Any

from .config import PlatformConfig
from .providers import ModelProvider, build_provider, provider_enabled
from .telemetry import Telemetry
from .types import DATA_CLASSES, Message, ModelResponse, ModelSpec, ProviderError, ToolSpec

ROUTE_OVERRIDE_ENV = "AGENT_ROUTE_OVERRIDE"


class ModelRouter:
    def __init__(
        self,
        config: PlatformConfig,
        telemetry: Telemetry,
        providers: dict[str, ModelProvider] | None = None,
        route_override: str | None = None,
    ):
        self.config = config
        self.telemetry = telemetry
        self._providers: dict[str, ModelProvider] = dict(providers or {})
        self.route_override = route_override or os.environ.get(ROUTE_OVERRIDE_ENV) or None
        if self.route_override and self.route_override not in config.routes:
            raise ProviderError(f"route override {self.route_override!r} is not in models/routing.yaml", kind="unavailable")

    def provider(self, name: str) -> ModelProvider:
        if name not in self._providers:
            self._providers[name] = build_provider(name, self.config.providers[name])
        return self._providers[name]

    def candidates(self, route: str, data_class: str) -> tuple[list[ModelSpec], list[str]]:
        """Return (usable models in order, reasons the others were skipped)."""
        route = self.route_override or route
        usable, skipped = [], []
        for key in self.config.routes.get(route, []):
            spec = self.config.models[key]
            cfg = self.config.providers[spec.provider]
            enabled, why = (True, "") if spec.provider in self._providers else provider_enabled(cfg)
            ceiling = cfg.get("max_data_classification", "internal")
            if not enabled:
                skipped.append(f"{key}: {why}")
            elif DATA_CLASSES.index(data_class) > DATA_CLASSES.index(ceiling):
                skipped.append(f"{key}: provider {spec.provider} is cleared for {ceiling}, task is {data_class}")
            else:
                usable.append(spec)
        return usable, skipped

    def complete(
        self,
        *,
        route: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        effort: str | None,
        max_output_tokens: int | None,
        data_class: str,
        agent: str,
    ) -> ModelResponse:
        usable, skipped = self.candidates(route, data_class)
        errors: list[str] = []
        for spec in usable:
            eff = (effort or spec.default_effort) if spec.supports_effort else None
            limit = min(max_output_tokens or spec.max_output_tokens, spec.max_output_tokens)
            attrs: dict[str, Any] = {
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": spec.provider,
                "gen_ai.request.model": spec.model,
                "gen_ai.request.max_tokens": limit,
                "gen_ai.agent.name": agent,
                "agent.route": self.route_override or route,
            }
            try:
                with self.telemetry.span(f"chat {spec.model}", **attrs) as span:
                    response = self.provider(spec.provider).complete(
                        model=spec,
                        system=system,
                        messages=messages,
                        tools=tools,
                        effort=eff,
                        max_output_tokens=limit,
                    )
                    span.update(
                        {
                            "gen_ai.response.model": response.model,
                            "gen_ai.response.finish_reasons": [response.stop_reason],
                            "gen_ai.usage.input_tokens": response.usage.input_tokens,
                            "gen_ai.usage.output_tokens": response.usage.output_tokens,
                        }
                    )
                self.telemetry.metric("gen_ai.client.token.usage", response.usage.input_tokens, type="input", model=spec.key)
                self.telemetry.metric("gen_ai.client.token.usage", response.usage.output_tokens, type="output", model=spec.key)
                return response
            except ProviderError as exc:
                errors.append(f"{spec.key}: {exc}")
                if exc.kind in self.config.fallback_on or exc.kind == "unavailable":
                    continue
                raise
        detail = "; ".join(errors + skipped) or "route has no models"
        raise ProviderError(f"no model available for route {self.route_override or route!r}: {detail}", kind="unavailable")
