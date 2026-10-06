"""THIS project's safety guarantees, checked against its real policies/.

Unlike the runtime tests (which use the fixture platform), these fail when the project's own
policies change. That is the point: widening a policy should be a deliberate act with a
matching edit here in the same commit - never a silent side effect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.policy_engine import PolicyEngine
from runtime.types import PolicyViolation

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def policy(project_config, tmp_path):
    for rel in (".env", "config/.env.local", "keys/server.pem", "state/tasks/t.json", "notes.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    return PolicyEngine(project_config.policies, tmp_path)


@pytest.mark.parametrize("path", [".env", "config/.env.local", "keys/server.pem", "state/tasks/t.json", "../outside"])
def test_secrets_state_and_outside_paths_are_unreadable(policy, path):
    with pytest.raises(PolicyViolation):
        policy.resolve_path(path, "read")


def test_ordinary_files_are_readable(policy):
    assert policy.resolve_path("notes.md", "read").name == "notes.md"


def test_writes_are_confined(policy):
    with pytest.raises(PolicyViolation):
        policy.resolve_path("src/app.py", "write")


def test_shell_is_denied_to_every_agent(project_config):
    policy = PolicyEngine(project_config.policies, ROOT)
    for agent in project_config.agents:
        assert not policy.check_tool(agent, "shell.run").allowed, agent


def test_unknown_tools_are_denied_by_default(project_config):
    policy = PolicyEngine(project_config.policies, ROOT)
    assert not policy.check_tool(next(iter(project_config.agents)), "made.up_tool").allowed


@pytest.mark.parametrize("url", ["http://example.com/", "https://127.0.0.1/", "https://169.254.169.254/latest/meta-data/"])
def test_insecure_and_internal_urls_are_refused(project_config, url):
    with pytest.raises(PolicyViolation):
        PolicyEngine(project_config.policies, ROOT).check_url(url)


def test_secret_formats_are_redacted(project_config):
    redacted = PolicyEngine(project_config.policies, ROOT).redact("key sk-ant-api03-" + "a" * 30)
    assert "sk-ant-api03" not in redacted


def test_side_effects_outside_the_workspace_need_approval(project_config):
    required = project_config.policies["approvals"]["require_approval"]["side_effects"]
    assert {"external_write", "destructive"} <= set(required)
    assert project_config.policies["approvals"]["non_interactive"] == "deny"
