"""ChatFlow engine — Round A scheduler.

Responsibilities:
- Hold a per-chatflow ``ChatFlowRuntime`` that serializes mutations
  (queue edits, node creation, cascade logic) under a single asyncio
  lock. The lock is released across LLM calls so branches run in
  parallel; provider concurrency is capped by the caller's
  ``RateLimitedProvider`` wrapper, not this file.
- Accept a user turn and either (a) immediately spawn a child turn
  node when the chain's live tip is idle, or (b) append it to that
  tip's ``pending_queue`` where the walk-down logic will pick it up
  on the next transition.
- Run the inner WorkFlow for each chat node via ``WorkflowEngine``,
  collapsing the terminal llm_call's output into the chat node's
  ``agent_response`` (same "leaf llm_call" rule as before — see
  the M4 doc string preserved below on ``_terminal_llm_call``).
- Walk the queue down the chain: when a node succeeds, pop its
  queue head as the new child's ``user_message`` and hand the tail
  to the child's own ``pending_queue``. The child becomes the new
  live tip.
- On failure, discard the queued turns (default channel policy) and
  resolve their waiting futures with ``DiscardedUpstreamFailure``.
  Recovering from failure is the user's job via retry_failed_node
  (creates a sibling, transfers the queue) or delete_failed_node
  (drops the node and its queue entirely).

The legacy synchronous entry point ``submit_user_turn`` is kept for
the existing M4 tests and one-shot API callers. It internally drives
the queue path so there's one schedule codepath — it registers a
future keyed by the pending turn id, submits the turn, then awaits.
External channels should prefer ``enqueue`` which returns the
``PendingTurn`` immediately and relies on SSE events for progress.

Out of scope here: branching/merge UI, auto-planner, sub-agent
delegation (M8+). The inner WorkFlow still only supports llm_call +
tool_call via WorkflowEngine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentloom.channels.base import ExternalTurn
from agentloom.engine.events import EventBus, WorkflowEvent
from agentloom.engine.recursive_planner_parser import (
    AtomicBrief,
    PlannerParseError,
    RecursivePlannerOutput,
    SubTask,
    parse_recursive_planner_output,
)
from agentloom import tenancy_runtime
from agentloom.engine.workflow_engine import (
    DEFAULT_COMPACT_TARGET_PCT,
    DEFAULT_COMPACT_TRIGGER_PCT,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_PRESERVE_RECENT_TURNS,
    PostNodeHook,
    ProviderCall,
    WorkflowEngine,
    _count_text_tokens,
    _estimate_tokens_from_wire,
)
from agentloom.schemas.common import JudgeVariant, JudgeVerdict, WorkNodeRole
from agentloom.schemas import (
    ChatFlow,
    ChatFlowNode,
    CompactSnapshot,
    PendingTurn,
    StepKind,
    WorkFlow,
    WorkFlowNode,
)
from agentloom.schemas.chatflow import PendingTurnSource, UpstreamFailurePolicy
from agentloom.schemas.common import (
    EditableText,
    ExecutionMode,
    NodeId,
    NodeScope,
    NodeStatus,
    ProviderModelRef,
    StepKind,
    utcnow,
)
from agentloom.schemas.workflow import WireMessage
from agentloom.templates.instantiate import instantiate_fixture
from agentloom.templates.loader import fragments_as_texts, load_fixtures
from agentloom.tools.base import ToolContext, ToolRegistry

log = logging.getLogger(__name__)


#: How many times we re-spawn a crashed judge_call (provider error,
#: malformed transport, etc.) before giving up and bubbling the
#: failure up to the ChatNode. Applies to every judge variant
#: (pre / planner_judge / worker_judge / post). Distinct from
#: ``judge_retry_budget`` which counts judge-issued ``retry``
#: verdicts; a network blip shouldn't eat into the legit redo
#: budget. 2 retries = 3 attempts total; tuned for transient
#: provider errors, not persistent bugs.
_JUDGE_CRASH_RETRY_BUDGET = 2


def _chat_inherited_model(
    chatflow: ChatFlow,
    parent_ids: list[str],
) -> ProviderModelRef | None:
    """Return the model a new child should inherit when its spawning
    turn didn't specify one — i.e. the primary parent's already-snapshot
    ``resolved_model``, or the chatflow default if we're bootstrapping.

    Since resolved_model is immutable after spawn (§4.10 rework), the
    walk degenerates to a single lookup on ``parents[0]``; we keep the
    defensive multi-hop walk for the edge case where a transitional
    node might still be missing its snapshot (old DB rows predating
    this field).
    """
    seen: set[str] = set()
    cursor = parent_ids[0] if parent_ids else None
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        ancestor = chatflow.nodes.get(cursor)
        if ancestor is None:
            break
        if ancestor.resolved_model is not None:
            return ancestor.resolved_model
        cursor = ancestor.parent_ids[0] if ancestor.parent_ids else None
    return chatflow.draft_model


#: Optional persistence hook. When supplied, the engine calls it with
#: the mutated ChatFlow after every state change (turn completion,
#: queue edit, retry, delete). Invoked outside the runtime lock so
#: implementations that acquire their own db session don't deadlock.
SaveCallback = Callable[[ChatFlow], Awaitable[None]]


class ExecutionSwitches:
    """Four boolean toggles an ``ExecutionMode`` unpacks into. Kept as a
    tiny class so callers can name the fields instead of remembering
    tuple positions."""

    __slots__ = ("plan", "judge_pre", "judge_during", "judge_post")

    def __init__(
        self, *, plan: bool, judge_pre: bool, judge_during: bool, judge_post: bool
    ) -> None:
        self.plan = plan
        self.judge_pre = judge_pre
        self.judge_during = judge_during
        self.judge_post = judge_post


def derive_switches_from_mode(mode: ExecutionMode) -> ExecutionSwitches:
    """Per §3.4.1, each execution mode implies a starting position for
    the four switches. These are defaults — individual WorkFlows may
    override any switch after creation.

    - ``native_react``: pure ReAct. No plan, no judges. Existing M4/M6 shape.
    - ``semi_auto``: plan on, judge_pre on, judge_post on, judge_during
      off (opt-in — adversarial critic is expensive and only helps when
      the user wants it).
    - ``auto_plan``: all four on. Halt conditions (§3.4.1) gate progression.
    """
    if mode == ExecutionMode.NATIVE_REACT:
        return ExecutionSwitches(
            plan=False, judge_pre=False, judge_during=False, judge_post=False
        )
    if mode == ExecutionMode.SEMI_AUTO:
        return ExecutionSwitches(
            plan=True, judge_pre=True, judge_during=False, judge_post=True
        )
    # auto_plan
    return ExecutionSwitches(
        plan=True, judge_pre=True, judge_during=True, judge_post=True
    )


class DiscardedUpstreamFailure(Exception):
    """Raised into a waiting pending-turn future when its upstream
    node failed (and the channel policy is the default 'discard') or
    was deleted by the user."""


class ChatFlowRuntime:
    """In-memory execution state for one attached ChatFlow.

    Stores the live ChatFlow object (mutations happen in place), the
    asyncio lock that serializes edits, futures that unblock waiting
    callers of ``submit_user_turn``, and the set of background
    execution tasks so we can drain them on detach.
    """

    def __init__(self, chatflow: ChatFlow) -> None:
        self.chatflow = chatflow
        self.lock = asyncio.Lock()
        self.pending_futures: dict[str, asyncio.Future[ChatFlowNode]] = {}
        self.active_tasks: set[asyncio.Task[Any]] = set()
        self.node_tasks: dict[str, asyncio.Task[Any]] = {}

    def track(self, task: asyncio.Task[Any], node_id: str | None = None) -> None:
        self.active_tasks.add(task)
        task.add_done_callback(self.active_tasks.discard)
        if node_id is not None:
            self.node_tasks[node_id] = task
            task.add_done_callback(lambda _t: self.node_tasks.pop(node_id, None))

    async def drain(self) -> None:
        """Wait for all spawned execution tasks, including cascades.

        Each completed task may launch a child (e.g. walk-down queue
        consumption), so we loop until no *running* task remains.
        We filter ``active_tasks`` by ``not done()`` rather than
        checking the set directly because done_callbacks are scheduled
        via ``call_soon`` — if ``gather`` only sees already-done
        tasks, it returns without yielding, and the callbacks that
        would normally remove them from ``active_tasks`` never fire.
        """
        while True:
            pending = [t for t in self.active_tasks if not t.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)


def _make_board_writer(
    tool_context: ToolContext | None,
) -> Callable[..., Awaitable[None]]:
    """Build the :data:`BoardWriter` closure that :meth:`WorkflowEngine.
    _run_brief` hands its distilled descriptions to.

    Opens a fresh session per write via ``get_session_maker()`` — same
    pattern used by ``GetNodeContextTool`` — so the closure doesn't
    have to thread a session through the engine's signatures, and
    two concurrent briefs don't deadlock on a shared connection.
    Workspace scoping comes from *tool_context*; when the caller
    didn't supply one (pure unit tests), the writer falls back to
    ``DEFAULT_WORKSPACE_ID`` so the repo's ADR-015 filter has
    something coherent to match.
    """
    from agentloom.db.base import get_session_maker
    from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
    from agentloom.db.repositories.board_item import BoardItemRepository

    workspace_id = (
        tool_context.workspace_id if tool_context is not None else DEFAULT_WORKSPACE_ID
    )

    async def write(
        *,
        chatflow_id: str | None,
        workflow_id: str,
        source_node_id: str,
        source_kind: str,
        scope: str,
        description: str,
        fallback: bool,
    ) -> None:
        if chatflow_id is None:
            # Without a chatflow id we can't satisfy the board_items
            # NOT NULL FK; the engine still keeps the description on
            # the WorkNode so consumers in-memory see the brief text.
            return
        try:
            async with get_session_maker()() as session:
                repo = BoardItemRepository(session, workspace_id=workspace_id)
                await repo.upsert_by_source(
                    chatflow_id=chatflow_id,
                    workflow_id=workflow_id,
                    source_node_id=source_node_id,
                    source_kind=source_kind,
                    scope=scope,
                    description=description,
                    fallback=fallback,
                )
                await session.commit()
        except Exception:  # noqa: BLE001 — board is best-effort
            log.exception(
                "BoardItemRepository upsert failed for source=%s scope=%s",
                source_node_id,
                scope,
            )

    return write


_CHAT_BRIEF_USER_SNIPPET = 120
_CHAT_BRIEF_AGENT_SNIPPET = 200


def _first_line(text: str) -> str:
    """Grab the first non-empty line of *text* (stripped)."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _chat_board_source_kind(node: ChatFlowNode) -> str:
    """Classify a finished ChatNode into a MemoryBoard ``source_kind``.

    Same tiered rule as ``_build_chat_context``: a populated
    ``compact_snapshot`` outranks the structural merge test
    (``len(parent_ids) >= 2``) because the two are mutually exclusive
    in practice — a compact ChatNode always has exactly one parent,
    a merge ChatNode always has two. Plain turn nodes fall through to
    ``"chat_turn"``.
    """
    if node.compact_snapshot is not None:
        return "chat_compact"
    if len(node.parent_ids) >= 2:
        return "chat_merge"
    return "chat_turn"


def _chat_board_description(node: ChatFlowNode) -> str:
    """Deterministic code-template ChatBoardItem description for *node*.

    Three shapes, one per ``source_kind``:

    - ``chat_turn``  — "user asked: <first 120 chars>; agent: <first 200>".
    - ``chat_compact`` — mentions the number of messages folded and the
      leading line of the compact summary stored in ``agent_response``.
    - ``chat_merge``   — names the two parent branches and shows the
      leading line of the merged reply.

    We keep it code-only for MVP (§PR-3 scope); a quality LLM-generated
    brief can follow in a later PR without touching any of the readers.
    """
    kind = _chat_board_source_kind(node)
    agent_text = node.agent_response.text if node.agent_response else ""
    agent_snippet = _first_line(agent_text)[:_CHAT_BRIEF_AGENT_SNIPPET].strip()
    if kind == "chat_compact":
        snap = node.compact_snapshot
        dropped = snap.dropped_count if snap is not None else 0
        preserved = len(snap.preserved_messages) if snap is not None else 0
        summary_snippet = agent_snippet or "(empty summary)"
        return (
            f"compacted {dropped} messages into a summary "
            f"(+{preserved} preserved verbatim): {summary_snippet}"
        ).rstrip(": ")
    if kind == "chat_merge":
        sources = node.parent_ids
        if len(sources) >= 2:
            src_label = f"{sources[0][:8]}+{sources[1][:8]}"
        elif sources:
            src_label = sources[0][:8]
        else:
            src_label = "?"
        reply_snippet = agent_snippet or "(empty merge reply)"
        return (
            f"merged branches {src_label} into one reply: {reply_snippet}"
        ).rstrip(": ")
    # chat_turn: user ask + agent reply.
    user_text = node.user_message.text if node.user_message else ""
    user_snippet = _first_line(user_text)[:_CHAT_BRIEF_USER_SNIPPET].strip()
    if not user_snippet and not agent_snippet:
        return "(empty turn)"
    if not user_snippet:
        return f"agent: {agent_snippet}"
    if not agent_snippet:
        return f"user asked: {user_snippet}"
    return f"user asked: {user_snippet}; agent: {agent_snippet}"


