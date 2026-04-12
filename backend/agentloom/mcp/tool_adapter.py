"""Adapter that exposes one remote MCP tool as an in-process ``Tool``.

An MCP server advertises tools with their own JSONSchema; we wrap each
one in an ``MCPRemoteTool`` that looks identical to the six built-in
tools (``Bash``, ``Read``, ...) to the engine and the LLM.

Naming: we prefix every MCP tool with ``mcp__<server_id>__`` so multiple
servers can coexist without collision and so ``ToolConstraints`` can
easily allow/deny a whole server with globs on the ``mcp__tavily__*``
prefix (handled at a higher layer) — the bare ``McpTool(server, name)``
syntax documented in ``schemas/common.py`` is a v2 concern and isn't
wired up here.
"""

from __future__ import annotations

from typing import Any

from agentloom.mcp.client import MCPClient, MCPClientError
from agentloom.schemas.common import ToolResult
from agentloom.tools.base import Tool, ToolContext, ToolError


def mcp_tool_name(server_id: str, tool_name: str) -> str:
    """Build the in-registry name for a remote MCP tool.

    Example: ``mcp_tool_name("tavily", "tavily_search") ->
    "mcp__tavily__tavily_search"``. The name is what the LLM sees in
    the tool list, so it must match ``[A-Za-z_][A-Za-z0-9_]*``.
    """
    # Replace anything non-identifier-safe with underscores. Servers in
    # the wild sometimes ship tools with dashes or dots in the name,
    # which would break the constraint regex.
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in tool_name)
    return f"mcp__{server_id}__{safe}"


class MCPRemoteTool(Tool):
    """Wraps one remote MCP tool behind the in-process ``Tool`` interface.

    The wrapper holds a reference to an already-connected
    :class:`MCPClient`. The client is owned by the surrounding
    :class:`agentloom.mcp.bridge.MCPToolSource` — the adapter never
    opens or closes connections itself.
    """

    def __init__(
        self,
        *,
        client: MCPClient,
        server_id: str,
        remote_name: str,
        description: str,
        input_schema: dict[str, Any],
    ) -> None:
        self._client = client
        self.server_id = server_id
        self.remote_name = remote_name
        self.name = mcp_tool_name(server_id, remote_name)
        self.description = description or f"Remote MCP tool {remote_name} from {server_id}"
        self.parameters = input_schema or {"type": "object", "properties": {}}

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        # Servers vary wildly in what their "main" arg is; surface a
        # stringified dict so constraint authors can glob over the
        # whole payload if they really need to.
        return str(args)

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            text, is_error = await self._client.call_tool(self.remote_name, args)
        except MCPClientError as exc:
            raise ToolError(str(exc)) from exc
        return ToolResult(content=text, is_error=is_error)
