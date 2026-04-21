"""Drop the dead ``forget_counter`` column from ``board_items``.

The forget-counter lives on :class:`CompactSnapshot.sticky_restored`
now (keyed by source node id), not on the BoardItem row. No code ever
read or decremented the column, so there's nothing to migrate.

Revision ID: 0017_drop_board_items_forget_counter
Revises: 0016_wipe_legacy_chatflows
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0017_drop_board_items_forget_counter"
down_revision: str | None = "0016_wipe_legacy_chatflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("board_items", "forget_counter")


def downgrade() -> None:
    op.add_column(
        "board_items",
        sa.Column(
            "forget_counter",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )
