"""Bring tools from MCP servers (mcp/servers.yaml) into the tool registry.

Layering:
  mcp/servers.yaml       how to reach each server (disabled by default)
  mcp/permissions.yaml   which of a server's tools may enter the platform at all
  mcp/adapters/*.yaml    per-server overrides (side-effect class, descriptions, timeouts)
  policies/              which agents may call the resulting `mcp.<server>.<tool>` ids

Unknown MCP tools are classified `external_write` unless an adapter says otherwise, so
by default every MCP call needs approval.

This reference bridge opens a fresh session per call: simple and stateless, at the cost
of process start-up latency. Keep a long-lived session in app/ if that matters.
Requires the `mcp` extra.
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from .tool_registry import ToolRegistry
from .types import ToolContext, ToolError, ToolSpec


@asynccontextmanager
async def _session(server: dict[str, Any]) -> AsyncIterator[Any]:
    from mcp import ClientSession

    if server["transport"] == "stdio":
        from mcp.client.stdio import stdio_client

        from mcp import StdioServerParameters

        params = StdioServerParameters(command=server["command"], args=server.get("args", []), env=server.get("env") or None)
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            yield session
    else:
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(server["url"], headers=server.get("headers") or None) as (read, write, _), ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _list_tools(server: dict[str, Any]) -> list[Any]:
    async with _session(server) as session:
        return list((await session.list_tools()).tools)


async def _call_tool(server: dict[str, Any], tool: str, args: dict[str, Any]) -> str:
    async with _session(server) as session:
        result = await session.call_tool(tool, args)
    text = "\n".join(getattr(c, "text", f"[{getattr(c, 'type', 'content')}]") for c in result.content)
    if result.isError:
        raise ToolError(text or "MCP tool returned an error")
    return text


def _caller(server: dict[str, Any], tool: str):
    def call(args: dict[str, Any], ctx: ToolContext) -> str:
        return asyncio.run(_call_tool(server, tool, args))

    return call


def register_mcp_tools(registry: ToolRegistry, mcp_config: dict[str, Any], wanted_servers: set[str]) -> list[str]:
    """Discover and register tools for every enabled server some agent uses. Returns ids."""
    servers = (mcp_config.get("servers") or {}).get("servers") or {}
    permissions = (mcp_config.get("permissions") or {}).get("servers") or {}
    adapters = mcp_config.get("adapters") or {}
    registered: list[str] = []
    for name in sorted(wanted_servers):
        server = servers.get(name, {})
        if not server.get("enabled", False):
            continue
        try:
            import mcp  # noqa: F401
        except ImportError as exc:
            raise ToolError("an MCP server is enabled but the `mcp` extra is not installed") from exc
        allow = permissions.get(name, {}).get("expose", [])
        deny = permissions.get(name, {}).get("hide", [])
        overrides = (adapters.get(name) or {}).get("tools", {})
        for tool in asyncio.run(_list_tools(server)):
            if not any(fnmatch.fnmatchcase(tool.name, p) for p in allow) or any(fnmatch.fnmatchcase(tool.name, p) for p in deny):
                continue
            override = overrides.get(tool.name, {})
            spec = ToolSpec(
                id=f"mcp.{name}.{tool.name}",
                description=override.get("description") or (tool.description or tool.name),
                implementation=_caller(server, tool.name),
                side_effects=override.get("side_effects", server.get("default_side_effects", "external_write")),
                input_schema=tool.inputSchema or {"type": "object"},
                timeout_s=override.get("timeout_s", server.get("timeout_s", 60)),
                source=f"mcp:{name}",
            )
            registry.register(spec)
            registered.append(spec.id)
    return registered
