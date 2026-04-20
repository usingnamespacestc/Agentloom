"""Rename Layer-2 enum values across JSONB payloads and board_items.

MemoryBoard PR 4.1. Canonical StepKind vocabulary changes:

    ``llm_call`` → ``draft``
    ``sub_agent_delegation`` → ``delegate``
    ``compact`` → ``compress``

WorkNodeRole vocabulary changes:

    ``planner`` → ``plan``
    ``planner_judge`` → ``plan_judge``

StepKind values live inside JSONB payloads (chatflows / workflows /
workflow_templates) under the ``step_kind`` key and inside the
``board_items.source_kind`` plain column (WorkNode briefs stamp the
StepKind value there). WorkNodeRole lives under the ``role`` key in
the same JSONB payloads. This migration rewrites both in place.

Approach mirrors 0011_execution_mode_rename: scoped text-level
REPLACE on JSONB::text so nested payloads (ChatFlow → ChatNode →
WorkFlow → WorkNode, recursively) all get rewritten in one pass.

Revision ID: 0014_layer2_enum_rename
Revises: 0013_board_items_retrieval
"""

from __future__ import annotations

from alembic import op

revision: str = "0014_layer2_enum_rename"
down_revision: str | None = "0013_board_items_retrieval"
branch_labels = None
depends_on = None


_STEP_KIND_UP = [
    ('"step_kind": "llm_call"', '"step_kind": "draft"'),
    ('"step_kind": "sub_agent_delegation"', '"step_kind": "delegate"'),
    ('"step_kind": "compact"', '"step_kind": "compress"'),
]

_ROLE_UP = [
    # planner_judge must come before planner to avoid a partial match
    # rewriting "planner_judge" → "plan_judge" via the shorter rule.
    ('"role": "planner_judge"', '"role": "plan_judge"'),
    ('"role": "planner"', '"role": "plan"'),
]

_PATTERNS_UP = _STEP_KIND_UP + _ROLE_UP

_STEP_KIND_DOWN = [(b, a) for a, b in _STEP_KIND_UP]
_ROLE_DOWN = [
    # Reverse order for the downgrade too: map "plan" back to
    # "planner" only after the longer "plan_judge" key has been
    # rewritten to "planner_judge".
    ('"role": "plan_judge"', '"role": "planner_judge"'),
    ('"role": "plan"', '"role": "planner"'),
]
_PATTERNS_DOWN = _STEP_KIND_DOWN + _ROLE_DOWN


_SOURCE_KIND_UP = [
    ("llm_call", "draft"),
    ("sub_agent_delegation", "delegate"),
    ("compact", "compress"),
]
_SOURCE_KIND_DOWN = [(b, a) for a, b in _SOURCE_KIND_UP]


def _rewrite_jsonb(
    table: str, column: str, patterns: list[tuple[str, str]]
) -> None:
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
            op.execute(
                f"UPDATE {table} "
                f"SET {column} = REPLACE({column}, '{old}', '{new}') "
                f"WHERE {column} LIKE '%{old}%'"
            )


def _rewrite_column(
    table: str, column: str, patterns: list[tuple[str, str]]
) -> None:
    for old, new in patterns:
        op.execute(
            f"UPDATE {table} SET {column} = '{new}' WHERE {column} = '{old}'"
        )


def upgrade() -> None:
    _rewrite_jsonb("chatflows", "payload", _PATTERNS_UP)
    _rewrite_jsonb("workflows", "payload", _PATTERNS_UP)
    _rewrite_jsonb("workflow_templates", "plan", _PATTERNS_UP)
    _rewrite_column("board_items", "source_kind", _SOURCE_KIND_UP)


def downgrade() -> None:
    _rewrite_jsonb("chatflows", "payload", _PATTERNS_DOWN)
    _rewrite_jsonb("workflows", "payload", _PATTERNS_DOWN)
    _rewrite_jsonb("workflow_templates", "plan", _PATTERNS_DOWN)
    _rewrite_column("board_items", "source_kind", _SOURCE_KIND_DOWN)
