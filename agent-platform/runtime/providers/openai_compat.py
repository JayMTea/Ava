"""Any OpenAI-compatible Chat Completions endpoint: OpenAI itself, vLLM, Ollama, LM Studio,
llama.cpp server, LiteLLM. Configured by models/providers/openai.yaml and local.yaml.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..types import Message, ModelResponse, ModelSpec, ProviderError, ToolCall, ToolSpec, Usage, wire_name

STOP_MAP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens", "content_filter": "refusal"}
EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for msg in messages:
        if msg.role == "tool":
            out.append({"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content})
        elif msg.role == "user":
            out.append({"role": "user", "content": msg.content})
        else:
            entry: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": wire_name(c.name), "arguments": json.dumps(c.arguments)},
                    }
                    for c in msg.tool_calls
                ]
            out.append(entry)
    return out


def from_openai_response(resp: Any, provider: str) -> ModelResponse:
    choice = resp.choices[0]
    msg = choice.message
    calls = []
    for tc in msg.tool_calls or []:
        raw_args = tc.function.arguments or "{}"
        try:
            args = json.loads(raw_args)
            error = None if isinstance(args, dict) else "arguments must be a JSON object"
        except json.JSONDecodeError as exc:
            args, error = {}, f"invalid JSON arguments: {exc}: {raw_args[:500]}"
        calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args if isinstance(args, dict) else {}, parse_error=error))
    usage = getattr(resp, "usage", None)
    return ModelResponse(
        text=(msg.content or "").strip(),
        tool_calls=calls,
        stop_reason=STOP_MAP.get(choice.finish_reason or "", "other"),
        usage=Usage(
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
        ),
        model=getattr(resp, "model", ""),
        provider=provider,
    )


def _map_error(exc: Exception, provider: str) -> ProviderError:
    try:
        import openai
    except ImportError:  # pragma: no cover
        return ProviderError(str(exc), kind="other", provider=provider)
    if isinstance(exc, openai.RateLimitError):
        kind = "rate_limit"
    elif isinstance(exc, openai.APITimeoutError):
        kind = "timeout"
    elif isinstance(exc, openai.APIConnectionError):
        kind = "connection"
    elif isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        kind = "auth"
    elif isinstance(exc, openai.APIStatusError):
        kind = "server_error" if exc.status_code >= 500 else "bad_request"
    else:
        kind = "other"
    return ProviderError(str(exc), kind=kind, provider=provider)


class OpenAICompatibleProvider:
    def __init__(self, name: str, cfg: dict[str, Any], client: Any | None = None):
        self.name = name
        self.cfg = cfg
        self.options = cfg.get("options", {})
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ProviderError("install the openai extra: pip install -e '.[openai]'", kind="unavailable", provider=self.name) from exc
            self._client = OpenAI(
                api_key=os.environ.get(self.cfg.get("api_key_env", ""), "") or "not-needed",
                base_url=self.cfg.get("base_url"),
                timeout=self.options.get("timeout_s", 600),
                max_retries=self.options.get("max_retries", 2),
            )
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
        params: dict[str, Any] = {"model": model.model, "messages": to_openai_messages(system, messages)}
        params[self.options.get("max_tokens_param", "max_completion_tokens")] = max_output_tokens
        if tools:
            params["tools"] = [
                {"type": "function", "function": {"name": t.wire_name, "description": t.description, "parameters": t.input_schema}}
                for t in tools
            ]
        if effort:
            params["reasoning_effort"] = EFFORT_MAP.get(effort, "medium")
        try:
            resp = self.client.chat.completions.create(**params)
        except Exception as exc:
            raise _map_error(exc, self.name) from exc
        return from_openai_response(resp, self.name)
