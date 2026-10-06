"""End-to-end behaviour of the agent loop, on the fixture platform and the mock provider."""

from __future__ import annotations

import json

from runtime.agent_runtime import INTERRUPTED_CALL, TRUNCATED_CALL
from runtime.types import Message, ModelResponse, ProviderError, ToolCall, Usage


def test_direct_answer_completes_and_is_recorded(make_platform):
    p = make_platform(script=[{"text": "Hello there."}])
    result = p.run("Say hello")
    assert result.status == "completed"
    assert result.output == "Hello there."
    task = p.tasks.get(result.task_id)
    assert task["status"] == "completed" and task["result"] == "Hello there."
    trace = (p.state_dir / "sessions" / p.session_id / "trace.jsonl").read_text(encoding="utf-8")
    names = [json.loads(line)["name"] for line in trace.splitlines()]
    assert "invoke_agent lead" in names and "chat mock" in names
    assert p.checkpoints.latest(result.task_id) is not None


def test_system_prompt_layers_platform_agent_skills_and_delegates(make_platform):
    p = make_platform(script=[{"text": "ok"}])
    p.run("anything")
    system = p.mock.calls[0]["system"]
    skills_block = "<available_skills>\nLoad a skill"
    assert system.index("<platform_instructions>") < system.index("<agent_instructions") < system.index(skills_block)
    assert "Fixture platform instructions" in system.split("<agent_instructions")[0]
    assert "- sample:" in system.split(skills_block)[1].split("</available_skills>")[0]
    assert "<delegates>" in system and "- helper:" in system


def test_tool_loop_reads_a_file_then_answers(make_platform):
    p = make_platform(script=[
        {"tool_calls": [{"name": "filesystem.read", "arguments": {"path": "knowledge/notes.md"}}]},
        {"text": "knowledge/ is the curated source of truth."},
    ])
    result = p.run("What is knowledge/?", agent="helper")
    assert result.status == "completed"
    assert result.tool_calls == 1
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert not tool_msg.is_error and "curated source of truth" in tool_msg.content
    # the model only ever saw tools the helper is granted, sorted
    offered = p.mock.calls[0]["tools"]
    assert offered == sorted(offered)
    assert "filesystem.write" not in offered and "task.delegate" not in offered


def test_delegation_runs_a_child_task(make_platform):
    p = make_platform(script=[
        {"tool_calls": [{"name": "task.delegate", "arguments": {"agent": "helper", "task": "Explain what the knowledge folder is for."}}]},
        {"text": "child answer"},
        {"text": "parent answer using child answer"},
    ])
    result = p.run("Ask the helper about knowledge/")
    assert result.status == "completed"
    children = [t for t in p.tasks.list() if t["parent_id"] == result.task_id]
    assert len(children) == 1 and children[0]["agent"] == "helper" and children[0]["result"] == "child answer"


def test_delegation_depth_is_capped(make_platform):
    p = make_platform(script=[
        {"tool_calls": [{"name": "task.delegate", "arguments": {"agent": "helper", "task": "Explain what the knowledge folder is for."}}]},
        {"text": "gave up"},
    ])
    p.policy.policies["safety"]["limits"]["max_delegation_depth"] = 0
    result = p.run("delegate something")
    tool_msg = next(m for m in result.messages if m.role == "tool")
    assert tool_msg.is_error and "max_delegation_depth" in tool_msg.content


def test_refusal_ends_the_task_without_running_tools(make_platform):
    p = make_platform(script=[{
        "text": "",
        "tool_calls": [{"name": "filesystem.read", "arguments": {"path": "README.md"}}],
        "stop_reason": "refusal",
        "refusal_category": "cyber",
    }])
    result = p.run("something declined")
    assert result.status == "failed" and "declined" in result.error and "cyber" in result.error
    assert result.tool_calls == 0


def test_max_tokens_with_tool_calls_does_not_execute_them(make_platform):
    p = make_platform(script=[
        {"tool_calls": [{"name": "filesystem.read", "arguments": {"path": "README.md"}}], "stop_reason": "max_tokens"},
        {"text": "done"},
    ])
    result = p.run("read")
    assert result.status == "completed" and result.tool_calls == 0
    assert any(m.role == "tool" and m.content == TRUNCATED_CALL for m in result.messages)


def test_turn_limit_stops_runaway_loops(make_platform):
    call = {"tool_calls": [{"name": "filesystem.list", "arguments": {}}]}
    p = make_platform(script=[call] * 5)
    p.policy.policies["safety"]["limits"]["max_turns"] = 2
    result = p.run("loop forever")
    assert result.status == "failed" and "max_turns=2" in result.error


def test_tool_call_budget_is_enforced(make_platform):
    call = {"tool_calls": [{"name": "filesystem.list", "arguments": {}}] * 3}
    p = make_platform(script=[call, call])
    p.policy.policies["safety"]["limits"]["max_tool_calls"] = 4
    result = p.run("many calls")
    assert result.status == "failed" and "max_tool_calls=4" in result.error
    assert result.tool_calls == 3


def test_resume_answers_interrupted_tool_calls_instead_of_rerunning_them(make_platform):
    p = make_platform(script=[{"text": "finished after resume"}])
    task = p.tasks.create("lead", "long job", session_id=p.session_id)
    p.tasks.update(task["id"], status="running")
    history = [
        Message("user", "<task>long job</task>"),
        Message("assistant", "", [ToolCall("c1", "filesystem.list", {})]),
    ]
    p.checkpoints.save(task["id"], 1, history, Usage(), 0)
    result = p.resume(task["id"])
    assert result.status == "completed" and result.output == "finished after resume"
    assert any(m.role == "tool" and m.content == INTERRUPTED_CALL for m in result.messages)


def test_memory_is_written_then_recalled_into_the_next_task(make_platform):
    p = make_platform(script=[
        {"tool_calls": [{"name": "memory.remember", "arguments": {"namespace": "project", "text": "The billing service is written in Go."}}]},
        {"text": "noted"},
        {"text": "It is written in Go."},
    ])
    p.run("Remember that billing is in Go")
    p.run("What language is the billing service in?")
    first_message = p.mock.calls[2]["messages"][0].content
    assert '<memory trust="untrusted">' in first_message and "billing service is written in Go" in first_message


def test_provider_error_fails_the_task_cleanly(make_platform):
    class Broken:
        name = "mock"

        def complete(self, **_):
            raise ProviderError("bad request", kind="bad_request", provider="mock")

    p = make_platform(providers={"mock": Broken()})
    result = p.run("anything")
    assert result.status == "failed" and "bad request" in result.error
    assert p.tasks.get(result.task_id)["status"] == "failed"


def test_router_falls_back_on_retryable_errors(make_platform):
    class Flaky:
        name = "anthropic"

        def __init__(self):
            self.calls = 0

        def complete(self, *, model, **_):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("slow down", kind="rate_limit", provider="anthropic")
            return ModelResponse("served by " + model.key, [], "end_turn", Usage(), model.model, "anthropic")

    flaky = Flaky()
    p = make_platform(providers={"anthropic": flaky}, route_override=None)
    result = p.run("hello")
    assert result.status == "completed"
    assert result.output == "served by claude-sonnet"  # claude-opus hit the rate limit, sonnet answered