class ChatFlowEngine:
    """Scheduler for ChatFlow turns.

    One engine per process. Callers attach a ChatFlow to get a
    runtime, then drive it via ``submit_user_turn`` / ``enqueue`` /
    queue-edit methods. The engine serializes mutations per chatflow
    and spawns per-node execution tasks that can run in parallel
    (bounded by the caller-supplied rate-limited provider).
    """

    def __init__(
        self,
        provider_call: ProviderCall,
        event_bus: EventBus,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        save_callback: SaveCallback | None = None,
    ) -> None:
        self._provider = provider_call
        self._bus = event_bus
        self._tools = tool_registry
        self._tool_ctx = tool_context
        self._save_callback = save_callback
        from agentloom.engine.provider_context_cache import lookup as _ctx_lookup

        self._inner = WorkflowEngine(
            provider_call,
            event_bus,
            tool_registry=tool_registry,
            tool_context=tool_context,
            context_window_lookup=_ctx_lookup,
            board_writer=_make_board_writer(tool_context),
        )
        self._runtimes: dict[str, ChatFlowRuntime] = {}
        self._registry_lock = asyncio.Lock()

        # Load builtin workflow templates from disk (sync), one set per
        # shipped language. The engine materializes planner / judge_pre /
        # judge_post / worker / compact inside ``_spawn_turn_node`` and
        # friends without an AsyncSession, and the current workspace
        # language selects which in-memory variant to use. Untranslated
        # fixtures in a non-default language fall back to the en-US plan
        # inside ``load_fixtures``.
        from agentloom.templates.loader import (
            DEFAULT_LANGUAGE,
            list_available_languages,
        )

        self._fixture_plans_by_lang: dict[str, dict[str, dict[str, Any]]] = {}
        self._fixture_includes_by_lang: dict[str, dict[str, str]] = {}
        for lang in list_available_languages() or [DEFAULT_LANGUAGE]:
            templates, fragments = load_fixtures(language=lang)
            self._fixture_plans_by_lang[lang] = {
                fx.builtin_id: fx.plan for fx in templates
            }
            self._fixture_includes_by_lang[lang] = fragments_as_texts(fragments)
        self._default_fixture_language = DEFAULT_LANGUAGE

    @property
    def _current_fixture_language(self) -> str:
        """Resolve the workspace's configured prompt language, falling
        back to the shipped default if the cache hasn't been primed or
        the configured language has no fixtures loaded."""
        from agentloom import tenancy_runtime
        from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID

        lang = tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID).language
        if lang in self._fixture_plans_by_lang:
            return lang
        return self._default_fixture_language

    @property
    def _fixture_plans(self) -> dict[str, dict[str, Any]]:
        """Active-language template plans keyed by ``builtin_id``.
        Existing callsites index by bare builtin_id — this property
        transparently picks the right per-language map."""
        return self._fixture_plans_by_lang[self._current_fixture_language]

    @property
    def _fixture_includes(self) -> dict[str, str]:
        """Active-language include-fragment texts keyed by fragment name."""
        return self._fixture_includes_by_lang[self._current_fixture_language]

    # ------------------------------------------------------------------ registry

    async def attach(self, chatflow: ChatFlow) -> ChatFlowRuntime:
        """Ensure a runtime exists for ``chatflow`` and return it.

        If the runtime already exists (a prior handler registered it)
        we keep the existing one — its in-memory state is
        authoritative, even if the caller passed a freshly-loaded
        ChatFlow instance with the same id.
        """
        async with self._registry_lock:
            runtime = self._runtimes.get(chatflow.id)
            if runtime is None:
                runtime = ChatFlowRuntime(chatflow)
                self._runtimes[chatflow.id] = runtime
            return runtime

    def get_runtime(self, chatflow_id: str) -> ChatFlowRuntime | None:
        return self._runtimes.get(chatflow_id)

    async def detach(self, chatflow_id: str) -> None:
        async with self._registry_lock:
            runtime = self._runtimes.pop(chatflow_id, None)
        if runtime is not None:
            await runtime.drain()

    # ------------------------------------------------------------------ turn submission

    async def submit_user_turn(
        self,
        chatflow: ChatFlow,
        user_text: str,
        *,
        parent_id: str | None = None,
        source: PendingTurnSource = "test",
        source_metadata: dict[str, Any] | None = None,
        on_upstream_failure: UpstreamFailurePolicy = "discard",
        spawn_model: ProviderModelRef | None = None,
        judge_spawn_model: ProviderModelRef | None = None,
        tool_call_spawn_model: ProviderModelRef | None = None,
    ) -> ChatFlowNode:
        """Submit a user turn and wait until its node finishes.

        Legacy synchronous entry point kept for tests and one-shot API
        callers. Channels and queue-driven flows should prefer
        :meth:`enqueue`.

        Resolution:
        - ``parent_id=None``: append to the latest leaf of the current
          chain, or bootstrap the first node on an empty chatflow.
        - ``parent_id=X``: fork directly from X (skip the queue walk);
          see the fork-semantics memory — forks must never reject.
        """
        runtime = await self.attach(chatflow)
        pending = PendingTurn(
            text=user_text,
            source=source,
            source_metadata=source_metadata or {},
            on_upstream_failure=on_upstream_failure,
            spawn_model=spawn_model,
            judge_spawn_model=judge_spawn_model,
            tool_call_spawn_model=tool_call_spawn_model,
        )
        future: asyncio.Future[ChatFlowNode] = asyncio.get_running_loop().create_future()

        async with runtime.lock:
            runtime.pending_futures[pending.id] = future
            await self._place_pending(runtime, pending, parent_id)

        try:
            return await future
        finally:
            runtime.pending_futures.pop(pending.id, None)

    async def on_external_turn(self, chatflow: ChatFlow, turn: ExternalTurn) -> str:
        """Channel adapter hook (ADR-016). See :meth:`submit_user_turn`.

        Carries ``turn.on_upstream_failure`` through to the PendingTurn
        so channel-level policy stays attached to the work even after
        the adapter context is gone.
        """
        node = await self.submit_user_turn(
            chatflow,
            turn.text,
            source="api",
            source_metadata=dict(turn.metadata),
            on_upstream_failure=turn.on_upstream_failure,
        )
        return node.agent_response.text

    # ------------------------------------------------------------------ queue ops

    async def enqueue(
        self,
        chatflow_id: str,
        node_id: str,
        text: str,
        *,
        source: PendingTurnSource = "web",
        source_metadata: dict[str, Any] | None = None,
        on_upstream_failure: UpstreamFailurePolicy = "discard",
        spawn_model: ProviderModelRef | None = None,
        judge_spawn_model: ProviderModelRef | None = None,
        tool_call_spawn_model: ProviderModelRef | None = None,
    ) -> PendingTurn:
        """Append a PendingTurn to the live tip of the chain rooted
        at ``node_id``.

        The caller picks a node from the UI, but if that node
        already has active children the queue must land on the
        *current* live tip (the most-recently-created leaf of the
        subtree) — otherwise the walk-down would strand the turn on
        an upstream queue nobody will drain. For branch-aware UIs
        the caller selects the branch via the node id it passes;
        walking from there picks the right descendant chain.
        """
        runtime = self._require_runtime(chatflow_id)
        pending = PendingTurn(
            text=text,
            source=source,
            source_metadata=source_metadata or {},
            on_upstream_failure=on_upstream_failure,
            spawn_model=spawn_model,
            judge_spawn_model=judge_spawn_model,
            tool_call_spawn_model=tool_call_spawn_model,
        )
        async with runtime.lock:
            if node_id not in runtime.chatflow.nodes:
                raise KeyError(node_id)
            tip_id = _live_tip(runtime.chatflow, node_id)
            tip = runtime.chatflow.get(tip_id)
            tip.pending_queue.append(pending)
            await self._publish_queue_updated(runtime, tip_id)
            await self._try_consume(runtime, tip_id)
        await self._save(runtime)
        return pending

    async def delete_queue_item(
        self, chatflow_id: str, node_id: str, item_id: str
    ) -> None:
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            node = runtime.chatflow.get(node_id)
            before = len(node.pending_queue)
            node.pending_queue = [p for p in node.pending_queue if p.id != item_id]
            if len(node.pending_queue) == before:
                raise KeyError(item_id)
            await self._publish_queue_updated(runtime, node_id)
        await self._save(runtime)

    async def patch_queue_item(
        self,
        chatflow_id: str,
        node_id: str,
        item_id: str,
        new_text: str,
    ) -> None:
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            node = runtime.chatflow.get(node_id)
            for p in node.pending_queue:
                if p.id == item_id:
                    p.text = new_text
                    break
            else:
                raise KeyError(item_id)
            await self._publish_queue_updated(runtime, node_id)
        await self._save(runtime)

    async def reorder_queue(
        self,
        chatflow_id: str,
        node_id: str,
        item_ids: list[str],
    ) -> None:
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            node = runtime.chatflow.get(node_id)
            by_id = {p.id: p for p in node.pending_queue}
            if set(by_id) != set(item_ids) or len(item_ids) != len(by_id):
                raise ValueError(
                    "reorder item_ids must be a permutation of the existing queue"
                )
            node.pending_queue = [by_id[i] for i in item_ids]
            await self._publish_queue_updated(runtime, node_id)
        await self._save(runtime)

    def _build_compact_chatnode(
        self,
        chatflow: ChatFlow,
        *,
        parent_id: str,
        preserve_recent_turns: int,
        target_tokens: int | None,
        model: ProviderModelRef | None,
        compact_instruction: str | None,
        must_keep: str = "",
        must_drop: str = "",
    ) -> ChatFlowNode:
        """Construct an unattached compact ChatFlowNode over ``parent_id``.

        Shared between Tier 2 manual compaction (``compact_chain``) and
        the ChatFlow-layer auto-compact trigger (``_spawn_turn_node``).
        The caller is responsible for adding the returned node to the
        chatflow and either launching or inlining its inner workflow.

        Raises ``ValueError`` if the chain rooted at ``parent_id`` has
        no prior turns to compact or if ``preserve_recent_turns`` is
        large enough to leave nothing to summarize.
        """
        # Pull real messages only — no synthetic "[Prior conversation —
        # summarized...]" preamble. If a prior compact exists on the
        # chain we feed its summary in separately as a prelude so the
        # worker can reference what was already condensed without
        # having to summarize-the-summary. This is the fix for the
        # cascade bug where preserve_recent_turns let the prior summary
        # message leak into the new compact's preserved tail, and the
        # next trigger would then fold it back into head_wire.
        tagged_full_real = _build_tagged_chat_context_for_compact(
            chatflow, parent_id
        )
        if not tagged_full_real:
            raise ValueError(
                f"chain rooted at {parent_id!r} has no prior turns to compact"
            )

        previous_summary = ""
        previous_summary_node_id: str | None = None
        current: str | None = parent_id
        while current is not None:
            node = chatflow.nodes[current]
            snap = node.compact_snapshot
            if snap is not None and snap.summary:
                previous_summary = snap.summary
                previous_summary_node_id = node.id
                break
            current = node.parent_ids[0] if node.parent_ids else None

        keep = max(0, min(len(tagged_full_real), preserve_recent_turns))
        head_tagged = tagged_full_real[:-keep] if keep else tagged_full_real
        tail_wire = (
            [m for _, m in tagged_full_real[-keep:]] if keep else []
        )
        head_wire = [m for _, m in head_tagged]
        full_real_wire = [m for _, m in tagged_full_real]

        if not head_tagged and not previous_summary:
            raise ValueError(
                "nothing to compact — preserve_recent_turns ≥ total turns"
            )

        head_parts: list[str] = []
        if previous_summary:
            tag = previous_summary_node_id or "?"
            head_parts.append(
                f"[node:{tag} | previously summarized context]\n{previous_summary}"
            )
        head_parts.extend(
            f"[node:{nid or '?'} | {m.role}] {m.content}"
            for nid, m in head_tagged
        )
        head_serialized = "\n".join(head_parts)

        resolved_target = (
            target_tokens
            if target_tokens is not None
            else max(512, int(4096 * DEFAULT_COMPACT_TARGET_PCT))
        )

        templated = instantiate_fixture(
            self._fixture_plans["compact"],
            {
                "messages_to_compact": head_serialized,
                "target_tokens": resolved_target,
                "must_keep": must_keep,
                "must_drop": must_drop,
                "compact_instruction": compact_instruction or "",
            },
            includes=self._fixture_includes,
        )

        inner_node = _single_node(templated)
        if model is not None:
            inner_node.model_override = model
            inner_node.resolved_model = model

        head_real_tokens = _estimate_tokens_from_wire(head_wire)
        prev_summary_tokens = (
            _count_text_tokens(previous_summary) if previous_summary else 0
        )
        original_tokens = head_real_tokens + prev_summary_tokens
        entry_tokens = _estimate_tokens_from_wire(full_real_wire) + prev_summary_tokens

        return ChatFlowNode(
            parent_ids=[parent_id],
            user_message=(
                EditableText.by_user(compact_instruction)
                if compact_instruction
                else None
            ),
            agent_response=EditableText.by_agent(""),
            workflow=templated,
            status=NodeStatus.PLANNED,
            entry_prompt_tokens=entry_tokens,
            compact_snapshot=CompactSnapshot(
                summary="",
                preserved_messages=list(tail_wire),
                source_range=(0, len(head_wire)),
                dropped_count=len(head_wire),
                original_tokens=original_tokens,
                compacted_tokens=0,
                compact_instruction=compact_instruction,
            ),
        )

    async def compact_chain(
        self,
        chatflow_id: str,
        parent_id: str,
        *,
        compact_instruction: str | None = None,
        must_keep: str = "",
        must_drop: str = "",
        preserve_recent_turns: int | None = None,
        target_tokens: int | None = None,
        model: ProviderModelRef | None = None,
    ) -> ChatFlowNode:
        """Tier 2 manual compaction: summarize the chain above
        ``parent_id`` into a new compact ChatNode rooted at that parent.

        The resulting node becomes the chain's new "shoulder": future
        turns forked from it (or its descendants) see the summary prose
        + preserved recent turns instead of the full pre-compact
        history. ``user_message`` on the compact ChatNode holds the
        user's compact instruction when provided — nothing is lost; the
        chain is queryable up-chain for anyone who wants the raw pre-
        compact view (the message panel, history exports).

        Returns the frozen compact ChatNode once the summary has been
        generated and written.
        """
        runtime = self._require_runtime(chatflow_id)
        preserve = (
            preserve_recent_turns
            if preserve_recent_turns is not None
            else DEFAULT_PRESERVE_RECENT_TURNS
        )

        async with runtime.lock:
            chatflow = runtime.chatflow
            if parent_id not in chatflow.nodes:
                raise KeyError(f"parent {parent_id!r} not in chatflow {chatflow_id!r}")
            compact_node = self._build_compact_chatnode(
                chatflow,
                parent_id=parent_id,
                preserve_recent_turns=preserve,
                target_tokens=target_tokens,
                model=model,
                compact_instruction=compact_instruction,
                must_keep=must_keep,
                must_drop=must_drop,
            )
            chatflow.add_node(compact_node)
            compact_id = compact_node.id

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.created",
                node_id=compact_id,
                data={
                    "parent_id": parent_id,
                    "compact": True,
                },
            )
        )
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.status",
                node_id=compact_id,
                data={"status": NodeStatus.RUNNING.value},
            )
        )

        async with runtime.lock:
            compact_node = chatflow.get(compact_id)
            compact_node.status = NodeStatus.RUNNING
            compact_node.started_at = utcnow()

        # Relay inner workflow events to the chatflow-level SSE channel
        # so the frontend sees the compact worker running. Mirrors
        # _execute_node's relay setup.
        inner_wf_id = compact_node.workflow.id
        relay_queue = self._bus.open_subscription(inner_wf_id)
        relay_task = asyncio.create_task(
            self._relay_inner_events(
                chatflow.id, compact_id, inner_wf_id, relay_queue
            )
        )

        runtime_error: str | None = None
        ws_settings = tenancy_runtime.get_settings(self._inner._tool_ctx.workspace_id)
        effective_disabled = (
            frozenset(chatflow.disabled_tool_names) | frozenset(ws_settings.globally_disabled())
        )
        try:
            await self._inner.execute(
                compact_node.workflow,
                chatflow_tool_loop_budget=chatflow.tool_loop_budget,
                chatflow_auto_mode_revise_budget=chatflow.auto_mode_revise_budget,
                chatflow_min_ground_ratio=None,  # compact workers are single-shot
                chatflow_ground_ratio_grace_nodes=20,
                # Tier 1 inside the compact worker itself is disabled —
                # the worker already IS a compaction, so auto-compacting
                # its own input would recurse uselessly. Downstream
                # ChatFlow turns re-read the chatflow settings on their
                # own execute() and re-enable Tier 1 normally.
                chatflow_compact_trigger_pct=None,
                chatflow_id=chatflow.id,
                disabled_tool_names=effective_disabled,
            )
        except Exception as exc:  # noqa: BLE001 — engine boundary
            log.exception("compact ChatNode %s inner workflow raised", compact_id)
            runtime_error = f"{type(exc).__name__}: {exc}"
        finally:
            await self._bus.close(inner_wf_id)
            try:
                await relay_task
            except Exception:
                pass

        async with runtime.lock:
            compact_node = chatflow.get(compact_id)
            inner_llm = _single_node(compact_node.workflow)
            summary = (
                (inner_llm.output_message.content or "").strip()
                if inner_llm.output_message
                else ""
            )
            if runtime_error is not None or not summary:
                compact_node.status = NodeStatus.FAILED
                compact_node.error = (
                    runtime_error or "compact worker returned empty summary"
                )
                compact_node.finished_at = utcnow()
            else:
                # Hard-cap the summary at ``chatnode_compact_target_pct``
                # of the resolved model's context window. Preserves the
                # ``trigger_pct + target_pct ≤ 1.0`` invariant even when
                # the summarizer ignores its target_tokens guidance.
                from agentloom.engine.workflow_engine import (
                    _truncate_text_to_tokens,
                )

                resolved_for_cap = (
                    compact_node.resolved_model
                    or inner_llm.resolved_model
                    or inner_llm.model_override
                )
                ctx_for_cap = self._inner._context_window_for(resolved_for_cap)
                target_cap = max(
                    256,
                    int(ctx_for_cap * chatflow.chatnode_compact_target_pct),
                )
                summary_tokens = _count_text_tokens(summary)
                if summary_tokens > target_cap:
                    log.warning(
                        "compact ChatNode %s summary %d tokens > target %d — truncating",
                        compact_id,
                        summary_tokens,
                        target_cap,
                    )
                    summary = _truncate_text_to_tokens(summary, target_cap)
                compacted_tokens = _count_text_tokens(summary) + _estimate_tokens_from_wire(
                    compact_node.compact_snapshot.preserved_messages
                    if compact_node.compact_snapshot
                    else []
                )
                if compact_node.compact_snapshot is not None:
                    compact_node.compact_snapshot = (
                        compact_node.compact_snapshot.model_copy(
                            update={
                                "summary": summary,
                                "compacted_tokens": compacted_tokens,
                            }
                        )
                    )
                compact_node.agent_response = EditableText.by_agent(summary)
                compact_node.status = NodeStatus.SUCCEEDED
                compact_node.finished_at = utcnow()

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.status",
                node_id=compact_id,
                data={
                    "status": compact_node.status.value,
                    **({"error": compact_node.error} if compact_node.error else {}),
                },
            )
        )
        await self._save(runtime)
        return compact_node

    #: Fixed overhead (system prompt + template scaffolding around the
    #: two branch summaries) reserved when budgeting the merge prompt.
    #: Rough estimate — the merge.yaml system+formatting preamble is
    #: ~200 chars; we leave extra slack for the model's own response
    #: headroom. Tweak if the template grows materially.
    _MERGE_PROMPT_OVERHEAD_TOKENS = 800

    async def _precompact_branch_for_merge(
        self,
        wire: list[WireMessage],
        *,
        target_tokens: int,
        model: ProviderModelRef | None,
        disabled_tool_names: frozenset[str],
    ) -> str:
        """Summarize one branch's wire chain via the ``compact`` template.

        Used by :meth:`merge_chain` when a branch would not fit the
        merge model's per-branch budget. Runs the compact builtin as a
        throwaway WorkFlow — not attached to any ChatNode and not
        persisted — and returns just the summary string the merge
        prompt needs. The compact WorkFlow's own Tier 1 auto-compact is
        disabled so we never recurse into another pre-compact.

        Raises ``RuntimeError`` if the compact worker returns an empty
        reply; the caller should surface this as a merge failure rather
        than proceed with a truncated input.
        """
        head_serialized = "\n".join(f"[{m.role}] {m.content}" for m in wire)
        templated = instantiate_fixture(
            self._fixture_plans["compact"],
            {
                "messages_to_compact": head_serialized,
                "target_tokens": target_tokens,
                "must_keep": "",
                "must_drop": "",
                "compact_instruction": (
                    "Summarize this conversation branch so it can be "
                    "merged with a parallel branch. Preserve decisions, "
                    "open questions, and concrete facts; drop small talk."
                ),
            },
            includes=self._fixture_includes,
        )
        inner = _single_node(templated)
        if model is not None:
            inner.model_override = model
            inner.resolved_model = model

        await self._inner.execute(
            templated,
            chatflow_tool_loop_budget=1,
            chatflow_auto_mode_revise_budget=0,
            chatflow_min_ground_ratio=None,
            chatflow_ground_ratio_grace_nodes=20,
            # No Tier 1 recursion — we're already the pre-compact step.
            chatflow_compact_trigger_pct=None,
            disabled_tool_names=disabled_tool_names,
        )
        summary = (
            (inner.output_message.content or "").strip()
            if inner.output_message
            else ""
        )
        if not summary:
            raise RuntimeError("pre-compact worker returned empty summary")
        return summary

    async def merge_chain(
        self,
        chatflow_id: str,
        *,
        left_id: str,
        right_id: str,
        merge_instruction: str | None = None,
        model: ProviderModelRef | None = None,
    ) -> ChatFlowNode:
        """Fold two ChatNode branches into a single synthesized reply.

        Mirrors :meth:`compact_chain` in shape: we build a new ChatNode
        with ``parent_ids=[left_id, right_id]``, run the ``merge``
        builtin template as its inner workflow, and stamp the worker's
        output onto ``agent_response``. Multi-parent is itself the
        structural marker — downstream context walks stop at this node
        just like they stop at a compact node (both branches' history
        is encoded in the merged reply).

        Context-overflow handling: if either branch's wire chain would
        exceed the per-branch budget (half of the merge model's context
        window times :data:`DEFAULT_COMPACT_TRIGGER_PCT`, minus a fixed
        :data:`_MERGE_PROMPT_OVERHEAD_TOKENS`), that branch is first
        summarized via :meth:`_precompact_branch_for_merge` and the
        summary string is fed into the merge prompt instead of the raw
        chain.

        MVP: exactly two source nodes; left/right ordering is the order
        the user picked them in the canvas.
        """
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            chatflow = runtime.chatflow
            if left_id == right_id:
                raise ValueError("cannot merge a node with itself")
            if left_id not in chatflow.nodes:
                raise KeyError(f"left {left_id!r} not in chatflow {chatflow_id!r}")
            if right_id not in chatflow.nodes:
                raise KeyError(f"right {right_id!r} not in chatflow {chatflow_id!r}")

            left_ctx = _build_chat_context(chatflow, [left_id])
            right_ctx = _build_chat_context(chatflow, [right_id])
            if not left_ctx and not right_ctx:
                raise ValueError("both branches are empty — nothing to merge")

            # Snapshot everything we need before releasing the lock for
            # the (potentially slow) pre-compact llm_calls. The chatflow
            # could technically mutate under us while pre-compact runs,
            # but the wire snapshots are immutable and the source_ids
            # only need to still exist when we re-enter the lock.
            merge_model_ref = (
                model or chatflow.compact_model or chatflow.draft_model
            )
            trigger_pct = (
                chatflow.compact_trigger_pct
                if chatflow.compact_trigger_pct is not None
                else DEFAULT_COMPACT_TRIGGER_PCT
            )
            ws_settings = tenancy_runtime.get_settings(
                self._inner._tool_ctx.workspace_id
            )
            effective_disabled = frozenset(
                chatflow.disabled_tool_names
            ) | frozenset(ws_settings.globally_disabled())

        # --- Outside lock: budget + maybe pre-compact each branch. ---
        from agentloom.engine.provider_context_cache import (
            lookup as _ctx_lookup,
        )

        window = _ctx_lookup(merge_model_ref) or DEFAULT_CONTEXT_WINDOW_TOKENS
        total_budget = int(window * trigger_pct)
        per_branch_budget = max(
            512,
            (total_budget - self._MERGE_PROMPT_OVERHEAD_TOKENS) // 2,
        )

        left_tokens = _estimate_tokens_from_wire(left_ctx)
        right_tokens = _estimate_tokens_from_wire(right_ctx)
        original_tokens = left_tokens + right_tokens

        if left_tokens > per_branch_budget:
            log.info(
                "merge pre-compact: left branch %d tokens > %d budget",
                left_tokens,
                per_branch_budget,
            )
            left_summary = await self._precompact_branch_for_merge(
                left_ctx,
                target_tokens=max(
                    512,
                    int(per_branch_budget * DEFAULT_COMPACT_TARGET_PCT),
                ),
                model=merge_model_ref,
                disabled_tool_names=effective_disabled,
            )
        else:
            left_summary = _serialize_wire_chain(left_ctx)

        if right_tokens > per_branch_budget:
            log.info(
                "merge pre-compact: right branch %d tokens > %d budget",
                right_tokens,
                per_branch_budget,
            )
            right_summary = await self._precompact_branch_for_merge(
                right_ctx,
                target_tokens=max(
                    512,
                    int(per_branch_budget * DEFAULT_COMPACT_TARGET_PCT),
                ),
                model=merge_model_ref,
                disabled_tool_names=effective_disabled,
            )
        else:
            right_summary = _serialize_wire_chain(right_ctx)

        # --- Back under lock: build the merge ChatNode. ---
        async with runtime.lock:
            chatflow = runtime.chatflow
            if left_id not in chatflow.nodes or right_id not in chatflow.nodes:
                raise KeyError(
                    f"source node was removed during pre-compact "
                    f"(left={left_id!r}, right={right_id!r})"
                )

            templated = instantiate_fixture(
                self._fixture_plans["merge"],
                {
                    "left_summary": left_summary,
                    "right_summary": right_summary,
                },
                includes=self._fixture_includes,
            )
            inner_node = _single_node(templated)
            if model is not None:
                inner_node.model_override = model
                inner_node.resolved_model = model

            merge_node = ChatFlowNode(
                parent_ids=[left_id, right_id],
                user_message=(
                    EditableText.by_user(merge_instruction)
                    if merge_instruction
                    else None
                ),
                agent_response=EditableText.by_agent(""),
                workflow=templated,
                status=NodeStatus.PLANNED,
                entry_prompt_tokens=original_tokens,
            )
            chatflow.add_node(merge_node)
            merge_id = merge_node.id

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.created",
                node_id=merge_id,
                data={
                    "parent_ids": [left_id, right_id],
                    "merge": True,
                },
            )
        )
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.status",
                node_id=merge_id,
                data={"status": NodeStatus.RUNNING.value},
            )
        )

        async with runtime.lock:
            merge_node = chatflow.get(merge_id)
            merge_node.status = NodeStatus.RUNNING
            merge_node.started_at = utcnow()

        inner_wf_id = merge_node.workflow.id
        relay_queue = self._bus.open_subscription(inner_wf_id)
        relay_task = asyncio.create_task(
            self._relay_inner_events(
                chatflow.id, merge_id, inner_wf_id, relay_queue
            )
        )

        runtime_error: str | None = None
        ws_settings = tenancy_runtime.get_settings(self._inner._tool_ctx.workspace_id)
        effective_disabled = (
            frozenset(chatflow.disabled_tool_names) | frozenset(ws_settings.globally_disabled())
        )
        try:
            await self._inner.execute(
                merge_node.workflow,
                chatflow_tool_loop_budget=chatflow.tool_loop_budget,
                chatflow_auto_mode_revise_budget=chatflow.auto_mode_revise_budget,
                chatflow_min_ground_ratio=None,  # single-shot merger
                chatflow_ground_ratio_grace_nodes=20,
                # Tier 1 auto-compact is off for the merge worker — the
                # pre-compact step above (_precompact_branch_for_merge)
                # already guaranteed each branch fits its budget. A
                # second Tier 1 trigger here would be redundant and
                # would fire on a prompt that isn't shaped like a
                # conversation (it's templated as two big text blobs).
                chatflow_compact_trigger_pct=None,
                chatflow_id=chatflow.id,
                disabled_tool_names=effective_disabled,
            )
        except Exception as exc:  # noqa: BLE001 — engine boundary
            log.exception("merge ChatNode %s inner workflow raised", merge_id)
            runtime_error = f"{type(exc).__name__}: {exc}"
        finally:
            await self._bus.close(inner_wf_id)
            try:
                await relay_task
            except Exception:
                pass

        async with runtime.lock:
            merge_node = chatflow.get(merge_id)
            inner_llm = _single_node(merge_node.workflow)
            merged_reply = (
                (inner_llm.output_message.content or "").strip()
                if inner_llm.output_message
                else ""
            )
            if runtime_error is not None or not merged_reply:
                merge_node.status = NodeStatus.FAILED
                merge_node.error = (
                    runtime_error or "merge worker returned empty reply"
                )
                merge_node.finished_at = utcnow()
            else:
                merged_tokens = _count_text_tokens(merged_reply)
                merge_node.agent_response = EditableText.by_agent(merged_reply)
                merge_node.output_response_tokens = merged_tokens
                merge_node.status = NodeStatus.SUCCEEDED
                merge_node.finished_at = utcnow()

        # ChatBoard hook (PR 3): a successful merge ChatNode gets its
        # own ChatBoardItem so descendants see a single "branches A+B
        # merged" entry on their ancestor walk. The write happens after
        # the runtime lock so board I/O doesn't serialize queued turns.
        if merge_node.status == NodeStatus.SUCCEEDED:
            await self._spawn_chat_board_item(chatflow, merge_node)

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.status",
                node_id=merge_id,
                data={
                    "status": merge_node.status.value,
                    **({"error": merge_node.error} if merge_node.error else {}),
                },
            )
        )
        await self._save(runtime)
        return merge_node

    async def delete_node_cascade(
        self, chatflow_id: str, node_id: str
    ) -> list[str]:
        """Delete a node and all its descendants.

        Raises ``ValueError`` if the node itself or any descendant is
        currently RUNNING. Returns the list of removed node ids.
        """
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            chat = runtime.chatflow
            if node_id not in chat.nodes:
                raise KeyError(f"node {node_id} not in chatflow")

            # Roots (the greeting, and any future root-level node) are
            # the anchor of the conversation — never allow removal.
            if node_id in chat.root_ids:
                raise ValueError(f"cannot delete root node {node_id}")

            subtree = chat.descendants(node_id)
            subtree.add(node_id)

            # Block deletion when any node in the subtree is running.
            for nid in subtree:
                n = chat.nodes.get(nid)
                if n and n.status == NodeStatus.RUNNING:
                    raise ValueError(
                        f"cannot delete: node {nid} is currently running"
                    )

            # Discard pending queues + resolve futures.
            for nid in subtree:
                n = chat.nodes.get(nid)
                if n and n.pending_queue:
                    self._fail_pending(
                        runtime,
                        n.pending_queue,
                        DiscardedUpstreamFailure(
                            f"node {nid} deleted by user"
                        ),
                    )

            removed = chat.remove_subtree(node_id)

        # Publish one deletion event per removed node.
        for nid in removed:
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=chat.id,
                    kind="chat.node.deleted",
                    node_id=nid,
                )
            )
        await self._save(runtime)
        return removed

    async def delete_failed_node(self, chatflow_id: str, node_id: str) -> None:
        """Delete a FAILED node and discard its pending queue.

        The user sees a confirmation dialog before this is called.
        Any futures waiting on items in the discarded queue are
        resolved with ``DiscardedUpstreamFailure``.
        """
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            node = runtime.chatflow.get(node_id)
            if node.status != NodeStatus.FAILED:
                raise ValueError(
                    f"node {node_id} is {node.status.value}; "
                    "only failed nodes may be deleted"
                )
            self._fail_pending(
                runtime,
                node.pending_queue,
                DiscardedUpstreamFailure(
                    f"upstream node {node_id} was deleted by user"
                ),
            )
            runtime.chatflow.remove_node(node_id)
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=runtime.chatflow.id,
                kind="chat.node.deleted",
                node_id=node_id,
            )
        )
        await self._save(runtime)

    async def retry_failed_node(
        self,
        chatflow_id: str,
        node_id: str,
        *,
        spawn_model: ProviderModelRef | None = None,
        judge_spawn_model: ProviderModelRef | None = None,
        tool_call_spawn_model: ProviderModelRef | None = None,
    ) -> ChatFlowNode:
        """Create a sibling of a FAILED node and transfer its queue.

        The failed node stays in place as a dialogue record; the new
        sibling inherits the same parent and user_message, plus the
        failed node's ``pending_queue`` which will walk down the new
        branch as it runs.

        When caller-supplied ``*_spawn_model`` overrides are given they
        take precedence over the failed node's original models — this
        lets the UI retry with a different model selection.
        """
        runtime = self._require_runtime(chatflow_id)
        async with runtime.lock:
            failed = runtime.chatflow.get(node_id)
            if failed.status != NodeStatus.FAILED:
                raise ValueError(
                    f"node {node_id} is {failed.status.value}; "
                    "only failed nodes may be retried"
                )
            if failed.user_message is None:
                raise ValueError(
                    f"node {node_id} has no user_message to retry"
                )

            inherited_queue = list(failed.pending_queue)
            failed.pending_queue = []
            effective_model = spawn_model or failed.resolved_model
            effective_judge = (
                judge_spawn_model
                or failed.workflow.judge_model_override
            )
            effective_tool_call = (
                tool_call_spawn_model
                or failed.workflow.tool_call_model_override
            )
            sibling = self._spawn_turn_node(
                runtime,
                parent_ids=list(failed.parent_ids),
                user_message_text=failed.user_message.text,
                pending_queue=inherited_queue,
                spawn_model=effective_model,
                judge_spawn_model=effective_judge,
                tool_call_spawn_model=effective_tool_call,
            )
            await self._publish_node_created(runtime, sibling.id)
            await self._publish_queue_updated(runtime, node_id)
            await self._publish_queue_updated(runtime, sibling.id)
            self._launch_execute(runtime, sibling.id, consumed_pending_id=None)
        await self._save(runtime)
        return sibling

    async def cancel_running_node(
        self, chatflow_id: str, node_id: str
    ) -> None:
        """Cancel a RUNNING node's execution and mark it FAILED.

        Cancels the background asyncio task, marks the node FAILED,
        discards queued turns behind it (respecting policy), and
        publishes the appropriate SSE events.
        """
        runtime = self._require_runtime(chatflow_id)
        task = runtime.node_tasks.get(node_id)
        if task is not None and not task.done():
            task.cancel()
            # Wait for the task to finish cancellation so the node
            # status is settled before we proceed.
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        cascaded: list[str] = []
        async with runtime.lock:
            if node_id not in runtime.chatflow.nodes:
                return
            node = runtime.chatflow.get(node_id)
            if node.status not in (NodeStatus.RUNNING, NodeStatus.PLANNED):
                return
            node.status = NodeStatus.FAILED
            node.error = "Cancelled by user"
            node.finished_at = utcnow()
            # task.cancel() interrupts WorkflowEngine mid-loop and the
            # in-flight WorkNode never reaches its own finalize block,
            # so its status stays RUNNING in the DB forever. Walk the
            # tree and force-fail every still-running WorkNode so the
            # UI doesn't show ghost spinners.
            cascaded = _cascade_fail_workflow(node.workflow)
            # Discard pending queue items.
            discarded = [
                p for p in node.pending_queue
                if p.on_upstream_failure == "discard"
            ]
            kept = [
                p for p in node.pending_queue
                if p.on_upstream_failure != "discard"
            ]
            self._fail_pending(
                runtime,
                discarded,
                DiscardedUpstreamFailure(
                    f"upstream node {node_id} cancelled by user"
                ),
            )
            node.pending_queue = kept

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=runtime.chatflow.id,
                kind="chat.node.status",
                node_id=node_id,
                data={"status": NodeStatus.FAILED.value},
            )
        )
        for wn_id in cascaded:
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=runtime.chatflow.id,
                    kind="chat.workflow.node.failed",
                    node_id=wn_id,
                    data={
                        "status": NodeStatus.FAILED.value,
                        "chat_node_id": node_id,
                        "error": "Cancelled by user",
                    },
                )
            )
        await self._save(runtime)

    # ------------------------------------------------------------------ internals

    def _require_runtime(self, chatflow_id: str) -> ChatFlowRuntime:
        runtime = self._runtimes.get(chatflow_id)
        if runtime is None:
            raise KeyError(f"chatflow {chatflow_id} is not attached")
        return runtime

    async def _place_pending(
        self,
        runtime: ChatFlowRuntime,
        pending: PendingTurn,
        parent_id: str | None,
    ) -> None:
        """Route a just-submitted pending turn. Called under the lock."""
        chatflow = runtime.chatflow

        if parent_id is not None:
            # Explicit parent → direct fork. Skip the queue walk so
            # the user gets a fresh branch even when that parent
            # already has children (see fork-semantics memory).
            if parent_id not in chatflow.nodes:
                raise KeyError(f"parent chat node {parent_id} not in chatflow")
            node = self._spawn_turn_node(
                runtime,
                parent_ids=[parent_id],
                user_message_text=pending.text,
                pending_queue=[],
                spawn_model=pending.spawn_model,
                judge_spawn_model=pending.judge_spawn_model,
                tool_call_spawn_model=pending.tool_call_spawn_model,
                originating_pending=pending,
            )
            await self._publish_node_created(runtime, node.id)
            await self._publish_queue_updated(runtime, node.id)
            self._launch_execute(
                runtime,
                node.id,
                consumed_pending_id=None
                if node.compact_snapshot is not None
                else pending.id,
            )
            return

        leaf_id = _latest_leaf(chatflow)
        if leaf_id is None:
            # Empty chatflow — bootstrap the first node directly from
            # the pending text. No parent, no queue carry-over.
            node = self._spawn_turn_node(
                runtime,
                parent_ids=[],
                user_message_text=pending.text,
                pending_queue=[],
                spawn_model=pending.spawn_model,
                judge_spawn_model=pending.judge_spawn_model,
                tool_call_spawn_model=pending.tool_call_spawn_model,
                originating_pending=pending,
            )
            await self._publish_node_created(runtime, node.id)
            self._launch_execute(
                runtime, node.id, consumed_pending_id=pending.id
            )
            return

        # Non-empty: drop the pending turn on the live tip. If the
        # tip is already idle, _try_consume will pop it right back
        # off and launch the child; otherwise the walk-down logic
        # will pick it up on the next transition.
        leaf = chatflow.get(leaf_id)
        leaf.pending_queue.append(pending)
        await self._publish_queue_updated(runtime, leaf_id)
        await self._try_consume(runtime, leaf_id)

    async def _try_consume(self, runtime: ChatFlowRuntime, node_id: str) -> None:
        """If ``node_id`` is the idle live tip of its chain and has a
        queued turn, pop the head and spawn a child.

        Called under the runtime lock. "Idle" means:
        - the node is succeeded (failed nodes never consume — their
          queue is either transferred by retry or discarded by delete),
        - it has no existing child (i.e. it's a true leaf).
        """
        chatflow = runtime.chatflow
        if node_id not in chatflow.nodes:
            return
        node = chatflow.get(node_id)
        if not node.pending_queue:
            return
        if node.status != NodeStatus.SUCCEEDED:
            return
        if any(node_id in other.parent_ids for other in chatflow.nodes.values()):
            return

        pending = node.pending_queue.pop(0)
        tail = list(node.pending_queue)
        node.pending_queue = []  # child takes ownership of the tail
        child = self._spawn_turn_node(
            runtime,
            parent_ids=[node_id],
            user_message_text=pending.text,
            pending_queue=tail,
            spawn_model=pending.spawn_model,
            judge_spawn_model=pending.judge_spawn_model,
            tool_call_spawn_model=pending.tool_call_spawn_model,
            originating_pending=pending,
        )
        await self._publish_node_created(runtime, child.id)
        await self._publish_queue_updated(runtime, node_id)
        await self._publish_queue_updated(runtime, child.id)
        self._launch_execute(
            runtime,
            child.id,
            consumed_pending_id=None
            if child.compact_snapshot is not None
            else pending.id,
        )

    def _spawn_turn_node(
        self,
        runtime: ChatFlowRuntime,
        *,
        parent_ids: list[str],
        user_message_text: str,
        pending_queue: list[PendingTurn],
        spawn_model: ProviderModelRef | None = None,
        judge_spawn_model: ProviderModelRef | None = None,
        tool_call_spawn_model: ProviderModelRef | None = None,
        originating_pending: PendingTurn | None = None,
    ) -> ChatFlowNode:
        """Create a PLANNED ChatFlowNode and attach it to the chatflow.

        The inner WorkFlow's shape depends on ``chatflow.default_execution_mode``:

        - ``direct``: single ``llm_call`` whose ``input_messages`` is the
          built conversation context + new user turn.
        - ``semi_auto`` / ``auto``: only ``judge_pre`` is spawned upfront.
          Option B (universal-exit-gate): the ``llm_call`` and
          ``judge_post`` are spawned dynamically by the post-node hook
          built in :meth:`_build_post_node_hook` based on judge_pre's
          verdict. This keeps the visible DAG honest — no orphan dashed
          nodes hanging around when judge_pre vetoes — and lets
          judge_post own all user-facing prose regardless of which
          path halted.

        Dual-track auto-compact: before returning the turn node the
        method consults ``chatflow.chatnode_compact_trigger_pct``. When
        the prospective context (``entry_tokens``) crosses the threshold
        the turn is NOT spawned directly. Instead a compact ChatNode is
        created over ``parent_ids[0]`` and the user's turn is forwarded
        onto the compact node's ``pending_queue`` so the regular drain
        path picks it up after compaction finishes. The returned node
        is the compact node; callers detect this via
        ``result.compact_snapshot is not None`` and skip ``consumed_pending_id``
        because the pending is not yet consumed. ``originating_pending``
        lets the caller forward an existing :class:`PendingTurn` id/metadata
        onto the compact queue so any future registered against the
        original id still resolves when the real turn eventually runs.
        """
        chatflow = runtime.chatflow
        context_wire = _build_chat_context(chatflow, parent_ids)
        context_wire.append(WireMessage(role="user", content=user_message_text))
        # Snapshot what this turn's judge_pre (or llm_call, in direct mode)
        # will see as its prompt. The card's TokenBar reads this so growth
        # is monotonic along the chain regardless of how the inner WorkFlow
        # evolves mid-run.
        entry_tokens = _estimate_tokens_from_wire(context_wire)

        # Pin this node's model. If the composer explicitly chose one
        # (``spawn_model``), honor it; otherwise inherit from the primary
        # parent's already-snapshotted ``resolved_model`` (or chatflow
        # default when bootstrapping). The result is stamped on the
        # ChatNode and propagated into its inner WorkFlow's LLM call
        # so chat-level model selection flows end-to-end.
        resolved = (
            spawn_model
            if spawn_model is not None
            else _chat_inherited_model(chatflow, parent_ids)
        )

        trigger_pct = chatflow.chatnode_compact_trigger_pct
        # Fuse: if the immediate parent is itself a compact ChatNode we
        # just came out of compaction on the drain path. A second
        # compact would summarize the prior summary — if the prior pass
        # didn't bring us under trigger the next one won't either, and
        # we'd loop forever. Let the turn proceed uncompacted instead.
        parent_is_fresh_compact = (
            bool(parent_ids)
            and parent_ids[0] in chatflow.nodes
            and chatflow.nodes[parent_ids[0]].compact_snapshot is not None
        )
        if trigger_pct is not None and parent_ids and not parent_is_fresh_compact:
            ctx = self._inner._context_window_for(resolved)
            if entry_tokens >= int(ctx * trigger_pct):
                forwarded = originating_pending or PendingTurn(
                    text=user_message_text,
                    spawn_model=spawn_model,
                    judge_spawn_model=judge_spawn_model,
                    tool_call_spawn_model=tool_call_spawn_model,
                )
                try:
                    compact_node = self._build_compact_chatnode(
                        chatflow,
                        parent_id=parent_ids[0],
                        preserve_recent_turns=chatflow.compact_preserve_recent_turns,
                        target_tokens=max(
                            256, int(ctx * chatflow.chatnode_compact_target_pct)
                        ),
                        model=chatflow.compact_model or resolved,
                        compact_instruction=None,
                    )
                except ValueError:
                    # preserve_recent_turns consumed the whole chain —
                    # nothing to summarize; fall through to the normal
                    # spawn path so the turn proceeds uncompacted.
                    compact_node = None
                if compact_node is not None:
                    compact_node.pending_queue = [forwarded, *pending_queue]
                    chatflow.add_node(compact_node)
                    return compact_node

        mode = chatflow.default_execution_mode
        switches = derive_switches_from_mode(mode)
        inner = WorkFlow(
            execution_mode=mode,
            plan_enabled=switches.plan,
            judge_pre_enabled=switches.judge_pre,
            judge_during_enabled=switches.judge_during,
            judge_post_enabled=switches.judge_post,
            # Trio starts empty. judge_pre is the first node that runs;
            # it reads the conversation and writes the distilled trio
            # back onto this WorkFlow (see ``_after_judge_pre``). The
            # planner then reads the judge_pre-authored trio; downstream
            # children get per-subtask trios the planner authors.
            description=None,
            inputs=None,
            expected_outcome=None,
            debate_round_budget=chatflow.debate_round_budget,
            judge_retry_budget=chatflow.judge_retry_budget,
            # Snapshot per-call-type overrides at spawn time so this
            # turn's judges / tool-call follow-ups have a stable pin
            # even if the chatflow defaults change mid-run. If the
            # chatflow doesn't pin a per-kind model, fall back to the
            # turn's resolved model so judges share the main turn's
            # provider/model rather than silently using the provider's
            # arbitrary default.
            # Per-turn composer override > chatflow default > main turn
            # model. Per-turn beats chatflow default so the user can
            # route a single turn's judges to a cheap model without
            # touching the chatflow-wide setting.
            judge_model_override=(
                judge_spawn_model
                or chatflow.default_judge_model
                or resolved
            ),
            tool_call_model_override=(
                tool_call_spawn_model
                or chatflow.default_tool_call_model
                or resolved
            ),
            # MemoryBoard brief pin (PR 1). brief is always on when a
            # board writer is wired; the pin is a *quality override*,
            # not an on/off switch. Fall back to ``draft_model`` when
            # the ChatFlow didn't explicitly set a brief_model so the
            # MemoryBoard always has *some* model to reach for.
            brief_model_override=chatflow.brief_model or chatflow.draft_model,
        )

        if switches.judge_pre:
            # Only the pre-judge runs upfront; the rest of the chain is
            # spawned dynamically once we know the verdict.
            self._spawn_judge_pre(inner, user_message_text, context_wire)
        else:
            inner.add_node(
                WorkFlowNode(
                    step_kind=StepKind.DRAFT,
                    parent_ids=[],
                    input_messages=list(context_wire),
                    model_override=resolved,
                )
            )

        chat_node = ChatFlowNode(
            parent_ids=list(parent_ids),
            user_message=EditableText.by_user(user_message_text),
            workflow=inner,
            pending_queue=list(pending_queue),
            resolved_model=resolved,
            entry_prompt_tokens=entry_tokens,
        )
        chatflow.add_node(chat_node)
        return chat_node

    # ------------------------------------------------------------- judge spawns
    #
    # Both helpers materialize the corresponding builtin template into a
    # standalone WorkFlow, lift its single judge_call node into the inner
    # WorkFlow, then append the real conversation context after the
    # template-rendered system+user pair so the judge sees the actual
    # exchange.
    #
    # judge_pre additionally distills the WorkFlow trio from the
    # conversation — its output carries ``extracted_{description,inputs,
    # expected_outcome}`` which ``_after_judge_pre`` writes onto the
    # parent WorkFlow before spawning the planner. judge_post reads the
    # now-populated trio via ``_trio_params``.

    def _spawn_judge_pre(
        self,
        inner: WorkFlow,
        user_message_text: str,  # noqa: ARG002 — kept for symmetry / future logging
        context_wire: list[WireMessage],
    ) -> WorkFlowNode:
        templated = instantiate_fixture(
            self._fixture_plans["judge_pre"],
            {},
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = []
        node.input_messages = [*(node.input_messages or []), *context_wire]
        node.model_override = inner.judge_model_override
        return inner.add_node(node)

    def _spawn_judge_post(
        self,
        inner: WorkFlow,
        *,
        user_message_text: str,  # noqa: ARG002 — kept for symmetry / future logging
        context_wire: list[WireMessage],
        parent_node: WorkFlowNode,
        upstream_kind: str,
        upstream_summary: str,
        worknode_catalog: str,
    ) -> WorkFlowNode:
        """Materialize the universal-exit-gate judge_post.

        ``parent_node`` is whichever node we're routing into judge_post:
        a terminal llm_call on the happy path, or judge_pre / a future
        judge_during on a halt path. ``upstream_kind`` and
        ``upstream_summary`` give the judge enough context to write the
        user-facing message in its own voice — see judge_post.yaml.
        """
        trio = _trio_params(inner)
        templated = instantiate_fixture(
            self._fixture_plans["judge_post"],
            {
                # judge_post.yaml uses ``workflow_*`` prefixes to disambiguate
                # the WorkFlow trio from any per-node trio it might also see.
                "workflow_description": trio["description"],
                "workflow_inputs": trio["inputs"],
                "workflow_expected_outcome": trio["expected_outcome"],
                "upstream_kind": upstream_kind,
                "upstream_summary": upstream_summary,
                "worknode_catalog": worknode_catalog,
                # Whitelist of node ids the engine will actually re-spawn
                # on a ``retry`` verdict (worker llm_calls +
                # sub_agent_delegations). Everything else gets dropped
                # from redo_targets, and if nothing eligible remains
                # the retry halts. Giving the judge the list up-front
                # stops it from naming judge / planner / tool_call ids.
                "redo_eligible_catalog": _format_redo_eligible(inner),
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        # PR 4.2.c: wait on every sibling NODE-brief so the exit gate
        # sees the full post-hoc trail. ``_run_judge_call`` injects a
        # ``Layer notes`` system message from those briefs at run time;
        # the old spawn-time ``shared_notes`` rendering is gone.
        sibling_briefs = [
            n.id
            for n in inner.nodes.values()
            if n.step_kind == StepKind.BRIEF and n.scope == NodeScope.NODE
        ]
        node.parent_ids = [parent_node.id, *sibling_briefs]
        node.judge_target_id = parent_node.id
        node.input_messages = [*(node.input_messages or []), *context_wire]
        node.model_override = inner.judge_model_override
        return inner.add_node(node)

    # ------------------------------------------------------------ planner spawns
    #
    # The recursive-planner pipeline (auto mode) inserts four extra
    # WorkNode kinds between judge_pre and judge_post, in this order:
    #
    #     judge_pre → planner → planner_judge → worker → worker_judge → judge_post
    #
    # Each helper here materializes one of those nodes from its YAML
    # fixture, splices it into the inner WorkFlow under the right
    # parent, and returns it. Debate (revise → fresh planner / worker)
    # and decompose (sub_agent_delegation) come in M12.4c / M12.4d;
    # this layer only handles the atomic happy path.

    def _spawn_planner(
        self,
        inner: WorkFlow,
        parent_node: WorkFlowNode,
        *,
        resolved_model: ProviderModelRef | None,
        prior_plan: str = "",
        critique: str = "",
        handoff_notes: str = "",
    ) -> WorkFlowNode:
        templated = instantiate_fixture(
            self._fixture_plans["plan"],
            {
                **_trio_params(inner),
                "prior_plan": prior_plan,
                "critique": critique,
                "handoff_notes": handoff_notes,
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = [parent_node.id]
        node.model_override = resolved_model
        return inner.add_node(node)

    def _spawn_planner_judge(
        self,
        inner: WorkFlow,
        planner_node: WorkFlowNode,
        *,
        round_index: int,
    ) -> WorkFlowNode:
        templated = instantiate_fixture(
            self._fixture_plans["plan_judge"],
            {
                **_trio_params(inner),
                "plan_json": (
                    planner_node.output_message.content
                    if planner_node.output_message
                    else ""
                ),
                "round": str(round_index),
                "round_budget": str(inner.debate_round_budget),
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = [planner_node.id]
        node.judge_target_id = planner_node.id
        node.model_override = inner.judge_model_override
        return inner.add_node(node)

    def _spawn_decompose_delegations(
        self,
        inner: WorkFlow,
        planner_judge_node: WorkFlowNode,
        plan: RecursivePlannerOutput,
    ) -> list[WorkFlowNode]:
        """Materialize one ``sub_agent_delegation`` per subtask in the
        plan, wired according to the ``after`` graph.

        Each delegation owns a fresh sub-WorkFlow seeded for AUTO mode
        — judge_pre at the root, AUTO switches, debate budget inherited,
        and the subtask's trio set as the WorkFlow trio so the
        planner/judges inside read from it via ``_trio_params``. Cross-
        layer isolation: the sub-WorkFlow does NOT carry the parent
        chat history; the trio is the entire context the children get.
        """
        if plan.subtasks is None or not plan.subtasks:
            return []

        order = _topo_order_subtasks(plan.subtasks)
        spawned: list[WorkFlowNode] = []
        # Map subtask index → spawned delegation id for parent wiring.
        index_to_node_id: dict[int, NodeId] = {}

        for idx in order:
            sub = plan.subtasks[idx]
            if sub.after:
                parent_ids = [index_to_node_id[a] for a in sub.after]
            else:
                # Root subtasks share the planner_judge as parent so the
                # decompose group is rooted under the planner_judge that
                # produced it (used by ``_decompose_group_planner_judge``).
                parent_ids = [planner_judge_node.id]

            sub_workflow = self._build_sub_workflow_for_subtask(inner, sub)
            delegation = WorkFlowNode(
                step_kind=StepKind.DELEGATE,
                parent_ids=parent_ids,
                sub_workflow=sub_workflow,
                description=EditableText.by_agent(sub.description),
                inputs=EditableText.by_agent(sub.inputs) if sub.inputs else None,
                expected_outcome=EditableText.by_agent(sub.expected_outcome),
            )
            inner.add_node(delegation)
            index_to_node_id[idx] = delegation.id
            spawned.append(delegation)

        return spawned

    def _build_sub_workflow_for_subtask(
        self, parent: WorkFlow, subtask: SubTask
    ) -> WorkFlow:
        """Construct the inner WorkFlow a delegation node will execute.

        AUTO mode (so the planner pipeline can recurse). Trio comes
        verbatim from the subtask. Debate budget inherited from the
        parent WorkFlow. judge_pre seeded at the root with no chat
        context — the trio is all the sub-WorkFlow gets, deliberately.
        """
        switches = derive_switches_from_mode(ExecutionMode.AUTO_PLAN)
        sub = WorkFlow(
            execution_mode=ExecutionMode.AUTO_PLAN,
            plan_enabled=switches.plan,
            judge_pre_enabled=switches.judge_pre,
            judge_during_enabled=switches.judge_during,
            judge_post_enabled=switches.judge_post,
            description=EditableText.by_agent(subtask.description),
            inputs=EditableText.by_agent(subtask.inputs),
            expected_outcome=EditableText.by_agent(subtask.expected_outcome),
            debate_round_budget=parent.debate_round_budget,
            judge_retry_budget=parent.judge_retry_budget,
            judge_model_override=parent.judge_model_override,
            tool_call_model_override=parent.tool_call_model_override,
            # Inherit the MemoryBoard brief pin so nested sub-WorkFlows
            # honor the enclosing ChatFlow's brief_model.
            brief_model_override=parent.brief_model_override,
        )
        # judge_pre is the universal entry gate; the post-node hook then
        # spawns the planner / worker chain when it votes OK.
        self._spawn_judge_pre(sub, subtask.description, [])
        return sub

    def _spawn_worker(
        self,
        inner: WorkFlow,
        parent_node: WorkFlowNode,
        atomic: AtomicBrief,
        *,
        resolved_model: ProviderModelRef | None,
        prior_output: str = "",
        critique: str = "",
        redo_source_id: NodeId | None = None,
    ) -> WorkFlowNode:
        """Spawn the worker WorkNode for an ``atomic`` planner brief.

        The brief's trio (description / inputs / expected_outcome) is
        the worker's *task* — distinct from the WorkFlow-level trio,
        which the planner consumed. We pass the brief verbatim into
        the worker template's params; the WorkFlow-level trio stays
        unchanged for the eventual judge_post.
        """
        templated = instantiate_fixture(
            self._fixture_plans["worker"],
            {
                "description": atomic.description,
                "inputs": atomic.inputs,
                "expected_outcome": atomic.expected_outcome,
                "prior_output": prior_output,
                "critique": critique,
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = [parent_node.id]
        node.model_override = resolved_model
        node.redo_source_id = redo_source_id
        return inner.add_node(node)

    def _spawn_worker_judge(
        self,
        inner: WorkFlow,
        worker_node: WorkFlowNode,
        *,
        round_index: int,
    ) -> WorkFlowNode:
        # The worker_judge needs the worker's brief — which the planner
        # produced and the worker consumed — not the WorkFlow trio.
        # We reconstruct it from the worker's own ``description``
        # (set by the worker template from the atomic brief), keeping
        # the chain self-contained.
        worker_brief_desc = (
            worker_node.description.text if worker_node.description else ""
        )
        templated = instantiate_fixture(
            self._fixture_plans["worker_judge"],
            {
                "description": worker_brief_desc,
                # We don't separately track the brief's inputs /
                # expected_outcome on the worker node — the planner's
                # full brief is in the worker's input_messages, which
                # the judge sees via ``worker_output``. Pass empty
                # placeholders for the param dict; the system prompt
                # explicitly cues the judge to read from worker_output.
                "inputs": "",
                "expected_outcome": "",
                "worker_output": (
                    worker_node.output_message.content
                    if worker_node.output_message
                    else ""
                ),
                "round": str(round_index),
                "round_budget": str(inner.debate_round_budget),
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = [worker_node.id]
        node.judge_target_id = worker_node.id
        node.model_override = inner.judge_model_override
        return inner.add_node(node)

    def _build_post_node_hook(
        self,
        chat_node: ChatFlowNode,
        chatflow: ChatFlow,
    ) -> "PostNodeHook":
        """Closure that grows the inner DAG dynamically.

        Fired by ``WorkflowEngine`` after every node success — including
        from inside ``_run_sub_agent_delegation`` for nested
        sub-WorkFlows, so the same orchestration applies at every
        recursion level. Per-call values that differ between the top
        chat WorkFlow and a nested sub-WorkFlow (``user_message_text``,
        ``context_wire``) are derived from the workflow itself when
        we're not in the chat's own WorkFlow, honoring the cross-layer
        isolation rule (sub-WorkFlows don't inherit chat context).
        """
        chat_user_message_text = (
            chat_node.user_message.text if chat_node.user_message else ""
        )
        chat_context_wire = _build_chat_context(chatflow, list(chat_node.parent_ids))
        chat_context_wire.append(
            WireMessage(role="user", content=chat_user_message_text)
        )
        top_workflow_id = chat_node.workflow.id

        def _context_for(workflow: WorkFlow) -> tuple[str, list[WireMessage]]:
            # Top WorkFlow: chat-derived. Nested sub-WorkFlow: derive
            # from the WorkFlow's own trio (the planner already wrote
            # the subtask brief in there). Empty context_wire — the
            # judges/planner templates render the trio explicitly.
            if workflow.id == top_workflow_id:
                return chat_user_message_text, chat_context_wire
            ut = workflow.description.text if workflow.description else ""
            return ut, []

        def hook(workflow: WorkFlow, node: WorkFlowNode) -> None:
            user_message_text, context_wire = _context_for(workflow)
            switches = derive_switches_from_mode(workflow.execution_mode)

            # FAILED nodes only get special handling for judge_call
            # crashes (auto-retry, applies to every variant). Every
            # other handler reads judge_verdict / output_message and
            # would no-op or do the wrong thing on a FAILED node, so
            # short-circuit early.
            if node.status == NodeStatus.FAILED:
                if node.step_kind == StepKind.JUDGE_CALL:
                    self._after_judge_failed(workflow, node)
                return

            if node.step_kind == StepKind.JUDGE_CALL:
                if node.judge_variant == JudgeVariant.PRE:
                    self._after_judge_pre(
                        workflow,
                        node,
                        user_message_text=user_message_text,
                        context_wire=context_wire,
                        chatflow=chatflow,
                        switches=switches,
                        resolved_model=chat_node.resolved_model,
                    )
                    return
                if node.judge_variant == JudgeVariant.DURING:
                    if node.role == WorkNodeRole.PLAN_JUDGE:
                        self._after_planner_judge(
                            workflow,
                            node,
                            user_message_text=user_message_text,
                            context_wire=context_wire,
                            resolved_model=chat_node.resolved_model,
                        )
                    elif node.role == WorkNodeRole.WORKER_JUDGE:
                        self._after_worker_judge(
                            workflow,
                            node,
                            user_message_text=user_message_text,
                            context_wire=context_wire,
                            resolved_model=chat_node.resolved_model,
                        )
                if node.judge_variant == JudgeVariant.POST:
                    self._after_judge_post(
                        workflow,
                        node,
                        user_message_text=user_message_text,
                        context_wire=context_wire,
                        resolved_model=chat_node.resolved_model,
                    )
                return

            if node.step_kind == StepKind.DELEGATE:
                self._after_delegation(
                    workflow,
                    node,
                    user_message_text=user_message_text,
                    context_wire=context_wire,
                )
                return

            if node.step_kind == StepKind.DRAFT:
                if node.role == WorkNodeRole.PLAN:
                    self._after_planner(workflow, node)
                    return
                if node.role == WorkNodeRole.WORKER:
                    # Workers can do tools too — wait for the terminal
                    # llm_call before handing off to the worker_judge.
                    if node.output_message and node.output_message.tool_uses:
                        return
                    self._after_worker(
                        workflow,
                        node,
                        user_message_text=user_message_text,
                        context_wire=context_wire,
                    )
                    return
                if switches.judge_post:
                    # Direct/semi_auto: ordinary terminal llm_call routes
                    # straight to judge_post (no recursive planner chain).
                    if node.output_message and node.output_message.tool_uses:
                        return
                    self._spawn_judge_post(
                        workflow,
                        user_message_text=user_message_text,
                        context_wire=context_wire,
                        parent_node=node,
                        upstream_kind="completed",
                        upstream_summary=(
                            node.output_message.content if node.output_message else ""
                        ),
                        worknode_catalog=(
                            f"{node.id}: terminal llm_call producing the agent's reply"
                        ),
                    )

        return hook

    def _after_judge_pre(
        self,
        workflow: WorkFlow,
        judge_pre_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
        chatflow: ChatFlow,
        switches: ExecutionSwitches,
        resolved_model: ProviderModelRef | None,
    ) -> None:
        """Branch on judge_pre's verdict: happy path spawns an llm_call,
        halt path goes straight to judge_post in halt-summary mode.

        Before branching we also copy judge_pre's extracted trio onto
        the parent WorkFlow so the planner (and every downstream
        template that reads the trio via ``_trio_params``) sees the
        judge_pre-distilled values rather than the ``None`` they were
        seeded with in ``_spawn_turn_node``.
        """
        verdict = judge_pre_node.judge_verdict
        if verdict is not None:
            _apply_judge_pre_trio(workflow, verdict)
        halt = verdict is None or _judge_pre_should_halt(verdict)

        if halt:
            if not switches.judge_post:
                # Auto/semi_auto without judge_post is unusual but
                # possible if the user toggled it off — fall back to the
                # legacy formatter so the user still gets a clarifying
                # prompt. Preserves behavior even on weird mode mixes.
                from agentloom.engine.judge_formatter import format_judge_pre_prompt
                if verdict is not None:
                    workflow.pending_user_prompt = format_judge_pre_prompt(verdict)
                return
            self._spawn_judge_post(
                workflow,
                user_message_text=user_message_text,
                context_wire=context_wire,
                parent_node=judge_pre_node,
                upstream_kind="judge_pre_halt",
                upstream_summary=_render_judge_pre_halt(verdict)
                if verdict is not None
                else "(judge_pre returned no parseable verdict)",
                worknode_catalog=(
                    f"{judge_pre_node.id}: judge_pre that vetoed the run"
                ),
            )
            return

        # Happy path: judge_pre cleared the run.
        if workflow.execution_mode == ExecutionMode.AUTO_PLAN:
            # Auto mode runs the recursive-planner pipeline:
            #   judge_pre → planner → planner_judge → worker
            #             → worker_judge → judge_post
            # The hook handlers below grow the chain step by step.
            handoff_notes = (
                _render_judge_pre_risky_assumptions(verdict)
                if verdict is not None
                else ""
            )
            self._spawn_planner(
                workflow,
                judge_pre_node,
                resolved_model=resolved_model,
                handoff_notes=handoff_notes,
            )
            return

        # Semi_auto / direct: spawn a plain llm_call; the post-node hook
        # attaches judge_post once it completes (and any tool loop has
        # terminated).
        workflow.add_node(
            WorkFlowNode(
                step_kind=StepKind.DRAFT,
                parent_ids=[judge_pre_node.id],
                input_messages=list(context_wire),
                model_override=resolved_model,
            )
        )

    # --------------------------------------------------- recursive planner hooks
    #
    # These handlers grow the auto-mode chain one node at a time. Each
    # is called from the post-node hook above when its predecessor
    # node finishes successfully. M12.4b only handles the atomic happy
    # path — non-continue verdicts and decompose plans short-circuit
    # to judge_post halt; M12.4c / M12.4d will replace those bail-outs
    # with debate-as-chain and sub_agent_delegation respectively.

    def _after_planner(
        self,
        workflow: WorkFlow,
        planner_node: WorkFlowNode,
    ) -> None:
        """Planner just produced its plan JSON. Attach planner_judge.

        We don't parse the JSON here — the judge sees the raw plan and
        decides; only when planner_judge votes ``continue`` and the
        plan is ``atomic`` do we materialize a worker. That parse lives
        in :meth:`_after_planner_judge`.
        """
        self._spawn_planner_judge(
            workflow, planner_node, round_index=_round_index_for(workflow, planner_node)
        )

    def _after_planner_judge(
        self,
        workflow: WorkFlow,
        planner_judge_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
        resolved_model: ProviderModelRef | None,
    ) -> None:
        """Decide what to do with the planner's plan."""
        verdict = planner_judge_node.judge_verdict
        decision = verdict.during_verdict if verdict is not None else None

        # Locate the planner this judge reviewed and re-parse its output.
        planner_node = workflow.nodes.get(planner_judge_node.judge_target_id or "")
        if planner_node is None or planner_node.output_message is None:
            self._halt_to_post_judge(
                workflow,
                parent_node=planner_judge_node,
                upstream_kind="planner_judge_halt",
                upstream_summary=(
                    "planner output unavailable for review"
                ),
                user_message_text=user_message_text,
                context_wire=context_wire,
            )
            return

        try:
            plan = parse_recursive_planner_output(planner_node.output_message.content)
        except PlannerParseError as exc:
            planner_count = sum(
                1 for n in workflow.nodes.values()
                if n.role == WorkNodeRole.PLAN
            )
            if planner_count < 2:
                self._spawn_planner(
                    workflow,
                    planner_judge_node,
                    resolved_model=resolved_model,
                    prior_plan=planner_node.output_message.content,
                    critique=(
                        f"Your previous plan output failed JSON parse: {exc}. "
                        "Reply with ONLY valid JSON matching the required "
                        "schema — all string values must be properly quoted."
                    ),
                )
                return
            self._halt_to_post_judge(
                workflow,
                parent_node=planner_judge_node,
                upstream_kind="planner_parse_error",
                upstream_summary=f"planner output failed to parse after retry: {exc}",
                user_message_text=user_message_text,
                context_wire=context_wire,
            )
            return

        # Continue + atomic: materialize the worker.
        if decision == "continue" and plan.mode == "atomic" and plan.atomic is not None:
            # Hard-block phantom tool names. If the planner picked
            # ``step_kind: tool_call`` with a ``tool_name`` that isn't
            # registered, spawning the worker is a guaranteed burn: the
            # worker will propagate the hallucinated name into its own
            # tool_use output, the registry will reject it, and we
            # burn a round to learn something we already know. Bounce
            # back into revise with a concrete list of real tools so
            # the planner can self-correct. At budget, halt to the
            # exit judge so the user sees a useful error.
            if (
                plan.atomic.step_kind == StepKind.TOOL_CALL
                and plan.atomic.tool_name
                and self._tools is not None
                and not self._tools.has(plan.atomic.tool_name)
            ):
                known = sorted(t.name for t in self._tools.all())
                known_repr = ", ".join(known) if known else "(none)"
                critique = (
                    f"Your atomic brief referenced tool_name="
                    f"{plan.atomic.tool_name!r}, which is not a "
                    f"registered tool. Available tools: {known_repr}. "
                    "Either pick an actual tool name (or leave "
                    "tool_name null so the worker chooses at run time), "
                    'or change step_kind to "draft" if the task '
                    "does not require a specific tool."
                )
                if (
                    _round_index_for(workflow, planner_node)
                    < workflow.debate_round_budget
                ):
                    self._spawn_planner(
                        workflow,
                        planner_judge_node,
                        resolved_model=resolved_model,
                        prior_plan=planner_node.output_message.content,
                        critique=critique,
                    )
                    return
                self._halt_to_post_judge(
                    workflow,
                    parent_node=planner_judge_node,
                    upstream_kind="planner_judge_halt",
                    upstream_summary=critique,
                    user_message_text=user_message_text,
                    context_wire=context_wire,
                )
                return
            self._spawn_worker(
                workflow,
                planner_judge_node,
                plan.atomic,
                resolved_model=resolved_model,
            )
            return

        # Continue + decompose: spawn one sub_agent_delegation per
        # subtask, wired by the ``after`` graph. Each runs its own
        # AUTO-mode sub-WorkFlow recursively (no depth cap by design).
        # Aggregation happens in ``_after_delegation`` once all siblings
        # in the decompose group complete.
        if (
            decision == "continue"
            and plan.mode == "decompose"
            and plan.subtasks is not None
        ):
            self._spawn_decompose_delegations(workflow, planner_judge_node, plan)
            return

        # Revise within debate budget: spawn a fresh planner sibling
        # under the planner_judge, threading the previous plan and the
        # judge's critiques. The post-node hook will then attach a new
        # planner_judge to that planner — same chain shape, deeper.
        if (
            decision == "revise"
            and verdict is not None
            and _round_index_for(workflow, planner_node) < workflow.debate_round_budget
        ):
            self._spawn_planner(
                workflow,
                planner_judge_node,
                resolved_model=resolved_model,
                prior_plan=planner_node.output_message.content,
                critique=_render_critiques(verdict),
            )
            return

        # Infeasible, halt, revise-at-budget, or unparseable → judge_post
        # halt. On revise-at-budget we surface the accumulated critiques
        # from every debate round so judge_post's user_message can cite
        # what the critic kept flagging (M12.4e).
        upstream_summary = _summarize_planner_outcome(decision, plan)
        if decision == "revise" and verdict is not None:
            concerns = _collect_debate_concerns(workflow, planner_judge_node)
            if concerns:
                upstream_summary = f"{upstream_summary}\n\n{concerns}"
        self._halt_to_post_judge(
            workflow,
            parent_node=planner_judge_node,
            upstream_kind="planner_judge_halt",
            upstream_summary=upstream_summary,
            user_message_text=user_message_text,
            context_wire=context_wire,
        )

    def _after_worker(
        self,
        workflow: WorkFlow,
        worker_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
    ) -> None:
        """Worker just produced its draft. Normal workers hand off to a
        worker_judge for debate; redo clones (M12.4d6 — workers spawned
        directly under a judge_post by a retry verdict) skip debate and
        trigger re-aggregation when the whole redo group has completed.
        """
        redo_owner = _redo_group_judge_post(workflow, worker_node)
        if redo_owner is not None:
            self._try_reaggregate_redo_group(
                workflow,
                redo_owner,
                user_message_text=user_message_text,
                context_wire=context_wire,
            )
            return
        self._spawn_worker_judge(
            workflow, worker_node, round_index=_round_index_for(workflow, worker_node)
        )

    def _after_worker_judge(
        self,
        workflow: WorkFlow,
        worker_judge_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
        resolved_model: ProviderModelRef | None,
    ) -> None:
        """On continue, pass the worker's output to judge_post; on revise
        within budget, spawn a fresh worker; otherwise halt."""
        verdict = worker_judge_node.judge_verdict
        decision = verdict.during_verdict if verdict is not None else None
        worker_node = workflow.nodes.get(worker_judge_node.judge_target_id or "")
        worker_output = (
            worker_node.output_message.content
            if worker_node is not None and worker_node.output_message is not None
            else ""
        )

        if decision == "continue" and worker_node is not None:
            self._spawn_judge_post(
                workflow,
                user_message_text=user_message_text,
                context_wire=context_wire,
                parent_node=worker_judge_node,
                upstream_kind="completed",
                upstream_summary=worker_output,
                worknode_catalog=(
                    f"{worker_node.id}: worker draft accepted by worker_judge"
                ),
            )
            return

        # Revise within debate budget: re-derive the planner's atomic
        # brief and spawn a fresh worker under the worker_judge with the
        # prior draft + critique threaded in. The post-node hook then
        # attaches a new worker_judge — same chain shape, deeper.
        if (
            decision == "revise"
            and verdict is not None
            and worker_node is not None
            and _round_index_for(workflow, worker_node) < workflow.debate_round_budget
        ):
            atomic = _atomic_brief_for_worker(workflow, worker_node)
            if atomic is not None:
                self._spawn_worker(
                    workflow,
                    worker_judge_node,
                    atomic,
                    resolved_model=resolved_model,
                    prior_output=worker_output,
                    critique=_render_critiques(verdict),
                )
                return

        # halt / revise-at-budget / unparseable / brief-recovery-failed
        # → judge_post halt. On revise-at-budget we thread accumulated
        # critiques from every debate round (M12.4e).
        upstream_summary = (
            f"worker_judge verdict={decision or 'unparseable'}; "
            f"worker draft: {worker_output}"
        )
        if decision == "revise" and verdict is not None:
            concerns = _collect_debate_concerns(workflow, worker_judge_node)
            if concerns:
                upstream_summary = f"{upstream_summary}\n\n{concerns}"
        self._halt_to_post_judge(
            workflow,
            parent_node=worker_judge_node,
            upstream_kind="worker_judge_halt",
            upstream_summary=upstream_summary,
            user_message_text=user_message_text,
            context_wire=context_wire,
        )

    def _after_delegation(
        self,
        workflow: WorkFlow,
        delegation_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
    ) -> None:
        """One delegation just succeeded. If all siblings in this
        decompose group are done, attach judge_post as the layer's
        aggregator. Otherwise wait — the engine will invoke this hook
        again as siblings complete.

        The aggregating judge_post sees each delegation's effective
        output as ``upstream_summary`` so it can both (a) judge whether
        this layer's task is complete and (b) emit a ``merged_response``
        that becomes the layer's effective output. The judge_post
        template change for that merged-response shape lands in M12.4d5;
        for now we pass the concatenated outputs as the legacy summary
        so the existing template can still produce something coherent.

        Redo-clone delegations (M12.4d6 — a delegation spawned directly
        under a judge_post by a retry verdict) are handled by the
        re-aggregation path instead of normal decompose aggregation.
        """
        self._inject_upstream_outputs_into_ready_children(
            workflow, delegation_node
        )

        redo_owner = _redo_group_judge_post(workflow, delegation_node)
        if redo_owner is not None:
            self._try_reaggregate_redo_group(
                workflow,
                redo_owner,
                user_message_text=user_message_text,
                context_wire=context_wire,
            )
            return

        owner = _decompose_group_planner_judge(workflow, delegation_node)
        if owner is None:
            return
        members = _decompose_group_members(workflow, owner.id)
        if not all(m.status == NodeStatus.SUCCEEDED for m in members):
            return
        # Guard against duplicate spawning when multiple delegations
        # finish in rapid succession.
        if _decompose_group_already_aggregated(workflow, owner.id):
            return

        upstream_summary = _format_decompose_aggregation(workflow, members)
        worknode_catalog = "\n".join(
            f"{m.id}: sub_agent_delegation for "
            f"{(m.description.text if m.description else '').strip() or '(no description)'}"
            for m in members
        )
        aggregator = self._spawn_judge_post(
            workflow,
            user_message_text=user_message_text,
            context_wire=context_wire,
            parent_node=delegation_node,
            upstream_kind="decompose_aggregation",
            upstream_summary=upstream_summary,
            worknode_catalog=worknode_catalog,
        )
        # The aggregating judge_post reads every member's output via
        # _format_decompose_aggregation, so it depends on all of them
        # — not just the last-finishing sibling that triggered this
        # hook. Overwrite parent_ids so the DAG edge set matches that
        # real dependency and the UI draws one edge per member.
        aggregator.parent_ids = [m.id for m in members]

    def _inject_upstream_outputs_into_ready_children(
        self, workflow: WorkFlow, finished_parent: WorkFlowNode
    ) -> None:
        """Pass upstream delegation outputs into their dependent children.

        Background: sub-WorkFlows are built eagerly at spawn time, before
        upstream siblings have run — so a dependent subtask's judge_pre
        only sees the planner's fabricated ``inputs`` placeholder and no
        upstream context. When every SUB_AGENT_DELEGATION parent of a
        child has succeeded, rewrite the child's ``sub.inputs`` with the
        parents' effective outputs and re-template the existing judge_pre
        node's ``input_messages`` so it reads the fresh trio.
        """
        for child in workflow.nodes.values():
            if child.step_kind != StepKind.DELEGATE:
                continue
            if finished_parent.id not in child.parent_ids:
                continue
            if child.status != NodeStatus.PLANNED:
                continue
            sub = child.sub_workflow
            if sub is None:
                continue
            delegation_parents = [
                workflow.nodes[pid]
                for pid in child.parent_ids
                if workflow.nodes.get(pid) is not None
                and workflow.nodes[pid].step_kind == StepKind.DELEGATE
            ]
            if not delegation_parents:
                continue
            if not all(
                p.status == NodeStatus.SUCCEEDED for p in delegation_parents
            ):
                continue
            if any(n.status != NodeStatus.PLANNED for n in sub.nodes.values()):
                # Sub already started — too late, leave alone.
                continue
            self._inject_upstream_outputs(sub, delegation_parents)

    def _inject_upstream_outputs(
        self, sub: WorkFlow, parents: list[WorkFlowNode]
    ) -> None:
        blocks: list[str] = []
        for p in parents:
            label = (
                (p.description.text if p.description else "").strip()
                or "(no description)"
            )
            out = (
                _sub_workflow_effective_output(p.sub_workflow)
                if p.sub_workflow is not None
                else ""
            )
            blocks.append(f"## Upstream: {label}\n{out}")
        injected = "\n\n".join(blocks)

        original = sub.inputs.text if sub.inputs else ""
        new_text = (
            f"{original}\n\n---\nUpstream dependency outputs:\n\n{injected}"
            if original
            else f"Upstream dependency outputs:\n\n{injected}"
        )
        sub.inputs = EditableText.by_agent(new_text)

        judge_pre = next(
            (
                n
                for n in sub.nodes.values()
                if n.step_kind == StepKind.JUDGE_CALL
                and n.judge_variant == JudgeVariant.PRE
                and n.status == NodeStatus.PLANNED
            ),
            None,
        )
        if judge_pre is None:
            return
        templated = instantiate_fixture(
            self._fixture_plans["judge_pre"],
            _trio_params(sub),
            includes=self._fixture_includes,
        )
        fresh = _single_node(templated)
        judge_pre.input_messages = fresh.input_messages

    def _after_judge_post(
        self,
        workflow: WorkFlow,
        judge_post_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
        resolved_model: ProviderModelRef | None,
    ) -> None:
        """judge_post finished. Decide what happens next:

        - ``accept``: nothing — the WorkFlow is done, agent_response is
          derived by the ChatFlow layer from the verdict (merged_response)
          or the terminal llm_call.
        - ``retry`` + ``redo_targets``: re-spawn each target as a fresh
          sibling under this judge_post, threading the target-specific
          critique as context. When all redo clones complete, the hook
          will re-aggregate via a new judge_post (option a, M12.4d6).
        - ``fail``, ``retry`` without redo_targets, or retry-round budget
          exhausted: halt with ``pending_user_prompt``.

        Retry-round budget reuses ``debate_round_budget`` (§3.4.5). The
        round count is the number of judge_post ancestors in this
        judge_post's chain, so each judge_post→redo→judge_post cycle
        advances the counter by one. When the count reaches the budget
        the hook halts instead of re-spawning — that's the fuse.

        Judges whose targets are redo clones don't get judged again
        themselves; the re-aggregation judge_post is the single arbiter.
        """
        verdict = judge_post_node.judge_verdict
        if verdict is None:
            return

        # accept → nothing to do; agent_response derivation happens in
        # _execute_node via _judge_post_response_text / _terminal_llm_call.
        if verdict.post_verdict == "accept":
            return

        # redo_targets only matter on a retry verdict. fail / retry-
        # without-targets fall through to the halt set in workflow_engine.
        if verdict.post_verdict != "retry" or not verdict.redo_targets:
            # workflow_engine._run_judge_call already set pending_user_prompt
            # in this case; nothing to do here.
            return

        # Halt fuse: count completed judge_post rounds in this chain
        # (including the one that just finished). Once we're at the
        # budget, stop re-spawning and let the user decide. A budget of
        # ``-1`` disables the fuse entirely.
        round_index = _judge_post_round_index(workflow, judge_post_node)
        budget = workflow.judge_retry_budget
        if budget >= 0 and round_index >= budget:
            workflow.pending_user_prompt = _compose_retry_halt_message(
                workflow,
                judge_post_node,
                verdict,
                reason="budget_exhausted",
                round_index=round_index,
                budget=budget,
            )
            return

        # Spawn redo clones for each target the judge named. Unknown or
        # unsupported targets are skipped — the re-aggregation judge_post
        # will see whatever actually got re-run.
        spawned = self._spawn_redo_clones(
            workflow,
            judge_post_node,
            verdict,
            resolved_model=resolved_model,
        )
        if not spawned:
            # Judge asked for retry but every target was unusable (missing
            # or unsupported kind). Treat as a halt so the user still
            # gets a message rather than a silent accept.
            workflow.pending_user_prompt = _compose_retry_halt_message(
                workflow,
                judge_post_node,
                verdict,
                reason="no_usable_targets",
                round_index=round_index,
                budget=budget,
            )

    def _spawn_redo_clones(
        self,
        workflow: WorkFlow,
        judge_post_node: WorkFlowNode,
        verdict: JudgeVerdict,
        *,
        resolved_model: ProviderModelRef | None,
    ) -> list[WorkFlowNode]:
        """Materialize a fresh clone for each ``redo_target`` the judge
        cited, parented on ``judge_post_node`` so the re-aggregation
        walk (``_redo_group_*`` helpers) can find them.

        Supported kinds: worker (LLM_CALL with role=WORKER) and
        sub_agent_delegation. Other kinds are skipped — judges shouldn't
        ask us to redo judges, and redoing tool_calls in isolation
        would divorce them from their parent llm_call.
        """
        clones: list[WorkFlowNode] = []
        for target in verdict.redo_targets:
            original = workflow.nodes.get(target.node_id)
            if original is None:
                continue
            if (
                original.step_kind == StepKind.DRAFT
                and original.role == WorkNodeRole.WORKER
            ):
                atomic = _atomic_brief_for_worker(workflow, original)
                if atomic is None:
                    continue
                prior_output = (
                    original.output_message.content
                    if original.output_message
                    else ""
                )
                clone = self._spawn_worker(
                    workflow,
                    judge_post_node,
                    atomic,
                    resolved_model=resolved_model,
                    prior_output=prior_output,
                    critique=target.critique,
                    redo_source_id=original.id,
                )
                clones.append(clone)
            elif original.step_kind == StepKind.DELEGATE:
                # Reconstruct a SubTask from the delegation's trio and
                # append the judge's critique to its description so the
                # fresh sub-WorkFlow's planner sees what to fix.
                desc = original.description.text if original.description else ""
                ins = original.inputs.text if original.inputs else ""
                exp = (
                    original.expected_outcome.text
                    if original.expected_outcome
                    else ""
                )
                critique_suffix = (
                    f"\n\n[critique from prior attempt]\n{target.critique}"
                    if target.critique
                    else ""
                )
                subtask = SubTask(
                    description=desc + critique_suffix,
                    inputs=ins,
                    expected_outcome=exp,
                    after=[],
                )
                sub_workflow = self._build_sub_workflow_for_subtask(
                    workflow, subtask
                )
                clone = WorkFlowNode(
                    step_kind=StepKind.DELEGATE,
                    parent_ids=[judge_post_node.id],
                    sub_workflow=sub_workflow,
                    description=EditableText.by_agent(desc + critique_suffix),
                    inputs=EditableText.by_agent(ins) if ins else None,
                    expected_outcome=EditableText.by_agent(exp),
                    redo_source_id=original.id,
                )
                workflow.add_node(clone)
                clones.append(clone)
            # Other kinds: silently skip. The re-aggregation judge_post
            # will see the partial retry and decide.
        return clones

    def _try_reaggregate_redo_group(
        self,
        workflow: WorkFlow,
        judge_post_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
    ) -> None:
        """One redo clone (worker or delegation) just succeeded. If
        every sibling in this redo group is done, spawn a new judge_post
        to re-evaluate — its parent is the most recently completed
        clone so the DAG shows the retry cycle cleanly. Otherwise wait
        for the remaining siblings to finish.

        The upstream_summary walks the retry lineage back to the round-1
        judge_post (the original decompose_aggregation, or the original
        atomic judge_post) and emits one block per round-1 subtask
        showing its *latest surviving version* — so siblings that
        succeeded in earlier rounds remain in the picture even when the
        current round only redid a subset.
        """
        members = _redo_group_members(workflow, judge_post_node.id)
        if not members or not all(
            m.status == NodeStatus.SUCCEEDED for m in members
        ):
            return
        if _redo_group_already_reaggregated(workflow, judge_post_node.id):
            return

        summary_parts, catalog_parts = _format_redo_aggregation(
            workflow, judge_post_node
        )

        # Parent on the most recently created clone for judge_target_id
        # continuity, then overwrite parent_ids to include every clone
        # so the DAG edge set reflects the real dependency.
        last_clone = max(members, key=lambda n: n.created_at)
        aggregator = self._spawn_judge_post(
            workflow,
            user_message_text=user_message_text,
            context_wire=context_wire,
            parent_node=last_clone,
            upstream_kind="redo_aggregation",
            upstream_summary="\n\n".join(summary_parts),
            worknode_catalog="\n".join(catalog_parts),
        )
        aggregator.parent_ids = [m.id for m in members]

    def _after_judge_failed(
        self,
        workflow: WorkFlow,
        failed_judge: WorkFlowNode,
    ) -> None:
        """A judge_call (any variant) raised an exception. Re-spawn a
        fresh sibling under the same parent so the engine picks it up
        next pass. After ``_JUDGE_CRASH_RETRY_BUDGET`` crashes for the
        same parent + variant we stop respawning; the outer
        ``_execute_node`` then sees an unrecoverable judge crash and
        marks the ChatNode FAILED, surfacing the crash to the user
        instead of silently falling through to the planner's raw plan
        JSON via ``_terminal_llm_call``.

        ``parent_ids`` is empty for pre_judge (workflow-root entry) and
        for sub-WorkFlow seed judges; group those under a sentinel key
        so the crash budget still applies.
        """
        parent_key = failed_judge.parent_ids[0] if failed_judge.parent_ids else ""
        crashes = sum(
            1
            for n in workflow.nodes.values()
            if n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == failed_judge.judge_variant
            and n.status == NodeStatus.FAILED
            and (n.parent_ids[0] if n.parent_ids else "") == parent_key
        )
        if crashes > _JUDGE_CRASH_RETRY_BUDGET:
            return
        clone = WorkFlowNode(
            step_kind=StepKind.JUDGE_CALL,
            judge_variant=failed_judge.judge_variant,
            role=failed_judge.role,
            parent_ids=list(failed_judge.parent_ids),
            judge_target_id=failed_judge.judge_target_id,
            input_messages=list(failed_judge.input_messages or []),
            model_override=failed_judge.model_override,
            description=failed_judge.description,
            inputs=failed_judge.inputs,
            expected_outcome=failed_judge.expected_outcome,
        )
        workflow.add_node(clone)

    def _halt_to_post_judge(
        self,
        workflow: WorkFlow,
        *,
        parent_node: WorkFlowNode,
        upstream_kind: str,
        upstream_summary: str,
        user_message_text: str,
        context_wire: list[WireMessage],
    ) -> None:
        """Bail out of the recursive-planner chain by routing to
        judge_post in halt-summary mode. Used for verdicts and plan
        shapes M12.4b doesn't yet handle (revise, decompose, halt,
        unparseable). M12.4c will replace the revise / debate branches
        with debate-as-chain.
        """
        self._spawn_judge_post(
            workflow,
            user_message_text=user_message_text,
            context_wire=context_wire,
            parent_node=parent_node,
            upstream_kind=upstream_kind,
            upstream_summary=upstream_summary,
            worknode_catalog=(
                f"{parent_node.id}: planner-pipeline halt at {upstream_kind}"
            ),
        )

    def _launch_execute(
        self,
        runtime: ChatFlowRuntime,
        node_id: str,
        *,
        consumed_pending_id: str | None,
    ) -> None:
        """Spawn a background task to run ``node_id``'s inner workflow."""
        task = asyncio.create_task(
            self._execute_node(runtime, node_id, consumed_pending_id)
        )
        runtime.track(task, node_id=node_id)

    async def _execute_node(
        self,
        runtime: ChatFlowRuntime,
        node_id: str,
        consumed_pending_id: str | None,
    ) -> None:
        """Run the inner workflow for ``node_id`` and apply the result.

        Three phases:

        1. Under the runtime lock: mark RUNNING.
        2. Outside the lock: ``WorkflowEngine.execute`` drives the
           inner DAG. The LLM call inside is where the rate limiter
           does its work — holding the lock here would serialize all
           turns on this chatflow, which we explicitly don't want.
        3. Under the lock: freeze agent_response, mark terminal,
           resolve the waiting future, and either cascade queue
           consumption (on success) or discard queued items (on
           failure).
        """
        chatflow = runtime.chatflow

        async with runtime.lock:
            chat_node = chatflow.get(node_id)
            chat_node.status = NodeStatus.RUNNING
            chat_node.started_at = utcnow()

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow.id,
                kind="chat.node.status",
                node_id=node_id,
                data={"status": NodeStatus.RUNNING.value},
            )
        )
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow.id,
                kind="chat.turn.started",
                node_id=node_id,
                data={
                    "user_message": chat_node.user_message.text
                    if chat_node.user_message
                    else None
                },
            )
        )

        # Relay inner workflow events to the chatflow's SSE channel
        # so the frontend sees step-by-step node growth in real time.
        # Register the subscriber *synchronously* before awaiting
        # execute(): a sufficiently fast inner workflow can complete and
        # call ``_bus.close`` before the relay task has been scheduled,
        # which then deadlocks on a queue.get() that will never see the
        # None sentinel.
        inner_wf_id = chat_node.workflow.id
        relay_queue = self._bus.open_subscription(inner_wf_id)
        relay_task = asyncio.create_task(
            self._relay_inner_events(
                chatflow.id, node_id, inner_wf_id, relay_queue
            )
        )

        runtime_error: str | None = None
        # Workspace-level "disabled" tools are a harder gate than the
        # per-chatflow denylist: even if the chatflow has opted-in to a
        # tool, the workspace veto wins. Union them here so the engine
        # gets a single frozenset to refuse against.
        ws_settings = tenancy_runtime.get_settings(self._inner._tool_ctx.workspace_id)
        effective_disabled = (
            frozenset(chatflow.disabled_tool_names) | frozenset(ws_settings.globally_disabled())
        )
        # Compact ChatNodes (auto-inserted by the dual-track trigger OR
        # queued by an explicit user compact) run the single-shot compact
        # worker. Disable Tier 1 inside them — the worker already IS a
        # compaction — and skip the judge post-node hook (only relevant
        # for turn nodes in semi_auto / auto mode).
        is_compact_node = chat_node.compact_snapshot is not None
        try:
            await self._inner.execute(
                chat_node.workflow,
                chatflow_tool_loop_budget=chatflow.tool_loop_budget,
                chatflow_auto_mode_revise_budget=chatflow.auto_mode_revise_budget,
                chatflow_min_ground_ratio=(
                    None if is_compact_node else chatflow.min_ground_ratio
                ),
                chatflow_ground_ratio_grace_nodes=chatflow.ground_ratio_grace_nodes,
                chatflow_compact_trigger_pct=(
                    None if is_compact_node else chatflow.compact_trigger_pct
                ),
                chatflow_compact_target_pct=chatflow.compact_target_pct,
                chatflow_compact_preserve_recent_turns=chatflow.compact_preserve_recent_turns,
                chatflow_compact_model=chatflow.compact_model,
                chatflow_id=chatflow.id,
                post_node_hook=(
                    None
                    if is_compact_node
                    else self._build_post_node_hook(chat_node, chatflow)
                ),
                disabled_tool_names=effective_disabled,
            )
        except Exception as exc:  # noqa: BLE001 — engine boundary
            log.exception("chat node %s inner workflow raised", node_id)
            runtime_error = f"{type(exc).__name__}: {exc}"
        finally:
            # Inner workflow is done (succeeded or raised). Signal
            # end-of-stream via close() — the relay's ``async for``
            # sees the None sentinel and returns naturally — then
            # await the task so queued events finish forwarding
            # before we tear down. cancel() here would race the
            # relay and silently drop the tail of the stream; under
            # Option B's dynamic spawning, judge_post's node events
            # land right at execute()'s end, so a cancel() would
            # deterministically lose them.
            await self._bus.close(inner_wf_id)
            try:
                await relay_task
            except Exception:
                pass

        async with runtime.lock:
            chat_node = chatflow.get(node_id)
            pending_prompt = chat_node.workflow.pending_user_prompt
            judge_post_text = _judge_post_response_text(chat_node.workflow)
            terminal = _terminal_llm_call(chat_node.workflow)
            if is_compact_node:
                # Compact ChatNode finalization mirrors the tail of
                # ``compact_chain``: fold the llm_call output into
                # ``compact_snapshot.summary`` so downstream
                # ``_build_chat_context`` treats this node as the cutoff.
                summary_text = (
                    (terminal.output_message.content or "").strip()
                    if terminal is not None and terminal.output_message is not None
                    else ""
                )
                if runtime_error is not None or not summary_text:
                    chat_node.status = NodeStatus.FAILED
                    chat_node.error = (
                        runtime_error
                        or "compact worker returned empty summary"
                    )
                else:
                    snap = chat_node.compact_snapshot
                    compacted_tokens = len(summary_text) // 4 + (
                        _estimate_tokens_from_wire(snap.preserved_messages)
                        if snap is not None
                        else 0
                    )
                    if snap is not None:
                        chat_node.compact_snapshot = snap.model_copy(
                            update={
                                "summary": summary_text,
                                "compacted_tokens": compacted_tokens,
                            }
                        )
                    chat_node.agent_response = EditableText.by_agent(summary_text)
                    chat_node.status = NodeStatus.SUCCEEDED
            elif runtime_error is not None:
                chat_node.status = NodeStatus.FAILED
                chat_node.error = runtime_error
            elif pending_prompt is not None:
                # A judge inside the WorkFlow decided it needs user
                # clarification before continuing. The pending prompt
                # becomes this ChatNode's agent_response; the user's
                # reply creates a child ChatNode in the normal way.
                # All user dialogue lives at the ChatFlow layer (§3.5).
                chat_node.agent_response = EditableText.by_agent(pending_prompt)
                chat_node.status = NodeStatus.SUCCEEDED
            elif judge_post_text is not None:
                # Decompose layers + halt paths: the outer judge_post
                # is the universal exit gate, and its merged_response /
                # user_message is the layer's effective reply. Takes
                # precedence over any terminal llm_call so a worker's
                # raw draft can't shadow the post-judge's verdict.
                chat_node.agent_response = EditableText.by_agent(judge_post_text)
                chat_node.status = NodeStatus.SUCCEEDED
            elif (judge_crash_error := _judge_crash_unrecoverable(
                chat_node.workflow
            )) is not None:
                # A judge_call (any variant) crashed (e.g. ProviderError)
                # and exhausted its retry budget. Without this branch
                # we'd fall through to _terminal_llm_call, which in
                # decompose mode returns the planner's raw plan JSON —
                # that silently degrades a failed turn to a SUCCEEDED
                # ChatNode whose agent_response is a JSON dump. Surface
                # the crash instead.
                chat_node.status = NodeStatus.FAILED
                chat_node.error = judge_crash_error
            elif terminal is None:
                chat_node.status = NodeStatus.FAILED
                chat_node.error = "inner workflow had no terminal llm_call"
            elif (
                terminal.status == NodeStatus.SUCCEEDED
                and terminal.output_message is not None
            ):
                chat_node.agent_response = EditableText.by_agent(
                    terminal.output_message.content
                )
                chat_node.status = NodeStatus.SUCCEEDED
            else:
                chat_node.status = NodeStatus.FAILED
                chat_node.error = (
                    terminal.error
                    or f"inner terminal status={terminal.status.value}"
                )
            # Freeze the output token count once agent_response is final.
            # Every descendant will pay this many tokens to include this
            # turn in its chain context (via _build_chat_context), so the
            # canvas adds it to entry_prompt_tokens to show what the
            # *next* turn will consume — not what this turn did.
            response_text = chat_node.agent_response.text if chat_node.agent_response else ""
            chat_node.output_response_tokens = _count_text_tokens(response_text)
            chat_node.finished_at = utcnow()

            if consumed_pending_id is not None:
                fut = runtime.pending_futures.get(consumed_pending_id)
                if fut is not None and not fut.done():
                    fut.set_result(chat_node)

            # ChatBoard cascade (PR 3, 2026-04-20): once the ChatNode has
            # frozen into SUCCEEDED, write a ``scope='chat'`` BoardItem so
            # descendants' ancestor walks can read it. FAILED turns don't
            # get a ChatBoardItem — there's no agent reply worth
            # briefing, and the retry will surface a sibling with its
            # own description.
            if chat_node.status == NodeStatus.SUCCEEDED:
                await self._spawn_chat_board_item(chatflow, chat_node)

            cascade = chat_node.status == NodeStatus.SUCCEEDED
            if not cascade:
                # Partition the queue by channel policy. 'discard'
                # turns get their futures failed and are dropped;
                # 'continue' turns stay in the queue so they survive
                # a later retry (which transfers the queue to the
                # sibling node). Delete-failed-node drops everything
                # regardless — policy only shapes the *retry* path.
                discarded = [
                    p for p in chat_node.pending_queue
                    if p.on_upstream_failure == "discard"
                ]
                kept = [
                    p for p in chat_node.pending_queue
                    if p.on_upstream_failure != "discard"
                ]
                self._fail_pending(
                    runtime,
                    discarded,
                    DiscardedUpstreamFailure(
                        f"upstream node {node_id} failed: {chat_node.error}"
                    ),
                )
                chat_node.pending_queue = kept
                if discarded:
                    await self._publish_queue_updated(runtime, node_id)

            if cascade:
                await self._try_consume(runtime, node_id)

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow.id,
                kind="chat.node.status",
                node_id=node_id,
                data={"status": chat_node.status.value},
            )
        )
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow.id,
                kind="chat.turn.completed",
                node_id=node_id,
                data={
                    "status": chat_node.status.value,
                    "agent_response": chat_node.agent_response.text,
                },
            )
        )
        await self._save(runtime)

    def _fail_pending(
        self,
        runtime: ChatFlowRuntime,
        queue: list[PendingTurn],
        exc: Exception,
    ) -> None:
        """Resolve futures for the given pending turns with ``exc``.

        Called under the runtime lock. Does not mutate ``queue`` — the
        caller decides whether to clear it.
        """
        for p in queue:
            fut = runtime.pending_futures.get(p.id)
            if fut is not None and not fut.done():
                fut.set_exception(exc)

    async def _publish_node_created(
        self, runtime: ChatFlowRuntime, node_id: str
    ) -> None:
        node = runtime.chatflow.get(node_id)
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=runtime.chatflow.id,
                kind="chat.node.created",
                node_id=node_id,
                data={
                    "parent_ids": list(node.parent_ids),
                    "user_message": node.user_message.text
                    if node.user_message
                    else None,
                    "status": node.status.value,
                },
            )
        )

    async def _publish_queue_updated(
        self, runtime: ChatFlowRuntime, node_id: str
    ) -> None:
        if node_id not in runtime.chatflow.nodes:
            return
        node = runtime.chatflow.get(node_id)
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=runtime.chatflow.id,
                kind="chat.node.queue.updated",
                node_id=node_id,
                data={
                    "pending_queue": [
                        {"id": p.id, "text": p.text, "source": p.source}
                        for p in node.pending_queue
                    ],
                },
            )
        )

    async def _spawn_chat_board_item(
        self, chatflow: ChatFlow, node: ChatFlowNode
    ) -> None:
        """Write one ChatBoardItem for *node* via the inner engine's
        ``board_writer`` (PR 3, 2026-04-20).

        Called synchronously from every ChatNode-success path: plain
        turn nodes, Tier-2 compact nodes, and manual merge nodes. The
        description is a deterministic code template — no LLM call —
        so the cost is bounded and offline tests don't need a provider
        stub. A future PR can swap in an LLM-generated brief without
        changing the call sites.

        Idempotent: the underlying ``BoardItemRepository.upsert_by_source``
        keys off ``source_node_id``, so a re-invocation (retry, engine
        replay) overwrites the existing row in place instead of growing
        duplicates.

        No-ops silently when no ``board_writer`` is wired (e.g. unit
        tests that constructed ``ChatFlowEngine`` without a DB) — same
        best-effort contract as the WorkBoard writer.
        """
        writer = self._inner._board_writer
        if writer is None:
            return
        # Greeting root has no turn content and no chat story to brief.
        # Skip it — downstream descendants won't read a board item from
        # the root anyway.
        if node.user_message is None and node.agent_response.text == "":
            return
        source_kind = _chat_board_source_kind(node)
        description = _chat_board_description(node)
        try:
            await writer(
                chatflow_id=chatflow.id,
                workflow_id=None,
                source_node_id=node.id,
                source_kind=source_kind,
                scope="chat",
                description=description,
                fallback=False,
            )
        except Exception:  # noqa: BLE001 — board is best-effort
            log.exception(
                "ChatBoardItem write failed for chatflow=%s node=%s "
                "kind=%s — ChatNode state is unchanged",
                chatflow.id,
                node.id,
                source_kind,
            )

    async def _relay_inner_events(
        self,
        chatflow_id: str,
        chat_node_id: str,
        inner_wf_id: str,
        queue: asyncio.Queue[WorkflowEvent | None],
    ) -> None:
        """Subscribe to inner workflow events and re-publish them
        under the chatflow's SSE channel so the frontend sees
        step-by-step node growth inside a running turn.

        The relay translates inner event kinds (``node.running``,
        ``node.succeeded``, ``node.failed``) into chatflow-scoped
        kinds (``chat.workflow.node.running``, etc.) and tags each
        event with the outer ``chat_node_id`` so the frontend knows
        which ChatFlowNode's workflow is being updated.
        """
        try:
            async for event in self._bus.drain(inner_wf_id, queue):
                if event.kind == "workflow.completed":
                    # No need to relay — chatflow_engine handles this.
                    continue
                await self._bus.publish(
                    WorkflowEvent(
                        workflow_id=chatflow_id,
                        kind=f"chat.workflow.{event.kind}",
                        node_id=event.node_id,
                        data={
                            **event.data,
                            "chat_node_id": chat_node_id,
                        },
                    )
                )
        except asyncio.CancelledError:
            pass

    async def _save(self, runtime: ChatFlowRuntime) -> None:
        if self._save_callback is None:
            return
        try:
            await self._save_callback(runtime.chatflow)
        except Exception:  # noqa: BLE001 — persistence is best-effort here
            log.exception(
                "chatflow save callback failed for %s", runtime.chatflow.id
            )


