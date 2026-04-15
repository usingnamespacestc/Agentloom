"""Workspace settings REST endpoints — currently only ``tool_states``.

Routes:
- ``GET   /api/workspace/settings``  read the current workspace settings
- ``PATCH /api/workspace/settings``  replace fields and sync the runtime cache

Mutations update the DB *and* the in-process cache (:mod:`tenancy_runtime`)
so engine calls see the new state without a restart.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom import tenancy_runtime
from agentloom.db.base import get_session
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.workspace_settings import (
    WorkspaceNotFoundError,
    WorkspaceSettingsRepository,
)
from agentloom.schemas.workspace_settings import ToolState, WorkspaceSettings

router = APIRouter(prefix="/api/workspace/settings", tags=["workspace"])


def _repo(session: AsyncSession) -> WorkspaceSettingsRepository:
    return WorkspaceSettingsRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


class PatchWorkspaceSettingsRequest(BaseModel):
    tool_states: dict[str, ToolState] | None = None


@router.get("")
async def get_workspace_settings(
    session: AsyncSession = Depends(get_session),
) -> WorkspaceSettings:
    try:
        return await _repo(session).get()
    except WorkspaceNotFoundError as exc:
        raise HTTPException(404, "workspace not found") from exc


@router.patch("")
async def patch_workspace_settings(
    body: PatchWorkspaceSettingsRequest,
    session: AsyncSession = Depends(get_session),
) -> WorkspaceSettings:
    repo = _repo(session)
    try:
        current = await repo.get()
    except WorkspaceNotFoundError as exc:
        raise HTTPException(404, "workspace not found") from exc

    provided = body.model_fields_set
    if "tool_states" in provided and body.tool_states is not None:
        current.tool_states = body.tool_states

    await repo.save(current)
    await session.commit()
    tenancy_runtime.set_settings(DEFAULT_WORKSPACE_ID, current)
    return current
