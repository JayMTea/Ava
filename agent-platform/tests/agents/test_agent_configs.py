"""The project's own agent config is consistent (generic: works for any set of agents),
and the loader rejects the mistakes it is meant to catch (checked on a fixture copy)."""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
import yaml

from runtime import ConfigError, load_platform, validate_platform
from runtime.policy_engine import PolicyEngine

ROOT = Path(__file__).resolve().parents[2]


def test_project_config_is_valid():
    problems, _warnings = validate_platform(ROOT)
    assert problems == []


def test_every_agent_directory_is_an_agent(project_config):
    dirs = {p.name for p in (ROOT / "agents").iterdir() if p.is_dir()}
    assert dirs == set(project_config.agents)


def test_every_listed_tool_is_granted(project_config):
    policy = PolicyEngine(project_config.policies, ROOT)
    for agent in project_config.agents.values():
        for pattern in agent.tools:
            for tool_id in fnmatch.filter(project_config.tools, pattern):
                assert policy.check_tool(agent.name, tool_id).allowed, (agent.name, tool_id)


def test_default_agent_exists(project_config):
    assert project_config.manifest["default_agent"] in project_config.agents


def _edit(path, fn):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_ungranted_tool_is_a_config_error(config_copy):
    _edit(config_copy / "agents/helper/agent.yaml", lambda d: d["tools"].append("filesystem.write"))
    with pytest.raises(ConfigError, match="not granted"):
        load_platform(config_copy, check_imports=False)


def test_raw_model_id_instead_of_route_is_a_config_error(config_copy):
    _edit(config_copy / "agents/helper/agent.yaml", lambda d: d["model"].update(route="claude-opus-5-5"))
    with pytest.raises(ConfigError, match="not defined in models/routing.yaml"):
        load_platform(config_copy, check_imports=False)


def test_agent_name_must_match_directory(config_copy):
    _edit(config_copy / "agents/helper/agent.yaml", lambda d: d.update(name="helper-bot"))
    with pytest.raises(ConfigError, match="must equal its directory name"):
        load_platform(config_copy, check_imports=False)


def test_memory_write_must_be_permitted(config_copy):
    _edit(config_copy / "agents/helper/agent.yaml", lambda d: d.setdefault("memory", {}).update(write=["project"]))
    with pytest.raises(ConfigError, match="does not let 'helper' write 'project'"):
        load_platform(config_copy, check_imports=False)


def test_delegating_to_an_unknown_agent_is_a_config_error(config_copy):
    _edit(config_copy / "agents/lead/agent.yaml", lambda d: d["delegates_to"].append("ghost"))
    with pytest.raises(ConfigError, match="delegates_to 'ghost' is not an agent"):
        load_platform(config_copy, check_imports=False)