def _cascade_fail_workflow(workflow: WorkFlow) -> list[str]:
    """Force-fail every still-RUNNING WorkNode under ``workflow``,
    recursing into sub_workflows. Returns the ids that were actually
    transitioned (so callers can publish events for each).

    Used by ``cancel_running_node`` because ``task.cancel()`` on the
    outer ChatNode task interrupts WorkflowEngine mid-loop and the
    in-flight WorkNode never reaches its own finalize block.
    """
    now = utcnow()
    cascaded: list[str] = []
    for wn in workflow.nodes.values():
        if wn.status == NodeStatus.RUNNING:
            wn.status = NodeStatus.FAILED
            wn.error = "Cancelled by user"
            wn.finished_at = now
            cascaded.append(wn.id)
        if wn.sub_workflow is not None:
            cascaded.extend(_cascade_fail_workflow(wn.sub_workflow))
    return cascaded


def _apply_judge_pre_trio(workflow: WorkFlow, verdict: JudgeVerdict) -> None:
    """Copy judge_pre's distilled trio onto ``workflow`` so every
    downstream node that reads via ``_trio_params`` sees the judge_pre
    authoring.

    Called *before* branching on feasibility so that even halt paths
    have a trio on the WorkFlow for judge_post / the UI to show. Any
    field the judge returned as ``None`` or empty string is left
    untouched on the WorkFlow (so re-running judge_pre won't
    accidentally clobber an earlier good extraction with a blank).
    """
    def _set(attr: str, value: str | None) -> None:
        if value is None or not value.strip():
            return
        setattr(workflow, attr, EditableText.by_agent(value))

    _set("description", verdict.extracted_description)
    _set("inputs", verdict.extracted_inputs)
    _set("expected_outcome", verdict.extracted_expected_outcome)


