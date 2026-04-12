"""Workspace-scoped repositories.

All reads MUST go through one of these. See ADR-015 and §4.7 of
``docs/requirements.md``.

``tests/backend/unit/test_repo_hygiene.py`` statically checks that
every ``select()`` inside this package is scoped by ``workspace_id``.
"""

from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.db.repositories.workflow import WorkflowRepository

__all__ = ["WorkflowRepository", "WorkspaceScopedRepository"]
