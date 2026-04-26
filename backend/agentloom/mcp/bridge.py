"""Bridge between :class:`MCPClient` and ``ToolRegistry``.

A :class:`MCPToolSource` owns one connected client plus the list of
:class:`MCPRemoteTool` instances it registered. Closing the source
closes the underlying client and un-registers the tools, so a workflow
engine can attach/detach MCP servers dynamically without leaking
connections or polluting the registry across chatflows.

The normal boot sequence is::

    source = MCPToolSource(config)
    await source.connect_and_register(registry)
    # ... run workflow ...
    await source.close()

``connect_and_register`` is idempotent — calling it twice is a no-op
after the first success.
"""

from __future__ import annotations

from typing import Any

from agentloom.mcp.client import MCPClient, MCPClientError
from agentloom.mcp.tool_adapter import MCPRemoteTool
from agentloom.mcp.types import MCPServerConfig
from agentloom.tools.base import SideEffect, ToolRegistry


class MCPToolSource:
    """One connected MCP server, registered into a ToolRegistry."""

    def __init__(self, config: MCPServerConfig, *, read_timeout: float = 30.0) -> None:
        self.config = config
        self.client = MCPClient(config, read_timeout=read_timeout)
        self._registered_names: list[str] = []
        self._connected = False
        self.last_error: str | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def registered_names(self) -> list[str]:
        """Names (as seen by the registry) of every tool this source
        added. Useful for tests and for detach logic."""
        return list(self._registered_names)

    async def connect_and_register(self, registry: ToolRegistry) -> list[str]:
        """Connect, discover tools, and register them. Returns the new
        tool names in the order the server advertised them.

        If the server has no tools, registration is a no-op and an
        empty list is returned."""
        if self._connected:
            return list(self._registered_names)

        await self.client.connect()
        try:
            remote_tools = await self.client.list_tools()
        except MCPClientError:
            await self.client.close()
            raise

        added: list[str] = []
        for rt in remote_tools:
            wrapped = MCPRemoteTool(
                client=self.client,
                server_id=self.config.server_id,
                remote_name=rt.name,
                description=rt.description or "",
                input_schema=rt.inputSchema or {"type": "object", "properties": {}},
                side_effect=_mcp_side_effect(rt),
            )
            registry.register(wrapped)
            added.append(wrapped.name)

        self._registered_names = added
        self._connected = True
        return added

    async def close(self) -> None:
        """Close the underlying MCP client. Does NOT un-register tools
        from the registry — tools that outlive their source will start
        raising ``ToolError`` on execute, which the engine surfaces as
        a normal tool failure. Callers that want clean detachment can
        iterate over ``registered_names`` and delete entries before
        calling close."""
        self._connected = False
        await self.client.close()


def _mcp_side_effect(remote_tool: Any) -> SideEffect:
    """Map an upstream MCP tool's annotations to ``SideEffect``.

    MCP 0.1.0+ tool advertisements carry an optional ``annotations``
    block (server-declared hints). The relevant key is ``readOnlyHint``:
    ``True`` means the tool is read-only, ``False`` / missing means
    write or unknown — we default to ``WRITE`` to stay conservative
    (cognitive nodes won't accidentally see unmarked tools).

    Different MCP client libraries surface annotations slightly
    differently (attribute vs dict). We probe both shapes and tolerate
    everything else as ``WRITE``.
    """
    annotations = getattr(remote_tool, "annotations", None)
    if annotations is None:
        return SideEffect.WRITE
    # Dict-style annotations
    if isinstance(annotations, dict):
        if annotations.get("readOnlyHint") is True:
            return SideEffect.READ
        return SideEffect.WRITE
    # Pydantic / dataclass style
    hint = getattr(annotations, "readOnlyHint", None)
    if hint is True:
        return SideEffect.READ
    return SideEffect.WRITE
