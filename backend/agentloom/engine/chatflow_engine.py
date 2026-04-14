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
    parse_recursive_planner_output,
)
from agentloom.engine.workflow_engine import PostNodeHook, ProviderCall, WorkflowEngine
from agentloom.schemas.common import JudgeVariant, JudgeVerdict, WorkNodeRole
from agentloom.schemas import (
    ChatFlow,
    ChatFlowNode,
    PendingTurn,
    StepKind,
    WorkFlow,
    WorkFlowNode,
)
from agentloom.schemas.chatflow import PendingTurnSource, UpstreamFailurePolicy
from agentloom.schemas.common import (
    EditableText,
    ExecutionMode,
    NodeStatus,
    ProviderModelRef,
    utcnow,
)
from agentloom.schemas.workflow import WireMessage
from agentloom.templates.instantiate import instantiate_fixture
from agentloom.templates.loader import fragments_as_texts, load_fixtures
from agentloom.tools.base import ToolContext, ToolRegistry

log = logging.getLogger(__name__)


#: Placeholder values for the WorkFlow trio's ``inputs`` and
#: ``expected_outcome`` at the moment a turn first lands. We don't yet
#: know what the user *really* expects — judge_pre is exactly the pass
#: that interrogates that — so we seed both with stable stock strings
#: and let the planner overwrite the real values for downstream nodes
#: (worker briefs, sub-WorkFlow trios). Memory recall + skills/MCP
#: injection will eventually replace these — see the
#: agentloom_judge_params_pragma memo.
_STOCK_INPUTS = "(prior conversation context — see messages below)"
_STOCK_EXPECTED_OUTCOME = "Helpful, accurate response to the user's request"


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
    return chatflow.default_model


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

    - ``direct``: pure ReAct. No plan, no judges. Existing M4/M6 shape.
    - ``semi_auto``: plan on, judge_pre on, judge_post on, judge_during
      off (opt-in — adversarial critic is expensive and only helps when
      the user wants it).
    - ``auto``: all four on. Halt conditions (§3.4.1) gate progression.
    """
    if mode == ExecutionMode.DIRECT:
        return ExecutionSwitches(
            plan=False, judge_pre=False, judge_during=False, judge_post=False
        )
    if mode == ExecutionMode.SEMI_AUTO:
        return ExecutionSwitches(
            plan=True, judge_pre=True, judge_during=False, judge_post=True
        )
    # auto
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
        self._inner = WorkflowEngine(
            provider_call,
            event_bus,
            tool_registry=tool_registry,
            tool_context=tool_context,
        )
        self._runtimes: dict[str, ChatFlowRuntime] = {}
        self._registry_lock = asyncio.Lock()

        # Load builtin workflow templates from disk (sync). The engine
        # uses these to materialize judge_pre / judge_post inside
        # ``_spawn_turn_node`` without needing an AsyncSession. Per the
        # judge-params pragma memory, the params we feed are naive
        # stock strings until NodeBaseFields gains a real ``inputs``
        # field; revisit then.
        templates, fragments = load_fixtures()
        self._fixture_plans: dict[str, dict[str, Any]] = {
            fx.builtin_id: fx.plan for fx in templates
        }
        self._fixture_includes: dict[str, str] = fragments_as_texts(fragments)

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
        self, chatflow_id: str, node_id: str
    ) -> ChatFlowNode:
        """Create a sibling of a FAILED node and transfer its queue.

        The failed node stays in place as a dialogue record; the new
        sibling inherits the same parent and user_message, plus the
        failed node's ``pending_queue`` which will walk down the new
        branch as it runs.
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
            sibling = self._spawn_turn_node(
                runtime,
                parent_ids=list(failed.parent_ids),
                user_message_text=failed.user_message.text,
                pending_queue=inherited_queue,
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

        async with runtime.lock:
            if node_id not in runtime.chatflow.nodes:
                return
            node = runtime.chatflow.get(node_id)
            if node.status not in (NodeStatus.RUNNING, NodeStatus.PLANNED):
                return
            node.status = NodeStatus.FAILED
            node.error = "Cancelled by user"
            node.finished_at = utcnow()
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
            )
            await self._publish_node_created(runtime, node.id)
            self._launch_execute(
                runtime, node.id, consumed_pending_id=pending.id
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
        )
        await self._publish_node_created(runtime, child.id)
        await self._publish_queue_updated(runtime, node_id)
        await self._publish_queue_updated(runtime, child.id)
        self._launch_execute(runtime, child.id, consumed_pending_id=pending.id)

    def _spawn_turn_node(
        self,
        runtime: ChatFlowRuntime,
        *,
        parent_ids: list[str],
        user_message_text: str,
        pending_queue: list[PendingTurn],
        spawn_model: ProviderModelRef | None = None,
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
        """
        chatflow = runtime.chatflow
        context_wire = _build_chat_context(chatflow, parent_ids)
        context_wire.append(WireMessage(role="user", content=user_message_text))

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

        mode = chatflow.default_execution_mode
        switches = derive_switches_from_mode(mode)
        inner = WorkFlow(
            execution_mode=mode,
            plan_enabled=switches.plan,
            judge_pre_enabled=switches.judge_pre,
            judge_during_enabled=switches.judge_during,
            judge_post_enabled=switches.judge_post,
            # Seed the WorkFlow trio so judges and the planner all read
            # from the same source. ``inputs`` and ``expected_outcome``
            # are stock placeholders for now (see _STOCK_*); the planner
            # writes real values onto worker / sub-WorkFlow trios as it
            # decomposes. The user's prompt becomes ``description``.
            description=EditableText.by_user(user_message_text),
            inputs=EditableText.by_agent(_STOCK_INPUTS),
            expected_outcome=EditableText.by_agent(_STOCK_EXPECTED_OUTCOME),
            debate_round_budget=chatflow.debate_round_budget,
        )

        if switches.judge_pre:
            # Only the pre-judge runs upfront; the rest of the chain is
            # spawned dynamically once we know the verdict.
            self._spawn_judge_pre(inner, user_message_text, context_wire)
        else:
            inner.add_node(
                WorkFlowNode(
                    step_kind=StepKind.LLM_CALL,
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
        )
        chatflow.add_node(chat_node)
        return chat_node

    # ------------------------------------------------------------- judge spawns
    #
    # Both helpers materialize the corresponding builtin template into a
    # standalone WorkFlow, lift its single judge_call node into the inner
    # WorkFlow, then append the real conversation context after the
    # template-rendered system+user pair so the judge sees the actual
    # exchange — not just the trio.
    #
    # Per the judge-params pragma memory: we feed naive stock strings
    # for ``inputs`` and ``expected_outcome`` because NodeBaseFields
    # doesn't yet carry them as first-class fields. When the schema
    # rework lands, switch to the real values.

    def _spawn_judge_pre(
        self,
        inner: WorkFlow,
        user_message_text: str,  # noqa: ARG002 — kept for symmetry / future logging
        context_wire: list[WireMessage],
    ) -> WorkFlowNode:
        templated = instantiate_fixture(
            self._fixture_plans["judge_pre"],
            _trio_params(inner),
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = []
        node.input_messages = [*(node.input_messages or []), *context_wire]
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
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = [parent_node.id]
        node.judge_target_id = parent_node.id
        node.input_messages = [*(node.input_messages or []), *context_wire]
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
            self._fixture_plans["planner"],
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
            self._fixture_plans["planner_judge"],
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
        return inner.add_node(node)

    def _spawn_worker(
        self,
        inner: WorkFlow,
        parent_node: WorkFlowNode,
        atomic: AtomicBrief,
        *,
        resolved_model: ProviderModelRef | None,
        prior_output: str = "",
        critique: str = "",
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
        return inner.add_node(node)

    def _build_post_node_hook(
        self,
        chat_node: ChatFlowNode,
        chatflow: ChatFlow,
    ) -> "PostNodeHook":
        """Closure that grows the inner DAG dynamically.

        Fired by ``WorkflowEngine`` after every node success. We only
        act on (a) judge_pre completion — decide between the happy path
        (spawn an llm_call) or the halt path (spawn judge_post directly),
        and (b) terminal llm_call completion — when judge_post is
        enabled and the llm_call has no pending tool_uses, attach
        judge_post for the post-mortem.
        """
        switches = derive_switches_from_mode(chat_node.workflow.execution_mode)
        user_message_text = (
            chat_node.user_message.text if chat_node.user_message else ""
        )
        # context_wire is the same shape we'd build at spawn time —
        # rebuild it here so the hook stays a pure closure over chat_node.
        context_wire = _build_chat_context(chatflow, list(chat_node.parent_ids))
        context_wire.append(WireMessage(role="user", content=user_message_text))

        def hook(workflow: WorkFlow, node: WorkFlowNode) -> None:
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
                    if node.role == WorkNodeRole.PLANNER_JUDGE:
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
                # judge_post halt → pending_user_prompt is set inside
                # WorkflowEngine._run_judge_call; nothing to do here.
                return

            if node.step_kind == StepKind.LLM_CALL:
                if node.role == WorkNodeRole.PLANNER:
                    self._after_planner(workflow, node)
                    return
                if node.role == WorkNodeRole.WORKER:
                    # Workers can do tools too — wait for the terminal
                    # llm_call before handing off to the worker_judge.
                    if node.output_message and node.output_message.tool_uses:
                        return
                    self._after_worker(workflow, node)
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
        halt path goes straight to judge_post in halt-summary mode."""
        verdict = judge_pre_node.judge_verdict
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
        if workflow.execution_mode == ExecutionMode.AUTO:
            # Auto mode runs the recursive-planner pipeline:
            #   judge_pre → planner → planner_judge → worker
            #             → worker_judge → judge_post
            # The hook handlers below grow the chain step by step.
            self._spawn_planner(
                workflow,
                judge_pre_node,
                resolved_model=resolved_model,
            )
            return

        # Semi_auto / direct: spawn a plain llm_call; the post-node hook
        # attaches judge_post once it completes (and any tool loop has
        # terminated).
        workflow.add_node(
            WorkFlowNode(
                step_kind=StepKind.LLM_CALL,
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
            self._halt_to_post_judge(
                workflow,
                parent_node=planner_judge_node,
                upstream_kind="planner_parse_error",
                upstream_summary=f"planner output failed to parse: {exc}",
                user_message_text=user_message_text,
                context_wire=context_wire,
            )
            return

        # Continue + atomic: materialize the worker.
        if decision == "continue" and plan.mode == "atomic" and plan.atomic is not None:
            self._spawn_worker(
                workflow,
                planner_judge_node,
                plan.atomic,
                resolved_model=resolved_model,
            )
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

        # Decompose, infeasible, halt, revise-at-budget, or unparseable
        # → judge_post halt. M12.4d will replace decompose; M12.4e will
        # surface budget-exhausted concerns through judge_post itself.
        upstream_summary = _summarize_planner_outcome(decision, plan)
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
    ) -> None:
        """Worker just produced its draft. Attach worker_judge."""
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
        # → judge_post halt. M12.4e will route budget-exhausted concerns
        # through judge_post itself.
        self._halt_to_post_judge(
            workflow,
            parent_node=worker_judge_node,
            upstream_kind="worker_judge_halt",
            upstream_summary=(
                f"worker_judge verdict={decision or 'unparseable'}; "
                f"worker draft: {worker_output}"
            ),
            user_message_text=user_message_text,
            context_wire=context_wire,
        )

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
        try:
            await self._inner.execute(
                chat_node.workflow,
                chatflow_tool_loop_budget=chatflow.tool_loop_budget,
                chatflow_auto_mode_revise_budget=chatflow.auto_mode_revise_budget,
                post_node_hook=self._build_post_node_hook(chat_node, chatflow),
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
            terminal = _terminal_llm_call(chat_node.workflow)
            if runtime_error is not None:
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
            chat_node.finished_at = utcnow()

            if consumed_pending_id is not None:
                fut = runtime.pending_futures.get(consumed_pending_id)
                if fut is not None and not fut.done():
                    fut.set_result(chat_node)

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


def _judge_pre_should_halt(verdict: JudgeVerdict) -> bool:
    """Mirror of :func:`judge_pre_needs_user_input` — re-implemented here
    to keep ``chatflow_engine`` independent of the formatter module."""
    if verdict.feasibility != "ok":
        return True
    if verdict.missing_inputs:
        return True
    return bool(verdict.blockers)


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
    """
    if not worker_node.parent_ids:
        return None
    planner_judge = workflow.nodes.get(worker_node.parent_ids[0])
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
    """Pluck the single node out of a freshly-instantiated judge template.

    Both ``judge_pre`` and ``judge_post`` fixtures define exactly one
    judge_call node. We don't keep the surrounding WorkFlow shell — the
    inner WorkFlow already exists.
    """
    if len(workflow.nodes) != 1:
        raise ValueError(
            f"expected single-node template, got {len(workflow.nodes)} nodes"
        )
    return next(iter(workflow.nodes.values()))


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
    llm_calls = [
        n
        for n in workflow.nodes.values()
        if n.step_kind == StepKind.LLM_CALL
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


def _build_chat_context(chatflow: ChatFlow, parent_ids: list[str]) -> list[WireMessage]:
    """Build the user/assistant message history from the ancestor chain.

    We walk ancestors topologically and, for each frozen ChatFlowNode
    with a ``user_message`` (skipping greeting roots for now), emit a
    ``user`` turn and an ``assistant`` turn. Unfrozen turns are
    ignored (they belong to branches in progress).
    """
    if not parent_ids:
        return []
    # Single-parent chain for now — multi-parent merges arrive post-MVP.
    chain: list[str] = []
    current: str | None = parent_ids[0]
    while current is not None:
        chain.append(current)
        node = chatflow.nodes[current]
        current = node.parent_ids[0] if node.parent_ids else None
    chain.reverse()

    messages: list[WireMessage] = []
    for nid in chain:
        node = chatflow.nodes[nid]
        if not node.is_frozen:
            continue
        if node.user_message is not None:
            messages.append(
                WireMessage(role="user", content=node.user_message.text)
            )
        messages.append(
            WireMessage(role="assistant", content=node.agent_response.text)
        )
    return messages
