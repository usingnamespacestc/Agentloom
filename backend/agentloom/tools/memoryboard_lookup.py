"""``memoryboard_lookup`` tool — read the MemoryBoard by various filters.

This is the structural replacement for ``get_node_context`` in the PR 2
migration window. The two tools coexist: ``get_node_context`` still
returns the raw node body (input/output messages, tool_args, etc),
while this tool returns the short ``description`` rows produced by the
``brief`` WorkNode (see :class:`agentloom.db.models.board_item.BoardItemRow`).

Callers that only need a node's take-away should prefer this tool — it
stays small even when the upstream node's tool_result is megabyte-sized.

Supported filters (all optional; all AND-combined; at least one of
``chatflow_id`` / ``workflow_id`` must be provided):

- ``chatflow_id`` — restrict to one ChatFlow's board items.
- ``workflow_id`` — restrict to items produced inside one WorkFlow.
- ``scope`` — ``"chat"`` / ``"node"`` / ``"flow"``; matches
  :class:`agentloom.schemas.common.NodeScope` plus the PR 3 chat scope.
- ``source_node_id`` — direct address; returns at most one row.
- ``query`` — case-insensitive substring match over ``description``.

The return shape is a JSON object ``{"items": [...], "truncated": bool}``
with at most ``limit`` (default 50, capped at 200) rows. Each item
carries the fields downstream consumers care about: ``description``,
``scope``, ``source_node_id``, ``source_kind``, ``fallback``,
``chatflow_id``, ``workflow_id``, ``created_at``.

Cross-workspace isolation is enforced by :class:`BoardItemRepository`
— every query filters by ``ctx.workspace_id``, so a node id from
another workspace simply yields zero rows.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from agentloom.db.base import get_session_maker
from agentloom.db.models.board_item import BoardItemRow
from agentloom.schemas.common import ToolResult
from agentloom.tools.base import Tool, ToolContext, ToolError

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_VALID_SCOPES = {"chat", "node", "flow"}


class MemoryBoardLookupTool(Tool):
    name = "memoryboard_lookup"
    description = (
        "Read the MemoryBoard — the short ``description`` distilled from "
        "every ChatNode/WorkNode's brief. Prefer this over "
        "``get_node_context`` when you only need the take-away, not the "
        "raw messages. Filter by ``chatflow_id`` or ``workflow_id`` "
        "(at least one required), plus optional ``scope`` "
        "(``chat``/``node``/``flow``), ``source_node_id`` for direct "
        "address, or ``query`` substring match over the description. "
        "Returns at most ``limit`` rows (default 50, max 200). "
        "Cross-workspace items are never visible."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chatflow_id": {
                "type": "string",
                "description": (
                    "Restrict results to one ChatFlow. At least one of "
                    "``chatflow_id`` / ``workflow_id`` must be provided."
                ),
            },
            "workflow_id": {
                "type": "string",
                "description": (
                    "Restrict results to items produced inside one "
                    "WorkFlow. At least one of ``chatflow_id`` / "
                    "``workflow_id`` must be provided."
                ),
            },
            "scope": {
                "type": "string",
                "enum": ["chat", "node", "flow"],
                "description": (
                    "Filter by MemoryBoardItem scope: ``chat`` for "
                    "ChatNode items (PR 3), ``node`` for WorkNode "
                    "node-briefs, ``flow`` for WorkFlow flow-briefs."
                ),
            },
            "source_node_id": {
                "type": "string",
                "description": (
                    "Return the single item summarizing exactly this "
                    "source node. Combine with ``chatflow_id`` for the "
                    "tightest lookup."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Case-insensitive substring match over the "
                    "description text. Applied after other filters."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    f"Max rows to return. Default {_DEFAULT_LIMIT}, "
                    f"capped at {_MAX_LIMIT}."
                ),
                "default": _DEFAULT_LIMIT,
            },
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        chatflow_id = _str_or_none(args, "chatflow_id")
        workflow_id = _str_or_none(args, "workflow_id")
        source_node_id = _str_or_none(args, "source_node_id")
        scope = _str_or_none(args, "scope")
        query = _str_or_none(args, "query")

        if not chatflow_id and not workflow_id:
            raise ToolError(
                "memoryboard_lookup: at least one of 'chatflow_id' or "
                "'workflow_id' must be provided"
            )

        if scope is not None and scope not in _VALID_SCOPES:
            raise ToolError(
                f"memoryboard_lookup: invalid scope {scope!r}; "
                f"must be one of {sorted(_VALID_SCOPES)}"
            )

        raw_limit = args.get("limit", _DEFAULT_LIMIT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise ToolError(
                f"memoryboard_lookup: 'limit' must be an integer, "
                f"got {raw_limit!r}"
            ) from None
        limit = max(1, min(limit, _MAX_LIMIT))

        async with get_session_maker()() as session:
            stmt = select(BoardItemRow).where(
                BoardItemRow.workspace_id == ctx.workspace_id
            )
            if chatflow_id is not None:
                stmt = stmt.where(BoardItemRow.chatflow_id == chatflow_id)
            if workflow_id is not None:
                stmt = stmt.where(BoardItemRow.workflow_id == workflow_id)
            if source_node_id is not None:
                stmt = stmt.where(BoardItemRow.source_node_id == source_node_id)
            if scope is not None:
                stmt = stmt.where(BoardItemRow.scope == scope)
            if query is not None and query:
                # ILIKE on Postgres, case-insensitive LIKE on sqlite.
                stmt = stmt.where(
                    BoardItemRow.description.ilike(f"%{query}%")
                )
            # Fetch limit+1 so we know whether the tail was truncated.
            stmt = stmt.order_by(BoardItemRow.created_at).limit(limit + 1)
            rows = list((await session.execute(stmt)).scalars().all())

        truncated = len(rows) > limit
        rows = rows[:limit]

        payload = {
            "items": [_serialize_row(r) for r in rows],
            "truncated": truncated,
        }
        return ToolResult(content=json.dumps(payload, ensure_ascii=False))


def _str_or_none(args: dict[str, Any], key: str) -> str | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise ToolError(
            f"memoryboard_lookup: {key!r} must be a string, got {type(val).__name__}"
        )
    val = val.strip()
    return val or None


def _serialize_row(row: BoardItemRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "chatflow_id": row.chatflow_id,
        "workflow_id": row.workflow_id,
        "source_node_id": row.source_node_id,
        "source_kind": row.source_kind,
        "scope": row.scope,
        "description": row.description,
        "fallback": row.fallback,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
