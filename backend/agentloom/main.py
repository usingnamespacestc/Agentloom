"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentloom import __version__
from agentloom.api import chatflows, health, workflows
from agentloom.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown hooks."""
    # Future: DB init, Redis connect, MCP servers attach, rate limiter load
    yield


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
    return app


app = create_app()