def _judge_pre_should_halt(verdict: JudgeVerdict) -> bool:
    """Only ``infeasible`` halts the run outright.

    ``risky`` — even with ``missing_inputs`` — proceeds: the worker
    answers with what it has, and the post_judge surfaces caveats /
    asks the user for clarification alongside the answer.
    """
    return verdict.feasibility == "infeasible"


def _render_judge_pre_risky_assumptions(verdict: JudgeVerdict) -> str:
    """Turn a ``risky`` judge_pre verdict's blockers into free-text
    planner-handoff notes so the planner can plan around them."""
    if verdict.feasibility != "risky" or not verdict.blockers:
        return ""
    lines = [
        "judge_pre flagged this task as risky — the following assumptions"
        " should hold for the plan to succeed. Plan around them, or fold"
        " them into individual subtasks as preconditions.",
        "",
    ]
    lines.extend(f"- {b}" for b in verdict.blockers)
    return "\n".join(lines)


def _render_judge_pre_halt(verdict: JudgeVerdict) -> str:
    """Compact human-readable summary of a judge_pre veto, fed into
    judge_post as the ``upstream_summary``. Lets judge_post quote the
    specifics back to the user without re-running the analysis."""
    parts: list[str] = []
    if verdict.feasibility:
        parts.append(f"Feasibility: {verdict.feasibility}")
    if verdict.blockers:
        parts.append("Blockers:")
        parts.extend(f"  - {b}" for b in verdict.blockers)
    if verdict.missing_inputs:
        parts.append("Missing inputs:")
        parts.extend(f"  - {m}" for m in verdict.missing_inputs)
    return "\n".join(parts) if parts else "(empty verdict)"


