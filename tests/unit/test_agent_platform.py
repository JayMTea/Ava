"""The adopted template is isolated, while app discovery uses its integration kit."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys

import pytest

from app.backend import agent_platform as platform


def test_installed_live_agent_loads_from_platform_outside_checkout(tmp_path):
    code = "import ava_agent, app.backend; print(ava_agent.__file__); print(app.backend.__file__)"
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path,
                            capture_output=True, text=True, check=True)
    agent, backend = map(Path, result.stdout.splitlines())
    assert agent.resolve() == platform.INTEGRATION_ROOT / "ava_agent/__init__.py"
    assert backend.resolve() == platform.ROOT / "app/backend/__init__.py"


def test_retired_root_trees_have_no_tracked_sources():
    from gitfiles import tracked

    assert not [p for p in tracked() if p.split("/", 1)[0] in {"runtime", "tools", "state", "artifacts"}]


def test_live_turn_is_executed_by_platform_owned_adapter(monkeypatch):
    from ava_agent import agent, adapters

    calls = []
    history = [{"role": "user", "content": "earlier"}]
    backend = SimpleNamespace(run_turn=lambda text, **kw: calls.append((text, kw)) or ("answer", ["tool"]))
    monkeypatch.setattr(adapters, "gate", lambda: (backend, None))
    assert agent.run_turn("task", "session", history) == ("answer", ["tool"])
    assert calls == [("task", {"session_id": "session", "history": history})]
    assert Path(agent.__file__).resolve().is_relative_to(platform.INTEGRATION_ROOT / "ava_agent")


@pytest.mark.parametrize(("arguments", "expected"), [
    ([], ["-m", "app.cli", "agents"]),
    (["validate"], ["-m", "app.cli", "validate"]),
    (["run", "a task with spaces", "--route", "test"],
     ["-m", "app.cli", "run", "a task with spaces", "--route", "test"]),
    (["check", "--fast"], ["scripts/check.py", "--fast"]),
    (["evals", "--route", "test"], ["-m", "evals.run", "--route", "test"]),
])
def test_launch_uses_separate_interpreter_and_clean_import_path(monkeypatch, tmp_path, arguments, expected):
    interpreter = tmp_path / "platform-python"
    interpreter.touch()
    monkeypatch.setenv("AVA_PLATFORM_PYTHON", str(interpreter))
    monkeypatch.setenv("PYTHONPATH", str(platform.ROOT))
    monkeypatch.setenv("PYTHONHOME", "/host/python")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(platform.subprocess, "run", run)
    assert platform.command(SimpleNamespace(platform_args=arguments)) == 7
    argv, opts = calls.pop()
    assert argv == [str(interpreter), *expected]
    assert opts["cwd"] == platform.PLATFORM_ROOT
    assert "PYTHONPATH" not in opts["env"]
    assert "PYTHONHOME" not in opts["env"]


def test_missing_interpreter_fails_with_install_instructions(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AVA_PLATFORM_PYTHON", str(tmp_path / "missing"))
    assert platform.command(SimpleNamespace(platform_args=["validate"])) == 2
    assert "uv venv agent-platform/.venv" in capsys.readouterr().err


def test_relative_interpreter_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("AVA_PLATFORM_PYTHON", "python")
    assert platform.command(SimpleNamespace(platform_args=[])) == 2
    assert "absolute" in capsys.readouterr().err


def test_app_discovers_relocated_assets():
    from app.backend import config
    from ava_agent import mcp_tools, policy_inventory, render_persona, skills

    assert Path(skills.CORE_DIR) == platform.INTEGRATION_ROOT / "skills"
    assert Path(policy_inventory.POLICY_DIR) == platform.INTEGRATION_ROOT / "policies/egress"
    servers = mcp_tools.servers(include_overlay=False)
    assert len(servers) == 5
    assert all((Path(config.ROOT) / rel).is_relative_to(platform.INTEGRATION_ROOT / "mcp")
               for _, rel, _ in servers)
    assert Path(render_persona.TEMPLATE) == platform.PLATFORM_ROOT / "agents/ava/persona.md.tmpl"
    assert Path(render_persona.TEMPLATE).is_file()


def test_architecture_reader_and_watcher_share_source():
    from app.backend import architecture, arch_watch

    script = platform.ROOT / "docs/architecture/arch.py"
    assert Path(architecture.ARCH_PY) == Path(arch_watch.ARCH) == script
    assert Path(architecture.MANIFEST) == Path(arch_watch.MANIFEST) == script.with_name("architecture.yaml")


def test_state_is_ignored_and_template_sources_are_trackable():
    private = ["agent-platform/state/sessions/private.json", "agent-platform/artifacts/reports/private.md",
               "models/hf/weights.bin", "agent/policies/generated/private.yaml",
               "agent/mcp_server_connectors/apps/private/tool.mjs",
               "agent-platform/integrations/ava/policies/egress/generated/private.yaml",
               "agent-platform/integrations/ava/mcp/servers/mcp_server_connectors/apps/private/tool.mjs"]
    public = ["agent-platform/agent.yaml", "agent-platform/models/registry.yaml",
              "agent-platform/agents/ava/agent.yaml", "config/ava.example.yaml",
              ".claude/skills/agent-component/SKILL.md", "agent-platform/.claude/skills/agent-component/SKILL.md"]
    for rel in private + public:
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", rel], cwd=platform.ROOT)
        assert result.returncode == (0 if rel in private else 1), rel
