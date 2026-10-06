from __future__ import annotations

import sqlite3

import pytest

from runtime.sandbox import Sandbox
from runtime.types import ToolError
from tools import database, git


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-p", "main;rm -rf /", "$(id)"])
def test_git_refs_cannot_smuggle_options(ref):
    with pytest.raises(ToolError, match="unsafe ref"):
        git._ref(ref)


def test_git_refs_accept_normal_refs():
    for ref in ["main", "HEAD~3", "origin/main", "v1.2.0", "main...feature"]:
        assert git._ref(ref) == ref


def test_database_is_read_only(tool_ctx, monkeypatch):
    db = tool_ctx.workspace / "data.db"
    conn = sqlite3.connect(db)
    conn.execute("create table t (n integer)")
    conn.execute("insert into t values (1), (2)")
    conn.commit()
    conn.close()
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data.db")
    result = database.query({"sql": "select count(*) as c from t"}, tool_ctx)
    assert result["rows"] == [[2]]
    with pytest.raises(ToolError, match="query failed"):
        database.query({"sql": "insert into t values (3)"}, tool_ctx)


def test_sandbox_mode_none_refuses(config, tmp_path):
    with pytest.raises(ToolError, match="disabled"):
        Sandbox(config.sandbox, tmp_path).wrap(["ls"], "shell.run")


def test_docker_sandbox_applies_profile(config, tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.sandbox.shutil.which", lambda _: "/usr/bin/docker")
    sandbox_cfg = {**config.sandbox, "permissions": {**config.sandbox["permissions"], "mode": "docker"}}
    argv = Sandbox(sandbox_cfg, tmp_path).wrap(["ls", "-la"], "shell.run")
    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "no-new-privileges" in argv
    assert argv[-3:] == ["agent-sandbox:latest", "ls", "-la"]
