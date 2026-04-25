"""Repository for MemoryBoard items.

Produced by :meth:`WorkflowEngine._run_brief` — one row per summarized
source node. The methods here are deliberately minimal this PR: upsert
by ``source_node_id`` (idempotent so retries don't duplicate rows) plus
two listing accessors used by tests and the PR-2 reader skill. Search
/ tag / vector indices land in PR 2.

All methods honor ADR-015 workspace scoping: every query filters by
``self.workspace_id`` even when the caller passes only a chatflow_id
or node_id — the repo is the trust boundary, not the caller.
"""

from __future__ import annotations

from sqlalchemy import select

from agentloom.db.models.board_item import BoardItemRow
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.schemas.common import NodeScope


class BoardItemRepository(WorkspaceScopedRepository):
    async def upsert_by_source(
        self,
        *,
        chatflow_id: str,
        workflow_id: str | None,
        source_node_id: str,
        source_kind: str,
        scope: NodeScope | str,
        description: str,
        fallback: bool = False,
        inner_chat_ids: list[str] | None = None,
        work_node_ids: list[str] | None = None,
    ) -> BoardItemRow:
        """Insert or update the BoardItem row keyed by ``source_node_id``.

        Idempotent: a brief that fires again on the same source node
        (e.g. a retry round) overwrites the description in place
        instead of cluttering the board with duplicate rows. Returns
        the resulting row — fresh id on insert, stable id on update.

        ``inner_chat_ids`` and ``work_node_ids`` carry drill-down
        pointers (see :class:`BoardItemRow` field docs); ``None`` keeps
        the existing column ``NULL``. On update both fields are
        overwritten from the call — the writer is the source of truth
        each round, since drill-down membership can change between
        retries (e.g. a pack range edit).
        """
        scope_value = scope.value if isinstance(scope, NodeScope) else scope
        stmt = (
            select(BoardItemRow)
            .where(BoardItemRow.workspace_id == self.workspace_id)
            .where(BoardItemRow.source_node_id == source_node_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = BoardItemRow(
                workspace_id=self.workspace_id,
                chatflow_id=chatflow_id,
                workflow_id=workflow_id,
                source_node_id=source_node_id,
                source_kind=source_kind,
                scope=scope_value,
                description=description,
                fallback=fallback,
                inner_chat_ids=inner_chat_ids,
                work_node_ids=work_node_ids,
            )
            self.session.add(row)
        else:
            row.chatflow_id = chatflow_id
            row.workflow_id = workflow_id
            row.source_kind = source_kind
            row.scope = scope_value
            row.description = description
            row.fallback = fallback
            row.inner_chat_ids = inner_chat_ids
            row.work_node_ids = work_node_ids
        await self.session.flush()
        return row

    async def list_by_chatflow(self, chatflow_id: str) -> list[BoardItemRow]:
        """All board items in a ChatFlow, ordered by creation time."""
        stmt = (
            select(BoardItemRow)
            .where(BoardItemRow.workspace_id == self.workspace_id)
            .where(BoardItemRow.chatflow_id == chatflow_id)
            .order_by(BoardItemRow.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_workflow(self, workflow_id: str) -> list[BoardItemRow]:
        """All board items produced by briefs inside a single WorkFlow."""
        stmt = (
            select(BoardItemRow)
            .where(BoardItemRow.workspace_id == self.workspace_id)
            .where(BoardItemRow.workflow_id == workflow_id)
            .order_by(BoardItemRow.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_source(self, source_node_id: str) -> BoardItemRow | None:
        """Look up the board item for a specific source node, or None."""
        stmt = (
            select(BoardItemRow)
            .where(BoardItemRow.workspace_id == self.workspace_id)
            .where(BoardItemRow.source_node_id == source_node_id)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
