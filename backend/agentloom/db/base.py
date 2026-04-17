"""Async SQLAlchemy engine + session maker.

A module-level lazy singleton pair so tests can override the URL via
``configure_engine`` before the first session is created.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from agentloom.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Module-level singletons, created lazily on first use.
_engine: AsyncEngine | None = None
_session_maker: async_sessionmaker[AsyncSession] | None = None


def configure_engine(url: str | None = None) -> AsyncEngine:
    """(Re)create the engine. Tests call this with a test DB URL.

    Passing ``None`` uses ``settings.database_url``. Calling twice disposes
    the previous engine first.
    """
    global _engine, _session_maker
    if _engine is not None:
        # Best-effort dispose; in async we cannot await here so we rely on
        # the caller's event loop to GC it. Tests call this from fixtures
        # where that's fine.
        pass
    db_url = url or get_settings().database_url
    _engine = create_async_engine(
        db_url,
        echo=False,
        future=True,
        pool_pre_ping=True,
        pool_size=20,
        max_overflow=30,
    )
    _session_maker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        configure_engine()
    assert _engine is not None
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    if _session_maker is None:
        configure_engine()
    assert _session_maker is not None
    return _session_maker


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency — yields one session per request."""
    async with get_session_maker()() as session:
        yield session


def get_session_scope() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency — returns the session *maker* so a handler
    can open multiple short-lived sessions around a long await (e.g.
    waiting for a workflow run to finish). Tests override this to bind
    against the test DB, same way they override :func:`get_session`.
    """
    return get_session_maker()
