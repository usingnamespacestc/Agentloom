"""Wipe legacy ChatFlow data so PR 4 can stop carrying Snapshot→BoardItem
read-path compatibility shims.

PR 4.2.c (b54c7e4) removed ``shared_notes``; PR 4.3 was supposed to
migrate the remaining ``CompactSnapshot`` / ``MergeSnapshot`` blobs
into the BoardItem view. Since every row in these tables is dev-era
exploration — nothing production, nothing irreplaceable — we just
delete instead. Dropping the rows lets PR 4.3 simply remove the
Snapshot read paths rather than writing a dual-track migrator.

Wipes (child tables first to stay FK-safe):
    - board_items
    - node_index
    - dashed_node_locks
    - chatflow_shares
    - workflows
    - chatflows
    - audit_log

Preserved: workspaces, users, providers, mcp_servers, folders,
channel_bindings, workflow_templates.

Revision ID: 0016_wipe_legacy_chatflows
Revises: 0015_fixture_builtin_id_rename
"""

from __future__ import annotations

from alembic import op

revision: str = "0016_wipe_legacy_chatflows"
down_revision: str | None = "0015_fixture_builtin_id_rename"
branch_labels = None
depends_on = None


_WIPE_ORDER = (
    "board_items",
    "node_index",
    "dashed_node_locks",
    "chatflow_shares",
    "workflows",
    "chatflows",
    "audit_log",
)


def upgrade() -> None:
    for table in _WIPE_ORDER:
        op.execute(f"DELETE FROM {table}")


def downgrade() -> None:
    # One-way: the rows are gone. A reversible ``downgrade`` would need
    # the original data, which is exactly what we're discarding.
    raise RuntimeError(
        "0016_wipe_legacy_chatflows is irreversible — downgrade drops data."
    )
