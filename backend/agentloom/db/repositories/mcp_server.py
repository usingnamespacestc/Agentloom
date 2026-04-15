"""MCP server repository — CRUD for MCPServerConfig via JSONB payload."""

from __future__ import annotations

from sqlalchemy import select

from agentloom.db.models.mcp_server import MCPServerRow
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.mcp.types import MCPServerConfig


class MCPServerNotFoundError(KeyError):
    pass


class MCPServerRepository(WorkspaceScopedRepository):
    async def create(self, config: MCPServerConfig) -> MCPServerRow:
        row = MCPServerRow(
            id=config.id,
            workspace_id=self.workspace_id,
            server_id=config.server_id,
            friendly_name=config.friendly_name,
            kind=config.kind.value,
            payload=config.model_dump(mode="json"),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, server_pk: str) -> MCPServerConfig:
        stmt = (
            select(MCPServerRow)
            .where(MCPServerRow.workspace_id == self.workspace_id)
            .where(MCPServerRow.id == server_pk)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise MCPServerNotFoundError(server_pk)
        return MCPServerConfig.model_validate(row.payload)

    async def list_all(self) -> list[MCPServerConfig]:
        stmt = (
            select(MCPServerRow)
            .where(MCPServerRow.workspace_id == self.workspace_id)
            .order_by(MCPServerRow.created_at)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [MCPServerConfig.model_validate(r.payload) for r in rows]

    async def update(self, config: MCPServerConfig) -> MCPServerRow:
        """Persist edits to an existing row. Raises if the id is gone."""
        stmt = (
            select(MCPServerRow)
            .where(MCPServerRow.workspace_id == self.workspace_id)
            .where(MCPServerRow.id == config.id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise MCPServerNotFoundError(config.id)
        row.server_id = config.server_id
        row.friendly_name = config.friendly_name
        row.kind = config.kind.value
        row.payload = config.model_dump(mode="json")
        await self.session.flush()
        return row

    async def delete(self, server_pk: str) -> None:
        stmt = (
            select(MCPServerRow)
            .where(MCPServerRow.workspace_id == self.workspace_id)
            .where(MCPServerRow.id == server_pk)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise MCPServerNotFoundError(server_pk)
        await self.session.delete(row)
        await self.session.flush()
