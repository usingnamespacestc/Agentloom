"""Workspace and User tables.

MVP ships a singleton ``default`` workspace; the ``users`` table is
empty until auth arrives in M22. Presence from day one avoids a
retrofit migration later — see ADR-015 / ADR-017.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import utcnow

DEFAULT_WORKSPACE_ID = "default"


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Free-form settings bag. Currently holds ``tool_states`` — the
    #: per-tool default-allow / available / disabled state used by the
    #: ChatFlow engine and pre-fill logic. Room to grow without schema
    #: churn.
    payload: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class User(Base):
    """Empty until M22 auth lands.

    Identity + credentials come later; MVP has exactly zero rows here.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(256), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
