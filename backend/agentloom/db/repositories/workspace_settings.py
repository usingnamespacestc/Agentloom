"""Workspace settings repository — reads/writes the ``payload`` JSONB."""

from __future__ import annotations

from sqlalchemy import select

from agentloom.db.models.tenancy import Workspace
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.schemas.workspace_settings import WorkspaceSettings


class WorkspaceNotFoundError(KeyError):
    pass


class WorkspaceSettingsRepository(WorkspaceScopedRepository):
    async def get(self) -> WorkspaceSettings:
        stmt = select(Workspace).where(Workspace.id == self.workspace_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WorkspaceNotFoundError(self.workspace_id)
        return WorkspaceSettings.model_validate(row.payload or {})

    async def save(self, settings: WorkspaceSettings) -> None:
        stmt = select(Workspace).where(Workspace.id == self.workspace_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WorkspaceNotFoundError(self.workspace_id)
        row.payload = settings.model_dump(mode="json")
        await self.session.flush()