def _round_index_for(workflow: WorkFlow, node: WorkFlowNode) -> int:
    """Compute the 1-indexed debate round for ``node`` (a planner or
    worker) by walking its parent chain and counting same-role
    ancestors. M12.4b only ever spawns one round of each, so this
    always returns 1; M12.4c (debate-as-chain) will surface higher
    rounds as the planner / worker is rerun with critiques attached.
    """
    if node.role is None:
        return 1
    count = 1
    cursor = node.parent_ids[0] if node.parent_ids else None
    seen: set[str] = set()
    while cursor is not None and cursor not in seen:
        seen.add(cursor)
        ancestor = workflow.nodes.get(cursor)
        if ancestor is None:
            break
        if ancestor.role == node.role:
            count += 1
        cursor = ancestor.parent_ids[0] if ancestor.parent_ids else None
    return count


def _summarize_planner_outcome(
    decision: str | None, plan: RecursivePlannerOutput
) -> str:
    """Render a one-paragraph halt summary for judge_post when the
    planner-pipeline can't proceed.

    Distinct cases the user / judge_post should see:
    - planner_judge said ``halt``, or ``revise`` after the debate
      budget has been spent
    - planner emitted ``decompose`` (sub-WorkFlows not implemented yet)
    - planner emitted ``infeasible`` (no decomposition exists)
    """
    if plan.mode == "infeasible":
        return f"planner reported infeasible: {plan.reason or '(no reason)'}"
    if plan.mode == "decompose":
        return (
            f"planner proposed {len(plan.subtasks or [])} sub-tasks "
            "(decompose support is pending — M12.4d)"
        )
    if decision == "halt":
        return "planner_judge halted the debate"
    if decision == "revise":
        return (
            "planner_judge still wants revisions after the debate "
            "round budget was exhausted"
        )
    return f"unhandled planner outcome: decision={decision} mode={plan.mode}"


