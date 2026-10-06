"""Ava's project configuration keeps restricted work local and tools read-only."""

from runtime import Platform
from runtime.policy_engine import PolicyEngine


def test_ava_cannot_route_restricted_work_to_cloud(project_config, tmp_path, monkeypatch):
    # Even available cloud credentials and an alternate route must not expose data.
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "offline-test-key")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
    platform = Platform(project_config, state_dir=tmp_path / "state")
    ava = project_config.agents[project_config.manifest["default_agent"]]
    assert ava.data_classification == "restricted"
    for route in project_config.routes:
        usable, _ = platform.router.candidates(route, ava.data_classification)
        assert all(model.provider in {"local", "mock"} for model in usable)


def test_ava_has_no_write_tools_or_implicit_grants(project_config):
    policy = PolicyEngine(project_config.policies, project_config.root)
    granted = {name for name in project_config.tools if policy.check_tool("ava", name).allowed}
    assert granted == {"filesystem.read", "filesystem.list", "skill.load", "memory.recall"}
    assert all(project_config.tools[name].side_effects == "none" for name in granted)
    assert not policy.check_tool("unconfigured-agent", "filesystem.read").allowed
