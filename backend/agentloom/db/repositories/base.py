"""Base class for workspace-scoped repositories.

Every subclass takes ``workspace_id`` in its constructor. Every
``select()`` issued by a subclass must include ``.where(
Model.workspace_id == self.workspace_id)``. The hygiene test in
``tests/backend/unit/test_repo_hygiene.py`` enforces this via AST scan.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession


class WorkspaceScopedRepository:
    """Base: holds the session + workspace binding."""

    def __init__(self, session: AsyncSession, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id
