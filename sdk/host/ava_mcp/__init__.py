"""ava_mcp — serve any ava-tools/1 facade as a real MCP server. Stdlib only."""
from .ava_mcp import (
    PROTOCOL_VERSION,
    FacadeSource,
    RegistrySource,
    ToolSource,
    serve_mcp,
)

__all__ = ["FacadeSource", "RegistrySource", "ToolSource", "serve_mcp",
           "PROTOCOL_VERSION"]
