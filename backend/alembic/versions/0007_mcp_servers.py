"""mcp_servers table — persisted MCPServerConfig per workspace.

Carries the JSON payload for an MCPServerConfig (kind, url/command,
headers/env, etc.). The lifespan hook in ``main.py`` reads these rows
on startup and connects each enabled server into the shared
``ToolRegistry`` so MCP tools become available to every chatflow.

Revision ID: 0007_mcp_servers
Revises: 0006_seed_builtin_workspace
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_mcp_servers"
down_revision: str | None = "0006_seed_builtin_workspace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(64),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("server_id", sa.String(64), nullable=False),
        sa.Column("friendly_name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_mcp_servers_workspace_id", "mcp_servers", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_workspace_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
