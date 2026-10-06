"""Isolated execution for tools marked `sandbox: required` (see sandbox/).

`mode: none`   - sandboxed tools are refused outright (the safe default).
`mode: docker` - commands run in a throwaway container built from sandbox/Dockerfile,
                 with the limits of the tool's profile in sandbox/profiles/.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .types import ToolError


class Sandbox:
    def __init__(self, config: dict[str, Any], workspace: Path):
        self.permissions = config.get("permissions", {}) or {}
        self.profiles = config.get("profiles", {}) or {}
        self.mode = self.permissions.get("mode", "none")
        self.workspace = workspace

    def profile_for(self, tool_id: str) -> dict[str, Any]:
        name = (self.permissions.get("tool_profiles") or {}).get(tool_id)
        if name not in self.profiles:
            raise ToolError(f"no sandbox profile configured for {tool_id}")
        return self.profiles[name]

    def wrap(self, argv: list[str], tool_id: str) -> list[str]:
        """Return the argv that runs `argv` inside the sandbox for this tool."""
        if self.mode != "docker":
            raise ToolError("sandbox mode is 'none'; sandboxed tools are disabled")
        if shutil.which("docker") is None:
            raise ToolError("sandbox mode is 'docker' but docker is not on PATH")
        p = self.profile_for(tool_id)
        mount_mode = "ro" if p.get("workspace_mount", "ro") == "ro" else "rw"
        cmd = [
            "docker", "run", "--rm", "-i",
            "--network", p.get("network", "none"),
            "--cpus", str(p.get("cpus", 1)),
            "--memory", str(p.get("memory", "512m")),
            "--pids-limit", str(p.get("pids_limit", 128)),
            "--user", str(p.get("user", "10001:10001")),
            "--workdir", "/workspace",
            "-v", f"{self.workspace}:/workspace:{mount_mode}",
        ]
        if p.get("read_only_root", True):
            cmd += ["--read-only", "--tmpfs", "/tmp:rw,size=64m"]
        if p.get("no_new_privileges", True):
            cmd += ["--security-opt", "no-new-privileges"]
        for cap in p.get("cap_drop", ["ALL"]):
            cmd += ["--cap-drop", cap]
        for writable in p.get("writable_paths", []):
            host = (self.workspace / writable).resolve()
            if host.is_relative_to(self.workspace):
                cmd += ["-v", f"{host}:/workspace/{writable}:rw"]
        return cmd + [self.permissions.get("image", "agent-sandbox:latest"), *argv]
