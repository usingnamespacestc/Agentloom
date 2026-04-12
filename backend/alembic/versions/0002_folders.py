"""Add folders table and chatflows.folder_id

Revision ID: 0002_folders
Revises: 0001_initial
Create Date: 2026-04-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_folders"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(64),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_folders_workspace_id", "folders", ["workspace_id"])
    op.create_index("ix_folders_ws_created", "folders", ["workspace_id", "created_at"])

    op.add_column(
        "chatflows",
        sa.Column(
            "folder_id",
            sa.String(64),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_chatflows_folder_id", "chatflows", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_chatflows_folder_id", table_name="chatflows")
    op.drop_column("chatflows", "folder_id")
    op.drop_index("ix_folders_ws_created", table_name="folders")
    op.drop_index("ix_folders_workspace_id", table_name="folders")
    op.drop_table("folders")
