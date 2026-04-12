"""initial schema — all tenancy + workflow tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tenancy ---
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("email", sa.String(256), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Seed the singleton workspace.
    op.execute(
        "INSERT INTO workspaces (id, name, created_at) "
        "VALUES ('default', 'default', now())"
    )

    # --- providers ---
    op.create_table(
        "providers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("friendly_name", sa.String(128), nullable=False),
        sa.Column("provider_kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_providers_workspace_id", "providers", ["workspace_id"])

    # --- chatflows ---
    op.create_table(
        "chatflows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("title", sa.String(256), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chatflows_workspace_id", "chatflows", ["workspace_id"])
    op.create_index("ix_chatflows_owner_id", "chatflows", ["owner_id"])
    op.create_index("ix_chatflows_ws_created", "chatflows", ["workspace_id", "created_at"])

    op.create_table(
        "chatflow_shares",
        sa.Column("chatflow_id", sa.String(64), sa.ForeignKey("chatflows.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("permission", sa.String(16), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("granted_by", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.PrimaryKeyConstraint("chatflow_id", "user_id"),
    )

    # --- workflows ---
    op.create_table(
        "workflows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workflows_workspace_id", "workflows", ["workspace_id"])
    op.create_index("ix_workflows_owner_id", "workflows", ["owner_id"])
    op.create_index("ix_workflows_ws_created", "workflows", ["workspace_id", "created_at"])

    # --- workflow_templates (stub) ---
    op.create_table(
        "workflow_templates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1024), nullable=False),
        sa.Column("overrides_system", sa.String(64), nullable=True),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_workflow_templates_workspace_id", "workflow_templates", ["workspace_id"]
    )

    # --- channel_bindings (ADR-016 hook) ---
    op.create_table(
        "channel_bindings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("chatflow_id", sa.String(64), sa.ForeignKey("chatflows.id"), nullable=False),
        sa.Column("channel_kind", sa.String(32), nullable=False),
        sa.Column("external_ref", sa.String(256), nullable=False),
        sa.Column("head_node_id", sa.String(64), nullable=True),
        sa.Column("config_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_channel_bindings_workspace_id", "channel_bindings", ["workspace_id"]
    )
    op.create_index("ix_channel_bindings_chatflow_id", "channel_bindings", ["chatflow_id"])

    # --- dashed_node_locks (ADR-017 v2+ hook) ---
    op.create_table(
        "dashed_node_locks",
        sa.Column("node_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_ws_created", "audit_log", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_ws_created", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("dashed_node_locks")
    op.drop_index("ix_channel_bindings_chatflow_id", table_name="channel_bindings")
    op.drop_index("ix_channel_bindings_workspace_id", table_name="channel_bindings")
    op.drop_table("channel_bindings")
    op.drop_index("ix_workflow_templates_workspace_id", table_name="workflow_templates")
    op.drop_table("workflow_templates")
    op.drop_index("ix_workflows_ws_created", table_name="workflows")
    op.drop_index("ix_workflows_owner_id", table_name="workflows")
    op.drop_index("ix_workflows_workspace_id", table_name="workflows")
    op.drop_table("workflows")
    op.drop_table("chatflow_shares")
    op.drop_index("ix_chatflows_ws_created", table_name="chatflows")
    op.drop_index("ix_chatflows_owner_id", table_name="chatflows")
    op.drop_index("ix_chatflows_workspace_id", table_name="chatflows")
    op.drop_table("chatflows")
    op.drop_index("ix_providers_workspace_id", table_name="providers")
    op.drop_table("providers")
    op.drop_table("users")
    op.drop_table("workspaces")
