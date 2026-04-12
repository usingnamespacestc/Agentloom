"""ChannelBinding table — M4 hook for future Discord/Feishu/etc adapters.

Empty in MVP apart from FakeAdapter integration tests. See ADR-016.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class ChannelBinding(Base):
    __tablename__ = "channel_bindings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False, index=True
    )
    chatflow_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("chatflows.id"), nullable=False, index=True
    )
    channel_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_ref: Mapped[str] = mapped_column(String(256), nullable=False)
    head_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
