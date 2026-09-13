"""Transfer generated connector tools to a remote runtime before provisioning.

Only generated policy YAML and connector MJS are accepted. No absolute paths,
archives, credentials, executable host scripts, or runtime configuration.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import tempfile
from . import settings

MAX_BYTES = 4 << 20
MAX_FILES = 256
PATH = re.compile(r"(?:policies/generated/[a-z][a-z0-9_-]{1,31}\.yaml|mcp_server_connectors/apps/[a-z][a-z0-9_-]{1,31}/[A-Za-z0-9_-]+\.mjs)\Z")


def collect(connector: str | None = None, scopes: set[str] | None = None) -> dict[str, str]:
    root = Path(settings.agent_state_dir())
    result = {}
    for pattern in ("policies/generated/*.yaml", "mcp_server_connectors/apps/*/*.mjs"):
        if scopes is not None and ("policies" if pattern.startswith("policies/") else "servers") not in scopes:
            continue
        for path in root.glob(pattern):
            rel = path.relative_to(root).as_posix()
            cid = path.stem if rel.startswith("policies/") else path.parent.name
            if connector and cid != connector:
                continue
            if path.is_symlink() or not PATH.fullmatch(rel):
                raise ValueError("invalid generated material path")
            result[rel] = path.read_text(encoding="utf-8")
    validate(result)
    return result


def validate(files: dict[str, str]) -> None:
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise ValueError("invalid material file count")
    if any(not isinstance(k, str) or not PATH.fullmatch(k) or not isinstance(v, str)
           for k, v in files.items()):
        raise ValueError("invalid generated material path or content")
    if len(json.dumps(files).encode()) > MAX_BYTES:
        raise ValueError("generated material exceeds size limit")


def install(files: dict[str, str], *, connector: str | None = None, scopes: set[str] | None = None) -> None:
    validate(files)
    root = Path(settings.agent_state_dir()).resolve()
    # Validate the entire bundle before writing anything. Refuse symlink escapes
    # even if an operator previously placed a link inside the generated tree.
    for rel in files:
        dest = root / rel
        if not dest.resolve().is_relative_to(root) or any(p.is_symlink() for p in [dest, *dest.parents] if p != root):
            raise ValueError("symlink in generated material destination")
    for rel, source in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".material-", dir=dest.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(source)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, dest)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    # The bridge owns these generated files. Withdraw stale material so a deleted
    # tool or policy cannot survive a later provision on the other host.
    for rel in collect(connector, scopes):
        if rel not in files:
            (root / rel).unlink()
