"""Process-level tracker for τ-bench sessions.

Mirrors :mod:`agentloom.mcp.runtime` — a singleton dict mapping
session_id → :class:`TauBenchToolSource`, with ``add_session`` /
``remove_session`` / ``get_session`` as the only public surface.

Each session's wrapper tools register into the **shared**
``ToolRegistry`` (the same one MCP and built-ins share). Per-session
prefixed tool names guarantee no collision when two sessions run
concurrently.
"""
from __future__ import annotations

import logging
from typing import Any

from agentloom.benchmarks.tau_bench.tool_source import TauBenchToolSource
from agentloom.mcp.runtime import get_shared_registry

log = logging.getLogger(__name__)


_sessions: dict[str, TauBenchToolSource] = {}


def _retail_tool_classes() -> list[type]:
    from tau_bench.envs.retail.tools import ALL_TOOLS

    return list(ALL_TOOLS)


def _airline_tool_classes() -> list[type]:
    from tau_bench.envs.airline.tools import ALL_TOOLS

    return list(ALL_TOOLS)


def _load_data(domain: str) -> dict[str, Any]:
    """Load a fresh mock DB for ``domain``. Each call creates an
    independent dict so two sessions on the same domain don't share
    state."""
    if domain == "retail":
        from tau_bench.envs.retail.data import load_data

        return load_data()
    if domain == "airline":
        from tau_bench.envs.airline.data import load_data

        return load_data()
    raise ValueError(f"unknown tau-bench domain {domain!r}")


def _tool_classes(domain: str) -> list[type]:
    if domain == "retail":
        return _retail_tool_classes()
    if domain == "airline":
        return _airline_tool_classes()
    raise ValueError(f"unknown tau-bench domain {domain!r}")


def add_session(*, session_id: str, domain: str) -> TauBenchToolSource:
    """Create a fresh session: load a clean DB, build wrappers, register.

    Raises ``RuntimeError`` if a session with the same id already
    exists; caller should ``remove_session`` first.
    """
    if session_id in _sessions:
        raise RuntimeError(
            f"tau-bench session {session_id!r} already exists; "
            "remove_session before re-adding"
        )
    registry = get_shared_registry()
    env_data = _load_data(domain)
    tool_classes = _tool_classes(domain)
    source = TauBenchToolSource(
        session_id=session_id,
        domain=domain,
        env_data=env_data,
        tool_classes=tool_classes,
    )
    source.connect_and_register(registry)
    _sessions[session_id] = source
    return source


def remove_session(session_id: str) -> None:
    """Close + unregister a session. No-op if not tracked."""
    source = _sessions.pop(session_id, None)
    if source is None:
        return
    registry = get_shared_registry()
    source.close(registry)


def get_session(session_id: str) -> TauBenchToolSource | None:
    return _sessions.get(session_id)


def all_session_ids() -> list[str]:
    return list(_sessions.keys())


def reset_for_tests() -> None:
    """Clear every tracked session (for unit tests). Closes wrappers
    so the shared registry sheds them."""
    registry = get_shared_registry()
    while _sessions:
        _, src = _sessions.popitem()
        src.close(registry)
