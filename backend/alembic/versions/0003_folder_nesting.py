"""Add folders.parent_id for nested folders

Revision ID: 0003_folder_nesting
Revises: 0002_folders
Create Date: 2026-04-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_folder_nesting"
down_revision: str | None = "0002_folders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "folders",
        sa.Column(
            "parent_id",
            sa.String(64),
            sa.ForeignKey("folders.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_folders_parent_id", table_name="folders")
    op.drop_column("folders", "parent_id")