def _render_critiques(verdict: JudgeVerdict) -> str:
    """Serialize a judge's critiques as a JSON array for the next
    planner / worker round. Empty list → empty string so the template
    renders cleanly when there's nothing to address."""
    if not verdict.critiques:
        return ""
    return json.dumps(
        [c.model_dump() for c in verdict.critiques],
        ensure_ascii=False,
        indent=2,
    )


def _collect_debate_concerns(
    workflow: WorkFlow, last_judge: WorkFlowNode
) -> str:
    """Walk every same-role judge ancestor of ``last_judge`` (plus the
    judge itself) and render their critiques as a multi-round concerns
    block. Used when a debate exhausts its round budget — judge_post
    sees the full trail of objections, not just the final round's
    verdict, so its user_message can summarize what the critic kept
    flagging across rounds.

    Returns an empty string when no critiques were recorded (shouldn't
    happen on a revise path but we stay defensive).
    """
    if last_judge.role is None:
        return ""
    ancestor_ids = workflow.ancestors(last_judge.id)
    same_role_judges: list[WorkFlowNode] = []
    for nid in ancestor_ids:
        n = workflow.nodes.get(nid)
        if n is not None and n.role == last_judge.role:
            same_role_judges.append(n)
    same_role_judges.append(last_judge)

    rendered: list[str] = []
    for idx, judge in enumerate(same_role_judges, start=1):
        v = judge.judge_verdict
        if v is None or not v.critiques:
            continue
        body = _render_critiques(v)
        rendered.append(f"Round {idx} critiques:\n{body}")
    return "\n\n".join(rendered)


