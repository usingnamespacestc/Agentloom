"""ChatFlow repository — persist and load schemas.ChatFlow via JSONB.

Mirrors WorkflowRepository (ADR-015: every read scoped by workspace_id;
frozen nodes cannot be modified on save).
"""

from __future__ import annotations

from sqlalchemy import select

from agentloom.db.models.chatflow import ChatFlowRow
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.schemas import ChatFlow
from agentloom.schemas.common import FrozenNodeError, NodeStatus


class ChatFlowNotFoundError(KeyError):
    pass


class ChatFlowRepository(WorkspaceScopedRepository):
    async def create(self, chatflow: ChatFlow, owner_id: str | None = None) -> ChatFlowRow:
        row = ChatFlowRow(
            id=chatflow.id,
            workspace_id=self.workspace_id,
            owner_id=owner_id,
            title=chatflow.title,
            payload=chatflow.model_dump(mode="json"),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, chatflow_id: str) -> ChatFlow:
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        return ChatFlow.model_validate(row.payload)

    async def save(self, chatflow: ChatFlow) -> None:
        """Overwrite an existing row. Rejects any mutation of a node
        that was frozen in the prior state — this covers both the outer
        ChatFlowNode and its inner WorkFlow nodes."""
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow.id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow.id)

        prior = ChatFlow.model_validate(row.payload)
        _assert_frozen_chatflow_nodes_unchanged(prior, chatflow)

        row.title = chatflow.title
        row.description = chatflow.description
        row.tags = chatflow.tags or None
        row.payload = chatflow.model_dump(mode="json")
        await self.session.flush()

    async def list_ids(self) -> list[str]:
        stmt = (
            select(ChatFlowRow.id)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .order_by(ChatFlowRow.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_summaries(self) -> list[dict]:
        """Return lightweight summaries for all chatflows, most recent first."""
        stmt = (
            select(
                ChatFlowRow.id,
                ChatFlowRow.title,
                ChatFlowRow.description,
                ChatFlowRow.tags,
                ChatFlowRow.folder_id,
                ChatFlowRow.created_at,
                ChatFlowRow.updated_at,
            )
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .order_by(ChatFlowRow.updated_at.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "description": r.description,
                "tags": r.tags or [],
                "folder_id": r.folder_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    async def patch_metadata(
        self,
        chatflow_id: str,
        *,
        title: str | None = ...,  # type: ignore[assignment]
        description: str | None = ...,  # type: ignore[assignment]
        tags: list[str] | None = ...,  # type: ignore[assignment]
    ) -> None:
        """Update title / description / tags. Pass ``...`` (default) to skip a field."""
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        if title is not ...:
            row.title = title
        if description is not ...:
            row.description = description
        if tags is not ...:
            row.tags = tags
        await self.session.flush()

    async def move_to_folder(self, chatflow_id: str, folder_id: str | None) -> None:
        """Set folder_id on a chatflow (None = unfiled)."""
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        row.folder_id = folder_id
        await self.session.flush()

    async def delete(self, chatflow_id: str) -> None:
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        await self.session.delete(row)
        await self.session.flush()


#: Fields on a ChatFlowNode that may be mutated even after the node is
#: frozen. ``pending_queue`` is ancillary scheduling state — see the
#: ADR-009 addendum in ``PendingTurn``'s docstring. Deleting a failed
#: node's queue (via the DELETE node route) is also allowed; that path
#: goes through ``remove_node`` before calling ``save`` and therefore
#: doesn't trip this check.
_FROZEN_EXEMPT_FIELDS = frozenset({"pending_queue", "position_x", "position_y"})


def _strip_frozen_exempt(dump: dict) -> dict:
    """Return a copy of ``dump`` with exempt fields removed."""
    return {k: v for k, v in dump.items() if k not in _FROZEN_EXEMPT_FIELDS}


def _assert_frozen_chatflow_nodes_unchanged(prior: ChatFlow, new: ChatFlow) -> None:
    """Reject changes to any previously-frozen ChatFlowNode.

    Inner WorkFlow nodes inherit the same immutability via their own
    status field — the comparison below does a full dump-equality so it
    naturally catches inner mutations too.

    ``pending_queue`` is exempt: it's a scheduling buffer attached to
    the node, not part of the dialogue record. Everything else on a
    frozen node is locked down.

    Node deletion is allowed here too — ``remove_node`` on the ChatFlow
    already handles cascade safety. A failed-node cleanup path deletes
    the node before save, and the resulting dump simply omits it.
    """
    for nid, prior_node in prior.nodes.items():
        if not prior_node.is_frozen:
            continue
        new_node = new.nodes.get(nid)
        if new_node is None:
            # Deletion of frozen nodes is allowed — the cascade-delete
            # and failed-node-cleanup paths both remove nodes before
            # calling save.
            continue
        prior_dump = _strip_frozen_exempt(prior_node.model_dump(mode="json"))
        new_dump = _strip_frozen_exempt(new_node.model_dump(mode="json"))
        if new_dump != prior_dump:
            raise FrozenNodeError(f"ChatFlow node {nid} is frozen and may not be modified")
