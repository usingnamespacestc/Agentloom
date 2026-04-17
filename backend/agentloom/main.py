"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    """Startup and shutdown hooks.

    MCP connect is fired as a background task so a slow or unreachable
    remote server can't block the app from serving. Tools that reference
    not-yet-connected MCP servers will 404 until the background task
    finishes — that's the tradeoff for non-blocking startup.
    """
    mcp_runtime.init_runtime()
    mcp_task: asyncio.Task[None] | None = None
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
        mcp_task = asyncio.create_task(
            mcp_runtime.load_and_connect_all(configs), name="mcp-connect-all"
        )
    except Exception as exc:  # noqa: BLE001 — never fail-fast on MCP boot
        logging.getLogger(__name__).exception("mcp: startup load failed")
        print(f"mcp: startup load failed: {exc!r}", flush=True)
    try:
        yield
    finally:
        if mcp_task is not None and not mcp_task.done():
            mcp_task.cancel()
            try:
                await mcp_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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

    @app.exception_handler(RequestValidationError)
    async def _log_422(request: Request, exc: RequestValidationError) -> JSONResponse:
        from fastapi.encoders import jsonable_encoder
        body = b""
        try:
            body = await request.body()
        except Exception:  # noqa: BLE001
            pass
        logging.getLogger(__name__).warning(
            "422 on %s %s\n  content-type=%s\n  headers=%s\n  errors=%s\n  body=%s",
            request.method,
            request.url.path,
            request.headers.get("content-type"),
            dict(request.headers),
            exc.errors(),
            body[:2000],
        )
        # ``exc.errors()`` may embed non-JSON-serializable objects (bytes,
        # ValueError instances) in the ``input``/``ctx`` fields.
        # ``jsonable_encoder`` coerces them to strings so the response can
        # serialize.
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})

    return app


app = create_app()
