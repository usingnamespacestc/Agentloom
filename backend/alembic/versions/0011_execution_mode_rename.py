"""Rename ExecutionMode enum values across JSONB payloads.

``direct`` → ``native_react``, ``auto`` → ``auto_plan``. ``semi_auto``
is unchanged. The enum lives inline inside JSONB blobs (chatflow /
workflow / workflow_template payloads), so this migration rewrites
string values in place. Payloads are nested deeply (a ChatFlow payload
carries ChatNodes, each of which may carry a WorkFlow with sub-
WorkFlows) — safer to do a text-level substitution on the ``text``
representation of the JSONB column, scoped to the two specific JSON
keys that hold an ExecutionMode so we don't clobber unrelated fields
that happen to contain the word ``direct`` or ``auto``.

Revision ID: 0011_execution_mode_rename
Revises: 0010_node_index
"""

from __future__ import annotations

from alembic import op

revision: str = "0011_execution_mode_rename"
down_revision: str | None = "0010_node_index"
branch_labels = None
depends_on = None


_PATTERNS_UP = [
    ('"execution_mode": "direct"', '"execution_mode": "native_react"'),
    ('"execution_mode": "auto"', '"execution_mode": "auto_plan"'),
    ('"default_execution_mode": "direct"', '"default_execution_mode": "native_react"'),
    ('"default_execution_mode": "auto"', '"default_execution_mode": "auto_plan"'),
]

_PATTERNS_DOWN = [(b, a) for a, b in _PATTERNS_UP]


def _rewrite(table: str, column: str, patterns: list[tuple[str, str]]) -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    for old, new in patterns:
        if dialect == "postgresql":
            op.execute(
                f"UPDATE {table} "
                f"SET {column} = REPLACE({column}::text, '{old}', '{new}')::jsonb "
                f"WHERE {column}::text LIKE '%{old}%'"
            )
        else:
            # SQLite (tests): JSON is stored as text already.
            op.execute(
                f"UPDATE {table} "
                f"SET {column} = REPLACE({column}, '{old}', '{new}') "
                f"WHERE {column} LIKE '%{old}%'"
            )


def upgrade() -> None:
    _rewrite("chatflows", "payload", _PATTERNS_UP)
    _rewrite("workflows", "payload", _PATTERNS_UP)
    _rewrite("workflow_templates", "plan", _PATTERNS_UP)


def downgrade() -> None:
    _rewrite("chatflows", "payload", _PATTERNS_DOWN)
    _rewrite("workflows", "payload", _PATTERNS_DOWN)
    _rewrite("workflow_templates", "plan", _PATTERNS_DOWN)
