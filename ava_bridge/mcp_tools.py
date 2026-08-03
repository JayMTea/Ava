"""Discover the agent's MCP tools by scanning the servers on disk.

There is no tool registry file, deliberately: `agent/mcp_server_<group>/_server.mjs`
auto-loads every non-`_`-prefixed `.mjs` beneath it and keys by the module's own
`name` field, and `agent/install.sh` discovers the groups the same way. So the
filesystem IS the registry, and nothing can drift out of sync with it.

Extracted from `agent/docs/arch.py:actual_tools`, which now delegates here — one
parser, because a second regex over the same files is a second thing that can
disagree about which tools exist. This version returns more than arch.py's
`name -> module` map needed: the group, and a repo-relative path, since
`os.path.relpath(full, base)` had already thrown the base away and callers that
need to locate a tool in the repo cannot recover it.

The overlay is included by default (a deployment wants the whole truth) and
excluded when building anything tracked (a fork must not see private tools).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import config

_NAME_RE = re.compile(r"^\s*name:\s*'([^']+)'", re.MULTILINE)


@dataclass(frozen=True)
class Tool:
    name: str
    group: str      # the mcp_server_<group> category
    module: str     # path relative to the server dir, e.g. "web/web_search.mjs"
    rel: str        # repo-relative path, for callers that must locate the file
    source: str     # core | overlay


def _bases() -> list[tuple[str, str]]:
    """(server_dir, source) for every mcp_server_* directory, core then overlay."""
    overlay = os.environ.get("AVA_OVERLAY") or os.path.join(config.ROOT, "overlay", "agent")
    out: list[tuple[str, str]] = []
    for root, source in ((os.path.join(config.ROOT, "agent"), "core"), (overlay, "overlay")):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if name.startswith("mcp_server_") and os.path.isdir(path):
                out.append((path, source))
    return out


def servers(include_overlay: bool = True) -> list[tuple[str, str, str]]:
    """(group, repo-relative server dir, source) for each discovered server."""
    out = []
    for base, source in _bases():
        if source == "overlay" and not include_overlay:
            continue
        group = os.path.basename(base)[len("mcp_server_"):]
        out.append((group, os.path.relpath(base, config.ROOT), source))
    return out


def scan(include_overlay: bool = True) -> list[Tool]:
    """Every tool module, in a stable order.

    A later server wins on a name collision, matching `_server.mjs`'s own
    last-loaded-wins behaviour — overlay shadows core, which is the point of the
    overlay.
    """
    found: dict[str, Tool] = {}
    for base, source in _bases():
        if source == "overlay" and not include_overlay:
            continue
        group = os.path.basename(base)[len("mcp_server_"):]
        for dirpath, _dirs, files in os.walk(base):
            for fn in sorted(files):
                if not fn.endswith(".mjs") or fn.startswith("_"):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    with open(full, encoding="utf-8") as f:
                        text = f.read()
                except OSError:
                    continue
                mo = _NAME_RE.search(text)
                if not mo:
                    continue
                found[mo.group(1)] = Tool(
                    name=mo.group(1),
                    group=group,
                    module=os.path.relpath(full, base),
                    rel=os.path.relpath(full, config.ROOT),
                    source=source,
                )
    return [found[k] for k in sorted(found)]


def name_to_module(include_overlay: bool = True) -> dict[str, str]:
    """The shape `arch.actual_tools()` has always returned."""
    return {t.name: t.module for t in scan(include_overlay)}
