from __future__ import annotations

import time

from runtime.types import ToolCall, ToolContext, ToolSpec


def _ctx(platform, agent="lead"):
    return ToolContext(platform.root, agent, "test", platform.session_id, platform.policy, {})


def _spec(tool_id="test.echo", side_effects="none", fn=None, timeout_s=5.0):
    return ToolSpec(
        id=tool_id,
        description="test tool used by the executor tests",
        implementation=fn or (lambda args, ctx: f"echo {args.get('text', '')}"),
        side_effects=side_effects,
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "additionalProperties": False},
        timeout_s=timeout_s,
    )


def _grant(platform, tool_id, agent="lead"):
    platform.policy.policies["tool_permissions"]["grants"].append({"agents": [agent], "tools": [tool_id]})


def test_tool_not_in_allowed_set_is_refused(make_platform):
    p = make_platform()
    out = p.executor.execute(ToolCall("1", "shell.run", {"command": ["ls"]}), [], _ctx(p))
    assert out.is_error and "not one of your tools" in out.content


def test_policy_is_checked_even_if_tool_is_offered(make_platform):
    p = make_platform()
    spec = _spec()
    out = p.executor.execute(ToolCall("1", spec.id, {}), [spec], _ctx(p))
    assert out.is_error and "default: deny" in out.content


def test_arguments_are_validated_against_the_schema(make_platform):
    p = make_platform()
    spec = _spec()
    _grant(p, spec.id)
    out = p.executor.execute(ToolCall("1", spec.id, {"text": 5}), [spec], _ctx(p))
    assert out.is_error and "Invalid arguments" in out.content


def test_unparseable_arguments_are_not_executed(make_platform):
    p = make_platform()
    spec = _spec()
    _grant(p, spec.id)
    out = p.executor.execute(ToolCall("1", spec.id, {}, parse_error="invalid JSON"), [spec], _ctx(p))
    assert out.is_error and "not executed" in out.content


def test_side_effects_need_approval(make_platform):
    spec = _spec("test.send", side_effects="external_write")
    denied = make_platform()
    _grant(denied, spec.id)
    out = denied.executor.execute(ToolCall("1", spec.id, {"text": "hi"}), [spec], _ctx(denied))
    assert out.is_error and "did not approve" in out.content

    approved = make_platform(approve={"test.send"})
    _grant(approved, spec.id)
    out = approved.executor.execute(ToolCall("1", spec.id, {"text": "hi"}), [spec], _ctx(approved))
    assert not out.is_error and "echo hi" in out.content


def test_output_is_redacted_and_wrapped_as_untrusted(make_platform):
    p = make_platform()
    spec = _spec(fn=lambda args, ctx: "token=sk-ant-api03-" + "x" * 30)
    _grant(p, spec.id)
    out = p.executor.execute(ToolCall("1", spec.id, {}), [spec], _ctx(p))
    assert out.content.startswith('<tool_output tool="test.echo" trust="untrusted">')
    assert "sk-ant-api03" not in out.content


def test_slow_tools_time_out(make_platform):
    p = make_platform()
    spec = _spec(fn=lambda args, ctx: time.sleep(2) or "late", timeout_s=0.2)
    _grant(p, spec.id)
    out = p.executor.execute(ToolCall("1", spec.id, {}), [spec], _ctx(p))
    assert out.is_error and "timed out" in out.content


def test_denials_are_audited(make_platform):
    p = make_platform()
    p.executor.execute(ToolCall("1", "shell.run", {}), [], _ctx(p))
    audit = (p.state_dir / "sessions" / p.session_id / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event": "tool_denied"' in audit
