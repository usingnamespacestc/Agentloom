"""WorkflowTemplate table.

System workflows ship as YAML fixtures loaded into this table under
``workspace_id = "__builtin__"`` (ADR-008 / ADR-019); user overrides live
under the user's own workspace and shadow the fixture by matching
``builtin_id``. User-authored free-form templates carry ``builtin_id =
NULL``.

Saved as a JSONB ``plan`` per ADR-007 (templates are saved Plans, not
Executions). ``params_schema`` describes the ``{{ param }}`` substitutions
expected at instantiation — advisory metadata for now, enforced by the
loader in M10.1.
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
    #: Stable key that identifies a system workflow (e.g. ``"plan"``,
    #: ``"judge_pre"``, ``"merge"``). Shipped fixtures and user overrides
    #: share this value so the lookup ``(workspace_id, builtin_id)`` can
    #: return a user override when present, else the builtin fixture.
    #: ``NULL`` for free-form user templates (M14+).
    builtin_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan: Mapped[dict[str, Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    #: Advisory description of required substitutions. The loader rejects
    #: unbound ``{{ param }}`` placeholders regardless — the schema makes
    #: the contract visible in the UI. M10.1 will start using this.
    params_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
