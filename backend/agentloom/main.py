"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentloom import __version__, tenancy_runtime
from agentloom.api import (
    chatflows,
    folders,
    health,
    mcp_servers,
    providers,
    tools,
    workflows,
    workspace_settings as workspace_settings_api,
)
from agentloom.config import get_settings
from agentloom.db.base import get_session_maker
from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
from agentloom.db.repositories.mcp_server import MCPServerRepository
from agentloom.db.repositories.workspace_settings import WorkspaceSettingsRepository
from agentloom.mcp import runtime as mcp_runtime


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown hooks."""
    mcp_runtime.init_runtime()
    try:
        session_maker = get_session_maker()
        async with session_maker() as session:
            settings_repo = WorkspaceSettingsRepository(
                session, workspace_id=DEFAULT_WORKSPACE_ID
            )
            ws_settings = await settings_repo.get()
            tenancy_runtime.set_settings(DEFAULT_WORKSPACE_ID, ws_settings)
            repo = MCPServerRepository(session, workspace_id=DEFAULT_WORKSPACE_ID)
            configs = await repo.list_all()
        await mcp_runtime.load_and_connect_all(configs)
    except Exception as exc:  # noqa: BLE001 — never fail-fast on MCP boot
        import logging
        logging.getLogger(__name__).exception("mcp: startup load failed")
        print(f"mcp: startup load failed: {exc!r}", flush=True)
    try:
        yield
    finally:
        await mcp_runtime.close_all()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Agentloom",
        version=__version__,
        description="Visual agent workflow platform.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"] if settings.environment == "dev" else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router, tags=["health"])
    app.include_router(workflows.router)
    app.include_router(chatflows.router)
    app.include_router(folders.router)
    app.include_router(providers.router)
    app.include_router(mcp_servers.router)
    app.include_router(tools.router)
    app.include_router(workspace_settings_api.router)
    return app


app = create_app()
