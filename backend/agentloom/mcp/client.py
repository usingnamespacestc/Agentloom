"""Thin async wrapper around the official ``mcp`` Python SDK.

Why we wrap it:

1. **Lifecycle**. The SDK exposes its transports as async context
   managers — ``streamable_http_client`` yields stream pairs that you
   hand to a ``ClientSession``, which itself is an async context
   manager. Holding both open across multiple tool calls means nesting
   two ``async with`` blocks at the point of use, which is awkward to
   plumb through a long-running tool registry. We flatten that into a
   single object with ``connect()`` / ``close()``.

2. **Error translation**. Anything the SDK raises gets wrapped in
   :class:`MCPClientError` so callers (the tool adapter, the engine)
   can catch a single exception type instead of importing SDK
   internals.

3. **Result shaping**. ``CallToolResult`` blocks are flattened into a
   plain ``(text, is_error)`` tuple for consumption by the engine's
   ``ToolResult`` type. Structured content and other block kinds can be
   exposed later without breaking callers.

ADR-013 is not affected: MCP calls are opaque leaf tools from the
engine's perspective — no message reordering happens here.
"""

from __future__ import annotations

import contextlib
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp import types as mcp_types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from agentloom.mcp.types import MCPServerConfig, MCPServerKind


class MCPClientError(Exception):
    """Raised for any MCP-level failure (connect, list, call)."""


class MCPClient:
    """Single-server async MCP client.

    Typical usage::

        client = MCPClient(config)
        await client.connect()
        tools = await client.list_tools()
        text, is_error = await client.call_tool("search", {"query": "cats"})
        await client.close()

    Not safe for concurrent use — one client per ``asyncio`` task. If
    you need parallelism, open multiple clients.
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        read_timeout: float = 30.0,
    ) -> None:
        self.config = config
        self._read_timeout = read_timeout
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    # ------------------------------------------------------------------ lifecycle

    async def connect(self) -> None:
        """Open the transport + initialise the MCP session."""
        if self._session is not None:
            return  # already connected; idempotent
        stack = AsyncExitStack()
        try:
            if self.config.kind == MCPServerKind.HTTP:
                assert self.config.url is not None
                # The new ``streamable_http_client`` API doesn't accept
                # ``headers`` or ``timeout`` directly — build an httpx
                # client with those pre-baked via the SDK's factory and
                # pass it in. Owned by this stack so it gets torn down
                # when we close.
                http_client = create_mcp_http_client(
                    headers=self.config.headers or None,
                    timeout=httpx.Timeout(self._read_timeout),
                )
                await stack.enter_async_context(http_client)
                read_s, write_s, _get_id = await stack.enter_async_context(
                    streamable_http_client(
                        url=self.config.url,
                        http_client=http_client,
                    )
                )
            elif self.config.kind == MCPServerKind.STDIO:
                assert self.config.command is not None
                params = StdioServerParameters(
                    command=self.config.command,
                    args=list(self.config.args),
                    env=dict(self.config.env) if self.config.env else None,
                )
                read_s, write_s = await stack.enter_async_context(stdio_client(params))
            else:  # pragma: no cover — validated upstream
                raise MCPClientError(f"unknown server kind: {self.config.kind}")

            session = await stack.enter_async_context(
                ClientSession(
                    read_s,
                    write_s,
                    read_timeout_seconds=timedelta(seconds=self._read_timeout),
                )
            )
            await session.initialize()
        except Exception as exc:  # pragma: no cover — defensive
            await stack.aclose()
            raise MCPClientError(f"connect failed: {exc}") from exc

        self._exit_stack = stack
        self._session = session

    async def close(self) -> None:
        """Tear down the session and transport."""
        stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def __aenter__(self) -> "MCPClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------ RPC

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise MCPClientError("MCP client is not connected")
        return self._session

    async def list_tools(self) -> list[mcp_types.Tool]:
        """Return every tool advertised by the server.

        The SDK's pagination is transparent for servers that fit in one
        page (all current public servers do). We can revisit chunked
        fetches if a server ever exceeds the default limit.
        """
        session = self._require_session()
        try:
            result = await session.list_tools()
        except Exception as exc:
            raise MCPClientError(f"list_tools failed: {exc}") from exc
        return list(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Invoke a remote tool and return (text, is_error).

        The MCP result is a list of content blocks; we concatenate any
        ``text`` blocks with newlines. Non-text blocks (image, resource
        reference, audio) are summarised with a ``[<kind> block]``
        placeholder so the LLM knows something was returned without us
        having to pipe binary bytes through the engine.
        """
        session = self._require_session()
        try:
            result = await session.call_tool(name, arguments or {})
        except Exception as exc:
            raise MCPClientError(f"call_tool {name!r} failed: {exc}") from exc

        parts: list[str] = []
        for block in result.content or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                parts.append(getattr(block, "text", "") or "")
            else:
                parts.append(f"[{btype or 'unknown'} block]")
        text = "\n".join(parts) if parts else ""
        return text, bool(result.isError)
