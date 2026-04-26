"""GIN indices on ``board_items.produced_tags`` / ``consumed_tags``.

The ``produced_tags`` and ``consumed_tags`` JSONB columns have existed
since the ``board_items`` table was created (PR-2 retrieval scaffold)
but were unused — defaulted to empty lists, never populated by the
brief writer. This migration completes the picture for MemoryBoard's
"logical index" axis (Karpathy llm-wiki concept link graph): the brief
fixture starts emitting structured tag lists, and ``memoryboard_lookup``
gets a tag-filter path that uses these indices.

Two GIN indices, one per column. JSONB GIN supports the ``?`` /
``?|`` / ``?&`` operators for "contains key X" / "contains any of" /
"contains all of" — these are exactly what the lookup skill needs
when filtering by ``produced_tags=['rag', 'pgvector']``. The
``jsonb_path_ops`` opclass is faster but only supports ``@>``;
default GIN gives us the broader operator set.

SQLite (test) path: skip the index — SQLite's JSON support is
emulated and these indices wouldn't apply anyway. The lookup skill
falls back to a Python-level filter on small datasets.

Revision ID: 0019_board_items_tag_indices
Revises: 0018_board_items_drill_down
"""

from __future__ import annotations

from alembic import op

revision: str = "0019_board_items_tag_indices"
down_revision: str | None = "0018_board_items_drill_down"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_board_items_produced_tags "
        "ON board_items USING gin (produced_tags)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_board_items_consumed_tags "
        "ON board_items USING gin (consumed_tags)"
    )


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute("DROP INDEX IF EXISTS ix_board_items_consumed_tags")
    op.execute("DROP INDEX IF EXISTS ix_board_items_produced_tags")
