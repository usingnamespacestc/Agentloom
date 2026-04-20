"""MemoryBoard brief foundation: board_items table + draft_model rename.

Creates the ``board_items`` table backing :class:`BoardItemRow` (see
``docs/design-memoryboard-brief.md`` §3) with the two lookup indices
called for in the design. Also renames the legacy ``default_model``
JSONB key inside persisted chatflow payloads to ``draft_model`` so
the freshly-renamed ChatFlow schema round-trips existing rows cleanly.

The schema-layer ``_accept_legacy_default_model`` validator keeps in-
flight Pydantic deserialization accepting either key during the
transition, but persisted rows get the canonical new name here.

Revision ID: 0012_memoryboard_brief
Revises: 0011_execution_mode_rename
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0012_memoryboard_brief"
down_revision: str | None = "0011_execution_mode_rename"
branch_labels = None
depends_on = None


def _rename_default_model_key(up: bool) -> None:
    """Rewrite the top-level ``default_model`` JSONB key on every
    chatflow payload to ``draft_model`` (or reverse on downgrade).

    Uses ``jsonb_set`` / ``json_set`` so only that one key is touched
    — nested occurrences inside nested workflow payloads or
    workflow_template.plan are left alone (they don't exist today but
    if they ever do, it's an unrelated field).
    """
    bind = op.get_bind()
    dialect = bind.dialect.name
    old, new = ("default_model", "draft_model") if up else ("draft_model", "default_model")

    if dialect == "postgresql":
        # jsonb_set inserts the new key; then subtract (via ``- old``)
        # to remove the old one. WHERE filter keeps the UPDATE cheap
        # on large tables.
        op.execute(
            f"""
            UPDATE chatflows
            SET payload = jsonb_set(payload, '{{{new}}}', payload -> '{old}')
                          - '{old}'
            WHERE payload ? '{old}'
            """
        )
    else:
        # SQLite (test DB): payload is stored as TEXT, a literal
        # replace of the JSON key is safe because neither key appears
        # anywhere except as an object key name (we always quote the
        # field name with an adjacent colon).
        op.execute(
            f"""
            UPDATE chatflows
            SET payload = REPLACE(payload, '"{old}":', '"{new}":')
            WHERE payload LIKE '%"{old}":%'
            """
        )


def upgrade() -> None:
    op.create_table(
        "board_items",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(64),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "chatflow_id",
            sa.String(64),
            sa.ForeignKey("chatflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(64), nullable=True),
        sa.Column("source_node_id", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "produced_tags",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "consumed_tags",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "fallback", sa.Boolean, nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "forget_counter", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_board_items_ws_cf_scope",
        "board_items",
        ["workspace_id", "chatflow_id", "scope"],
    )
    op.create_index(
        "ix_board_items_source_node_id",
        "board_items",
        ["source_node_id"],
    )

    _rename_default_model_key(up=True)


def downgrade() -> None:
    _rename_default_model_key(up=False)
    op.drop_index("ix_board_items_source_node_id", table_name="board_items")
    op.drop_index("ix_board_items_ws_cf_scope", table_name="board_items")
    op.drop_table("board_items")
