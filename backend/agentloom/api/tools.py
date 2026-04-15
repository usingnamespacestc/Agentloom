"""Tools surface — read-only listing of every tool exposed to the LLM.

Mainly used by the Settings UI to confirm what's available after wiring
an MCP server, and to drive the per-ChatFlow allow/deny picker.
"""

from __future__ import annotations

from fastapi import APIRouter

from agentloom.mcp import runtime as mcp_runtime

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
async def list_tools() -> list[dict]:
    """Every registered tool — built-ins plus all connected MCP tools."""
    registry = mcp_runtime.get_shared_registry()
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in registry.all()
    ]
