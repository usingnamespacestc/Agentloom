"""Folder repository — CRUD for sidebar folders (supports nesting)."""

from __future__ import annotations

from sqlalchemy import select

from agentloom.db.models.chatflow import ChatFlowRow
from agentloom.db.models.folder import FolderRow
from agentloom.db.repositories.base import WorkspaceScopedRepository


class FolderNotFoundError(KeyError):
    pass


class FolderRepository(WorkspaceScopedRepository):
    async def create(self, name: str, parent_id: str | None = None) -> FolderRow:
        row = FolderRow(
            workspace_id=self.workspace_id, name=name, parent_id=parent_id
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_all(self) -> list[dict]:
        stmt = (
            select(
                FolderRow.id,
                FolderRow.parent_id,
                FolderRow.name,
                FolderRow.created_at,
                FolderRow.updated_at,
            )
            .where(FolderRow.workspace_id == self.workspace_id)
            .order_by(FolderRow.created_at.asc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "id": r.id,
                "parent_id": r.parent_id,
                "name": r.name,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    async def rename(self, folder_id: str, name: str) -> None:
        row = await self._get(folder_id)
        row.name = name
        await self.session.flush()

    async def move(self, folder_id: str, parent_id: str | None) -> None:
        """Move a folder under another folder (or to root if parent_id=None)."""
        row = await self._get(folder_id)
        row.parent_id = parent_id
        await self.session.flush()

    async def delete(self, folder_id: str) -> list[str]:
        """Delete a folder and return IDs of all chatflows that were inside
        it or any of its descendant folders (DB CASCADE handles the rows)."""
        # Collect all descendant folder IDs (including self).
        all_folder_ids = await self._descendants(folder_id)
        all_folder_ids.add(folder_id)

        # Collect chatflow IDs in any of those folders.
        stmt = (
            select(ChatFlowRow.id)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.folder_id.in_(all_folder_ids))
        )
        chatflow_ids = list((await self.session.execute(stmt)).scalars().all())

        # Delete the root folder; FK CASCADE deletes children + chatflows.
        row = await self._get(folder_id)
        await self.session.delete(row)
        await self.session.flush()
        return chatflow_ids

    async def _get(self, folder_id: str) -> FolderRow:
        stmt = (
            select(FolderRow)
            .where(FolderRow.workspace_id == self.workspace_id)
            .where(FolderRow.id == folder_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise FolderNotFoundError(folder_id)
        return row

    async def _descendants(self, folder_id: str) -> set[str]:
        """Return all descendant folder IDs (not including self)."""
        # Load all folders once and walk in-memory (cheap for sidebar-scale).
        stmt = (
            select(FolderRow.id, FolderRow.parent_id)
            .where(FolderRow.workspace_id == self.workspace_id)
        )
        rows = (await self.session.execute(stmt)).all()
        children: dict[str, list[str]] = {}
        for r in rows:
            if r.parent_id:
                children.setdefault(r.parent_id, []).append(r.id)

        result: set[str] = set()
        stack = list(children.get(folder_id, []))
        while stack:
            fid = stack.pop()
            if fid in result:
                continue
            result.add(fid)
            stack.extend(children.get(fid, []))
        return result
