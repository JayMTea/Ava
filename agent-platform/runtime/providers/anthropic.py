"""Claude via the official Anthropic SDK (Messages API, manual tool loop).

Behaviour that matters on current Claude models, and why this adapter does it:
- Streams every request (`messages.stream` + `get_final_message`) so large max_tokens
  values never hit HTTP timeouts.
- Replays assistant turns as the raw content blocks the API returned. Thinking blocks are
  bound to the conversation; rebuilding them from text breaks the next request.
- Sends no `thinking` and no sampling params: current models think adaptively by default
  and reject `budget_tokens` / `temperature`. Depth is controlled with `output_config.effort`.
- Never forces `tool_choice` (current models reject `any` / `tool`).
- Opts into server-side refusal fallback per model (registry option `refusal_fallback`).
- With eager input streaming on, tool inputs are not validated server-side; the tool
  executor validates every input against its schema before running anything.
Non-core request fields go through `extra_body` so the adapter works across SDK versions.
"""

from __future__ import annotations

import os
from typing import Any

from ..types import Message, ModelResponse, ModelSpec, ProviderError, ToolCall, ToolSpec, Usage, wire_name

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # pairs with fallbacks="default"
STOP_MAP = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return block
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return block.model_dump(exclude_none=True)


def _sanitize_fallback(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """After a mid-output refusal fallback, only text survives from before the boundary."""
    boundary = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if boundary is None:
        return blocks
    return [b for i, b in enumerate(blocks) if i >= boundary or b.get("type") == "text"]


def to_anthropic_messages(messages: list[Message], provider: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    def flush() -> None:
        # All results for one assistant turn go back in ONE user message (keeps parallel calls working).
        if results:
            out.append({"role": "user", "content": list(results)})
            results.clear()

    for msg in messages:
        if msg.role == "tool":
            block: dict[str, Any] = {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.content}
            if msg.is_error:
                block["is_error"] = True
            results.append(block)
            continue
        flush()
        if msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        elif msg.raw is not None and msg.raw_provider == provider:
            out.append({"role": "assistant", "content": msg.raw})
        else:
            content: list[dict[str, Any]] = [{"type": "text", "text": msg.content}] if msg.content else []
            content += [
                {"type": "tool_use", "id": c.id, "name": wire_name(c.name), "input": c.arguments}
                for c in msg.tool_calls
            ]
            out.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no output)"}]})
    flush()
    return out


def from_anthropic_message(message: Any, provider: str) -> ModelResponse:
    blocks = _sanitize_fallback([_block_to_dict(b) for b in message.content])
    stop = STOP_MAP.get(message.stop_reason or "", "other")
    details = getattr(message, "stop_details", None)
    usage = message.usage
    return ModelResponse(
        text="\n".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip(),
        tool_calls=[
            ToolCall(id=b["id"], name=b["name"], arguments=b.get("input") or {})
            for b in blocks
            if b.get("type") == "tool_use"
        ],
        stop_reason=stop,
        usage=Usage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        ),
        model=getattr(message, "model", ""),
        provider=provider,
        raw=blocks,
        refusal_category=getattr(details, "category", None) if stop == "refusal" and details else None,
    )


def _map_error(exc: Exception, provider: str) -> ProviderError:
    try:
        import anthropic
    except ImportError:  # pragma: no cover - only when a fake client is injected without the SDK
        return ProviderError(str(exc), kind="other", provider=provider)
    if isinstance(exc, anthropic.RateLimitError):
        kind = "rate_limit"
    elif isinstance(exc, anthropic.APITimeoutError):
        kind = "timeout"
    elif isinstance(exc, anthropic.APIConnectionError):
        kind = "connection"
    elif isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        kind = "auth"
    elif isinstance(exc, anthropic.APIStatusError):
        kind = "overloaded" if exc.status_code == 529 else "server_error" if exc.status_code >= 500 else "bad_request"
    else:
        kind = "other"
    return ProviderError(str(exc), kind=kind, provider=provider)


class AnthropicProvider:
    def __init__(self, name: str, cfg: dict[str, Any], client: Any | None = None):
        self.name = name
        self.cfg = cfg
        self.options = cfg.get("options", {})
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ProviderError("install the anthropic extra: pip install -e '.[anthropic]'", kind="unavailable", provider=self.name) from exc
            kwargs: dict[str, Any] = {
                "max_retries": self.options.get("max_retries", 2),
                "timeout": self.options.get("timeout_s", 600),
            }
            # Unset key is fine: the SDK also resolves ANTHROPIC_AUTH_TOKEN and `ant auth login` profiles.
            if key := os.environ.get(self.cfg.get("api_key_env", "ANTHROPIC_API_KEY")):
                kwargs["api_key"] = key
            if self.cfg.get("base_url"):
                kwargs["base_url"] = self.cfg["base_url"]
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

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
        eager = self.options.get("eager_input_streaming", True)
        params: dict[str, Any] = {
            "model": model.model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages, self.name),
        }
        if tools:
            params["tools"] = [
                {
                    "name": t.wire_name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    **({"eager_input_streaming": True} if eager else {}),
                }
                for t in tools
            ]
        extra_body: dict[str, Any] = {}
        if self.options.get("prompt_caching", True):
            extra_body["cache_control"] = {"type": "ephemeral"}
        if effort:
            extra_body["output_config"] = {"effort": effort}
        fallback = model.options.get("refusal_fallback")
        if fallback:
            extra_body["fallbacks"] = fallback

        attempts = 1 + self.options.get("invalid_json_retries", 1)
        for attempt in range(attempts):
            try:
                if fallback:
                    manager = self.client.beta.messages.stream(**params, betas=[FALLBACK_BETA], extra_body=extra_body)
                else:
                    manager = self.client.messages.stream(**params, extra_body=extra_body or None)
                with manager as stream:
                    message = stream.get_final_message()
                return from_anthropic_message(message, self.name)
            except ValueError as exc:
                # Eager input streaming: the SDK could not parse a tool input at all. There is
                # no tool_use id to answer, so re-issue the request (capped).
                if attempt + 1 >= attempts:
                    raise ProviderError(f"unparseable tool input: {exc}", kind="other", provider=self.name) from exc
            except Exception as exc:
                raise _map_error(exc, self.name) from exc
        raise ProviderError("unreachable", provider=self.name)  # pragma: no cover