def _topo_order_subtasks(subtasks: list[SubTask]) -> list[int]:
    """Return subtask indices in dependency order.

    Kahn's algorithm: subtasks with no unmet ``after`` references come
    first; siblings at the same depth keep their declaration order. The
    parser already validated the ``after`` graph (no self-refs, no
    out-of-range), so we don't re-check here. Cycles would have been
    rejected by validation; we still detect them defensively.
    """
    n = len(subtasks)
    remaining = {i: set(s.after) for i, s in enumerate(subtasks)}
    order: list[int] = []
    while remaining:
        ready = [i for i, deps in remaining.items() if not deps]
        if not ready:
            raise ValueError("decompose subtasks contain a cycle")
        ready.sort()  # stable: declaration order among parallel siblings
        for i in ready:
            order.append(i)
            remaining.pop(i)
        for deps in remaining.values():
            for i in ready:
                deps.discard(i)
    return order


def _decompose_group_planner_judge(
    workflow: WorkFlow, delegation_node: WorkFlowNode
) -> WorkFlowNode | None:
    """Walk back from a delegation through ``parent_ids[0]`` until we
    hit the planner_judge that triggered the decompose. Returns
    ``None`` if no planner_judge ancestor exists (= delegation wasn't
    spawned by a planner_judge, e.g. test fixtures or future
    user-authored decompose flows)."""
    visited: set[NodeId] = set()
    cursor = delegation_node.parent_ids[0] if delegation_node.parent_ids else None
    while cursor is not None and cursor not in visited:
        visited.add(cursor)
        ancestor = workflow.nodes.get(cursor)
        if ancestor is None:
            break
        if (
            ancestor.step_kind == StepKind.JUDGE_CALL
            and ancestor.role == WorkNodeRole.PLAN_JUDGE
        ):
            return ancestor
        cursor = ancestor.parent_ids[0] if ancestor.parent_ids else None
    return None


def _decompose_group_members(
    workflow: WorkFlow, planner_judge_id: NodeId
) -> list[WorkFlowNode]:
    """Every sub_agent_delegation whose ancestor chain includes
    ``planner_judge_id`` — i.e. the full decompose group spawned by
    that planner_judge."""
    out: list[WorkFlowNode] = []
    for n in workflow.nodes.values():
        if n.step_kind != StepKind.DELEGATE:
            continue
        if planner_judge_id in workflow.ancestors(n.id):
            out.append(n)
    return out


def _decompose_group_already_aggregated(
    workflow: WorkFlow, planner_judge_id: NodeId
) -> bool:
    """Has a judge_post node whose ancestor chain includes
    ``planner_judge_id`` already been spawned? Guards against
    double-spawn when several delegations finish in quick succession."""
    for n in workflow.nodes.values():
        if (
            n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == JudgeVariant.POST
            and planner_judge_id in workflow.ancestors(n.id)
        ):
            return True
    return False


def _redo_group_judge_post(
    workflow: WorkFlow, clone_node: WorkFlowNode
) -> WorkFlowNode | None:
    """Return the judge_post that directly spawned ``clone_node`` as a
    redo clone (M12.4d6). A redo clone's immediate parent is always a
    judge_post — that's the distinguishing mark vs a normal worker
    (parented on a planner_judge or worker_judge)."""
    if not clone_node.parent_ids:
        return None
    parent = workflow.nodes.get(clone_node.parent_ids[0])
    if (
        parent is not None
        and parent.step_kind == StepKind.JUDGE_CALL
        and parent.judge_variant == JudgeVariant.POST
    ):
        return parent
    return None


def _redo_group_members(
    workflow: WorkFlow, judge_post_id: NodeId
) -> list[WorkFlowNode]:
    """All direct redo clones of ``judge_post_id`` — i.e. every non-judge
    child whose immediate parent is that judge_post. The re-aggregation
    judge_post is also a child of the original, but it's excluded from
    the redo group (it's the aggregator, not a member). BRIEF children
    are observability nodes (MemoryBoard PR 1), not redo clones, and
    must not gate re-aggregation."""
    out: list[WorkFlowNode] = []
    for n in workflow.nodes.values():
        if not n.parent_ids or n.parent_ids[0] != judge_post_id:
            continue
        if n.step_kind == StepKind.JUDGE_CALL:
            continue
        if n.step_kind == StepKind.BRIEF:
            continue
        out.append(n)
    return out


def _redo_group_already_reaggregated(
    workflow: WorkFlow, judge_post_id: NodeId
) -> bool:
    """Has a new judge_post already been spawned to re-aggregate this
    redo group? Guards against duplicate spawning when multiple clones
    finish in rapid succession."""
    for n in workflow.nodes.values():
        if (
            n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == JudgeVariant.POST
            and judge_post_id in workflow.ancestors(n.id)
        ):
            return True
    return False


def _judge_post_round_index(
    workflow: WorkFlow, judge_post_node: WorkFlowNode
) -> int:
    """1-indexed retry round: count judge_post ancestors (including the
    current one) in this chain. Round 1 is the first judge_post; round
    N means N-1 redo cycles have already run. The halt fuse compares
    this to ``workflow.judge_retry_budget``."""
    count = 1
    for nid in workflow.ancestors(judge_post_node.id):
        n = workflow.nodes.get(nid)
        if (
            n is not None
            and n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == JudgeVariant.POST
        ):
            count += 1
    return count


def _format_decompose_aggregation(
    workflow: WorkFlow, members: list[WorkFlowNode]
) -> str:
    """Build the aggregating judge_post's ``upstream_summary`` as a set
    of structured per-subtask blocks so the judge can tell the
    ``ok`` / ``sub_halt`` / ``worker_failed`` / ``sub_judge_post_*``
    cases apart without guessing from prose.

    Block shape (one per subtask, 1-indexed)::

        [subtask N id=<worknode_id> status=<status>{ detail="..."}]
        <label>
        <body>

    Status vocabulary matches ``_classify_sub_outcome``. The judge is
    told in its system prompt to decide between aggregate / partial
    aggregate / retry / escalate based on the mix of statuses.
    """
    parts: list[str] = []
    for i, m in enumerate(members, start=1):
        label = (m.description.text or "").strip() or m.id
        sub = m.sub_workflow
        if sub is None:
            parts.append(
                f"[subtask {i} id={m.id} status=empty]\n{label}\n(no sub_workflow)"
            )
            continue
        status, body = _classify_sub_outcome(sub)
        detail = ""
        if status != "ok" and m.error:
            # ``error`` was set by workflow_engine when it absorbed a
            # sub halt. Surface it in the header so the judge can see
            # the short reason alongside the classifier's body.
            detail = f' detail="{_escape_attr(m.error)}"'
        header = f"[subtask {i} id={m.id} status={status}{detail}]"
        parts.append(f"{header}\n{label}\n{body}")
    return "\n\n".join(parts)


