"""MCPServerRow — persisted MCPServerConfig.

Workspace-scoped row holding one registered MCP server. The ``payload``
column carries the full ``MCPServerConfig`` JSON so adding optional
fields later doesn't require a migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class MCPServerRow(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    server_id: Mapped[str] = mapped_column(String(64), nullable=False)
    friendly_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
