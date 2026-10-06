from __future__ import annotations

import pytest

from runtime.types import PolicyViolation, ToolError
from tools import filesystem


def test_read_inside_workspace(tool_ctx):
    (tool_ctx.workspace / "notes.md").write_text("hello", encoding="utf-8")
    assert filesystem.read({"path": "notes.md"}, tool_ctx) == "hello"


@pytest.mark.parametrize("path", ["../outside.txt", "../../etc/passwd", "/etc/passwd", "sub/../../escape.txt"])
def test_paths_outside_workspace_are_refused(tool_ctx, path):
    with pytest.raises(PolicyViolation):
        filesystem.read({"path": path}, tool_ctx)


@pytest.mark.parametrize("path", [".env", ".env.local", "config/.env", "sub/.ENV", "keys/server.pem", "state/tasks/x.json", ".git/config"])
def test_sensitive_paths_are_refused(tool_ctx, path):
    target = tool_ctx.workspace / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("SECRET=1", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="denied"):
        filesystem.read({"path": path}, tool_ctx)


def test_writes_only_under_artifacts(tool_ctx):
    with pytest.raises(PolicyViolation, match="not allowed"):
        filesystem.write({"path": "src/app.py", "content": "x"}, tool_ctx)
    msg = filesystem.write({"path": "artifacts/reports/r.md", "content": "# report"}, tool_ctx)
    assert "artifacts/reports/r.md" in msg
    with pytest.raises(ToolError, match="exists"):
        filesystem.write({"path": "artifacts/reports/r.md", "content": "again"}, tool_ctx)


def test_list_hides_denied_entries(tool_ctx):
    (tool_ctx.workspace / ".env").write_text("K=V", encoding="utf-8")
    (tool_ctx.workspace / "readme.md").write_text("hi", encoding="utf-8")
    listing = filesystem.list_dir({"path": "."}, tool_ctx)
    assert "readme.md" in listing
    assert ".env" not in listing
