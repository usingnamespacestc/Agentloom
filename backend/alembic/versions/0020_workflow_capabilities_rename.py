"""Rename ``WorkFlow.capabilities`` JSON key → ``capabilities_origin``.

Part of M7.5 PR 2 (capability model schema). Pre-M7.5 the WorkFlow
schema's natural-language capability list lived under the
``capabilities`` JSON key inside ``chatflows.payload``. M7.5 splits
that semantically:

- ``capabilities_origin``: natural-language list (the renamed field
  for UI display + provenance)
- ``inheritable_tools``: registry tool names (NEW field, engine
  consumed; left empty by this migration — gets populated lazily on
  the next ``judge_pre`` run for each chatflow)

The Pydantic validator on ``WorkFlow`` already accepts the legacy
``capabilities`` key as an alias on ingest, so this migration is
**not strictly required** for correctness. But rewriting persisted
payloads has two benefits:

1. Avoids per-decode validator overhead for every chatflow read.
2. Keeps ``select`` queries doing JSONB key-lookups consistent with
   the new schema (e.g. future ``payload->'capabilities_origin'``).

Recursively walks every ``WorkFlow`` payload nested inside
``chatflows.payload`` (each ChatNode carries its inner WorkFlow which
may carry sub_workflows under DELEGATE nodes). For every dict with a
``capabilities`` key (whether list or anything else, indicating a
WorkFlow shape), rename to ``capabilities_origin``. Idempotent: if
both keys are present, drop the legacy ``capabilities`` (the new key
is authoritative going forward).

Downgrade reverses the rename — but loses the new ``inheritable_tools``
data since pre-M7.5 schema can't carry it.

Revision ID: 0020_workflow_capabilities_rename
Revises: 0019_board_items_tag_indices
"""

from __future__ import annotations

from typing import Any

from alembic import op


revision: str = "0020_workflow_capabilities_rename"
down_revision: str | None = "0019_board_items_tag_indices"
branch_labels = None
depends_on = None


def _rewrite_workflow_capabilities(node: Any, *, reverse: bool = False) -> bool:
    """Walk an arbitrary nested dict / list and rename any
    ``capabilities`` key to ``capabilities_origin`` (or back, on
    downgrade). Returns True if any rewrite happened — caller uses
    it to decide whether to UPDATE the DB row.

    The traversal is intentionally permissive: we don't try to detect
    "this dict is a WorkFlow" (would require importing the schema and
    coupling the migration to model evolution). Any dict with a
    ``capabilities`` key gets renamed, since this key isn't used
    elsewhere in the chatflow payload.
    """
    if isinstance(node, dict):
        rewrote = False
        if reverse:
            if "capabilities_origin" in node and "capabilities" not in node:
                node["capabilities"] = node.pop("capabilities_origin")
                # Downgrade also drops inheritable_tools — pre-M7.5
                # schema can't carry it.
                node.pop("inheritable_tools", None)
                rewrote = True
        else:
            if "capabilities" in node and "capabilities_origin" not in node:
                node["capabilities_origin"] = node.pop("capabilities")
                rewrote = True
            elif "capabilities" in node and "capabilities_origin" in node:
                # Both present — keep the new one, drop legacy.
                node.pop("capabilities")
                rewrote = True
        for v in node.values():
            if _rewrite_workflow_capabilities(v, reverse=reverse):
                rewrote = True
        return rewrote
    if isinstance(node, list):
        rewrote = False
        for item in node:
            if _rewrite_workflow_capabilities(item, reverse=reverse):
                rewrote = True
        return rewrote
    return False


def _migrate(reverse: bool = False) -> None:
    bind = op.get_bind()
    # JSONB cast + back so we get a real Python dict to mutate
    rows = bind.execute(
        op.text(
            "SELECT id, payload FROM chatflows"
        )
        if False
        else __import__("sqlalchemy").text("SELECT id, payload FROM chatflows")
    ).fetchall()
    import json

    import sqlalchemy as sa

    for row in rows:
        cf_id, payload = row[0], row[1]
        if payload is None:
            continue
        if isinstance(payload, str):
            payload = json.loads(payload)
        if _rewrite_workflow_capabilities(payload, reverse=reverse):
            bind.execute(
                sa.text(
                    "UPDATE chatflows SET payload = :p WHERE id = :id"
                ).bindparams(
                    sa.bindparam("p", type_=sa.dialects.postgresql.JSONB),
                    sa.bindparam("id"),
                ),
                {"p": payload, "id": cf_id},
            )


def upgrade() -> None:
    _migrate(reverse=False)


def downgrade() -> None:
    _migrate(reverse=True)
