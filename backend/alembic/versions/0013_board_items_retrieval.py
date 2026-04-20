"""Postgres retrieval DDL for ``board_items``: tsvector + pg_trgm + pgvector.

Foundation migration for the PR-2 MemoryBoard read path. Adds the three
retrieval primitives the design doc (§4.5, 2026-04-20) calls for:

1. **Full-text search** — a generated ``description_tsv`` column built
   from ``to_tsvector('simple', description)`` plus a GIN index.
   ``'simple'`` is chosen deliberately: Postgres' language-specific
   stemmers do the wrong thing on Chinese (CJK has no whitespace to
   tokenize on), so we keep the dictionary trivial and let the index
   match on verbatim terms. English remains searchable because
   ``'simple'`` still lower-cases and word-splits ASCII text.
2. **Trigram similarity** — a ``pg_trgm``-backed GIN index on
   ``description`` (``gin_trgm_ops``) so the upcoming PR-4 reader skill
   can fall back to fuzzy matching when exact keywords miss.
3. **Vector embeddings** — a ``pgvector`` ``vector(1536)`` column
   (``text-embedding-3-small`` dimensionality) plus an ``ivfflat``
   index with ``lists=100``. Column is nullable with no backfill — the
   brief writer will start populating it once PR-4 wires up the
   embedding provider.

No callers yet — this PR only sets the infrastructure. Retry/skip
gracefully if ``pg_trgm`` or ``vector`` aren't installed; SQLite path
is a full no-op (the test suite and lightweight installs stay usable).

Revision ID: 0013_board_items_retrieval
Revises: 0012_memoryboard_brief
"""

from __future__ import annotations

from alembic import op

revision: str = "0013_board_items_retrieval"
down_revision: str | None = "0012_memoryboard_brief"
branch_labels = None
depends_on = None


def _pg() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _pg():
        # SQLite / other backends: retrieval extensions don't exist here;
        # the reader skill falls back to plain ``ILIKE`` automatically.
        return

    bind = op.get_bind()

    # 1. Full-text: ``description_tsv`` computed column + GIN.
    #    Using ``GENERATED ALWAYS AS ... STORED`` keeps the value
    #    in sync with every UPDATE without trigger maintenance.
    op.execute(
        """
        ALTER TABLE board_items
        ADD COLUMN IF NOT EXISTS description_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('simple', description)) STORED
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_board_items_description_tsv
        ON board_items USING gin (description_tsv)
        """
    )

    # 2. Trigram similarity — skip if pg_trgm isn't available in
    #    ``pg_available_extensions`` (lightweight local Postgres
    #    installs may omit contrib extensions).
    trgm_row = bind.exec_driver_sql(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm'"
    ).first()
    if trgm_row is not None:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_board_items_description_trgm
            ON board_items USING gin (description gin_trgm_ops)
            """
        )

    # 3. pgvector embeddings — pre-check so we never issue a CREATE
    #    that would abort the transaction. Managed Postgres hosts
    #    without pgvector skip this section entirely; the brief writer
    #    probes ``information_schema.columns`` before writing.
    vector_row = bind.exec_driver_sql(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
    ).first()
    if vector_row is None:
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        ALTER TABLE board_items
        ADD COLUMN IF NOT EXISTS description_embedding vector(1536)
        """
    )
    # ivfflat requires ``lists`` to be specified; 100 is the pgvector
    # docs' "good starting point" recommendation for a few-thousand-
    # row table. We never rebuild this — the brief writer doesn't
    # batch-insert so fragmentation stays low.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_board_items_description_embedding
        ON board_items
        USING ivfflat (description_embedding vector_cosine_ops)
        WITH (lists = 100)
        """
    )


def downgrade() -> None:
    if not _pg():
        return

    op.execute("DROP INDEX IF EXISTS ix_board_items_description_embedding")
    op.execute("ALTER TABLE board_items DROP COLUMN IF EXISTS description_embedding")
    # Don't ``DROP EXTENSION`` — other tables may rely on pg_trgm /
    # vector. Dropping the indices is sufficient to undo this migration
    # without surprising co-tenants.
    op.execute("DROP INDEX IF EXISTS ix_board_items_description_trgm")
    op.execute("DROP INDEX IF EXISTS ix_board_items_description_tsv")
    op.execute("ALTER TABLE board_items DROP COLUMN IF EXISTS description_tsv")
