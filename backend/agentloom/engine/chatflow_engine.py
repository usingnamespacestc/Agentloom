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
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from agentloom.channels.base import ExternalTurn
from agentloom.engine.events import EventBus, WorkflowEvent
from agentloom.engine.workflow_engine import ProviderCall, WorkflowEngine
from agentloom.schemas import (
    ChatFlow,
    ChatFlowNode,
    PendingTurn,
    StepKind,
    WorkFlow,
    WorkFlowNode,
)
from agentloom.schemas.chatflow import PendingTurnSource, UpstreamFailurePolicy
from agentloom.schemas.common import EditableText, NodeStatus, utcnow
from agentloom.schemas.workflow import WireMessage
from agentloom.tools.base import ToolContext, ToolRegistry

log = logging.getLogger(__name__)

#: Optional persistence hook. When supplied, the engine calls it with
#: the mutated ChatFlow after every state change (turn completion,
#: queue edit, retry, delete). Invoked outside the runtime lock so
#: implementations that acquire their own db session don't deadlock.
SaveCallback = Callable[[ChatFlow], Awaitable[None]]


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
    ) -> ChatFlowNode:
        """Create a PLANNED ChatFlowNode and attach it to the chatflow.

        Seeds the inner WorkFlow with a single ``llm_call`` whose
        ``input_messages`` is the built conversation context plus the
        new user turn appended.
        """
        chatflow = runtime.chatflow
        context_wire = _build_chat_context(chatflow, parent_ids)
        context_wire.append(WireMessage(role="user", content=user_message_text))

        inner = WorkFlow()
        inner.add_node(
            WorkFlowNode(
                step_kind=StepKind.LLM_CALL,
                input_messages=context_wire,
            )
        )

        chat_node = ChatFlowNode(
            parent_ids=list(parent_ids),
            user_message=EditableText.by_user(user_message_text),
            workflow=inner,
            pending_queue=list(pending_queue),
        )
        chatflow.add_node(chat_node)
        return chat_node

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
        inner_wf_id = chat_node.workflow.id
        relay_task = asyncio.create_task(
            self._relay_inner_events(
                chatflow.id, node_id, inner_wf_id
            )
        )

        runtime_error: str | None = None
        try:
            await self._inner.execute(chat_node.workflow)
        except Exception as exc:  # noqa: BLE001 — engine boundary
            log.exception("chat node %s inner workflow raised", node_id)
            runtime_error = f"{type(exc).__name__}: {exc}"
        finally:
            # Stop the relay — inner workflow is done (either
            # succeeded or raised). Close the inner subscription
            # so the relay task terminates cleanly.
            await self._bus.close(inner_wf_id)
            relay_task.cancel()

        async with runtime.lock:
            chat_node = chatflow.get(node_id)
            terminal = _terminal_llm_call(chat_node.workflow)
            if runtime_error is not None:
                chat_node.status = NodeStatus.FAILED
                chat_node.error = runtime_error
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
            async for event in self._bus.subscribe(inner_wf_id):
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


def _terminal_llm_call(workflow: WorkFlow) -> WorkFlowNode | None:
    """Return the leaf llm_call whose output should become the turn's
    ``agent_response``.

    A leaf is a node no other node lists as a parent. We want the leaf
    that is an ``llm_call`` — in a plain one-shot turn the root is the
    only node and is both root and leaf; in a tool loop the sequence
    looks like ``llm → tool → llm (→ tool → llm)*`` and the leaf is
    the final llm_call that produced the user-facing reply after all
    tool results came back.

    If multiple llm_call leaves exist (shouldn't happen on a linear
    tool loop, but might once branching lands), we pick the most
    recently created one — same tiebreaker as ``_latest_leaf``.
    """
    if not workflow.nodes:
        return None
    referenced: set[str] = set()
    for n in workflow.nodes.values():
        referenced.update(n.parent_ids)
    llm_leaves = [
        n
        for n in workflow.nodes.values()
        if n.id not in referenced and n.step_kind == StepKind.LLM_CALL
    ]
    if not llm_leaves:
        return None
    llm_leaves.sort(key=lambda n: n.created_at)
    return llm_leaves[-1]


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
