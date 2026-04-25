"""Drill-down id lists on ``board_items``.

Adds two nullable JSONB columns so MemoryBoard items can carry the
node-id pointers that let downstream readers traverse one layer further
into the aggregate the row summarizes:

* ``inner_chat_ids`` — the ChatNode ids this item folds over. Set by
  pack rows (``packed_range``), merge rows (the merged parents), and
  compact rows (the single-hop upstream ChatNode). ``NULL`` for plain
  turns and for WorkBoard rows.
* ``work_node_ids`` — the WorkNode ids inside this ChatNode's WorkFlow
  that already carry their own WorkBoardItem. Lets a downstream agent
  drill from a ChatBoard summary into the inner WorkFlow without
  re-walking the JSONB payload. ``NULL`` when no node-scope brief was
  written (e.g. greeting roots, sub-engine writes without DB).

Both columns are populated only by *new* writes after this migration —
no backfill. Old rows stay ``NULL`` and the API contract treats those
as "drill-down not available", which is exactly the same UX as before
the columns existed.

Revision ID: 0018_board_items_drill_down
Revises: 0017_drop_board_items_forget_counter
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0018_board_items_drill_down"
down_revision: str | None = "0017_drop_board_items_forget_counter"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "board_items",
        sa.Column(
            "inner_chat_ids",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    op.add_column(
        "board_items",
        sa.Column(
            "work_node_ids",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("board_items", "work_node_ids")
    op.drop_column("board_items", "inner_chat_ids")
