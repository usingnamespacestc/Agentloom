"""Database layer — SQLAlchemy 2.x async.

Every user-scoped table carries ``workspace_id`` and (nullable) ``owner_id``.
Reads go through ``agentloom.db.repositories``; do not issue raw selects
outside the repository layer. See ADR-015 / ADR-017.
"""

from agentloom.db.base import Base, get_session, get_session_maker

__all__ = ["Base", "get_session", "get_session_maker"]
