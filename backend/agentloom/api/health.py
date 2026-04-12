"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from agentloom import __version__

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Return service liveness."""
    return {"status": "ok", "version": __version__}
