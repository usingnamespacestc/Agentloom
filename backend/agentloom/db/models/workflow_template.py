"""WorkflowTemplate table — stub for M14.

Saved as a JSONB plan per ADR-007 (templates are saved Plans, not
Executions).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class WorkflowTemplateRow(Base):
    __tablename__ = "workflow_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False, default="")
    overrides_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
