"""WorkflowRow — one persisted WorkFlow.

The WorkFlow's nodes, edges, statuses, and usage are stored as a JSONB
blob (``payload``) that round-trips through ``agentloom.schemas.WorkFlow``.
Invariants (frozen nodes, DAG acyclicity, workspace scope) are enforced
in the repository layer, not by SQL constraints.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class WorkflowRow(Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True, index=True
    )

    # The full serialized WorkFlow (schemas.WorkFlow.model_dump()).
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (Index("ix_workflows_ws_created", "workspace_id", "created_at"),)
