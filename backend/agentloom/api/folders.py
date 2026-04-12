"""Folder REST endpoints.

Surface:
- ``GET    /api/folders``             list all folders
- ``POST   /api/folders``             create folder
- ``PATCH  /api/folders/{id}``        rename folder
- ``DELETE /api/folders/{id}``        delete folder + cascade chatflows
- ``PATCH  /api/chatflows/{id}/folder``  move chatflow into/out of folder
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom.db.base import get_session
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.folder import FolderNotFoundError, FolderRepository

router = APIRouter(prefix="/api/folders", tags=["folders"])


def _repo(session: AsyncSession) -> FolderRepository:
    return FolderRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


class CreateFolderRequest(BaseModel):
    name: str
    parent_id: str | None = None


class CreateFolderResponse(BaseModel):
    id: str
    name: str


class PatchFolderRequest(BaseModel):
    name: str | None = None
    parent_id: str | None = "__unset__"  # distinguish "move to root" (null) from "not provided"


@router.get("")
async def list_folders(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    return await _repo(session).list_all()


@router.post("", response_model=CreateFolderResponse)
async def create_folder(
    body: CreateFolderRequest,
    session: AsyncSession = Depends(get_session),
) -> CreateFolderResponse:
    row = await _repo(session).create(body.name, parent_id=body.parent_id)
    await session.commit()
    return CreateFolderResponse(id=row.id, name=row.name)


@router.patch("/{folder_id}")
async def patch_folder(
    folder_id: str,
    body: PatchFolderRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        if body.name is not None:
            await repo.rename(folder_id, body.name)
        if body.parent_id != "__unset__":
            await repo.move(folder_id, body.parent_id)
    except FolderNotFoundError as exc:
        raise HTTPException(404, f"folder {folder_id} not found") from exc
    await session.commit()
    return {"ok": True}


@router.delete("/{folder_id}")
async def delete_folder(
    folder_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a folder and all chatflows in it."""
    from agentloom.api.chatflows import _get_engine

    engine = _get_engine(request)
    repo = _repo(session)
    try:
        chatflow_ids = await repo.delete(folder_id)
    except FolderNotFoundError as exc:
        raise HTTPException(404, f"folder {folder_id} not found") from exc
    # Detach any engine runtimes for the deleted chatflows.
    for cid in chatflow_ids:
        if engine.get_runtime(cid) is not None:
            await engine.detach(cid)
    await session.commit()
    return {"ok": True, "deleted_chatflows": chatflow_ids}
