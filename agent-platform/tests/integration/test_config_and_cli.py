from __future__ import annotations

import pytest

from app.cli import main
from runtime.config import interpolate_env, parse_frontmatter
from runtime.memory_manager import MemoryManager
from runtime.types import PolicyViolation


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("SET_VAR", "value")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    data = {"a": "${SET_VAR}", "b": ["${UNSET_VAR:-fallback}"], "c": "x-${UNSET_VAR}-y"}
    assert interpolate_env(data) == {"a": "value", "b": ["fallback"], "c": "x--y"}


def test_frontmatter_parsing():
    meta, body = parse_frontmatter("---\nname: x\ndescription: y\n---\n\n# Body\n")
    assert meta == {"name": "x", "description": "y"} and body == "# Body"
    with pytest.raises(ValueError):
        parse_frontmatter("# no frontmatter")


def test_memory_permissions_and_redaction(config, tmp_path):
    from runtime.policy_engine import PolicyEngine

    memory = MemoryManager(config.memory, tmp_path, PolicyEngine(config.policies, tmp_path), "s1")
    with pytest.raises(PolicyViolation):
        memory.remember("helper", "project", "helpers may not write project memory")
    entry = memory.remember("lead", "project", "deploy key is sk-ant-api03-" + "z" * 30)
    assert "sk-ant" not in entry["text"]
    assert memory.recall("helper", "deploy key", ["project"])


def test_cli_validate_passes(capsys):
    assert main(["validate"]) == 0
    assert "platform config OK" in capsys.readouterr().out
