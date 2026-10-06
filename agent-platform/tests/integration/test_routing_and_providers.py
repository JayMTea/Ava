"""Model routing rules and provider wire-format translation (no network)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from runtime.providers.anthropic import FALLBACK_BETA, AnthropicProvider, from_anthropic_message, to_anthropic_messages
from runtime.providers.openai_compat import from_openai_response, to_openai_messages
from runtime.types import Message, ModelSpec, ToolCall, ToolSpec


def test_disabled_providers_are_skipped(make_platform):
    usable, skipped = make_platform(route_override=None).router.candidates("reasoning", "internal")
    assert usable == []
    assert any("missing env: ANTHROPIC_API_KEY" in s for s in skipped)


def test_restricted_data_never_reaches_a_provider_not_cleared_for_it(make_platform, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    router = make_platform(route_override=None).router
    usable, _ = router.candidates("reasoning", "confidential")
    assert [m.key for m in usable][:2] == ["claude-opus", "claude-sonnet"]
    usable, skipped = router.candidates("reasoning", "restricted")
    assert usable == []
    assert any("cleared for confidential" in s for s in skipped)


def _spec():
    return ToolSpec("filesystem.read", "Read a file from the workspace.", "tools.filesystem:read", "none", {"type": "object"})


def test_anthropic_messages_group_results_and_use_wire_names():
    history = [
        Message("user", "hi"),
        Message("assistant", "checking", [ToolCall("t1", "filesystem.read", {"path": "a"}), ToolCall("t2", "filesystem.read", {"path": "b"})]),
        Message("tool", "A", tool_call_id="t1"),
        Message("tool", "boom", tool_call_id="t2", is_error=True),
    ]
    wire = to_anthropic_messages(history, "anthropic")
    assert [m["role"] for m in wire] == ["user", "assistant", "user"]
    assert wire[1]["content"][1]["name"] == "filesystem__read"
    results = wire[2]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # one user message for all results
    assert results[1]["is_error"] is True and "is_error" not in results[0]


def test_anthropic_raw_blocks_are_replayed_verbatim_only_to_their_provider():
    raw = [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": "hi"}]
    msg = Message("assistant", "hi", raw=raw, raw_provider="anthropic")
    assert to_anthropic_messages([msg], "anthropic")[0]["content"] is raw
    assert to_anthropic_messages([msg], "other")[0]["content"] == [{"type": "text", "text": "hi"}]


def test_anthropic_fallback_boundary_drops_pre_fallback_internal_blocks():
    message = SimpleNamespace(
        content=[
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": "partial"},
            {"type": "tool_use", "id": "old", "name": "filesystem__read", "input": {}},
            {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
            {"type": "tool_use", "id": "new", "name": "filesystem__read", "input": {"path": "x"}},
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
        model="claude-opus-4-8",
    )
    response = from_anthropic_message(message, "anthropic")
    assert [b["type"] for b in response.raw] == ["text", "fallback", "tool_use"]
    assert [c.id for c in response.tool_calls] == ["new"]


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class _FakeClient:
    def __init__(self):
        self.requests = []
        reply = SimpleNamespace(
            content=[{"type": "text", "text": "ok"}],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_read_input_tokens=0),
            model="claude-opus-5-5",
        )

        def stream(**params):
            self.requests.append(params)
            return _FakeStream(reply)

        self.messages = SimpleNamespace(stream=stream)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=stream))


def test_anthropic_request_shape_follows_current_api_rules():
    client = _FakeClient()
    provider = AnthropicProvider("anthropic", {"options": {"prompt_caching": True}}, client=client)
    model = ModelSpec("claude-opus", "anthropic", "claude-opus-5-5", 64000, supports_effort=True, options={"refusal_fallback": "default"})
    provider.complete(model=model, system="sys", messages=[Message("user", "hi")], tools=[_spec()], effort="high", max_output_tokens=64000)
    req = client.requests[0]
    assert req["betas"] == [FALLBACK_BETA]
    assert req["extra_body"] == {"cache_control": {"type": "ephemeral"}, "output_config": {"effort": "high"}, "fallbacks": "default"}
    assert req["tools"][0]["name"] == "filesystem__read" and req["tools"][0]["eager_input_streaming"] is True
    for forbidden in ("thinking", "temperature", "top_p", "tool_choice"):
        assert forbidden not in req


def test_openai_translation_round_trip():
    history = [Message("user", "hi"), Message("assistant", "", [ToolCall("c1", "git.log", {"limit": 2})]), Message("tool", "log", tool_call_id="c1")]
    wire = to_openai_messages("sys", history)
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[2]["tool_calls"][0]["function"] == {"name": "git__log", "arguments": json.dumps({"limit": 2})}
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "log"}

    bad = SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="c2", function=SimpleNamespace(name="git__log", arguments="{not json"))]),
        )],
        usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        model="gpt",
    )
    response = from_openai_response(bad, "openai")
    assert response.stop_reason == "tool_use"
    assert response.tool_calls[0].parse_error and "invalid JSON" in response.tool_calls[0].parse_error
