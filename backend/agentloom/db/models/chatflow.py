"""ChatFlow tables — stubbed for M3, populated in M4.

The ``chatflows`` row is the top-level conversation DAG; its inner
WorkFlows live under the ChatFlowNodes stored in ``payload``. The
per-node WorkFlow is serialized alongside so a ChatFlow round-trip is
one JSONB column.

``chatflow_shares`` exists from M1 per ADR-017 but is empty until v2+.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, PrimaryKeyConstraint, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class ChatFlowRow(Base):
    __tablename__ = "chatflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True, index=True
    )
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (Index("ix_chatflows_ws_created", "workspace_id", "created_at"),)


class ChatFlowShare(Base):
    """v2+ share table. Present and empty in MVP per ADR-017."""

    __tablename__ = "chatflow_shares"

    chatflow_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chatflows.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False)  # "read" | "write"
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    granted_by: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True
    )

    __table_args__ = (PrimaryKeyConstraint("chatflow_id", "user_id"),)
