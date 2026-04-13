"""Rename overrides_system -> builtin_id and add params_schema to workflow_templates.

Part of M10.0 (see docs/plan.md). System workflows become pure Templates
identified by ``builtin_id`` (ADR-008 / ADR-019). The engine looks up a
row by ``(workspace_id, builtin_id)`` with user workspace rows shadowing
the shipped ``__builtin__`` rows.

``overrides_system`` was the old name for this pointer; the semantics are
the same, but shipped fixture rows also use it now, so the clearer name
is ``builtin_id``.

``params_schema`` (JSONB, nullable) describes the required ``{{ param }}``
substitutions at instantiation time — purely advisory metadata for now.

Revision ID: 0005
Revises: 0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_workflow_templates_builtin_id"
down_revision = "0004_chatflow_description_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "workflow_templates",
        "overrides_system",
        new_column_name="builtin_id",
    )
    op.add_column(
        "workflow_templates",
        sa.Column(
            "params_schema",
            JSONB().with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
    )
    # Unique (workspace_id, builtin_id) where builtin_id IS NOT NULL, so
    # within any workspace a given builtin_id resolves to at most one row.
    # The shipped fixtures live under workspace_id="__builtin__"; a user
    # override lives under their own workspace_id and shadows the fixture.
    op.create_index(
        "ix_workflow_templates_workspace_builtin",
        "workflow_templates",
        ["workspace_id", "builtin_id"],
        unique=True,
        postgresql_where=sa.text("builtin_id IS NOT NULL"),
        sqlite_where=sa.text("builtin_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_templates_workspace_builtin",
        table_name="workflow_templates",
    )
    op.drop_column("workflow_templates", "params_schema")
    op.alter_column(
        "workflow_templates",
        "builtin_id",
        new_column_name="overrides_system",
    )
