"""``memoryboard_lookup`` tool — read the MemoryBoard by various filters.

This is the structural replacement for ``get_node_context`` in the PR 2
migration window. The two tools coexist: ``get_node_context`` still
returns the raw node body (input/output messages, tool_args, etc),
while this tool returns the short ``description`` rows produced by the
``brief`` WorkNode (see :class:`agentloom.db.models.board_item.BoardItemRow`).

Callers that only need a node's take-away should prefer this tool — it
stays small even when the upstream node's tool_result is megabyte-sized.

Supported filters (all optional; AND-combined; at least one of
``chatflow_id`` / ``workflow_id`` must be provided):

- ``chatflow_id`` / ``workflow_id`` — scope by ChatFlow or WorkFlow.
- ``scope`` — ``"chat"`` / ``"node"`` / ``"flow"``.
- ``source_node_id`` — direct address; returns at most one row.
- ``query`` — case-insensitive substring match over ``description``.
- ``tag`` — single concept; returns rows that match it on EITHER
  ``produced_tags`` OR ``consumed_tags`` (the producer+consumer dual
  view from the 2026-04-25 logical-index design — gets the rejection
  context at node 2 even when the search is for "plan_x" emitted by
  node 1).
- ``produced_tags`` / ``consumed_tags`` — explicit producer- or
  consumer-only filters when the caller wants to narrow by side.
- ``tag_match_mode`` — ``"any"`` (default) or ``"all"`` for multi-tag.
- ``prefix_match`` — when true, a tag like ``plan_x`` also matches
  ``plan_x_rejected`` / ``plan_x_approved`` etc. Use to pull the
  full lifecycle of a concept across status changes.
- ``expand_chain`` — after matching, also include downstream board
  items that drill from each matched row (one hop). Helps surface
  the consumer-side context of a producer match.
- ``since`` — ISO 8601 datetime; only return rows created at or after.
- ``limit`` — max rows (default 50, capped at 200).

Two return shapes (opt in via ``format``, default ``"prompt"``):

- ``"prompt"`` — readable prose block for LLM consumption. Each item
  becomes a metadata line (``scope``, ``source_kind``,
  ``source_node_id``, tag set, ``created_at``) followed by the
  indented brief description.
- ``"json"`` — programmatic ``{items: [...], truncated: bool}``
  shape with full structured fields.

Cross-workspace isolation is enforced by :class:`BoardItemRepository`
— every query filters by ``ctx.workspace_id``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select

from agentloom.db.base import get_session_maker
from agentloom.db.models.board_item import BoardItemRow
from agentloom.schemas.common import ToolResult
from agentloom.tools.base import SideEffect, Tool, ToolContext, ToolError

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_VALID_SCOPES = {"chat", "node", "flow"}
_VALID_FORMATS = {"prompt", "json"}
_VALID_TAG_MATCH_MODES = {"any", "all"}
_DEFAULT_FORMAT = "prompt"
_DEFAULT_TAG_MATCH_MODE = "any"


class MemoryBoardLookupTool(Tool):
    name = "memoryboard_lookup"
    side_effect = SideEffect.READ
    description = (
        "Read the MemoryBoard — the short ``description`` distilled from "
        "every ChatNode/WorkNode's brief, plus its concept tags. Prefer "
        "this over ``get_node_context`` when you only need the take-away "
        "and the concept lifecycle, not the raw messages. Filter by "
        "``chatflow_id`` / ``workflow_id`` (at least one), plus optional "
        "``scope``, ``source_node_id``, ``query`` substring, ``tag`` "
        "(single concept matched against either produced or consumed — "
        "use this to find both the proposer and the rejecter of one "
        "concept), ``produced_tags`` / ``consumed_tags`` (side-specific), "
        "``tag_match_mode`` (any / all), ``prefix_match`` (plan_x also "
        "matches plan_x_rejected etc), ``expand_chain`` (one hop of "
        "drill-down to surface consumers), or ``since`` ISO datetime "
        "for time windows. ``limit`` defaults to 50, capped at 200. "
        "``format`` chooses ``prompt`` (readable, default) or ``json`` "
        "(structured rows). Cross-workspace items are never visible."
    )
    parameters = {
        "type": "object",
        "properties": {
            "chatflow_id": {"type": "string"},
            "workflow_id": {"type": "string"},
            "scope": {"type": "string", "enum": ["chat", "node", "flow"]},
            "source_node_id": {"type": "string"},
            "query": {
                "type": "string",
                "description": "Case-insensitive substring on description.",
            },
            "tag": {
                "type": "string",
                "description": (
                    "Single concept tag. Matched against EITHER "
                    "``produced_tags`` OR ``consumed_tags`` so the "
                    "lookup returns both the node that proposed the "
                    "concept and downstream nodes that referenced or "
                    "rejected it. Combine with ``prefix_match`` to "
                    "include status-suffixed forms."
                ),
            },
            "produced_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Restrict to rows whose ``produced_tags`` matches. "
                    "Combine via ``tag_match_mode`` for multi-tag."
                ),
            },
            "consumed_tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Restrict to rows whose ``consumed_tags`` matches."
                ),
            },
            "tag_match_mode": {
                "type": "string",
                "enum": sorted(_VALID_TAG_MATCH_MODES),
                "default": _DEFAULT_TAG_MATCH_MODE,
                "description": (
                    "``any`` (default) — row matches if at least one "
                    "requested tag is in the array. ``all`` — every "
                    "requested tag must be present."
                ),
            },
            "prefix_match": {
                "type": "boolean",
                "default": False,
                "description": (
                    "When true, treat each requested tag as a prefix. "
                    "``plan_x`` then also matches ``plan_x_rejected``, "
                    "``plan_x_approved``, etc — covers the concept's "
                    "full lifecycle across status changes."
                ),
            },
            "expand_chain": {
                "type": "boolean",
                "default": False,
                "description": (
                    "After matching, also include one hop of downstream "
                    "BoardItems (rows whose ``inner_chat_ids`` / "
                    "``work_node_ids`` reference a matched row, OR whose "
                    "``consumed_tags`` overlap a matched row's "
                    "``produced_tags``). Helps surface the consumer-"
                    "side context of a producer hit."
                ),
            },
            "since": {
                "type": "string",
                "description": (
                    "ISO 8601 datetime. Drop rows created before this. "
                    "Use to scope the time-index axis and bound volume "
                    "on long chats."
                ),
            },
            "limit": {
                "type": "integer",
                "default": _DEFAULT_LIMIT,
                "description": (
                    f"Max rows. Default {_DEFAULT_LIMIT}, capped at {_MAX_LIMIT}."
                ),
            },
            "format": {
                "type": "string",
                "enum": sorted(_VALID_FORMATS),
                "default": _DEFAULT_FORMAT,
                "description": (
                    "``prompt`` (default) renders prose for LLMs. "
                    "``json`` returns ``{items, truncated}``."
                ),
            },
        },
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        chatflow_id = _str_or_none(args, "chatflow_id")
        workflow_id = _str_or_none(args, "workflow_id")
        source_node_id = _str_or_none(args, "source_node_id")
        scope = _str_or_none(args, "scope")
        query = _str_or_none(args, "query")
        fmt = _str_or_none(args, "format") or _DEFAULT_FORMAT
        tag_single = _str_or_none(args, "tag")
        produced_arg = _list_str_or_none(args, "produced_tags")
        consumed_arg = _list_str_or_none(args, "consumed_tags")
        tag_match_mode = (
            _str_or_none(args, "tag_match_mode") or _DEFAULT_TAG_MATCH_MODE
        )
        prefix_match = bool(args.get("prefix_match", False))
        expand_chain = bool(args.get("expand_chain", False))
        since_str = _str_or_none(args, "since")

        # Auto-fill chatflow_id from caller context when the LLM didn't
        # supply it. Issue #3 from the 2026-04-29 qwen36 batch: workers
        # spawned inside sub-WorkFlows have no way to know the enclosing
        # chatflow's id (it's an engine concern, not user-visible), so
        # without this fallback every sub-WF lookup either errored
        # ("at least one of 'chatflow_id' or 'workflow_id' must be
        # provided") or the worker invented a description-style
        # placeholder. The engine already plumbs the top-level
        # chatflow id into ``ctx.caller_chatflow_id`` (workflow_engine
        # ``_run_tool_call``), so this fallback is the natural
        # default — workers can omit the arg and lookup just works.
        if not chatflow_id and not workflow_id:
            inferred = ctx.caller_chatflow_id
            if inferred:
                chatflow_id = inferred

        if not chatflow_id and not workflow_id:
            raise ToolError(
                "memoryboard_lookup: at least one of 'chatflow_id' or "
                "'workflow_id' must be provided (and no caller context "
                "was available to infer it)"
            )

        if scope is not None and scope not in _VALID_SCOPES:
            raise ToolError(
                f"memoryboard_lookup: invalid scope {scope!r}; "
                f"must be one of {sorted(_VALID_SCOPES)}"
            )

        if fmt not in _VALID_FORMATS:
            raise ToolError(
                f"memoryboard_lookup: invalid format {fmt!r}; "
                f"must be one of {sorted(_VALID_FORMATS)}"
            )

        if tag_match_mode not in _VALID_TAG_MATCH_MODES:
            raise ToolError(
                f"memoryboard_lookup: invalid tag_match_mode "
                f"{tag_match_mode!r}; must be one of "
                f"{sorted(_VALID_TAG_MATCH_MODES)}"
            )

        since_dt: datetime | None = None
        if since_str:
            try:
                since_dt = datetime.fromisoformat(since_str)
            except ValueError as exc:
                raise ToolError(
                    f"memoryboard_lookup: 'since' must be ISO 8601 datetime, "
                    f"got {since_str!r}"
                ) from exc

        raw_limit = args.get("limit", _DEFAULT_LIMIT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                f"memoryboard_lookup: 'limit' must be an integer, "
                f"got {raw_limit!r}"
            ) from exc
        limit = max(1, min(limit, _MAX_LIMIT))

        # Resolve which side filters apply. Single ``tag`` shorthand
        # expands to "match in EITHER produced or consumed" — that's
        # the producer+consumer dual view designed for "find the
        # rejection context even when the search keyword is the
        # proposed concept".
        union_tags = [tag_single] if tag_single else []

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
                stmt = stmt.where(
                    BoardItemRow.description.ilike(f"%{query}%")
                )
            if since_dt is not None:
                stmt = stmt.where(BoardItemRow.created_at >= since_dt)
            stmt = stmt.order_by(BoardItemRow.created_at)
            # Fetch a generous candidate pool; tag-filter happens in
            # Python so we may need to over-fetch before truncating.
            # SQLAlchemy doesn't expose a clean dialect-portable JSONB
            # ``?|`` op — Python filtering keeps SQLite (test) happy
            # AND avoids an extra dialect branch.
            stmt = stmt.limit(_MAX_LIMIT * 4)
            all_rows = list((await session.execute(stmt)).scalars().all())

        wants_tag_filter = (
            bool(union_tags) or bool(produced_arg) or bool(consumed_arg)
        )
        if wants_tag_filter:
            filtered = [
                r
                for r in all_rows
                if _row_matches_tags(
                    r,
                    union_tags=union_tags,
                    produced_required=produced_arg or [],
                    consumed_required=consumed_arg or [],
                    mode=tag_match_mode,
                    prefix_match=prefix_match,
                )
            ]
        else:
            filtered = all_rows

        if expand_chain and filtered:
            extras = _expand_chain_one_hop(filtered, all_rows)
            # Preserve original order while appending non-duplicates.
            seen_ids = {r.id for r in filtered}
            for extra in extras:
                if extra.id not in seen_ids:
                    filtered.append(extra)
                    seen_ids.add(extra.id)
            filtered.sort(key=lambda r: (r.created_at, r.id))

        truncated = len(filtered) > limit
        rows = filtered[:limit]

        if fmt == "json":
            payload = {
                "items": [_serialize_row(r) for r in rows],
                "truncated": truncated,
            }
            return ToolResult(content=json.dumps(payload, ensure_ascii=False))

        return ToolResult(content=_render_prompt_block(rows, truncated=truncated))


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


def _list_str_or_none(args: dict[str, Any], key: str) -> list[str] | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, list):
        raise ToolError(
            f"memoryboard_lookup: {key!r} must be a list of strings, "
            f"got {type(val).__name__}"
        )
    out: list[str] = []
    for item in val:
        if not isinstance(item, str):
            raise ToolError(
                f"memoryboard_lookup: {key!r} entries must be strings"
            )
        s = item.strip()
        if s:
            out.append(s)
    return out or None


def _tag_in_list(needle: str, hay: list[str], *, prefix_match: bool) -> bool:
    """Membership check honoring prefix_match.

    With ``prefix_match=False``: exact match. With ``prefix_match=True``:
    ``needle`` must equal a tag OR be its underscore-prefix (so
    ``plan_x`` matches ``plan_x``, ``plan_x_rejected``, ``plan_x_done``).
    The underscore guard prevents ``plan`` from matching ``planet``.
    """
    if not prefix_match:
        return needle in hay
    needle_us = needle + "_"
    for tag in hay:
        if tag == needle or tag.startswith(needle_us):
            return True
    return False


def _row_matches_tags(
    row: BoardItemRow,
    *,
    union_tags: list[str],
    produced_required: list[str],
    consumed_required: list[str],
    mode: str,
    prefix_match: bool,
) -> bool:
    """Apply tag filters in Python (works on both PG JSONB and SQLite
    JSON since we operate on the deserialized list).

    A row passes when:
    - For each requested ``union_tag``: at least one of the row's
      ``produced_tags`` OR ``consumed_tags`` matches (mode + prefix
      semantics applied per element).
    - For each requested ``produced_required``: matches against the
      row's ``produced_tags`` only.
    - For each requested ``consumed_required``: matches against the
      row's ``consumed_tags`` only.
    """
    produced_tags = list(row.produced_tags or [])
    consumed_tags = list(row.consumed_tags or [])

    def side_matches(needles: list[str], hay: list[str]) -> bool:
        if not needles:
            return True
        hits = [
            _tag_in_list(n, hay, prefix_match=prefix_match) for n in needles
        ]
        if mode == "all":
            return all(hits)
        return any(hits)

    def union_side_matches(needles: list[str]) -> bool:
        if not needles:
            return True
        results = []
        for n in needles:
            results.append(
                _tag_in_list(n, produced_tags, prefix_match=prefix_match)
                or _tag_in_list(n, consumed_tags, prefix_match=prefix_match)
            )
        if mode == "all":
            return all(results)
        return any(results)

    if not union_side_matches(union_tags):
        return False
    if not side_matches(produced_required, produced_tags):
        return False
    if not side_matches(consumed_required, consumed_tags):
        return False
    return True


def _expand_chain_one_hop(
    seeds: list[BoardItemRow], pool: list[BoardItemRow]
) -> list[BoardItemRow]:
    """Find one hop of downstream rows for each seed.

    Two relations count as "downstream":
    1. ``inner_chat_ids`` / ``work_node_ids`` reference: pool row's
       drill-down id list contains the seed's source_node_id, OR the
       seed's drill ids contain the pool row's source_node_id.
    2. Tag-flow: pool row's ``consumed_tags`` overlaps the seed's
       ``produced_tags`` (the pool row was 'built on' the seed's
       concepts).

    Returns rows from ``pool`` that aren't already in ``seeds``.
    """
    seed_ids = {r.id for r in seeds}
    seed_source_ids = {r.source_node_id for r in seeds}
    seed_drill = set()
    for r in seeds:
        for did in (r.inner_chat_ids or []):
            seed_drill.add(did)
        for did in (r.work_node_ids or []):
            seed_drill.add(did)

    seed_produced_by_source: dict[str, set[str]] = {
        r.source_node_id: set(r.produced_tags or []) for r in seeds
    }

    out: list[BoardItemRow] = []
    for cand in pool:
        if cand.id in seed_ids:
            continue
        # Drill reference (either direction).
        cand_drill = set(cand.inner_chat_ids or []) | set(cand.work_node_ids or [])
        if cand_drill & seed_source_ids:
            out.append(cand)
            continue
        if cand.source_node_id in seed_drill:
            out.append(cand)
            continue
        # Tag flow: cand consumed something a seed produced.
        cand_consumed = set(cand.consumed_tags or [])
        if cand_consumed:
            for produced in seed_produced_by_source.values():
                if cand_consumed & produced:
                    out.append(cand)
                    break
    return out


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
        "produced_tags": list(row.produced_tags or []),
        "consumed_tags": list(row.consumed_tags or []),
        "inner_chat_ids": list(row.inner_chat_ids or []),
        "work_node_ids": list(row.work_node_ids or []),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _render_prompt_block(rows: list[BoardItemRow], *, truncated: bool) -> str:
    """Format ``rows`` as a readable prose block for LLM consumption.

    Each row renders as a metadata line (``scope`` / ``source_kind`` /
    ``source_node_id`` / produced + consumed tag set / ``created_at``)
    followed by the indented brief description. Tags surface as
    sets of comma-separated names so the consumer can reason about
    concept lifecycles (e.g. seeing both ``plan_x`` on a producer
    row and ``plan_x_rejected`` on a later consumer row tells the
    full story without a second lookup).
    """
    if not rows:
        return "MemoryBoard lookup — no items matched."

    header = f"MemoryBoard lookup — {len(rows)} item{'s' if len(rows) != 1 else ''}"
    if truncated:
        header += " (truncated; raise ``limit`` for more)"
    header += "."

    blocks: list[str] = [header]
    for idx, row in enumerate(rows, start=1):
        ts = row.created_at.isoformat() if row.created_at else "?"
        meta_parts = [
            f"scope={row.scope}",
            f"kind={row.source_kind}",
            f"source={row.source_node_id}",
            f"at={ts}",
        ]
        if row.produced_tags:
            meta_parts.append("produced=" + ",".join(row.produced_tags))
        if row.consumed_tags:
            meta_parts.append("consumed=" + ",".join(row.consumed_tags))
        if row.fallback:
            meta_parts.append("fallback=true")
        meta = " · ".join(meta_parts)
        desc = (row.description or "").strip() or "(empty description)"
        indented = "\n".join(f"    {line}" for line in desc.splitlines())
        blocks.append(f"[{idx}] {meta}\n{indented}")
    return "\n\n".join(blocks)
