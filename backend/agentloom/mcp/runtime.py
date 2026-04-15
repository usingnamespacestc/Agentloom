"""Process-lifetime MCP runtime — shared ``ToolRegistry`` + connected
``MCPToolSource``s, indexed by config id.

The FastAPI lifespan hook calls :func:`init_runtime` on startup to
build the singleton registry (built-in tools), then
:func:`load_and_connect_all` to read ``mcp_servers`` rows from the DB
and attach each enabled server. Engine factories obtain the registry
via :func:`get_shared_registry`.

CRUD endpoints mutate the live runtime via :func:`add_source`,
:func:`remove_source`, and :func:`reconnect_source` so changes take
effect without a process restart.

Failures during connect/register are recorded on the source's
``last_error`` and surfaced through :func:`get_state` for the API
response, but never propagated — a flaky remote server can't take down
the whole API process.

Lifespan shutdown calls :func:`close_all` to release connections.
"""

from __future__ import annotations

import logging
from typing import Any

from agentloom.mcp.bridge import MCPToolSource
from agentloom.mcp.types import MCPServerConfig
from agentloom.tools.base import ToolRegistry
from agentloom.tools.registry import default_registry

log = logging.getLogger(__name__)

_shared_registry: ToolRegistry | None = None
_sources: dict[str, MCPToolSource] = {}


def init_runtime() -> ToolRegistry:
    """Initialize the shared registry with built-in tools. Idempotent —
    re-calling returns the existing registry without re-creating."""
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = default_registry()
    return _shared_registry


def get_shared_registry() -> ToolRegistry:
    """Return the shared registry. Lazily initialises with built-ins if
    lifespan hasn't run (test paths, scripts)."""
    return init_runtime()


def get_sources() -> list[MCPToolSource]:
    """Return the list of currently-tracked MCP sources (connected or
    failed). Order matches the dict's insertion order."""
    return list(_sources.values())


def get_source(config_id: str) -> MCPToolSource | None:
    return _sources.get(config_id)


def get_state(source: MCPToolSource) -> dict[str, Any]:
    """Build the state dict the API surfaces for one source."""
    return {
        "id": source.config.id,
        "server_id": source.config.server_id,
        "friendly_name": source.config.friendly_name,
        "kind": source.config.kind.value,
        "enabled": source.config.enabled,
        "url": source.config.url,
        "command": source.config.command,
        "is_connected": source.is_connected,
        "tool_count": len(source.registered_names),
        "tool_names": list(source.registered_names),
        "last_error": source.last_error,
    }


async def load_and_connect_all(configs: list[MCPServerConfig]) -> None:
    """Connect every config and register its tools into the shared
    registry. Failures are recorded and skipped so a single broken
    server can't block startup."""
    init_runtime()
    for cfg in configs:
        await add_source(cfg)


async def add_source(config: MCPServerConfig) -> MCPToolSource:
    """Create a source from ``config``, connect, and register tools.

    Always returns a source — even if connection failed, the source
    is tracked with ``last_error`` populated so the UI can show what
    went wrong. Disabled sources are tracked but not connected.
    """
    if config.id in _sources:
        # Caller should reconnect_source instead; bail rather than leak
        # a dangling connection.
        raise RuntimeError(f"source {config.id} already added; use reconnect_source")
    registry = init_runtime()
    source = MCPToolSource(config)
    _sources[config.id] = source
    if not config.enabled:
        msg = f"mcp: skipping disabled server {config.server_id}"
        log.info(msg)
        print(msg, flush=True)
        return source
    try:
        names = await source.connect_and_register(registry)
    except Exception as exc:  # noqa: BLE001 — keep startup resilient
        source.last_error = repr(exc)
        msg = f"mcp: failed to connect server {config.server_id}: {exc!r}"
        log.exception(msg)
        print(msg, flush=True)
        await _safe_close(source)
        return source
    msg = (
        f"mcp: connected {config.server_id} "
        f"({len(names)} tools: {', '.join(names) if names else 'none'})"
    )
    log.info(msg)
    print(msg, flush=True)
    return source


async def remove_source(config_id: str) -> None:
    """Close the source (if connected) and unregister its tools.

    No-op if the id isn't tracked."""
    source = _sources.pop(config_id, None)
    if source is None:
        return
    registry = init_runtime()
    for name in source.registered_names:
        registry.unregister(name)
    await _safe_close(source)


async def reconnect_source(new_config: MCPServerConfig) -> MCPToolSource:
    """Replace the existing source for ``new_config.id`` (closing it
    first) and re-add with the new config. Use after PATCH."""
    await remove_source(new_config.id)
    return await add_source(new_config)


async def close_all() -> None:
    """Close every tracked source, clearing the runtime."""
    while _sources:
        _, src = _sources.popitem()
        registry = _shared_registry
        if registry is not None:
            for name in src.registered_names:
                registry.unregister(name)
        await _safe_close(src)


async def _safe_close(src: MCPToolSource) -> None:
    try:
        await src.close()
    except Exception:  # noqa: BLE001
        log.exception("mcp: failed to close source %s", src.config.server_id)


def reset_for_tests() -> None:
    """Drop the shared registry + source list. Tests that don't
    actually connect can call this between cases."""
    global _shared_registry
    _shared_registry = None
    _sources.clear()
