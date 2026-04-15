"""MCP server REST endpoints.

Surface:
- ``GET    /api/mcp-servers``               list servers + live runtime state
- ``POST   /api/mcp-servers``               create + connect
- ``GET    /api/mcp-servers/{id}``          one server
- ``PATCH  /api/mcp-servers/{id}``          edit + reconnect
- ``DELETE /api/mcp-servers/{id}``          remove + close
- ``POST   /api/mcp-servers/{id}/reconnect`` re-attempt connect

Plus a sibling tools surface so the UI can show what's exposed:

- ``GET    /api/tools``                      every registered tool (built-in + MCP)

Mutations always update the DB *and* the live runtime so changes take
effect without a restart.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from agentloom.db.base import get_session
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.mcp_server import (
    MCPServerNotFoundError,
    MCPServerRepository,
)
from agentloom.mcp import runtime as mcp_runtime
from agentloom.mcp.types import MCPServerConfig, MCPServerKind
from agentloom.schemas.common import utcnow

router = APIRouter(prefix="/api/mcp-servers", tags=["mcp"])


def _repo(session: AsyncSession) -> MCPServerRepository:
    return MCPServerRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)


# ---------------------------------------------------------------- schemas


class CreateMCPServerRequest(BaseModel):
    server_id: str
    friendly_name: str
    kind: MCPServerKind
    url: str | None = None
    headers: dict[str, str] = {}
    command: str | None = None
    args: list[str] = []
    env: dict[str, str] = {}
    enabled: bool = True


class PatchMCPServerRequest(BaseModel):
    friendly_name: str | None = None
    enabled: bool | None = None
    url: str | None = None
    headers: dict[str, str] | None = None
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None


def _state_for(config: MCPServerConfig) -> dict:
    """Build the response payload for a single server.

    Looks up the live source so callers see ``is_connected`` and
    ``last_error``. If the runtime has no source for this id (e.g. the
    process just started and load failed silently) we still return the
    config bits with ``is_connected=False``.
    """
    source = mcp_runtime.get_source(config.id)
    if source is not None:
        return mcp_runtime.get_state(source)
    return {
        "id": config.id,
        "server_id": config.server_id,
        "friendly_name": config.friendly_name,
        "kind": config.kind.value,
        "enabled": config.enabled,
        "url": config.url,
        "command": config.command,
        "is_connected": False,
        "tool_count": 0,
        "tool_names": [],
        "last_error": None,
    }


# ---------------------------------------------------------------- routes


@router.get("")
async def list_mcp_servers(
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    configs = await _repo(session).list_all()
    return [_state_for(c) for c in configs]


@router.post("")
async def create_mcp_server(
    body: CreateMCPServerRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        config = MCPServerConfig(
            server_id=body.server_id,
            friendly_name=body.friendly_name,
            kind=body.kind,
            url=body.url,
            headers=body.headers,
            command=body.command,
            args=body.args,
            env=body.env,
            enabled=body.enabled,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    repo = _repo(session)
    await repo.create(config)
    await session.commit()
    await mcp_runtime.add_source(config)
    return _state_for(config)


@router.get("/{server_pk}")
async def get_mcp_server(
    server_pk: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        config = await repo.get(server_pk)
    except MCPServerNotFoundError as exc:
        raise HTTPException(404, f"mcp server {server_pk} not found") from exc
    return _state_for(config)


@router.patch("/{server_pk}")
async def patch_mcp_server(
    server_pk: str,
    body: PatchMCPServerRequest,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        config = await repo.get(server_pk)
    except MCPServerNotFoundError as exc:
        raise HTTPException(404, f"mcp server {server_pk} not found") from exc

    provided = body.model_fields_set
    if "friendly_name" in provided and body.friendly_name is not None:
        config.friendly_name = body.friendly_name
    if "enabled" in provided and body.enabled is not None:
        config.enabled = body.enabled
    if "url" in provided:
        config.url = body.url
    if "headers" in provided and body.headers is not None:
        config.headers = body.headers
    if "command" in provided:
        config.command = body.command
    if "args" in provided and body.args is not None:
        config.args = body.args
    if "env" in provided and body.env is not None:
        config.env = body.env
    config.updated_at = utcnow()

    # Re-validate kind invariants after mutation.
    try:
        config = MCPServerConfig.model_validate(config.model_dump(mode="json"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    await repo.update(config)
    await session.commit()
    await mcp_runtime.reconnect_source(config)
    return _state_for(config)


@router.delete("/{server_pk}")
async def delete_mcp_server(
    server_pk: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        await repo.delete(server_pk)
    except MCPServerNotFoundError as exc:
        raise HTTPException(404, f"mcp server {server_pk} not found") from exc
    await session.commit()
    await mcp_runtime.remove_source(server_pk)
    return {"ok": True}


@router.post("/{server_pk}/reconnect")
async def reconnect_mcp_server(
    server_pk: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    repo = _repo(session)
    try:
        config = await repo.get(server_pk)
    except MCPServerNotFoundError as exc:
        raise HTTPException(404, f"mcp server {server_pk} not found") from exc
    await mcp_runtime.reconnect_source(config)
    return _state_for(config)
