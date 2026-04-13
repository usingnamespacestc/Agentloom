"""Seed the ``__builtin__`` workspace row.

System-workflow template fixtures (M10.1) live under this workspace.
Every ``workflow_templates`` row FKs to ``workspaces.id``, so the row
must exist before the loader runs at startup.

The loader (``agentloom.templates.loader.upsert_builtin_templates``)
also self-heals this row if missing, but putting the insert in a
dedicated migration makes the intent visible in the schema history and
keeps ``create_all`` test paths aligned with production.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006_seed_builtin_workspace"
down_revision = "0005_workflow_templates_builtin_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO workspaces (id, name, created_at) "
        "VALUES ('__builtin__', '__builtin__', now()) "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    # Only remove the row if no template still references it; otherwise
    # the FK will protect us and the downgrade aborts.
    op.execute(
        "DELETE FROM workspaces WHERE id = '__builtin__' "
        "AND NOT EXISTS (SELECT 1 FROM workflow_templates "
        "WHERE workspace_id = '__builtin__')"
    )
