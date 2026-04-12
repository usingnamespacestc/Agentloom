"""MCP integration (M7).

A thin async wrapper around the official ``mcp`` Python SDK plus a
bridge that registers remote tools into the in-process
``ToolRegistry`` so they can be called from a WorkFlow exactly like
the built-in Bash/Read/Write/Edit/Glob/Grep tools.

Why we bother: the roadmap needs external tool support (search, code
execution, vector DBs) without us reimplementing every integration.
MCP is the interop surface that's actually getting adoption, and
wrapping it through ``Tool`` means the engine doesn't need to grow a
second tool dispatch path.
"""

from agentloom.mcp.bridge import MCPToolSource
from agentloom.mcp.client import MCPClient, MCPClientError
from agentloom.mcp.tool_adapter import MCPRemoteTool, mcp_tool_name
from agentloom.mcp.types import MCPServerConfig, MCPServerKind

__all__ = [
    "MCPClient",
    "MCPClientError",
    "MCPRemoteTool",
    "MCPServerConfig",
    "MCPServerKind",
    "MCPToolSource",
    "mcp_tool_name",
]
