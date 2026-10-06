"""Workflow engine behaviour, on the fixture platform's workflows."""

from __future__ import annotations

import pytest

from runtime.orchestrator import render


def test_render_fills_inputs_and_step_outputs():
    scope = {"inputs": {"q": "why"}, "steps": {"a": {"output": "because"}}}
    assert render("Q={{ inputs.q }} A={{steps.a.output}}", scope) == "Q=why A=because"
    assert render({"k": ["{{ inputs.q }}"]}, scope) == {"k": ["why"]}


def test_steps_run_in_order_and_template_earlier_outputs(make_platform):
    p = make_platform(script=[{"text": "Draft: MCP is a protocol."}, {"text": "Checked: MCP is a protocol."}])
    result = p.run_workflow("pipeline", {"question": "What is MCP?"})
    assert result.status == "completed"
    assert result.output == "Checked: MCP is a protocol."
    assert list(result.steps) == ["gather", "check"]
    assert "Draft: MCP is a protocol." in p.mock.calls[1]["messages"][0].content


def test_missing_required_input_is_rejected(make_platform):
    with pytest.raises(ValueError, match="question"):
        make_platform().run_workflow("pipeline", {})


def test_approval_gate_stops_without_approval(make_platform):
    result = make_platform().run_workflow("gated", {"summary": "500s on checkout"})
    assert result.status == "rejected"
    assert result.failed_step == "approve"
    assert "act" not in result.steps


def test_approval_gate_continues_when_approved(make_platform):
    p = make_platform(approve={"workflow.gated.approve"})
    result = p.run_workflow("gated", {"summary": "500s on checkout"})
    assert result.status == "completed"
    assert list(result.steps) == ["triage", "approve", "act"]


def test_tool_steps_run_through_policy(make_platform):
    ok = make_platform().run_workflow("with-tool", {})
    assert ok.steps["read"]["status"] == "completed" and "curated source of truth" in ok.steps["read"]["output"]

    refused = make_platform().run_workflow("with-tool", {"path": "../outside.txt"})
    assert refused.steps["read"]["status"] == "failed"  # confinement; continue_on_error lets it finish
    assert "outside the workspace" in refused.steps["read"]["output"]
    assert refused.status == "completed"
