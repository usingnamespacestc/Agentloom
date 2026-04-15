"""One-shot seed: register Tavily as an HTTP MCP server.

Run once after the 0007 migration:

    cd ~/Agentloom/backend
    conda run -n agentloom python -m scripts.seed_tavily_mcp

Idempotent — re-running skips if a server with the same ``server_id``
exists. The Tavily API key is appended to the URL per their public
docs; we pull it from ``TAVILY_API_KEY``.
"""

from __future__ import annotations

import asyncio
import os
import sys

from agentloom.db.base import get_session_maker
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.mcp_server import MCPServerRepository
from agentloom.mcp.types import MCPServerConfig, MCPServerKind

SERVER_ID = "tavily"
ENV_VAR = "TAVILY_API_KEY"


async def main() -> int:
    api_key = os.environ.get(ENV_VAR)
    if not api_key:
        print(f"error: {ENV_VAR} not set in environment", file=sys.stderr)
        return 1

    session_maker = get_session_maker()
    async with session_maker() as session:
        repo = MCPServerRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
        existing = await repo.list_all()
        if any(s.server_id == SERVER_ID for s in existing):
            print(f"mcp server '{SERVER_ID}' already registered — skipping")
            return 0

        config = MCPServerConfig(
            server_id=SERVER_ID,
            friendly_name="Tavily Search",
            kind=MCPServerKind.HTTP,
            url=f"https://mcp.tavily.com/mcp/?tavilyApiKey={api_key}",
        )
        await repo.create(config)
        await session.commit()
        print(f"registered mcp server '{SERVER_ID}' (id={config.id})")
        print(f"  kind: {config.kind.value}")
        print(f"  url:  https://mcp.tavily.com/mcp/?tavilyApiKey=***")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
