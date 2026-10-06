"""Shared fixtures. Every test runs offline on the mock provider with state in a temp dir.

Two configs, on purpose:
- `config` is the FIXTURE platform in tests/fixtures/platform (agents `lead` and `helper`).
  Runtime tests use it, so they keep passing however the project tailors its own agents.
- `project_config` is THIS project's platform (the repository root). Tests that check the
  project's own config and safety guarantees use it.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from runtime import AllowListApprover, MockProvider, Platform, load_platform
from runtime.policy_engine import PolicyEngine
from runtime.types import ToolContext

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "platform"


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch):
    """Tests must never reach a real model, whatever the developer's shell exports."""
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LOCAL_LLM_BASE_URL", "AGENT_ROUTE_OVERRIDE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="session")
def config():
    # check_imports=False: tool implementations are resolved only if a test actually calls them.
    return load_platform(FIXTURE, check_imports=False)


@pytest.fixture(scope="session")
def project_config():
    return load_platform(ROOT)


@pytest.fixture
def make_platform(config, tmp_path):
    """Build a fixture Platform on the mock route. `script` drives the model turn by turn."""

    def _make(script=None, approve=(), route_override="test", providers=None, **kwargs):
        mock = MockProvider(script=script or [])
        platform = Platform(
            copy.deepcopy(config),  # tests may mutate policies; never leak that across tests
            state_dir=tmp_path / "state",
            approver=AllowListApprover(set(approve)),
            providers=providers or {"mock": mock},
            route_override=route_override,
            **kwargs,
        )
        platform.mock = mock
        return platform

    return _make


@pytest.fixture
def config_copy(tmp_path):
    """A writable copy of the fixture platform, for tests that break config on purpose."""
    dest = tmp_path / "platform"
    shutil.copytree(FIXTURE, dest)
    return dest


@pytest.fixture
def workspace(tmp_path, config):
    """An empty workspace with the fixture policies, for tool tests that touch the filesystem."""
    ws = tmp_path / "ws"
    (ws / "artifacts" / "reports").mkdir(parents=True)
    return ws, PolicyEngine(config.policies, ws)


@pytest.fixture
def tool_ctx(workspace):
    ws, policy = workspace
    return ToolContext(workspace=ws.resolve(), agent="tester", task_id="test", session_id="s", policy=policy)
