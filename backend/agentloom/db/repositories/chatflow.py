"""ChatFlow repository — persist and load schemas.ChatFlow via JSONB.

Mirrors WorkflowRepository (ADR-015: every read scoped by workspace_id;
frozen nodes cannot be modified on save).
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from sqlalchemy import delete, exists, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentloom.db.models.chatflow import ChatFlowRow
from agentloom.db.models.node_index import NodeIndexRow
from agentloom.db.repositories.base import WorkspaceScopedRepository
from agentloom.schemas import ChatFlow
from agentloom.schemas.chatflow import CompactPreserveMode
from agentloom.schemas.common import (
    ExecutionMode,
    FrozenNodeError,
    NodeStatus,
    ProviderModelRef,
    utcnow,
)


class ChatFlowNotFoundError(KeyError):
    pass


class ChatFlowRepository(WorkspaceScopedRepository):
    async def create(self, chatflow: ChatFlow, owner_id: str | None = None) -> ChatFlowRow:
        row = ChatFlowRow(
            id=chatflow.id,
            workspace_id=self.workspace_id,
            owner_id=owner_id,
            title=chatflow.title,
            payload=chatflow.model_dump(mode="json"),
        )
        self.session.add(row)
        await self.session.flush()
        await self._rebuild_node_index(chatflow)
        return row

    async def get(self, chatflow_id: str) -> ChatFlow:
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        return ChatFlow.model_validate(_clamp_legacy_compact(row.payload))

    async def save(self, chatflow: ChatFlow) -> None:
        """Overwrite an existing row. Rejects any mutation of a node
        that was frozen in the prior state — this covers both the outer
        ChatFlowNode and its inner WorkFlow nodes."""
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow.id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow.id)

        prior = ChatFlow.model_validate(_clamp_legacy_compact(row.payload))
        _assert_frozen_chatflow_nodes_unchanged(prior, chatflow)

        row.title = chatflow.title
        row.description = chatflow.description
        row.tags = chatflow.tags or None
        row.payload = chatflow.model_dump(mode="json")
        await self.session.flush()
        await self._rebuild_node_index(chatflow)

    async def list_ids(self) -> list[str]:
        stmt = (
            select(ChatFlowRow.id)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .order_by(ChatFlowRow.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_summaries(self) -> list[dict]:
        """Return lightweight summaries for all chatflows, most recent first."""
        stmt = (
            select(
                ChatFlowRow.id,
                ChatFlowRow.title,
                ChatFlowRow.description,
                ChatFlowRow.tags,
                ChatFlowRow.folder_id,
                ChatFlowRow.created_at,
                ChatFlowRow.updated_at,
            )
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .order_by(ChatFlowRow.updated_at.desc())
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            {
                "id": r.id,
                "title": r.title,
                "description": r.description,
                "tags": r.tags or [],
                "folder_id": r.folder_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    async def patch_metadata(
        self,
        chatflow_id: str,
        *,
        title: str | None = ...,  # type: ignore[assignment]
        description: str | None = ...,  # type: ignore[assignment]
        tags: list[str] | None = ...,  # type: ignore[assignment]
        draft_model: ProviderModelRef | None = ...,  # type: ignore[assignment]
        brief_model: ProviderModelRef | None = ...,  # type: ignore[assignment]
        default_judge_model: ProviderModelRef | None = ...,  # type: ignore[assignment]
        default_tool_call_model: ProviderModelRef | None = ...,  # type: ignore[assignment]
        default_execution_mode: ExecutionMode | None = ...,  # type: ignore[assignment]
        tool_loop_budget: int | None = ...,  # type: ignore[assignment]
        auto_mode_revise_budget: int | None = ...,  # type: ignore[assignment]
        judge_retry_budget: int | None = ...,  # type: ignore[assignment]
        min_ground_ratio: float | None = ...,  # type: ignore[assignment]
        ground_ratio_grace_nodes: int | None = ...,  # type: ignore[assignment]
        disabled_tool_names: list[str] | None = ...,  # type: ignore[assignment]
        compact_trigger_pct: float | None = ...,  # type: ignore[assignment]
        compact_target_pct: float | None = ...,  # type: ignore[assignment]
        compact_keep_recent_count: int | None = ...,  # type: ignore[assignment]
        compact_preserve_mode: CompactPreserveMode | None = ...,  # type: ignore[assignment]
        recalled_context_sticky_turns: int | None = ...,  # type: ignore[assignment]
        compact_model: ProviderModelRef | None = ...,  # type: ignore[assignment]
        compact_require_confirmation: bool | None = ...,  # type: ignore[assignment]
        chatnode_compact_trigger_pct: float | None = ...,  # type: ignore[assignment]
        chatnode_compact_target_pct: float | None = ...,  # type: ignore[assignment]
    ) -> None:
        """Update metadata fields. Pass ``...`` (default) to skip a field.

        ``title`` / ``description`` / ``tags`` live on top-level columns
        (for efficient sidebar queries) AND inside the ``payload`` JSON
        (so ``get()`` which reads only the payload stays in sync).
        ``draft_model`` / ``default_execution_mode`` / ``judge_retry_budget``
        live only in the payload.
        """
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)

        payload = dict(row.payload)

        if title is not ...:
            row.title = title
            payload["title"] = title
        if description is not ...:
            row.description = description
            payload["description"] = description
        if tags is not ...:
            row.tags = tags
            payload["tags"] = tags or []
        if draft_model is not ...:
            payload["draft_model"] = (
                draft_model.model_dump(mode="json") if draft_model else None
            )
        if brief_model is not ...:
            payload["brief_model"] = (
                brief_model.model_dump(mode="json") if brief_model else None
            )
        if default_judge_model is not ...:
            payload["default_judge_model"] = (
                default_judge_model.model_dump(mode="json")
                if default_judge_model
                else None
            )
        if default_tool_call_model is not ...:
            payload["default_tool_call_model"] = (
                default_tool_call_model.model_dump(mode="json")
                if default_tool_call_model
                else None
            )
        if default_execution_mode is not ...:
            if default_execution_mode is not None:
                payload["default_execution_mode"] = default_execution_mode.value
        if tool_loop_budget is not ...:
            # ``None`` is legal here (= unlimited), mirror verbatim.
            payload["tool_loop_budget"] = tool_loop_budget
        if auto_mode_revise_budget is not ...:
            payload["auto_mode_revise_budget"] = auto_mode_revise_budget
        if judge_retry_budget is not ...:
            if judge_retry_budget is not None:
                payload["judge_retry_budget"] = judge_retry_budget
        if min_ground_ratio is not ...:
            payload["min_ground_ratio"] = min_ground_ratio
        if ground_ratio_grace_nodes is not ...:
            if ground_ratio_grace_nodes is not None:
                payload["ground_ratio_grace_nodes"] = ground_ratio_grace_nodes
        if disabled_tool_names is not ...:
            if disabled_tool_names is not None:
                payload["disabled_tool_names"] = list(disabled_tool_names)
        if compact_trigger_pct is not ...:
            payload["compact_trigger_pct"] = compact_trigger_pct
        if compact_target_pct is not ...:
            if compact_target_pct is not None:
                payload["compact_target_pct"] = compact_target_pct
        if compact_keep_recent_count is not ...:
            if compact_keep_recent_count is not None:
                payload["compact_keep_recent_count"] = compact_keep_recent_count
        if compact_preserve_mode is not ...:
            if compact_preserve_mode is not None:
                payload["compact_preserve_mode"] = compact_preserve_mode
        if recalled_context_sticky_turns is not ...:
            if recalled_context_sticky_turns is not None:
                payload["recalled_context_sticky_turns"] = recalled_context_sticky_turns
        if compact_model is not ...:
            payload["compact_model"] = (
                compact_model.model_dump(mode="json") if compact_model else None
            )
        if compact_require_confirmation is not ...:
            if compact_require_confirmation is not None:
                payload["compact_require_confirmation"] = compact_require_confirmation
        if chatnode_compact_trigger_pct is not ...:
            payload["chatnode_compact_trigger_pct"] = chatnode_compact_trigger_pct
        if chatnode_compact_target_pct is not ...:
            if chatnode_compact_target_pct is not None:
                payload["chatnode_compact_target_pct"] = chatnode_compact_target_pct
        row.payload = payload
        await self.session.flush()

    async def move_to_folder(self, chatflow_id: str, folder_id: str | None) -> None:
        """Set folder_id on a chatflow (None = unfiled)."""
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        row.folder_id = folder_id
        await self.session.flush()

    async def delete(self, chatflow_id: str) -> None:
        stmt = (
            select(ChatFlowRow)
            .where(ChatFlowRow.workspace_id == self.workspace_id)
            .where(ChatFlowRow.id == chatflow_id)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise ChatFlowNotFoundError(chatflow_id)
        await self.session.delete(row)
        await self.session.flush()

    async def _rebuild_node_index(self, chatflow: ChatFlow) -> None:
        """Drop every ``node_index`` row for this chatflow and reinsert
        one row per ChatNode and per nested WorkNode. Called after every
        ``create`` / ``save`` so the index stays in lockstep with the
        stored payload.

        Full-rebuild (rather than diff) because ChatFlow mutations can
        add, remove, or move nodes anywhere in the tree — and the payload
        is JSONB, so a structural diff from SQL would be just as
        expensive as re-extraction from the loaded Pydantic model.
        """
        await self.session.execute(
            delete(NodeIndexRow).where(NodeIndexRow.chatflow_id == chatflow.id)
        )
        for node_id, kind in _iter_indexed_nodes(chatflow):
            self.session.add(
                NodeIndexRow(
                    node_id=node_id,
                    chatflow_id=chatflow.id,
                    workspace_id=self.workspace_id,
                    kind=kind,
                )
            )
        await self.session.flush()


def _iter_indexed_nodes(chatflow: ChatFlow) -> Iterable[tuple[str, str]]:
    """Yield ``(node_id, kind)`` for every ChatNode and every WorkNode
    in the ChatFlow, including those nested inside sub-workflows.

    ``kind`` is ``"chatnode"`` for top-level ChatFlowNode entries and
    ``"worknode"`` for every WorkFlowNode reached via any ChatNode's
    attached workflow (and recursively through
    ``WorkFlowNode.sub_workflow``).
    """
    for chat_node_id, chat_node in chatflow.nodes.items():
        yield chat_node_id, "chatnode"
        workflow = chat_node.workflow
        if workflow is not None:
            yield from _iter_worknodes(workflow)


def _iter_worknodes(workflow: Any) -> Iterable[tuple[str, str]]:
    for wn_id, wn in workflow.nodes.items():
        yield wn_id, "worknode"
        sub = getattr(wn, "sub_workflow", None)
        if sub is not None:
            yield from _iter_worknodes(sub)


async def backfill_missing_node_index(
    session_maker: async_sessionmaker[Any],
) -> int:
    """Populate ``node_index`` for any chatflow that lacks entries.

    Called from the app lifespan on startup so the ``get_node_context``
    tool can resolve nodes from chatflows that predate the index (which
    would otherwise only get entries on their next save). A chatflow is
    considered "unindexed" if *zero* rows in ``node_index`` reference
    it — a coarse but safe predicate since create/save always rebuild
    the full set atomically.

    Returns the number of chatflows rebuilt. Commits its own session.
    """
    async with session_maker() as session:
        stmt = select(ChatFlowRow.id, ChatFlowRow.workspace_id).where(
            ~exists().where(NodeIndexRow.chatflow_id == ChatFlowRow.id)
        )
        targets = list((await session.execute(stmt)).all())
        if not targets:
            return 0
        count = 0
        for chatflow_id, workspace_id in targets:
            row = (
                await session.execute(
                    select(ChatFlowRow).where(ChatFlowRow.id == chatflow_id)
                )
            ).scalar_one()
            chatflow = ChatFlow.model_validate(_clamp_legacy_compact(row.payload))
            for node_id, kind in _iter_indexed_nodes(chatflow):
                session.add(
                    NodeIndexRow(
                        node_id=node_id,
                        chatflow_id=chatflow_id,
                        workspace_id=workspace_id,
                        kind=kind,
                    )
                )
            count += 1
        await session.commit()
        return count


_ORPHAN_STATUSES = frozenset(
    {
        NodeStatus.RUNNING,
        NodeStatus.RETRYING,
        NodeStatus.WAITING_FOR_RATE_LIMIT,
    }
)

_ORPHAN_ERROR_MSG = "orphaned: engine restarted mid-run"


async def sweep_orphaned_running_nodes(
    session_maker: async_sessionmaker[Any],
    *,
    skip_chatflow_ids: set[str] | None = None,
) -> int:
    """Transition orphaned in-flight nodes to FAILED.

    The engine keeps its scheduler state (active tasks, rate-limit waits,
    retry timers) in memory. When the process dies or is hard-killed,
    any node persisted as ``running`` / ``retrying`` /
    ``waiting_for_rate_limit`` becomes an orphan — nothing left alive
    will ever mark it done, so it hangs forever in the UI and blocks
    downstream edits (frozen guard won't fire, but the engine also
    won't resume it).

    Two callers:
    - **Startup**: runs once when the lifespan hook opens, before any
      traffic. No chatflows are attached yet so every persisted
      in-flight status is stale by definition — pass
      ``skip_chatflow_ids=None`` (or omit) and sweep everything.
    - **Watchdog**: runs periodically in a background task, catches
      coroutine leaks that happen without a process restart. Must pass
      ``skip_chatflow_ids={cf_id for cf in engine.active_chatflow_ids()}``
      so legitimate in-flight turns don't get flipped to FAILED.

    ``waiting_for_user`` is intentionally left alone regardless of
    caller — that's a persistent halt-awaiting-resume state, not an
    orphan.

    Mutates the JSONB payload directly (no frozen guard needed — these
    statuses are non-frozen) and returns the total number of nodes
    cleaned.
    """
    async with session_maker() as session:
        rows = (await session.execute(select(ChatFlowRow))).scalars().all()
        total = 0
        for row in rows:
            if skip_chatflow_ids is not None and row.id in skip_chatflow_ids:
                continue
            try:
                chatflow = ChatFlow.model_validate(_clamp_legacy_compact(row.payload))
            except Exception:  # noqa: BLE001 — one bad row mustn't block the rest
                logging.getLogger(__name__).exception(
                    "orphan_sweep: failed to load chatflow %s", row.id
                )
                continue
            changed = _sweep_chatflow_orphans(chatflow)
            if changed:
                row.payload = chatflow.model_dump(mode="json")
                total += changed
                logging.getLogger(__name__).debug(
                    "orphan_sweep: cleaned %d node(s) in chatflow %s (workspace %s)",
                    changed,
                    row.id,
                    row.workspace_id,
                )
        if total:
            await session.commit()
        return total


def _sweep_chatflow_orphans(chatflow: ChatFlow) -> int:
    now = utcnow()
    count = 0
    for chat_node in chatflow.nodes.values():
        if chat_node.status in _ORPHAN_STATUSES:
            _mark_node_orphaned(chat_node, now)
            count += 1
        workflow = chat_node.workflow
        if workflow is not None:
            count += _sweep_workflow_orphans(workflow, now)
    return count


def _sweep_workflow_orphans(workflow: Any, now: Any) -> int:
    count = 0
    for wn in workflow.nodes.values():
        if wn.status in _ORPHAN_STATUSES:
            _mark_node_orphaned(wn, now)
            count += 1
        sub = getattr(wn, "sub_workflow", None)
        if sub is not None:
            count += _sweep_workflow_orphans(sub, now)
    return count


def _mark_node_orphaned(node: Any, now: Any) -> None:
    node.status = NodeStatus.FAILED
    node.error = node.error or _ORPHAN_ERROR_MSG
    node.finished_at = now
    node.updated_at = now


def _clamp_legacy_compact(payload: dict) -> dict:
    """Normalize rows saved before the ``trigger + target <= 1.0`` invariant.

    Rows created under the old ``compact_target_pct=0.5`` default violate
    the new schema validator when combined with ``compact_trigger_pct=0.7``.
    Clamp target down to ``1.0 - trigger`` so historical data stays
    loadable; new writes still go through the strict validator.
    """
    out = dict(payload)
    trig = out.get("compact_trigger_pct")
    tgt = out.get("compact_target_pct")
    if (
        isinstance(trig, (int, float))
        and isinstance(tgt, (int, float))
        and trig + tgt > 1.0
    ):
        out["compact_target_pct"] = max(0.0, round(1.0 - trig, 6))
    cn_trig = out.get("chatnode_compact_trigger_pct")
    cn_tgt = out.get("chatnode_compact_target_pct")
    if (
        isinstance(cn_trig, (int, float))
        and isinstance(cn_tgt, (int, float))
        and cn_trig + cn_tgt > 1.0
    ):
        out["chatnode_compact_target_pct"] = max(0.0, round(1.0 - cn_trig, 6))
    return out


#: Fields on a ChatFlowNode that may be mutated even after the node is
#: frozen. ``pending_queue`` is ancillary scheduling state — see the
#: ADR-009 addendum in ``PendingTurn``'s docstring. Deleting a failed
#: node's queue (via the DELETE node route) is also allowed; that path
#: goes through ``remove_node`` before calling ``save`` and therefore
#: doesn't trip this check.
_FROZEN_EXEMPT_FIELDS = frozenset({"pending_queue", "position_x", "position_y"})


def _strip_frozen_exempt(dump: dict) -> dict:
    """Return a copy of ``dump`` with exempt fields removed.

    Recursively strips ``sticky_notes`` from embedded workflow dicts so
    that adding/editing canvas notes never trips the frozen-node guard.
    """
    out = {}
    for k, v in dump.items():
        if k in _FROZEN_EXEMPT_FIELDS:
            continue
        if k == "workflow" and isinstance(v, dict):
            out[k] = _strip_workflow_sticky(v)
        else:
            out[k] = v
    return out


def _strip_workflow_sticky(wf: dict) -> dict:
    """Strip fields from a workflow dict that should not trip the frozen
    guard: ``sticky_notes`` (user canvas notes), plus every inner WorkNode's
    ``position_x`` / ``position_y`` (user drag-positioned layout). The
    outer-level ``_strip_frozen_exempt`` handles ``position_x`` /
    ``position_y`` on the ChatFlowNode itself, but the dump-equality check
    compares nested ``workflow.nodes[*]`` verbatim — without this recursive
    strip, dragging any WorkNode in a frozen (succeeded) ChatNode's inner
    workflow raises ``FrozenNodeError`` and the PATCH silently drops the
    change (the in-memory runtime is still mutated, so a GET sees the new
    position, but a backend restart or detach reveals the DB never saw it
    and the node snaps back to auto-layout on the next page load).
    Recurses into ``sub_workflow`` for nested delegation trees.
    """
    out = {k: v for k, v in wf.items() if k != "sticky_notes"}
    if "nodes" in out and isinstance(out["nodes"], dict):
        cleaned_nodes = {}
        for nid, node in out["nodes"].items():
            if not isinstance(node, dict):
                cleaned_nodes[nid] = node
                continue
            cleaned = {
                k: v
                for k, v in node.items()
                if k not in ("position_x", "position_y")
            }
            if "sub_workflow" in cleaned and isinstance(cleaned["sub_workflow"], dict):
                cleaned["sub_workflow"] = _strip_workflow_sticky(cleaned["sub_workflow"])
            cleaned_nodes[nid] = cleaned
        out["nodes"] = cleaned_nodes
    return out


def _assert_frozen_chatflow_nodes_unchanged(prior: ChatFlow, new: ChatFlow) -> None:
    """Reject changes to any previously-frozen ChatFlowNode.

    Inner WorkFlow nodes inherit the same immutability via their own
    status field — the comparison below does a full dump-equality so it
    naturally catches inner mutations too.

    ``pending_queue`` is exempt: it's a scheduling buffer attached to
    the node, not part of the dialogue record. Everything else on a
    frozen node is locked down.

    Node deletion is allowed here too — ``remove_node`` on the ChatFlow
    already handles cascade safety. A failed-node cleanup path deletes
    the node before save, and the resulting dump simply omits it.
    """
    for nid, prior_node in prior.nodes.items():
        if not prior_node.is_frozen:
            continue
        new_node = new.nodes.get(nid)
        if new_node is None:
            # Deletion of frozen nodes is allowed — the cascade-delete
            # and failed-node-cleanup paths both remove nodes before
            # calling save.
            continue
        prior_dump = _strip_frozen_exempt(prior_node.model_dump(mode="json"))
        new_dump = _strip_frozen_exempt(new_node.model_dump(mode="json"))
        if new_dump != prior_dump:
            raise FrozenNodeError(f"ChatFlow node {nid} is frozen and may not be modified")
