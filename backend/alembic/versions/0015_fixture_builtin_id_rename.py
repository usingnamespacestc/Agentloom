"""Resolve the plan/planner fixture builtin_id collision.

Follow-up to MemoryBoard PR 4.1. The Layer-2 enum rename lined
``WorkNodeRole.PLAN`` up with the engine's internal planner fixture,
but the fixture's ``builtin_id`` was still ``planner`` while
``builtin_id: plan`` was occupied by the user-facing goal-expansion
template. This migration resolves the naming clash by pushing the
goal-expansion template over to ``goal_expand`` and promoting
``planner`` / ``planner_judge`` into ``plan`` / ``plan_judge``.

    plan          -> goal_expand
    planner       -> plan
    planner_judge -> plan_judge

Shipped ``__builtin__`` rows are stored with a language suffix
(``<builtin_id>@<language>``); user-workspace overrides use the
unsuffixed form. Both are rewritten.

Critical: step order matters. Move ``plan`` out of the way first,
then promote ``planner``, or we'd violate the ``builtin_id`` unique
constraint mid-migration.

Revision ID: 0015_fixture_builtin_id_rename
Revises: 0014_layer2_enum_rename
"""

from __future__ import annotations

from alembic import op

revision: str = "0015_fixture_builtin_id_rename"
down_revision: str | None = "0014_layer2_enum_rename"
branch_labels = None
depends_on = None


_LANGS = ("en-US", "zh-CN")

# Ordered: vacate "plan" before filling it from "planner".
_UP_STEPS: list[tuple[str, str]] = [
    ("plan", "goal_expand"),
    ("planner", "plan"),
    ("planner_judge", "plan_judge"),
]

_DOWN_STEPS: list[tuple[str, str]] = [
    ("plan_judge", "planner_judge"),
    ("plan", "planner"),
    ("goal_expand", "plan"),
]


def _apply(steps: list[tuple[str, str]]) -> None:
    for old, new in steps:
        # Unsuffixed form (user workspace overrides).
        op.execute(
            f"UPDATE workflow_templates SET builtin_id = '{new}' "
            f"WHERE builtin_id = '{old}'"
        )
        # Language-suffixed form (__builtin__ rows).
        for lang in _LANGS:
            op.execute(
                f"UPDATE workflow_templates SET builtin_id = '{new}@{lang}' "
                f"WHERE builtin_id = '{old}@{lang}'"
            )


def upgrade() -> None:
    _apply(_UP_STEPS)


def downgrade() -> None:
    _apply(_DOWN_STEPS)
