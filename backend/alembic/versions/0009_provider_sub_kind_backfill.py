"""Backfill providers.payload.provider_sub_kind.

``provider_sub_kind`` (see :class:`agentloom.schemas.provider.ProviderSubKind`)
lives inside the JSONB ``payload`` column, so no DDL is required. For
existing rows with ``provider_kind = 'anthropic_native'`` the classification
is deterministic → set ``provider_sub_kind = 'anthropic'``. All other
rows (``openai_compat``) are left NULL: the admin must pick between
``openai_chat`` / ``ollama`` / ``volcengine`` in the UI before per-model
params can be edited.

Revision ID: 0009_provider_sub_kind_backfill
Revises: 0008_workspace_payload
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_provider_sub_kind_backfill"
down_revision: str | None = "0008_workspace_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE providers
        SET payload = jsonb_set(payload, '{provider_sub_kind}', '"anthropic"'::jsonb, true)
        WHERE provider_kind = 'anthropic_native'
          AND (payload->>'provider_sub_kind') IS NULL
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE providers
        SET payload = payload - 'provider_sub_kind'
        WHERE payload ? 'provider_sub_kind'
        """
    )
