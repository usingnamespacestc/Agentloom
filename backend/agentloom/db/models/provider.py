"""ProviderRow — persisted ProviderConfig.

Full CRUD in M4. Present in the initial migration so the schema is
stable from day one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class ProviderRow(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    owner_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=True
    )
    friendly_name: Mapped[str] = mapped_column(String(128), nullable=False)
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    base_url: Mapped[str] = mapped_column(String(512), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
