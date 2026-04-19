"""node_index table — map any ChatNode / WorkNode id to its containing
chatflow so the ``get_node_context`` tool can fetch a node by id in O(1).

ChatNodes and WorkNodes both live inside ``chatflows.payload`` (JSONB),
so a raw SELECT by node id would have to scan every chatflow in the
workspace. A dedicated index table keeps that a single-row lookup.

Rows are maintained by :class:`agentloom.db.repositories.chatflow.ChatFlowRepository`
on every create/save; the chatflow-level ``ON DELETE CASCADE`` takes
care of cleanup when a chatflow is deleted. Pre-existing chatflows are
back-filled on app boot by ``main.py``'s lifespan hook.

Revision ID: 0010_node_index
Revises: 0009_provider_sub_kind_backfill
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0010_node_index"
down_revision: str | None = "0009_provider_sub_kind_backfill"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "node_index",
        sa.Column("node_id", sa.String(64), primary_key=True),
        sa.Column(
            "chatflow_id",
            sa.String(64),
            sa.ForeignKey("chatflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            sa.String(64),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
    )
    op.create_index("ix_node_index_chatflow_id", "node_index", ["chatflow_id"])
    op.create_index("ix_node_index_workspace_id", "node_index", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_node_index_workspace_id", table_name="node_index")
    op.drop_index("ix_node_index_chatflow_id", table_name="node_index")
    op.drop_table("node_index")
