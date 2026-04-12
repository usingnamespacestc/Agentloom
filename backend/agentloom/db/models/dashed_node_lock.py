"""DashedNodeLock — advisory lock for concurrent dashed-node edits.

v2+ collaborative editing populates this table. Empty in MVP.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base


class DashedNodeLock(Base):
    __tablename__ = "dashed_node_locks"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
