"""workspaces.payload JSONB — per-workspace settings bag.

Currently carries ``tool_states``: a mapping of tool name to one of
``default_allow`` / ``available`` / ``disabled`` that governs which
tools are visible to LLM calls globally and which get pre-listed in
a fresh ChatFlow's ``disabled_tool_names``. Extra fields may land in
this column later without schema churn.

Revision ID: 0008_workspace_payload
Revises: 0007_mcp_servers
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_workspace_payload"
down_revision: str | None = "0007_mcp_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "payload")
