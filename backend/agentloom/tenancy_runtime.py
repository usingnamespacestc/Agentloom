"""Process-wide cache of per-workspace settings.

The ChatFlow engine consults :func:`get_settings` on every inner
workflow run (cheap dict lookup), so we can't afford a DB round-trip
per call. The cache is populated at app startup from the ``workspaces``
rows and refreshed whenever the settings API patches a workspace.

MVP is single-workspace, but the cache keys by workspace id so M22
(multi-tenant) lands without re-plumbing.
"""

from __future__ import annotations

from agentloom.schemas.workspace_settings import WorkspaceSettings

_cache: dict[str, WorkspaceSettings] = {}


def set_settings(workspace_id: str, settings: WorkspaceSettings) -> None:
    """Replace the cached settings for ``workspace_id``."""
    _cache[workspace_id] = settings


def get_settings(workspace_id: str) -> WorkspaceSettings:
    """Return the cached settings; empty defaults if the workspace
    hasn't been loaded yet (e.g. a test that skips lifespan)."""
    return _cache.get(workspace_id) or WorkspaceSettings()


def clear() -> None:
    """Drop the cache (test-only)."""
    _cache.clear()
