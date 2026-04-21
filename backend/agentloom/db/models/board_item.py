"""MemoryBoard storage — one row per summarized source node.

Produced by the MemoryBoard brief WorkNode (PR 1 of the MemoryBoard
series, 2026-04-20). Each :class:`BoardItemRow` captures the short
prose description distilled from a ChatNode or WorkNode so downstream
readers can look at a node's take-away without re-reading the full
content. PR 1 only writes rows; the reader side and the retrieval
indices land in PR 2.

Column layout matches §3 of ``docs/design-memoryboard-brief.md``. The
tag fields are reserved for PR 2's labeled-tag search precision story —
they default to empty lists and are not populated yet.

Tenancy: every row is scoped by ``workspace_id`` and ``chatflow_id``
(both foreign keys). ``workflow_id`` is null for ChatBoard items
(ChatNode-scoped, PR 3) and set for WorkBoard items (WorkNode /
WorkFlow-scoped, PR 1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base
from agentloom.schemas.common import generate_node_id, utcnow


class BoardItemRow(Base):
    __tablename__ = "board_items"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_node_id)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    chatflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chatflows.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Null for ChatBoardItem (ChatNode-scoped — PR 3); set for
    #: WorkBoardItem (WorkNode / WorkFlow-scoped — PR 1). No FK because
    #: WorkFlows live inside ``chatflows.payload`` JSONB, not a standalone
    #: table.
    workflow_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The ChatNode or WorkNode whose content this item summarizes.
    #: No FK for the same reason: nodes live in JSONB.
    source_node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Node kind label. For WorkNodes this is the :class:`StepKind`
    #: string (``"draft"``, ``"tool_call"``, ``"judge_call"``,
    #: ``"merge"``, ``"compress"``, ``"delegate"``). For
    #: ChatNodes (PR 3) this is ``"chat_turn"``. ``"flow"`` tags the
    #: WorkFlow-level flow-brief produced by a ``scope=FLOW`` brief.
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``"chat"`` (PR 3 ChatBoardItem), ``"node"`` (PR 1 node-brief),
    #: or ``"flow"`` (PR 1 flow-brief). Matches
    #: :class:`agentloom.schemas.common.NodeScope` plus the PR 3 value.
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Reserved for PR 2 — labeled tags for search precision. Default
    #: empty list so existing callers don't have to supply them.
    produced_tags: Mapped[list[Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    consumed_tags: Mapped[list[Any]] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False, default=list
    )
    #: True when the description came from the deterministic code
    #: template (tool_call source or LLM failure fallback) rather than
    #: a live LLM call. Lets downstream consumers weigh reliability.
    fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        Index(
            "ix_board_items_ws_cf_scope",
            "workspace_id",
            "chatflow_id",
            "scope",
        ),
        Index("ix_board_items_source_node_id", "source_node_id"),
    )
