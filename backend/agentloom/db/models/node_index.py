"""Side-index mapping every ChatNode / WorkNode id to its containing
ChatFlow so the ``get_node_context`` tool can look up a node in one
query. ChatNodes and WorkNodes both live inside ``chatflows.payload``
(JSONB), so without this table a resolver would have to scan every
ChatFlow row.

Maintained by :class:`agentloom.db.repositories.chatflow.ChatFlowRepository`
on every create/save; chatflow-level ``ON DELETE CASCADE`` handles
cleanup when a chatflow is deleted.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from agentloom.db.base import Base


class NodeIndexRow(Base):
    __tablename__ = "node_index"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chatflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("chatflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )
    #: ``"chatnode"`` or ``"worknode"`` — used by the resolver to decide
    #: which shape to return without having to re-parse the payload.
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
