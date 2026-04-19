"""Process-wide ``(provider_id, model_id) -> context_window`` cache.

The workflow engine's compact decision needs to compare estimated
context size against the target model's real context window. That
window lives on ``ModelInfo.context_window`` in the provider registry
(DB-backed), but the engine is a long-lived object that doesn't carry
a session, so it can't read the DB synchronously at decision time.

This module keeps a small in-memory mirror populated by any async
handler that has a session handy (see ``refresh``) and read
synchronously via :func:`lookup`. Callers wire :func:`lookup` to the
engines as their ``context_window_lookup`` hook.

Staleness model: cache entries are re-written whenever ``refresh`` is
called. API handlers that touch a chatflow or workflow already call
into the DB, so refreshing there adds one cheap query per request and
keeps the cache warm without a background task.
"""

from __future__ import annotations

from agentloom.schemas.common import ProviderModelRef

_cache: dict[tuple[str, str], int | None] = {}


async def refresh(repo) -> None:  # type: ignore[no-untyped-def]
    """Reload the cache from a ``ProviderRepository`` snapshot.

    Passes through ``list_all()`` which returns lightweight summaries
    — one select + payload deserialize per workspace.
    """
    providers = await repo.list_all()
    new: dict[tuple[str, str], int | None] = {}
    for p in providers:
        pid = p.get("id")
        if not pid:
            continue
        for m in p.get("available_models", []) or []:
            mid = m.get("id")
            if not mid:
                continue
            new[(pid, mid)] = m.get("context_window")
    _cache.clear()
    _cache.update(new)


def lookup(ref: ProviderModelRef | None) -> int | None:
    """Sync lookup used as ``context_window_lookup`` on the engine."""
    if ref is None:
        return None
    return _cache.get((ref.provider_id, ref.model_id))


def seed(entries: dict[tuple[str, str], int | None]) -> None:
    """Test helper: replace the cache wholesale with ``entries``."""
    _cache.clear()
    _cache.update(entries)