def _escape_attr(s: str) -> str:
    """Inline-attribute escaping: collapse newlines, escape quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _find_round_one_judge_post(
    workflow: WorkFlow, judge_post_node: WorkFlowNode
) -> WorkFlowNode:
    """Walk back through the retry lineage to the earliest judge_post.

    The aggregator's ``parent_ids`` list the members of its own round
    (round-1 decompose subtasks, or round-N redo clones). Each member's
    ``parent_ids[0]`` is the preceding judge_post (or nothing, for
    round-1 originals). Follow that link back until there is no further
    judge_post — that is round-1.
    """
    current = judge_post_node
    while True:
        if not current.parent_ids:
            return current
        any_member = workflow.nodes.get(current.parent_ids[0])
        if any_member is None or not any_member.parent_ids:
            return current
        pred = workflow.nodes.get(any_member.parent_ids[0])
        if (
            pred is None
            or pred.step_kind != StepKind.JUDGE_CALL
            or pred.judge_variant != JudgeVariant.POST
        ):
            return current
        current = pred


def _member_round_index(workflow: WorkFlow, member: WorkFlowNode) -> int:
    """1-indexed round a member belongs to — how many post-judge
    ancestors it sits below, plus one. Round-1 originals have no judge
    ancestors; a clone whose parent is the round-1 judge is round 2."""
    count = 1
    for nid in workflow.ancestors(member.id):
        n = workflow.nodes.get(nid)
        if (
            n is not None
            and n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == JudgeVariant.POST
        ):
            count += 1
    return count


def _format_redo_aggregation(
    workflow: WorkFlow, judge_post_node: WorkFlowNode
) -> tuple[list[str], list[str]]:
    """Build structured upstream_summary + worknode catalog for a redo
    re-aggregation.

    One block per round-1 subtask, showing the latest surviving version
    (the tail of its redo chain). The block's ``round=`` attribute tells
    the judge which retry round that version came from — letting it
    see the full cross-round picture instead of only the current round's
    clones.
    """
    round_one = _find_round_one_judge_post(workflow, judge_post_node)
    round_one_members = [
        workflow.nodes[mid]
        for mid in round_one.parent_ids
        if mid in workflow.nodes
    ]

    replaced_by: dict[NodeId, NodeId] = {}
    for n in workflow.nodes.values():
        if n.redo_source_id is not None:
            replaced_by[n.redo_source_id] = n.id

    summary_parts: list[str] = []
    catalog_parts: list[str] = []
    for i, original in enumerate(round_one_members, start=1):
        surviving_id = original.id
        while surviving_id in replaced_by:
            surviving_id = replaced_by[surviving_id]
        surviving = workflow.nodes[surviving_id]
        round_idx = _member_round_index(workflow, surviving)

        label = (
            (surviving.description.text or "").strip()
            if surviving.description is not None
            else ""
        ) or surviving.id
        status, body = _redo_clone_classification(surviving)
        detail = ""
        if status != "ok" and surviving.error:
            detail = f' detail="{_escape_attr(surviving.error)}"'
        header = (
            f"[subtask {i} id={surviving.id} status={status} round={round_idx}{detail}]"
        )
        summary_parts.append(f"{header}\n{label}\n{body}")

        kind_desc = (
            "worker" if surviving.step_kind == StepKind.DRAFT
            else "delegation"
        )
        catalog_parts.append(
            f"{surviving.id}: {kind_desc} (round {round_idx}) for {label}"
        )

    return summary_parts, catalog_parts


#: Map structured-status tokens to short human-readable phrases for the
#: halt prompt. Keys match the vocabulary of ``_classify_sub_outcome`` /
#: ``_redo_clone_classification``.
_STATUS_HUMAN = {
    "ok": "completed successfully",
    "worker_failed": "worker failed",
    "sub_pre_halt": "sub-agent refused before starting",
    "sub_judge_post_failed": "sub-agent's reviewer crashed",
    "sub_judge_post_fail": "sub-agent's reviewer returned fail",
    "sub_judge_post_retry_exhausted": "sub-agent's retry budget ran out",
    "empty": "produced no output",
}


def _compose_retry_halt_message(
    workflow: WorkFlow,
    judge_post_node: WorkFlowNode,
    verdict: JudgeVerdict,
    *,
    reason: str,
    round_index: int,
    budget: int,
) -> str:
    """Deterministic halt message for retry-budget exhaustion and
    retry-with-no-usable-targets.

    Rather than echoing the last judge's ``user_message`` verbatim (which
    may only describe round-N's view and can factually misrepresent
    successes from earlier rounds — 2026-04-14 gemma4 regression), walk
    every round-1 subtask to its latest surviving version and report
    per-subtask final status. That way partial successes stay visible
    even when the overall layer halts.

    For atomic (non-decompose) layers the walk still works — round-1
    parent_ids is a single worker/llm node and we report its latest
    clone's status.
    """
    round_one = _find_round_one_judge_post(workflow, judge_post_node)
    round_one_members = [
        workflow.nodes[mid]
        for mid in round_one.parent_ids
        if mid in workflow.nodes
    ]
    replaced_by: dict[NodeId, NodeId] = {}
    for n in workflow.nodes.values():
        if n.redo_source_id is not None:
            replaced_by[n.redo_source_id] = n.id

    lines: list[str] = []
    if reason == "budget_exhausted":
        lines.append(
            f"I retried {round_index} round(s) but hit the retry budget "
            f"of {budget} without landing a clean result. Here is where "
            "each part of the plan stands:"
        )
    else:  # no_usable_targets
        lines.append(
            "The reviewer asked to retry, but none of the targets it "
            "named could be redone. Here is where each part of the plan "
            "stands:"
        )
    lines.append("")

    for i, original in enumerate(round_one_members, start=1):
        surviving_id = original.id
        while surviving_id in replaced_by:
            surviving_id = replaced_by[surviving_id]
        surviving = workflow.nodes[surviving_id]
        round_idx = _member_round_index(workflow, surviving)

        label = (
            (surviving.description.text or "").strip()
            if surviving.description is not None
            else ""
        ) or surviving.id

        if surviving.step_kind == StepKind.DELEGATE and surviving.sub_workflow is not None:
            status, _body = _classify_sub_outcome(surviving.sub_workflow)
        else:
            status, _body = _redo_clone_classification(surviving)
        phrase = _STATUS_HUMAN.get(status, status)

        round_note = "" if round_idx == 1 else f" (after {round_idx - 1} retry round(s))"
        detail = f" — {surviving.error}" if status != "ok" and surviving.error else ""
        lines.append(f"- **{label}** — {phrase}{round_note}{detail}")

    lines.append("")
    lines.append("How would you like to proceed?")
    return "\n".join(lines)


def _redo_clone_classification(m: WorkFlowNode) -> tuple[str, str]:
    """Classify a redo clone (worker or delegation) for the structured
    redo_aggregation summary. Returns ``(status, body)`` using the same
    vocabulary as ``_classify_sub_outcome`` for delegation clones, and
    a worker-specific ``ok`` / ``worker_failed`` / ``empty`` for LLM
    clones.
    """
    if m.step_kind == StepKind.DRAFT:
        if m.status == NodeStatus.SUCCEEDED and m.output_message is not None:
            return ("ok", m.output_message.content)
        if m.status == NodeStatus.FAILED:
            return (
                "worker_failed",
                f"worker raised: {m.error or 'unknown error'}",
            )
        return ("empty", "(worker produced no output)")
    if m.step_kind == StepKind.DELEGATE and m.sub_workflow is not None:
        return _classify_sub_outcome(m.sub_workflow)
    return ("empty", "(clone produced no output)")


def _judge_crash_unrecoverable(workflow: WorkFlow) -> str | None:
    """Returns an error string iff a judge_call (any variant) crashed
    and its retry budget is exhausted, with no successful post_judge
    output to compensate. Used by ``_execute_node`` to mark the
    ChatNode FAILED instead of falling through to ``_terminal_llm_call``
    (which in decompose mode returns the planner's raw plan JSON —
    see ``_after_judge_failed``).

    Caller must check ``_judge_post_response_text`` first; if any
    post_judge produced ``merged_response`` we have a usable reply
    and shouldn't fail the turn just because a sibling crashed.

    For each (parent_id, variant) group with a FAILED judge, check
    whether a SUCCEEDED sibling exists — that means a retry succeeded
    and the chain moved forward. Only groups where every sibling is
    FAILED count as unrecoverable.
    """
    judges = [
        n
        for n in workflow.nodes.values()
        if n.step_kind == StepKind.JUDGE_CALL
    ]
    by_group: dict[tuple[str, str], list[WorkFlowNode]] = {}
    for j in judges:
        if j.judge_variant is None:
            continue
        # pre_judge (and sub-WorkFlow seed judges) have empty
        # parent_ids; bucket those under a sentinel so they still
        # participate in the unrecoverable-group check.
        parent_key = j.parent_ids[0] if j.parent_ids else ""
        key = (parent_key, j.judge_variant.value)
        by_group.setdefault(key, []).append(j)
    last_failure: WorkFlowNode | None = None
    for siblings in by_group.values():
        if any(s.status == NodeStatus.SUCCEEDED for s in siblings):
            continue
        failed = [s for s in siblings if s.status == NodeStatus.FAILED]
        if not failed:
            continue
        newest = max(failed, key=lambda n: n.created_at)
        if last_failure is None or newest.created_at > last_failure.created_at:
            last_failure = newest
    if last_failure is None:
        return None
    variant = (
        last_failure.judge_variant.value
        if last_failure.judge_variant is not None
        else "judge"
    )
    return (
        f"{variant} judge crashed after retries: "
        f"{last_failure.error or 'unknown error'}"
    )


def _judge_post_response_text(workflow: WorkFlow) -> str | None:
    """Return the user-facing reply produced by the terminal judge_post,
    if any, so the ChatNode's ``agent_response`` can use it as the
    layer's effective reply.

    Priority for an ``accept`` verdict:

    - ``merged_response`` (decompose-accept aggregation).
    - ``user_message`` (atomic-accept override). The judge_post prompt
      nominally tells atomic-accept not to write ``user_message`` and
      to let the worker's draft reach the user verbatim, but in
      practice judges sometimes overwrite when the worker's draft is
      unusable (tool-loop artifacts, raw planner JSON leaks). The judge
      is the universal exit gate — trust its final word over a suspect
      terminal llm_call.

    Returns ``None`` for atomic accepts where the judge left both
    fields empty (worker's draft is the reply) and for halt paths
    (``pending_user_prompt`` is already set via
    ``judge_post_needs_user_input``).
    """
    for n in reversed(list(workflow.nodes.values())):
        if (
            n.step_kind != StepKind.JUDGE_CALL
            or n.judge_variant != JudgeVariant.POST
            or n.judge_verdict is None
        ):
            continue
        v = n.judge_verdict
        if v.post_verdict != "accept":
            # retry / fail are surfaced through pending_user_prompt;
            # the caller uses that instead.
            return None
        return v.merged_response or v.user_message or None
    return None


def _classify_sub_outcome(sub: WorkFlow) -> tuple[str, str]:
    """Classify a sub-WorkFlow's outcome. Returns ``(status, body)``.

    Status vocabulary (used in structured upstream_summary blocks):

    - ``ok`` — sub produced a usable result (``merged_response`` or a
      successful worker draft).
    - ``sub_pre_halt`` — judge_pre vetoed before any worker ran
      (infeasible / missing inputs).
    - ``worker_failed`` — worker ``llm_call`` node is FAILED (provider
      error / validation failure).
    - ``sub_judge_post_failed`` — judge_post crashed (malformed JSON,
      etc.) — the worker's draft is unreviewed.
    - ``sub_judge_post_fail`` — judge_post returned ``post_verdict=fail``.
    - ``sub_judge_post_retry_exhausted`` — judge_post returned
      ``retry`` after exhausting its budget.
    - ``empty`` — no recognizable output (pathological).

    The outer aggregating judge_post reads these to decide between
    full aggregate, partial aggregate, retry, and escalate.
    """
    # 1. judge_pre veto (no worker ever ran)
    for n in sub.nodes.values():
        if (
            n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == JudgeVariant.PRE
            and n.judge_verdict is not None
            and n.judge_verdict.feasibility
            and n.judge_verdict.feasibility != "ok"
        ):
            v = n.judge_verdict
            reason_bits: list[str] = []
            if v.blockers:
                reason_bits.append("blockers: " + "; ".join(v.blockers))
            if v.missing_inputs:
                reason_bits.append("missing_inputs: " + ", ".join(v.missing_inputs))
            if v.user_message:
                reason_bits.append(v.user_message)
            body = " | ".join(reason_bits) or f"judge_pre={v.feasibility}"
            return ("sub_pre_halt", body)

    # 2. Worker crashed
    for n in sub.nodes.values():
        if (
            n.step_kind == StepKind.DRAFT
            and n.role == WorkNodeRole.WORKER
            and n.status == NodeStatus.FAILED
        ):
            return ("worker_failed", f"worker raised: {n.error or 'unknown error'}")

    # 3. judge_post state (terminal)
    for n in reversed(list(sub.nodes.values())):
        if n.step_kind != StepKind.JUDGE_CALL or n.judge_variant != JudgeVariant.POST:
            continue
        if n.status == NodeStatus.FAILED:
            worker_out = _latest_worker_output(sub) or "(no draft)"
            return (
                "sub_judge_post_failed",
                f"judge_post crashed: {n.error or 'unknown error'}\n"
                f"worker draft (unreviewed):\n{worker_out}",
            )
        if n.judge_verdict is None:
            break
        v = n.judge_verdict
        if v.post_verdict == "accept":
            if v.merged_response:
                return ("ok", v.merged_response)
            # Atomic accept — terminal llm_call is the effective output.
            worker_out = _latest_worker_output(sub)
            if worker_out:
                return ("ok", worker_out)
            break
        if v.post_verdict == "fail":
            return (
                "sub_judge_post_fail",
                v.user_message or "judge_post=fail (no user_message)",
            )
        if v.post_verdict == "retry":
            return (
                "sub_judge_post_retry_exhausted",
                v.user_message or "judge_post=retry exhausted (no user_message)",
            )
        break

    # 4. Fallback — latest worker output even without a judge_post.
    worker_out = _latest_worker_output(sub)
    if worker_out:
        return ("ok", worker_out)
    return ("empty", "(sub-WorkFlow produced no output)")


def _latest_worker_output(sub: WorkFlow) -> str:
    """Return the most-recent llm_call output's content, or empty."""
    for n in reversed(list(sub.nodes.values())):
        if n.step_kind == StepKind.DRAFT and n.output_message is not None:
            return n.output_message.content
    return ""


def _sub_workflow_effective_output(sub: WorkFlow) -> str:
    """Back-compat shim: the single-string view of a sub's outcome.

    Used by the redo-aggregation path, which still emits a flat
    ``[label] (retried)\\n<out>`` summary. Decompose aggregation uses
    the structured classifier directly.
    """
    _status, body = _classify_sub_outcome(sub)
    return body


def _atomic_brief_for_worker(
    workflow: WorkFlow, worker_node: WorkFlowNode
) -> AtomicBrief | None:
    """Recover the planner's atomic brief that spawned ``worker_node``.

    The worker's own ``description`` is a stock string from the worker
    template — it doesn't carry the brief. Walk back to the planner_judge
    that parented the worker, then to the planner it judged, and re-parse
    the planner's plan JSON. Returns ``None`` if any link is missing or
    the plan no longer parses as atomic (which shouldn't happen on a
    happy debate path, but we'd rather halt than crash).

    Redo clones are parented on ``judge_post``, not planner_judge — so
    chase the ``redo_source_id`` chain back to the round-1 original first
    to recover the planner debate context.
    """
    cursor = worker_node
    while cursor.redo_source_id is not None:
        origin = workflow.nodes.get(cursor.redo_source_id)
        if origin is None:
            break
        cursor = origin
    if not cursor.parent_ids:
        return None
    planner_judge = workflow.nodes.get(cursor.parent_ids[0])
    if planner_judge is None or not planner_judge.judge_target_id:
        return None
    planner = workflow.nodes.get(planner_judge.judge_target_id)
    if planner is None or planner.output_message is None:
        return None
    try:
        plan = parse_recursive_planner_output(planner.output_message.content)
    except PlannerParseError:
        return None
    return plan.atomic if plan.mode == "atomic" else None


def _format_redo_eligible(workflow: WorkFlow) -> str:
    """Render the whitelist of nodes the engine is willing to re-spawn.

    Matches the gating in ``_spawn_redo_clones``: worker llm_calls and
    sub_agent_delegations only. Other kinds (judges, planners,
    tool_calls) are silently dropped from ``redo_targets`` and make
    the whole retry halt with "none of the targets it named could be
    redone" if nothing eligible is left. Feeding this list to
    judge_post lets it name ids it knows will actually be re-run.
    """
    lines: list[str] = []
    for n in workflow.nodes.values():
        desc = (n.description.text if n.description else "").strip() or "(no description)"
        if n.step_kind == StepKind.DRAFT and n.role == WorkNodeRole.WORKER:
            lines.append(f"{n.id}: worker llm_call — {desc}")
        elif n.step_kind == StepKind.DELEGATE:
            lines.append(f"{n.id}: sub_agent_delegation — {desc}")
    return "\n".join(lines) if lines else "(none — do not request retry with redo_targets; prefer fail)"


def _trio_params(workflow: WorkFlow) -> dict[str, str]:
    """Read the WorkFlow's trio into the param dict every judge / planner
    template expects. Falls back to ``""`` for any unset field so a
    template that asks for ``{{ inputs }}`` doesn't blow up on a
    legacy WorkFlow that predates trio seeding.
    """
    return {
        "description": workflow.description.text if workflow.description else "",
        "inputs": workflow.inputs.text if workflow.inputs else "",
        "expected_outcome": (
            workflow.expected_outcome.text if workflow.expected_outcome else ""
        ),
    }


def _single_node(workflow: WorkFlow) -> WorkFlowNode:
    """Pluck the single main-axis node out of a judge/compact template.

    Both ``judge_pre`` and ``judge_post`` fixtures define exactly one
    judge_call node. Compact/merge templates likewise produce a single
    llm_call. MemoryBoard briefs auto-spawn on terminal WorkFlows and
    are off-main-axis, so this helper filters them out before asserting
    the single-node invariant.
    """
    from agentloom.schemas.common import StepKind as _StepKind

    mains = [n for n in workflow.nodes.values() if n.step_kind != _StepKind.BRIEF]
    if len(mains) != 1:
        raise ValueError(
            f"expected single-node template, got {len(mains)} main-axis nodes "
            f"(total including briefs: {len(workflow.nodes)})"
        )
    return mains[0]


def _terminal_llm_call(workflow: WorkFlow) -> WorkFlowNode | None:
    """Return the llm_call whose output should become the turn's
    ``agent_response``.

    The relevant node is the most recently created ``llm_call`` — this
    rule covers three shapes we produce:

    - plain one-shot turn: single root llm_call.
    - tool loop ``llm → tool → llm (→ tool → llm)*``: final follow-up
      llm_call is newest.
    - judge-gated turn ``[judge_pre →] llm_call [→ judge_post]``: the
      llm_call is the only one; judges are not llm_calls, so they do
      not participate in this selection and the earlier "leaf" rule
      (which excluded llm_calls with any children, including judge
      children) would have wrongly returned None.
    """
    if not workflow.nodes:
        return None
    # Exclude planners — their output is plan JSON for the inner
    # pipeline, never a user-facing reply. If the chain crashed before
    # reaching a worker / terminal llm_call, the caller's
    # ``_judge_crash_unrecoverable`` branch should mark FAILED rather
    # than hand the user the planner's brainstorming output.
    llm_calls = [
        n
        for n in workflow.nodes.values()
        if n.step_kind == StepKind.DRAFT
        and n.role != WorkNodeRole.PLAN
    ]
    if not llm_calls:
        return None
    llm_calls.sort(key=lambda n: n.created_at)
    return llm_calls[-1]


def _live_tip(chatflow: ChatFlow, start_id: str) -> str:
    """Walk down from ``start_id`` following the most recently
    created child at each step until a leaf is reached.

    In a linear conversation this is the only descendant path. In a
    branched chatflow this picks the newest branch — which is the
    one actively accepting new turns. If you want a *specific*
    branch, pass that branch's join node as ``start_id``; the walk
    stays inside its subtree by construction.
    """
    current = start_id
    while True:
        children = [
            n for n in chatflow.nodes.values() if current in n.parent_ids
        ]
        if not children:
            return current
        children.sort(key=lambda n: n.created_at)
        current = children[-1].id


def _latest_leaf(chatflow: ChatFlow) -> str | None:
    """Return the id of the most recently created leaf node, or None.

    A leaf is a node that no other node lists as a parent. Ties broken
    by ``created_at`` (node ids are UUIDv7 so this is deterministic).
    """
    if not chatflow.nodes:
        return None
    referenced: set[str] = set()
    for n in chatflow.nodes.values():
        referenced.update(n.parent_ids)
    leaves = [n for n in chatflow.nodes.values() if n.id not in referenced]
    if not leaves:
        return None
    leaves.sort(key=lambda n: n.created_at)
    return leaves[-1].id


def _build_chat_context(
    chatflow: ChatFlow,
    parent_ids: list[str],
    *,
    include_summary_preamble: bool = True,
) -> list[WireMessage]:
    """Build the user/assistant message history from the ancestor chain.

    We walk ancestors topologically and, for each frozen ChatFlowNode
    with a ``user_message`` (skipping greeting roots for now), emit a
    ``user`` turn and an ``assistant`` turn. Unfrozen turns are
    ignored (they belong to branches in progress).

    Tier 2 compaction: when a compact ChatNode (``compact_snapshot``
    populated + settled summary) exists on the chain, everything
    above it is replaced by a synthetic summary message plus the
    snapshot's preserved tail. The latest compact wins when the chain
    carries more than one — the older one has already been folded
    into the newer one's input so we skip it.

    ``include_summary_preamble=False`` omits the synthetic summary
    message so only *real* messages are returned (prev-compact's
    preserved tail + any turns after the cutoff). Used by
    :meth:`ChatFlowEngine._build_compact_chatnode` so the next compact
    doesn't fold the prior summary back into its own head_wire.
    """
    if not parent_ids:
        return []
    # Primary-parent walk: follow parent_ids[0] up. A merge ChatNode is
    # a hard stop — its ``agent_response`` already encodes both source
    # branches, so the walk includes the merge node itself and then
    # stops (same stop-rule as a compact snapshot, but applied at the
    # chain-walk layer rather than as a summary-preamble swap).
    chain: list[str] = []
    current: str | None = parent_ids[0]
    while current is not None:
        chain.append(current)
        node = chatflow.nodes[current]
        # Merge ChatNodes are a hard stop: multi-parent is the structural
        # marker for a manual branch-merge synthesized reply. Walking
        # past it would re-pull both source branches' history, defeating
        # the merge's whole point.
        if len(node.parent_ids) >= 2:
            break
        current = node.parent_ids[0] if node.parent_ids else None
    chain.reverse()

    # Find the most-recent settled compact ancestor. Chain is root→tip
    # after reverse(); the last match wins.
    compact_cutoff_idx: int | None = None
    for i, nid in enumerate(chain):
        node = chatflow.nodes[nid]
        snap = node.compact_snapshot
        if snap is not None and snap.summary:
            compact_cutoff_idx = i

    messages: list[WireMessage] = []
    start_idx = 0
    if compact_cutoff_idx is not None:
        snap = chatflow.nodes[chain[compact_cutoff_idx]].compact_snapshot
        assert snap is not None  # loop guarantees
        if include_summary_preamble:
            messages.append(
                WireMessage(
                    role="user",
                    content=(
                        "[Prior conversation — summarized to save context]\n\n"
                        f"{snap.summary}"
                    ),
                )
            )
        messages.extend(snap.preserved_messages)
        start_idx = compact_cutoff_idx + 1

    for nid in chain[start_idx:]:
        node = chatflow.nodes[nid]
        if not node.is_frozen:
            continue
        # A compact ChatNode after the cutoff (only possible if the
        # newest compact isn't the cutoff — can't currently happen but
        # guard anyway) should not re-emit its own user/assistant
        # pair: its summary already lives in agent_response as the
        # compaction output, which would leak upstream text into the
        # new context.
        if node.compact_snapshot is not None:
            continue
        if node.user_message is not None:
            messages.append(
                WireMessage(role="user", content=node.user_message.text)
            )
        messages.append(
            WireMessage(role="assistant", content=node.agent_response.text)
        )
    return messages


def _serialize_wire_chain(msgs: list[WireMessage]) -> str:
    """Flatten a chat-context WireMessage list into a newline-separated
    ``[role] content`` string. Used as the ``left_summary`` / ``right_summary``
    input to the merge builtin template — gives the LLM a readable
    transcript of a single branch without needing multi-turn role
    fidelity (the merger only produces one assistant turn downstream).
    """
    if not msgs:
        return "(empty branch)"
    return "\n\n".join(f"[{m.role}] {m.content}" for m in msgs)


def _build_tagged_chat_context_for_compact(
    chatflow: ChatFlow, parent_id: str
) -> list[tuple[str | None, WireMessage]]:
    """Return ``(chatnode_id, WireMessage)`` pairs for the real-message
    chain rooted at ``parent_id`` — the same set :func:`_build_chat_context`
    produces when called with ``include_summary_preamble=False``, but
    with the originating ChatNode id attached so the compact worker can
    cite them in its summary.

    Preserved-tail messages from a prior compact snapshot are tagged
    with the compact ChatNode's id (they lived there after the prior
    compaction) rather than with whichever pre-compact ChatNode they
    originally came from — that lineage is gone by the time the snapshot
    ships. Non-frozen nodes on the chain are skipped (in-progress
    branches don't enter compaction input).
    """
    chain: list[str] = []
    current: str | None = parent_id
    while current is not None:
        chain.append(current)
        node = chatflow.nodes[current]
        # Merge ChatNodes are a hard stop — same rule as _build_chat_context.
        if len(node.parent_ids) >= 2:
            break
        current = node.parent_ids[0] if node.parent_ids else None
    chain.reverse()

    compact_cutoff_idx: int | None = None
    for i, nid in enumerate(chain):
        node = chatflow.nodes[nid]
        snap = node.compact_snapshot
        if snap is not None and snap.summary:
            compact_cutoff_idx = i

    tagged: list[tuple[str | None, WireMessage]] = []
    start_idx = 0
    if compact_cutoff_idx is not None:
        cutoff_id = chain[compact_cutoff_idx]
        snap = chatflow.nodes[cutoff_id].compact_snapshot
        assert snap is not None
        for m in snap.preserved_messages:
            tagged.append((cutoff_id, m))
        start_idx = compact_cutoff_idx + 1

    for nid in chain[start_idx:]:
        node = chatflow.nodes[nid]
        if not node.is_frozen:
            continue
        if node.compact_snapshot is not None:
            continue
        if node.user_message is not None:
            tagged.append(
                (nid, WireMessage(role="user", content=node.user_message.text))
            )
        tagged.append(
            (nid, WireMessage(role="assistant", content=node.agent_response.text))
        )
    return tagged
