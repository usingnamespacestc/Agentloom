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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agentloom.channels.base import ExternalTurn
from agentloom.engine.events import EventBus, WorkflowEvent
from agentloom.engine.recursive_planner_parser import (
    AtomicBrief,
    PlannerParseError,
    RecursivePlannerOutput,
    SUBMIT_PLAN_TOOL_NAME,
    SubTask,
    parse_planner_from_tool_args,
    parse_recursive_planner_output,
)
from agentloom import tenancy_runtime
from agentloom.engine.workflow_engine import (
    DEFAULT_COMPACT_TARGET_PCT,
    DEFAULT_COMPACT_TRIGGER_PCT,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_COMPACT_KEEP_RECENT_COUNT,
    PostNodeHook,
    ProviderCall,
    WorkflowEngine,
    _count_text_tokens,
    _estimate_tokens_from_wire,
)
from agentloom.schemas.common import (
    JudgeVariant,
    JudgeVerdict,
    ReconToolCall,
    WorkNodeRole,
)
from agentloom.schemas import (
    ChatFlow,
    ChatFlowNode,
    CompactSnapshot,
    PackSnapshot,
    PendingTurn,
    StepKind,
    WorkFlow,
    WorkFlowNode,
)
from agentloom.schemas.chatflow import (
    CbiEntry,
    CompactPreserveMode,
    InboundContextSegment,
    PendingTurnSource,
    UpstreamFailurePolicy,
)
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
from agentloom.tools.base import (
    SideEffect,
    ToolContext,
    ToolRegistry,
    accessed_scope,
)
from agentloom.tools.node_context import CROSS_CHATFLOW_CAPABILITY

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


def _make_board_reader(
    tool_context: ToolContext | None,
) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
    """Build the :data:`BoardReader` closure that :meth:`WorkflowEngine.
    _render_node_briefs_from_board` (PR A, 2026-04-21) calls when it
    needs judge_post's layer-notes. (flow_brief was retired in the same pass.)

    Same fresh-session pattern as ``_make_board_writer`` — one session
    per read via ``get_session_maker()`` — so we don't have to thread
    a session through the engine's signatures or worry about concurrent
    readers sharing state. Workspace scoping comes from *tool_context*;
    bare engines fall back to ``DEFAULT_WORKSPACE_ID``.
    """
    from agentloom.db.base import get_session_maker
    from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
    from agentloom.db.repositories.board_item import BoardItemRepository

    workspace_id = (
        tool_context.workspace_id if tool_context is not None else DEFAULT_WORKSPACE_ID
    )

    async def read(*, workflow_id: str) -> list[dict[str, Any]]:
        try:
            async with get_session_maker()() as session:
                repo = BoardItemRepository(session, workspace_id=workspace_id)
                rows = await repo.list_by_workflow(workflow_id)
                return [
                    {
                        "source_node_id": row.source_node_id,
                        "source_kind": row.source_kind,
                        "scope": row.scope,
                        "description": row.description,
                        "fallback": row.fallback,
                    }
                    for row in rows
                ]
        except Exception:  # noqa: BLE001 — board is best-effort
            log.exception(
                "BoardItemRepository list_by_workflow failed for workflow=%s",
                workflow_id,
            )
            return []

    return read


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
        inner_chat_ids: list[str] | None = None,
        work_node_ids: list[str] | None = None,
        produced_tags: list[str] | None = None,
        consumed_tags: list[str] | None = None,
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
                    inner_chat_ids=inner_chat_ids,
                    work_node_ids=work_node_ids,
                    produced_tags=produced_tags,
                    consumed_tags=consumed_tags,
                )
                await session.commit()
        except Exception as exc:  # noqa: BLE001 — board is best-effort
            # FK violation on ``chatflow_id`` is the in-memory /
            # not-yet-persisted case — pure-engine integration tests
            # never persist their ChatFlow row, and a brief that
            # races a chatflow DELETE in production hits the same
            # path. Either way the brief is best-effort and the
            # engine still kept the description on the WorkNode, so
            # logging a full traceback is just noise. Other failures
            # (schema mismatch, connection drop, etc.) keep the loud
            # ``log.exception`` so genuine bugs stay visible.
            msg = str(exc)
            if (
                "board_items_chatflow_id_fkey" in msg
                or "violates foreign key constraint" in msg
                and "chatflow" in msg
            ):
                log.debug(
                    "BoardItemRepository upsert skipped: chatflow %s "
                    "not in DB (source=%s scope=%s)",
                    chatflow_id,
                    source_node_id,
                    scope,
                )
            else:
                log.exception(
                    "BoardItemRepository upsert failed for source=%s scope=%s",
                    source_node_id,
                    scope,
                )

    return write


_CHAT_BRIEF_USER_SNIPPET = 120
_CHAT_BRIEF_AGENT_SNIPPET = 200

#: Matches the closing ``(节点: …)`` / ``(nodes: …)`` / ``(已打包:
#: …)`` / ``(packed: …)`` tail that the pack fixture asks every
#: per-node paragraph (detailed-index mode) or the monolithic closing
#: line (use_detailed_index=false) to end with. Used to insert blank
#: lines between paragraphs so markdown renders them as separate
#: paragraphs instead of one run-on line. Non-greedy ``[^)]*`` so it
#: doesn't span across paragraphs when content contains parens.
_PACK_PARA_TAIL_RE = re.compile(
    r"(\([^)]*(?:节点|nodes|已打包|packed)[^)]*\))(?!\s*\n\s*\n)(?=\s*\S)",
    re.IGNORECASE,
)


def _normalize_pack_summary(text: str) -> str:
    """Normalize pack-summary whitespace for readable markdown rendering.

    LLMs (Ark included) reliably put a single ``\\n`` between pack
    paragraphs; markdown collapses single newlines into spaces so the
    UI would show the whole pack as one run-on paragraph. Force
    double-newlines:

    - Between the leading pointer line (``"...get_node_context..."``)
      and the first paragraph.
    - After every per-node citation closing-paren — ``(节点: id)`` or
      ``(nodes: id)`` or the monolithic ``(已打包: …)`` / ``(packed: …)``
      tail — when the next non-blank character is on a following line.

    No-op when the paragraphs are already separated by a blank line.
    Idempotent — running twice yields the same text.
    """
    lines = text.split("\n")
    # First blank line between pointer and body.
    if len(lines) >= 2 and lines[0].strip() and lines[1].strip():
        lines.insert(1, "")
    joined = "\n".join(lines)
    # Then the per-paragraph separators.
    return _PACK_PARA_TAIL_RE.sub(r"\1\n", joined)


#: Synthetic user turn injected before the greeting root's assistant
#: response so the wire never starts with role="assistant". Many
#: chat_templates (Qwen / Llama / Mistral on llama.cpp, strict
#: OpenAI-compat shims) reject or mis-render a conversation whose
#: first message is assistant. Greeting roots have ``user_message=None``
#: by construction; this anchor fills that hole without changing the
#: visible ChatNode tree — it appears in ``_build_chat_context`` output
#: only, the UI preview and the persisted ChatFlow are unaffected.
_GREETING_ANCHOR_USER_CONTENT = "Hello"


def _first_line(text: str) -> str:
    """Grab the first non-empty line of *text* (stripped)."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _chat_board_source_kind(node: ChatFlowNode) -> str:
    """Classify a finished ChatNode into a MemoryBoard ``source_kind``.

    Tiered rule, matching ``_build_chat_context``:
    - ``pack_snapshot`` (a user-initiated mid-chain pack) is the most
      specific classification and outranks both others. Pack and
      compact are mutually exclusive on any ChatNode (validator in
      ``schemas.chatflow``).
    - A populated ``compact_snapshot`` outranks the structural merge
      test because the two are mutually exclusive in practice (compact
      always has exactly one parent, merge always two).
    - Multi-parent → ``"chat_merge"``.
    - Plain turn nodes fall through to ``"chat_turn"``.
    """
    if node.pack_snapshot is not None:
        return "chat_pack"
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
        preserved = len(snap.preserved_messages) if snap is not None else 0
        summary_snippet = agent_snippet or "(empty summary)"
        return (
            f"compacted prior chain into a summary "
            f"(+{preserved} preserved verbatim): {summary_snippet}"
        ).rstrip(": ")
    if kind == "chat_pack":
        psnap = node.pack_snapshot
        range_size = len(psnap.packed_range) if psnap is not None else 0
        summary_snippet = agent_snippet or "(empty summary)"
        return (
            f"packed {range_size} ChatNode(s) into a mid-chain summary: "
            f"{summary_snippet}"
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


def _collect_inner_chat_ids(node: ChatFlowNode) -> list[str] | None:
    """Drill-down ChatNode ids this ChatNode aggregates over.

    - ``chat_pack`` → full ``packed_range``.
    - ``chat_merge`` → parent ids (the heads of the merged branches).
    - ``chat_compact`` → single-hop primary parent. Compact implicitly
      covers the whole root→here prefix; we deliberately store only one
      hop so drill-down recurses (next layer is whatever that parent
      was — turn/pack/compact/merge — and its own BoardItem advertises
      *its* drill-down). Storing the full prefix would balloon the
      column on long chains and make pack/merge inconsistent.
    - plain turn → ``None`` (no aggregation).
    """
    if node.pack_snapshot is not None:
        return list(node.pack_snapshot.packed_range) or None
    if len(node.parent_ids) >= 2:
        return list(node.parent_ids)
    if node.compact_snapshot is not None:
        return list(node.parent_ids[:1]) or None
    return None


def _collect_work_node_ids(node: ChatFlowNode) -> list[str] | None:
    """WorkNode ids inside *node*'s WorkFlow that have a node-scope brief.

    Filter rule: pick the parent of every successful ``BRIEF`` WorkNode
    whose ``scope`` is ``NODE``. That's exactly the set of WorkNodes
    a WorkBoardItem was written for — using "is the brief there?" as
    the gate avoids chasing a separate "noteworthy node" criterion
    and keeps the drill-down map in sync with what the MemoryBoard
    actually indexes.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for wn in node.workflow.nodes.values():
        if wn.step_kind != StepKind.BRIEF:
            continue
        if wn.scope != NodeScope.NODE:
            continue
        if wn.status != NodeStatus.SUCCEEDED:
            continue
        if not wn.parent_ids:
            continue
        src_id = wn.parent_ids[0]
        if src_id in seen:
            continue
        seen.add(src_id)
        ids.append(src_id)
    return ids or None


#: Built-in default for ``ChatFlow.runtime_environment_note`` — prepended
#: to every tool-bearing LLM call to push back on the "I don't have access
#: to your filesystem" hallucination some general-purpose models fall into
#: under long prompts. ChatFlowEngine selects the zh-CN vs en-US variant
#: based on the workspace fixture language. Users override per-chatflow
#: via the field on :class:`ChatFlow`.
_DEFAULT_RUNTIME_NOTE_ZH = (
    "[运行环境]\n"
    "你运行在 Agentloom 工作流引擎里，是真实工具调用 agent，不是云端对话助手。\n"
    "\n"
    "- 你的可用工具列表已在系统消息的 tools 字段里声明，这些工具**真实可调**——"
    "发起工具调用后引擎会真的执行（读文件 / 写文件 / 查询 / ...）并把结果以 "
    "tool message 返回。\n"
    "- 不论你的角色是 worker / judge / monitor / brief，需要外部信息或副作用时"
    "**直接发起工具调用**，不要回复\"我无访问权限\"、\"请粘贴文件内容\"、"
    "\"作为 AI 我无法...\"。这些回复在本环境里是错误行为。\n"
    "- 工具调用失败时，错误信息会通过 tool_result 返回；按错误调整后续步骤，"
    "不要因单次失败放弃任务。"
)

_DEFAULT_RUNTIME_NOTE_EN = (
    "[Runtime Environment]\n"
    "You are running inside the Agentloom workflow engine — a real "
    "tool-calling agent, not a cloud chat assistant.\n"
    "\n"
    "- The tools declared in the system `tools` field are **real and "
    "callable**. When you emit a tool call, the engine will actually "
    "execute it (read, write, search, ...) and return the result as a "
    "tool message.\n"
    "- Regardless of your role (worker / judge / monitor / brief), "
    "when the task needs external information or side effects, **issue "
    "a tool call**. Do NOT reply \"I don't have access\", \"please "
    "paste the file contents\", or \"as an AI I cannot ...\". Those "
    "responses are errors in this environment.\n"
    "- Tool failures arrive as tool_result with an error message. "
    "Adapt to the error; don't abandon the task on a single failure."
)


_DRILL_DOWN_NUDGE_ZH = (
    "[历史回查提示]\n"
    "本对话存在 compact / pack 节点（早期内容已被压缩或打包成摘要）。"
    "当用户询问被压缩/打包内容里的**具体细节**——确切数字、特定名称、"
    "原话引用、参数设置、之前提到过的事实——**请调用 ``get_node_context``"
    " 工具按节点 id 取回原文**。pack/compact 摘要里以 ``[node:xxx]`` "
    "或 ``(节点: xxx)`` 形式注明的就是源节点 id，把它当作 ``node_id``"
    " 参数传入。\n"
    "- 不要凭对摘要的记忆作答——摘要是 lossy 的，可能丢失你需要回答的"
    "具体细节。\n"
    "- 不要因为节点 id 看起来像内部技术细节就回避使用——这是引擎为你"
    "提供的合法检索手段。\n"
    "- 一次取回单个节点；如果不确定哪个节点含答案，先按 summary 提到的"
    "节点 id 取回最相关那个，再决定是否需要更多。"
)

_DRILL_DOWN_NUDGE_EN = (
    "[Historical retrieval hint]\n"
    "This conversation contains compact / pack ChatNodes — earlier "
    "content has been condensed into summaries. When the user asks "
    "about **specific details** within the compacted/packed range — "
    "exact numbers, specific names, verbatim quotes, parameter "
    "settings, facts mentioned earlier — **call ``get_node_context`` "
    "with the source node id**. The pack/compact summary cites source "
    "ids in the form ``[node:xxx]`` or ``(nodes: xxx)``; pass that id "
    "verbatim as the ``node_id`` argument.\n"
    "- Do NOT answer from your memory of the summary alone — summaries "
    "are lossy and may drop the specific detail the user is asking for.\n"
    "- Do NOT avoid using node ids because they look like internal "
    "implementation details — they are the engine's intended retrieval "
    "handle for your use.\n"
    "- Fetch one node at a time; if you're unsure which node holds the "
    "answer, retrieve the most relevant one cited in the summary first, "
    "then decide if you need more."
)


def _drill_down_nudge_if_needed(chatflow, language: str | None) -> str:
    """Return the drill-down recall nudge IFF this chatflow has at
    least one compact / pack ChatNode. Skips silently otherwise so we
    don't bloat every chatflow's system prompt with irrelevant
    instructions.

    Why a separate hint vs cramming into the default runtime note:
    most chatflows will never accumulate enough turns to trigger
    compact/pack — adding the recall nudge unconditionally wastes
    ~150 tokens on every LLM call across the workflow. Keying it on
    "has compact/pack ancestor in the chat-layer DAG" makes it pay
    its own way: appears only when the situation it addresses
    actually exists.

    Trigger: any ChatNode with non-null ``compact_snapshot`` or
    ``pack_snapshot``. Independent of whether the *current* turn is
    a child of one — even if the user happens to be on a sibling
    branch right now, switching back to the compacted branch later
    in the conversation should keep the hint active.
    """
    if not chatflow:
        return ""
    has_compact_or_pack = any(
        n.compact_snapshot is not None or n.pack_snapshot is not None
        for n in chatflow.nodes.values()
    )
    if not has_compact_or_pack:
        return ""
    is_zh = (language or "").lower().startswith("zh")
    return _DRILL_DOWN_NUDGE_ZH if is_zh else _DRILL_DOWN_NUDGE_EN


def _render_runtime_system_info(
    language: str,
    disabled_tool_names: list[str] | frozenset[str],
) -> str:
    """Dynamic OS / shell / cwd hint appended to the runtime note.

    Cache-friendly by design: nothing here changes per LLM call —
    OS / shell strings are stable across the backend process; the
    Bash-disabled flag only flips when the user edits chatflow tool
    settings (rare). No timestamp is included, deliberately, so the
    KV-cache prefix stays warm across calls within a session.
    """
    import os
    import platform

    os_name = platform.platform(terse=True)
    shell = (
        os.environ.get("SHELL")
        or os.environ.get("ComSpec")
        or "/bin/sh"
    )
    bash_disabled = "Bash" in (disabled_tool_names or [])
    is_zh = (language or "").lower().startswith("zh")
    if is_zh:
        bash_note = "（Bash 工具当前禁用，路径用 POSIX 风格）" if bash_disabled else ""
        return (
            "[系统信息]\n"
            f"- OS: {os_name}\n"
            f"- Shell: {shell}{bash_note}\n"
            "- ChatFlow 没有绑定特定 cwd——文件路径请使用绝对路径"
        )
    bash_note = " (Bash tool currently disabled; use POSIX-style paths)" if bash_disabled else ""
    return (
        "[System Info]\n"
        f"- OS: {os_name}\n"
        f"- Shell: {shell}{bash_note}\n"
        "- ChatFlow has no bound cwd — use absolute paths for files"
    )


#: Tag suffixes that mark a concept as no-longer-active. Matches the
#: ``concept_status`` syntax pinned in the brief fixture system
#: prompts. Used when computing ``ancestral_tags_active`` so a concept
#: that was rejected upstream isn't surfaced as an anchor for the
#: current ChatNode's brief.
_DEAD_TAG_STATUSES: set[str] = {"rejected", "deferred"}
_KNOWN_TAG_STATUSES: set[str] = {
    "rejected",
    "approved",
    "deferred",
    "revived",
    "finalized",
}


def _split_tag_status(tag: str) -> tuple[str, str | None]:
    """Decompose a possibly-status-suffixed tag into ``(base, status)``.

    Convention (set in chat_brief / node_brief fixtures): a status
    suffix is the LAST underscore-delimited segment when it matches
    one of :data:`_KNOWN_TAG_STATUSES`. Other underscores are part of
    a multi-word base concept (``retrieval_augmented_generation`` →
    base = ``retrieval_augmented_generation``, status = None).
    """
    if "_" not in tag:
        return tag, None
    base, _, last = tag.rpartition("_")
    if last in _KNOWN_TAG_STATUSES:
        return base, last
    return tag, None


def _walk_chat_chain_to_root(
    chatflow: ChatFlow, node: ChatFlowNode
) -> list[NodeId]:
    """Walk parent chain(s) from *node* up to root, returning ids in
    root-first order. Excludes *node* itself — only ancestors are
    returned (the brief's own produced_tags are what we're computing).

    For ordinary nodes (1 parent) this is a linear walk via
    ``parent_ids[0]``. For **merge** nodes (≥2 parents) we union both
    parent chains so the brief's ``ancestral_tags_active`` reflects
    concepts from BOTH branches — without this, the merge brief sees
    only LEFT's anchors and misclassifies RIGHT's concepts as
    "produced" (they're aggregated-not-introduced).
    """
    if not node.parent_ids:
        return []

    seen: set[NodeId] = set()
    # BFS from each parent so we collect every ancestor (across
    # multiple parent chains for merge / future multi-parent nodes).
    # We don't preserve strict chronological order across branches,
    # but ``_aggregate_active_tags`` only cares about the multiset
    # of (concept, status) pairs — its dict update treats merge
    # ordering as "latest within each branch wins" which is fine.
    chain: list[NodeId] = []
    queue: list[NodeId] = [pid for pid in node.parent_ids if pid]
    while queue:
        cursor = queue.pop(0)
        if cursor in seen:
            continue
        seen.add(cursor)
        chain.append(cursor)
        ancestor = chatflow.nodes.get(cursor)
        if ancestor is None:
            continue
        for pid in ancestor.parent_ids or ():
            if pid and pid not in seen:
                queue.append(pid)
    chain.reverse()  # root-first (approximate for multi-parent nodes)
    return chain


def _aggregate_active_tags(
    chain_tags: list[list[str]],
    *,
    max_anchors: int = 12,
) -> list[str]:
    """Walk per-ancestor produced_tags lists in chronological order
    (root first, leaf-parent last) and return the active anchor set.

    Active = base concept whose latest observed status isn't dead
    (``_rejected`` / ``_deferred``). A later ``plan_x_revived`` revives
    the concept that an earlier ``plan_x_rejected`` killed; the dict
    update preserves this naturally because we walk oldest-to-newest
    and "latest wins".

    Capped at the most-recent ``max_anchors`` to keep the brief prompt
    bounded (long chains can accumulate dozens of concepts; we want a
    focused anchor list).
    """
    concept_status: dict[str, str | None] = {}
    for tags in chain_tags:
        for tag in tags:
            base, status = _split_tag_status(tag)
            concept_status[base] = status
    active = [c for c, s in concept_status.items() if s not in _DEAD_TAG_STATUSES]
    if len(active) > max_anchors:
        active = active[-max_anchors:]
    return active


def _drill_down_footer(
    language: str,
    inner_chat_ids: list[str] | None,
    work_node_ids: list[str] | None,
) -> str:
    """Render the drill-down footer text appended to a ChatBoardItem
    description so the LLM reading the board sees the ids it can pass
    to ``get_node_context`` for the next layer.

    Returns ``""`` (no separator, no header) when there's nothing to
    drill into — caller can append unconditionally.
    """
    has_inner = bool(inner_chat_ids)
    has_work = bool(work_node_ids)
    if not has_inner and not has_work:
        return ""
    is_zh = (language or "").lower().startswith("zh")
    lines: list[str] = []
    if is_zh:
        lines.append(
            f"[drill-down: 内含 {len(inner_chat_ids) if has_inner else 0} 个 "
            f"ChatNode / {len(work_node_ids) if has_work else 0} 个 WorkNode；"
        )
        if has_inner:
            lines.append(f" ChatNode: {', '.join(inner_chat_ids)}")
        if has_work:
            lines.append(f" WorkNode: {', '.join(work_node_ids)}")
        lines.append(" 详情请调 get_node_context(node_id='<id>')]")
    else:
        lines.append(
            f"[drill-down: {len(inner_chat_ids) if has_inner else 0} inner "
            f"ChatNode(s) / {len(work_node_ids) if has_work else 0} WorkNode(s);"
        )
        if has_inner:
            lines.append(f" ChatNode: {', '.join(inner_chat_ids)}")
        if has_work:
            lines.append(f" WorkNode: {', '.join(work_node_ids)}")
        lines.append(" Call get_node_context(node_id='<id>') for details.]")
    return "\n".join(lines)


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
            board_reader=_make_board_reader(tool_context),
        )
        self._runtimes: dict[str, ChatFlowRuntime] = {}
        self._registry_lock = asyncio.Lock()
        #: Per-chatflow turn-submission lock. The engine's `ChatFlow`
        #: runtime object is shared across concurrent submit_turn /
        #: enqueue / merge / compact_chain handlers; their `repo.save`
        #: phases interleave on the same in-memory state and trip
        #: `_assert_frozen_chatflow_nodes_unchanged` (any frozen node
        #: that the engine wrote to during the other handler's window —
        #: e.g. `sticky_restored` not in `_FROZEN_EXEMPT_FIELDS`, or
        #: `child_ids` mutation on fork — looks "modified" to the
        #: second save's prior-vs-new dump comparison). Until the
        #: engine's mutation paths are reworked for true concurrency,
        #: serialize whole submissions per chatflow id.
        #:
        #: Lock allocation is itself protected by `_registry_lock`
        #: (acquire via `submit_lock(cf_id)`).
        self._submit_locks: dict[str, asyncio.Lock] = {}

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

    def _resolve_runtime_note(self, chatflow: ChatFlow) -> str:
        """Build the final runtime-environment note prepended to every
        tool-bearing LLM call inside this chatflow's WorkFlow runs.

        Combines the user-editable static text (or the workspace-language
        default if the chatflow's field is ``None``) with a dynamic
        OS/shell/cwd hint. Both halves are cache-friendly: the static
        text is stable until the user edits it; the dynamic block has
        no per-call variability (no timestamps, no per-turn ids).
        Returns an empty string when the user explicitly cleared their
        note **and** no system info is available — the engine treats
        empty as "skip the prepend".
        """
        lang = self._current_fixture_language
        is_zh = (lang or "").lower().startswith("zh")
        static = chatflow.runtime_environment_note
        if static is None:
            static = _DEFAULT_RUNTIME_NOTE_ZH if is_zh else _DEFAULT_RUNTIME_NOTE_EN
        static = (static or "").strip()
        sysinfo = _render_runtime_system_info(lang, chatflow.disabled_tool_names)
        # Drill-down nudge: when this chatflow already has compact / pack
        # ChatNodes in it, downstream turns may need to retrieve verbatim
        # content from the compacted/packed ranges. Without an explicit
        # instruction, weak models tend to answer from the summary alone
        # (observed 2026-04-26 night: qwen36 in pack demo gave 0
        # ``get_node_context`` calls when asked for an earlier specific
        # detail). Inject the prescriptive hint only when the trigger
        # condition is met — we don't want to bloat every chatflow's
        # system prompt unconditionally.
        drill_hint = _drill_down_nudge_if_needed(chatflow, lang)
        parts = [p for p in (static, sysinfo, drill_hint) if p]
        return "\n\n".join(parts)

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

    async def submit_lock(self, chatflow_id: str) -> asyncio.Lock:
        """Return the per-chatflow submission lock, creating it on first
        use. Callers wrap the entire submit-turn flow (load → engine →
        save) in ``async with await engine.submit_lock(cf_id):`` so
        concurrent submissions for the same chatflow serialize. See
        ``_submit_locks`` field doc for the underlying rationale.
        """
        async with self._registry_lock:
            lock = self._submit_locks.get(chatflow_id)
            if lock is None:
                lock = asyncio.Lock()
                self._submit_locks[chatflow_id] = lock
            return lock

    def get_runtime(self, chatflow_id: str) -> ChatFlowRuntime | None:
        return self._runtimes.get(chatflow_id)

    def active_chatflow_ids(self) -> set[str]:
        """Return chatflow ids whose runtime currently has at least one
        non-done scheduler task. Narrower than :meth:`attached_chatflow_ids`
        — kept for callers that explicitly want "is there work in
        flight right now". Most callers want
        :meth:`attached_chatflow_ids` instead."""
        return {
            cf_id
            for cf_id, rt in self._runtimes.items()
            if any(not t.done() for t in rt.active_tasks)
        }

    def attached_chatflow_ids(self) -> set[str]:
        """Return chatflow ids that currently have an attached runtime.

        Used by the orphan watchdog as the skip-set: once a chatflow is
        in the runtime registry, the engine owns its in-flight nodes
        across the full lifecycle — including the transient gaps
        between asyncio tasks (e.g. while a turn is mid-LLM-call but
        the immediate scheduler task has briefly returned to wait on
        a child). :meth:`active_chatflow_ids` is too narrow for that
        skip-set: it misses those gaps, the watchdog then writes
        ``status=failed`` rows to DB, and when the turn eventually
        completes the engine's later ``save`` trips frozen-guard
        because the in-memory ``status=succeeded`` doesn't match the
        DB-snapshotted ``status=failed`` — observed 2026-04-25 on the
        v6 qwen36 run, where multi-minute auto_plan turns hit the gap
        repeatedly. Detach is the formal lifecycle exit; everything
        before that is engine-owned."""
        return set(self._runtimes.keys())

    async def detach(self, chatflow_id: str, *, cancel: bool = False) -> None:
        async with self._registry_lock:
            runtime = self._runtimes.pop(chatflow_id, None)
            self._submit_locks.pop(chatflow_id, None)
        if runtime is None:
            return
        if cancel:
            for t in list(runtime.active_tasks):
                t.cancel()
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

    async def _await_ancestor_briefs(
        self, chatflow: ChatFlow, primary_parent_id: str
    ) -> None:
        """Strict gate: every ancestor ChatNode above ``primary_parent_id``
        on the primary-parent chain must have its scope='chat'
        ChatBoardItem row committed before compaction proceeds.

        Skips ``primary_parent_id`` itself — the preserved tail keeps
        that turn verbatim, so its board brief isn't load-bearing for
        the summary. Every node older than that has its content folded
        into the summary preamble; a missing brief there means the
        summarizer would skip over a node whose chat-board description
        should have fed into downstream retrieval, which hurts recall
        more than a visible failure does.

        Raises ``ValueError`` the moment an ancestor is found without a
        scope='chat' BoardItemRow. No poll/retry — the brief writer
        runs under the runtime lock today, so a missing row indicates
        the writer genuinely failed, not a race; surfacing that is the
        point.

        No-ops silently when the engine was constructed without a
        ``tool_context`` (unit-test engines). That setup never writes
        real brief rows to the DB — the closure still exists but its
        FK target isn't there — so gating on them would break every
        compact-path test that didn't stand up a full tenancy.
        """
        if self._tool_ctx is None:
            return
        # Walk strictly older than primary_parent_id (skip it).
        primary = chatflow.nodes.get(primary_parent_id)
        if primary is None or not primary.parent_ids:
            return
        missing: list[str] = []
        current: str | None = primary.parent_ids[0]
        from agentloom.db.base import get_session_maker
        from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
        from agentloom.db.repositories.board_item import BoardItemRepository

        workspace_id = (
            self._tool_ctx.workspace_id
            if self._tool_ctx is not None
            else DEFAULT_WORKSPACE_ID
        )
        async with get_session_maker()() as session:
            repo = BoardItemRepository(session, workspace_id=workspace_id)
            while current is not None:
                node = chatflow.nodes.get(current)
                if node is None:
                    break
                # Greeting root and compact ChatNodes whose inner brief
                # writer path didn't run are the two skip cases. The
                # greeting root is pre-seeded by ``make_chatflow`` and
                # never goes through ``_execute_node``, so no brief
                # exists — identify it by a missing ``user_message``
                # (real turns always carry one). Compact nodes do run
                # the brief writer, so they're included.
                if node.user_message is None and node.compact_snapshot is None:
                    if len(node.parent_ids) >= 2:
                        break
                    current = node.parent_ids[0] if node.parent_ids else None
                    continue
                row = await repo.get_by_source(current)
                if row is None or row.scope != "chat":
                    missing.append(current)
                # Merge stop: same rule as ``_build_chat_context``.
                if len(node.parent_ids) >= 2:
                    break
                current = node.parent_ids[0] if node.parent_ids else None
        if missing:
            raise ValueError(
                "compact blocked — missing scope='chat' ChatBoardItem for "
                f"ancestor(s) {missing!r} of primary parent {primary_parent_id!r}. "
                "Wait for the brief writer to commit before retrying compaction."
            )

    async def _fetch_chat_board_descriptions(
        self, chatflow_id: str
    ) -> dict[str, str]:
        """Return ``{source_node_id: description}`` for every scope='chat'
        BoardItemRow in the given ChatFlow.

        Used by the runtime context builder to embed a per-ancestor
        ChatBoardItem recap in the compact summary preamble (see
        ``_build_chat_context`` for the embedding rules). Returns an
        empty dict when the engine has no ``tool_context`` (unit-test
        engines — no tenancy available) or when the DB round-trip
        fails. CBI embedding is best-effort so a repo hiccup doesn't
        prevent the turn from running.
        """
        if self._tool_ctx is None:
            return {}
        from agentloom.db.base import get_session_maker
        from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
        from agentloom.db.repositories.board_item import BoardItemRepository

        workspace_id = (
            self._tool_ctx.workspace_id
            if self._tool_ctx is not None
            else DEFAULT_WORKSPACE_ID
        )
        try:
            async with get_session_maker()() as session:
                repo = BoardItemRepository(session, workspace_id=workspace_id)
                rows = await repo.list_by_chatflow(chatflow_id)
        except Exception:  # noqa: BLE001 — CBI lookup is best-effort
            log.exception(
                "CBI fetch failed for chatflow=%s — compact preamble will "
                "fall back to the bare summary",
                chatflow_id,
            )
            return {}
        return {
            row.source_node_id: row.description
            for row in rows
            if row.scope == "chat"
        }

    def _build_compact_chatnode(
        self,
        chatflow: ChatFlow,
        *,
        parent_id: str,
        preserve_recent_turns: int,
        preserve_mode: CompactPreserveMode,
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

        # by_budget defers tail selection to after the summary is known
        # (the finalizer in ``_finalize_compact_chatnode_snapshot`` does
        # the greedy pack). At build time we feed the entire chain to the
        # summarizer (keep=0) and leave preserved_messages empty on the
        # snapshot.
        #
        # Preservation granularity is a ChatNode (one conversational
        # turn = user_message + agent_response + any tool-use traffic).
        # We group the flat tagged stream into per-node groups and keep
        # the last N groups intact — never split a turn across the
        # summary/preserved boundary.
        groups_full = _group_tagged_by_chatnode(tagged_full_real)
        if preserve_mode == "by_budget":
            keep_groups = 0
        else:
            keep_groups = max(0, min(len(groups_full), preserve_recent_turns))
        head_groups = groups_full[:-keep_groups] if keep_groups else groups_full
        tail_groups = groups_full[-keep_groups:] if keep_groups else []
        head_tagged = [
            (nid, m) for nid, msgs in head_groups for m in msgs
        ]
        tail_wire = [m for _, msgs in tail_groups for m in msgs]
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

        prev_summary_tokens = (
            _count_text_tokens(previous_summary) if previous_summary else 0
        )
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
            ),
        )

    def _finalize_compact_chatnode_snapshot(
        self,
        chatflow: ChatFlow,
        compact_node: ChatFlowNode,
        summary: str,
        resolved_for_cap: ProviderModelRef | None,
    ) -> str:
        """Apply the ``chatnode_compact_target_pct`` cap + fill in the
        preserved-tail per ``compact_preserve_mode``. Mutates
        ``compact_node.compact_snapshot`` and returns the (possibly
        truncated) summary so the caller can also stash it on
        ``agent_response``.

        - ``by_count``: the verbatim tail was already baked onto the
          snapshot at build time; this pass only touches the summary.
        - ``by_budget``: summary is safety-capped, then tail messages
          are greedy-packed from the pre-compact chain into whatever
          token budget is left under ``target_pct × context_window``.
        """
        from agentloom.engine.workflow_engine import _truncate_text_to_tokens

        ctx_for_cap = self._inner._context_window_for(resolved_for_cap)
        target_cap = max(
            256, int(ctx_for_cap * chatflow.chatnode_compact_target_pct)
        )
        summary_tokens = _count_text_tokens(summary)
        if summary_tokens > target_cap:
            log.warning(
                "compact ChatNode %s summary %d tokens > target %d — truncating",
                compact_node.id,
                summary_tokens,
                target_cap,
            )
            summary = _truncate_text_to_tokens(summary, target_cap)
            summary_tokens = _count_text_tokens(summary)
        snap = compact_node.compact_snapshot
        if snap is None:
            return summary
        if chatflow.compact_preserve_mode == "by_budget":
            preserved_wire: list = []
            if compact_node.parent_ids:
                tagged_full = _build_tagged_chat_context_for_compact(
                    chatflow, compact_node.parent_ids[0]
                )
                groups = _group_tagged_by_chatnode(tagged_full)
                remaining = target_cap - summary_tokens
                preserved_wire = _greedy_pack_chatnode_groups_within_budget(
                    groups, remaining
                )
            compact_node.compact_snapshot = snap.model_copy(
                update={
                    "summary": summary,
                    "preserved_messages": preserved_wire,
                }
            )
        else:
            compact_node.compact_snapshot = snap.model_copy(
                update={"summary": summary}
            )
        return summary

    # --------------------------------------------------------------- pack (ChatFlow-layer mid-chain)

    def _build_pack_chatnode(
        self,
        chatflow: ChatFlow,
        *,
        packed_range: list[str],
        use_detailed_index: bool,
        preserve_last_n: int,
        target_tokens: int | None,
        model: ProviderModelRef | None,
        pack_instruction: str,
        must_keep: str,
        must_drop: str,
    ) -> ChatFlowNode:
        """Construct an unattached pack ChatFlowNode over a validated
        mid-chain range. Mirrors :meth:`_build_compact_chatnode` but
        takes an explicit ``packed_range`` instead of walking the
        primary-parent chain to root.

        Parent_ids is set to the last packed ChatNode; the explicit
        range (including nested packs / compacts) is gathered into the
        prompt and a pre-stub ``pack_snapshot`` is stamped so the
        caller can execute the inner workflow and finalize the summary.
        """
        ordered = _validate_chat_packed_range(chatflow, packed_range)
        last_id = ordered[-1]

        # Carve off the verbatim tail at ChatNode granularity so a
        # user turn + its assistant reply stay together. preserve_last_n
        # is counted in ChatNodes, not messages.
        keep = max(0, min(len(ordered), preserve_last_n))
        head_ids = ordered[:-keep] if keep else list(ordered)
        tail_ids = ordered[-keep:] if keep else []

        head_tagged = _gather_chat_pack_range_messages(chatflow, head_ids)
        tail_wire: list[WireMessage] = []
        for _nid, m in _gather_chat_pack_range_messages(chatflow, tail_ids):
            tail_wire.append(m)
        full_wire = [m for _nid, m in _gather_chat_pack_range_messages(chatflow, ordered)]

        if not head_tagged:
            raise PackRangeError(
                "nothing to pack — preserve_last_n covers the entire range"
            )

        head_serialized = "\n".join(
            f"[node:{nid or '?'} | {m.role}] {m.content}"
            for nid, m in head_tagged
        )

        resolved_target = (
            target_tokens
            if target_tokens is not None
            else max(512, int(4096 * DEFAULT_COMPACT_TARGET_PCT))
        )

        templated = instantiate_fixture(
            self._fixture_plans["pack"],
            {
                "messages_to_pack": head_serialized,
                "packed_node_ids": ", ".join(ordered),
                "target_tokens": resolved_target,
                "use_detailed_index": str(bool(use_detailed_index)).lower(),
                "must_keep": must_keep,
                "must_drop": must_drop,
                "pack_instruction": pack_instruction,
            },
            includes=self._fixture_includes,
        )

        inner_node = _single_node(templated)
        if model is not None:
            inner_node.model_override = model
            inner_node.resolved_model = model

        entry_tokens = _estimate_tokens_from_wire(full_wire)

        return ChatFlowNode(
            parent_ids=[last_id],
            user_message=(
                EditableText.by_user(pack_instruction)
                if pack_instruction
                else None
            ),
            agent_response=EditableText.by_agent(""),
            workflow=templated,
            status=NodeStatus.PLANNED,
            entry_prompt_tokens=entry_tokens,
            pack_snapshot=PackSnapshot(
                summary="",
                packed_range=list(ordered),
                use_detailed_index=use_detailed_index,
                preserve_last_n=keep,
                preserved_messages=list(tail_wire),
            ),
        )

    async def pack_chain_range(
        self,
        chatflow_id: str,
        *,
        packed_range: list[str],
        use_detailed_index: bool = True,
        preserve_last_n: int = 0,
        pack_instruction: str = "",
        must_keep: str = "",
        must_drop: str = "",
        target_tokens: int | None = None,
        model: ProviderModelRef | None = None,
    ) -> ChatFlowNode:
        """User-initiated mid-chain pack.

        Creates a pack ChatNode hanging off ``packed_range[-1]``, runs
        its inner workflow (which invokes the ``pack.yaml`` fixture to
        produce the summary), writes the summary onto
        ``pack_snapshot.summary`` + ``agent_response``, and returns the
        frozen pack node.

        Pre-pack / global-canvas views still see the packed range
        unchanged; only the pack node itself and anything downstream
        of it see the summary substitution (enforced at read time in
        :func:`_build_chat_context`).
        """
        runtime = self._require_runtime(chatflow_id)

        async with runtime.lock:
            chatflow = runtime.chatflow
            # Strict brief-sync gate (mirrors compact_chain): the pack
            # worker cites ChatNode ids from its brief, so we must not
            # pack a node whose brief hasn't landed yet.
            await self._await_ancestor_briefs(chatflow, packed_range[-1])
            pack_node = self._build_pack_chatnode(
                chatflow,
                packed_range=packed_range,
                use_detailed_index=use_detailed_index,
                preserve_last_n=preserve_last_n,
                target_tokens=target_tokens,
                model=model,
                pack_instruction=pack_instruction,
                must_keep=must_keep,
                must_drop=must_drop,
            )
            chatflow.add_node(pack_node)
            pack_id = pack_node.id

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.created",
                node_id=pack_id,
                data={
                    "parent_id": packed_range[-1],
                    "pack": True,
                },
            )
        )
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.status",
                node_id=pack_id,
                data={"status": NodeStatus.RUNNING.value},
            )
        )

        async with runtime.lock:
            pack_node = chatflow.get(pack_id)
            pack_node.status = NodeStatus.RUNNING
            pack_node.started_at = utcnow()

        inner_wf_id = pack_node.workflow.id
        relay_queue = self._bus.open_subscription(inner_wf_id)
        relay_task = asyncio.create_task(
            self._relay_inner_events(
                chatflow.id, pack_id, inner_wf_id, relay_queue
            )
        )

        runtime_error: str | None = None
        ws_settings = tenancy_runtime.get_settings(self._inner._tool_ctx.workspace_id)
        effective_disabled = (
            frozenset(chatflow.disabled_tool_names)
            | frozenset(ws_settings.globally_disabled())
            | self._foreign_tau_tools(chatflow.id)
        )
        try:
            await self._inner.execute(
                pack_node.workflow,
                chatflow_tool_loop_budget=chatflow.tool_loop_budget,
                chatflow_auto_mode_revise_budget=chatflow.auto_mode_revise_budget,
                chatflow_min_ground_ratio=None,
                chatflow_ground_ratio_grace_nodes=20,
                # Pack is single-shot; auto-compacting its own input
                # would recurse uselessly.
                chatflow_compact_trigger_pct=None,
                chatflow_runtime_environment_note=self._resolve_runtime_note(chatflow),
                chatflow_tool_catalog=self._resolve_tool_catalog(chatflow),
                chatflow_max_produced_tags=chatflow.max_produced_tags,
                chatflow_max_consumed_tags=chatflow.max_consumed_tags,
                chatflow_id=chatflow.id,
                disabled_tool_names=effective_disabled,
                chatflow_extra_capabilities=self._resolve_extra_capabilities(),
            )
        except Exception as exc:  # noqa: BLE001 — engine boundary
            log.exception("pack ChatNode %s inner workflow raised", pack_id)
            runtime_error = f"{type(exc).__name__}: {exc}"
        finally:
            await self._bus.close(inner_wf_id)
            try:
                await relay_task
            except Exception:
                pass

        async with runtime.lock:
            pack_node = chatflow.get(pack_id)
            inner_llm = _single_node(pack_node.workflow)
            summary = (
                (inner_llm.output_message.content or "").strip()
                if inner_llm.output_message
                else ""
            )
            if runtime_error is not None or not summary:
                pack_node.status = NodeStatus.FAILED
                pack_node.error = (
                    runtime_error or "pack worker returned empty summary"
                )
                pack_node.finished_at = utcnow()
            else:
                # Hard-cap the pack summary at target × ctx so an
                # overshooting summarizer can't strand the next turn.
                from agentloom.engine.workflow_engine import (
                    _truncate_text_to_tokens,
                )

                resolved_for_cap = (
                    pack_node.resolved_model
                    or inner_llm.resolved_model
                    or inner_llm.model_override
                )
                ctx = self._inner._context_window_for(resolved_for_cap)
                cap_tokens = max(
                    256,
                    int(ctx * chatflow.chatnode_compact_target_pct),
                )
                if _count_text_tokens(summary) > cap_tokens:
                    log.warning(
                        "pack summary exceeds target: node=%s cap=%d — truncating",
                        pack_id,
                        cap_tokens,
                    )
                    summary = _truncate_text_to_tokens(summary, cap_tokens)
                # Insert blank lines between the pointer / per-node
                # paragraphs so markdown renders them as separate
                # blocks instead of one run-on line.
                summary = _normalize_pack_summary(summary)
                assert pack_node.pack_snapshot is not None
                pack_node.pack_snapshot = pack_node.pack_snapshot.model_copy(
                    update={"summary": summary}
                )
                pack_node.agent_response = EditableText.by_agent(summary)
                pack_node.status = NodeStatus.SUCCEEDED
                pack_node.finished_at = utcnow()

        # MemoryBoard: pack's summary IS its ChatBoard brief. Write
        # the row directly via the inner engine's board_writer — no
        # secondary chat_brief LLM call (same rationale as
        # PR 4.2.a for compact at the WorkFlow layer).
        if (
            pack_node.status == NodeStatus.SUCCEEDED
            and self._inner._board_writer is not None
        ):
            inner_chat_ids = _collect_inner_chat_ids(pack_node)
            work_node_ids = _collect_work_node_ids(pack_node)
            footer = _drill_down_footer(
                self._current_fixture_language,
                inner_chat_ids,
                work_node_ids,
            )
            description = f"{summary}\n\n{footer}" if footer else summary
            try:
                # Pack write bypasses ``chat_brief`` (the pack's own
                # summary IS the brief), so produced_tags / consumed_tags
                # are empty here. Downstream tag-walkers traverse through
                # the pack to its pre-pack ancestors via the primary
                # parent chain, so an untagged pack doesn't block the
                # ancestral anchor lookup.
                await self._inner._board_writer(
                    chatflow_id=chatflow.id,
                    workflow_id=None,
                    source_node_id=pack_id,
                    source_kind="chat_pack",
                    scope="chat",
                    description=description,
                    fallback=False,
                    inner_chat_ids=inner_chat_ids,
                    work_node_ids=work_node_ids,
                    produced_tags=[],
                    consumed_tags=[],
                )
            except Exception:  # noqa: BLE001 — board is best-effort
                log.exception(
                    "ChatBoardItem write failed for pack node %s — state unchanged",
                    pack_id,
                )

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.status",
                node_id=pack_id,
                data={
                    "status": pack_node.status.value,
                    **({"error": pack_node.error} if pack_node.error else {}),
                },
            )
        )
        await self._save(runtime)
        return pack_node

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
            else DEFAULT_COMPACT_KEEP_RECENT_COUNT
        )

        async with runtime.lock:
            chatflow = runtime.chatflow
            if parent_id not in chatflow.nodes:
                raise KeyError(f"parent {parent_id!r} not in chatflow {chatflow_id!r}")
            # Strict brief sync gate (PR4 step 1a, 2026-04-21) — see
            # ``_await_ancestor_briefs`` for rationale. Manual compaction
            # goes through the same gate as auto-compact so the summary
            # never folds a node whose brief hasn't landed yet.
            await self._await_ancestor_briefs(chatflow, parent_id)
            compact_node = self._build_compact_chatnode(
                chatflow,
                parent_id=parent_id,
                preserve_recent_turns=preserve,
                preserve_mode=chatflow.compact_preserve_mode,
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
            frozenset(chatflow.disabled_tool_names)
            | frozenset(ws_settings.globally_disabled())
            | self._foreign_tau_tools(chatflow.id)
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
                chatflow_runtime_environment_note=self._resolve_runtime_note(chatflow),
                chatflow_tool_catalog=self._resolve_tool_catalog(chatflow),
                chatflow_max_produced_tags=chatflow.max_produced_tags,
                chatflow_max_consumed_tags=chatflow.max_consumed_tags,
                chatflow_id=chatflow.id,
                disabled_tool_names=effective_disabled,
                chatflow_extra_capabilities=self._resolve_extra_capabilities(),
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
                resolved_for_cap = (
                    compact_node.resolved_model
                    or inner_llm.resolved_model
                    or inner_llm.model_override
                )
                summary = self._finalize_compact_chatnode_snapshot(
                    chatflow, compact_node, summary, resolved_for_cap
                )
                compact_node.agent_response = EditableText.by_agent(summary)
                compact_node.status = NodeStatus.SUCCEEDED
                compact_node.finished_at = utcnow()

        # ChatBoard hook: a successful compact ChatNode gets its own
        # scope='chat' ChatBoardItem (source_kind='chat_compact') so any
        # later compact/pack/merge that walks the chain sees a brief
        # for this ancestor. Without this, PR A's brief-sync gate rejects
        # descendant compact calls with "missing scope='chat' ChatBoardItem
        # for ancestor(s) …" (regression found 2026-04-22 integration test).
        if compact_node.status == NodeStatus.SUCCEEDED:
            await self._spawn_chat_board_item(chatflow, compact_node)

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
            chatflow_extra_capabilities=self._resolve_extra_capabilities(),
        )
        summary = (
            (inner.output_message.content or "").strip()
            if inner.output_message
            else ""
        )
        if not summary:
            raise RuntimeError("pre-compact worker returned empty summary")
        return summary

    async def preview_merge(
        self,
        chatflow_id: str,
        *,
        left_id: str,
        right_id: str,
        model: ProviderModelRef | None = None,
    ) -> MergePreview:
        """Dry-run a merge: compute LCA, segment tokens, and whether a
        joint-compact would be needed before the actual merge runs.

        Raises ``ValueError`` for self-merge, ``KeyError`` for an
        unknown node id. No mutation — safe to call anytime.
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

            lca_id = _chat_lca(chatflow, left_id, right_id)
            # prefix: root → LCA inclusive (walk LCA up through primary
            # parents until there is none). LCA may be None for
            # pathological DAGs; treat that as zero prefix.
            prefix_tokens = (
                _count_chat_chain_tokens(chatflow, lca_id) if lca_id else 0
            )
            left_suffix_tokens = _count_chat_chain_tokens(
                chatflow, left_id, stop_at=lca_id
            )
            right_suffix_tokens = _count_chat_chain_tokens(
                chatflow, right_id, stop_at=lca_id
            )

            merge_model_ref = (
                model or chatflow.compact_model or chatflow.draft_model
            )
            trigger_pct = (
                chatflow.compact_trigger_pct
                if chatflow.compact_trigger_pct is not None
                else DEFAULT_COMPACT_TRIGGER_PCT
            )

        from agentloom.engine.provider_context_cache import (
            lookup as _ctx_lookup,
        )

        window = _ctx_lookup(merge_model_ref) or DEFAULT_CONTEXT_WINDOW_TOKENS
        total_budget = int(window * trigger_pct)
        # After reserving the fixed prompt overhead and the prefix that
        # will ride along with the merge prompt, whatever's left is what
        # the two branch suffixes must fit into.
        effective_budget = max(
            512,
            total_budget - self._MERGE_PROMPT_OVERHEAD_TOKENS - prefix_tokens,
        )
        combined_suffix_tokens = left_suffix_tokens + right_suffix_tokens
        compact_needed = combined_suffix_tokens > effective_budget
        # If a joint-compact is needed, aim for half the remaining
        # budget so both branches collapse into roughly symmetric sizes
        # with room for the merge prompt itself. Floor at 512 matches
        # the existing per-branch pre-compact floor in merge_chain.
        suggested_target = (
            max(512, effective_budget // 2) if compact_needed else 0
        )
        return MergePreview(
            lca_id=lca_id,
            compact_needed=compact_needed,
            suggested_target_tokens=suggested_target,
            prefix_tokens=prefix_tokens,
            left_suffix_tokens=left_suffix_tokens,
            right_suffix_tokens=right_suffix_tokens,
            combined_suffix_tokens=combined_suffix_tokens,
            effective_budget_tokens=effective_budget,
        )

    async def merge_chain(
        self,
        chatflow_id: str,
        *,
        left_id: str,
        right_id: str,
        merge_instruction: str | None = None,
        model: ProviderModelRef | None = None,
        compact_target_tokens: int | None = None,
        compact_instruction: str | None = None,
    ) -> ChatFlowNode:
        """Fold two ChatNode branches into a single synthesized reply.

        Mirrors :meth:`compact_chain` in shape: we build a new ChatNode
        with ``parent_ids=[left_id, right_id]``, run the ``merge``
        builtin template as its inner workflow, and stamp the worker's
        output onto ``agent_response``. Multi-parent is itself the
        structural marker — downstream context walks stop at this node
        just like they stop at a compact node (both branches' history
        is encoded in the merged reply).

        Context-overflow handling has two shapes:

        - **Per-branch pre-compact** (default): when either branch's
          wire chain exceeds the per-branch budget, that branch is
          summarised via :meth:`_precompact_branch_for_merge` and the
          summary string is fed into the merge prompt — no ChatNode is
          created for the summary.
        - **Joint-compact** (when the caller passes
          ``compact_target_tokens``): the engine inserts a *visible*
          compact ChatNode with ``parent_ids=[left_id, right_id]`` above
          the merge node. That ChatNode holds one joint summary and a
          ``CompactSnapshot`` whose ``preserved_messages`` is the
          root→LCA prefix with ``preserved_before_summary=True`` so
          downstream context walks see the prefix first and the joint
          summary second. The merge ChatNode is reparented to this
          compact node (single parent), so walks stop at one node.

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

            # Joint-compact path needs LCA + prefix/suffix split before
            # we leave the lock, so the actual compact LLM call runs on
            # immutable snapshots. The suffix is derived positionally
            # from each branch's full ctx (both walks share the root→
            # LCA head, so the prefix slice length is the same).
            joint_prefix: list[WireMessage] | None = None
            joint_left_suffix: list[WireMessage] | None = None
            joint_right_suffix: list[WireMessage] | None = None
            if compact_target_tokens is not None:
                lca_id = _chat_lca(chatflow, left_id, right_id)
                joint_prefix = (
                    _build_chat_context(chatflow, [lca_id]) if lca_id else []
                )
                head = len(joint_prefix)
                joint_left_suffix = left_ctx[head:]
                joint_right_suffix = right_ctx[head:]

        original_tokens = _estimate_tokens_from_wire(
            left_ctx
        ) + _estimate_tokens_from_wire(right_ctx)

        # --- Outside lock: joint-compact OR per-branch pre-compact. ---
        compact_node_id: str | None = None
        if compact_target_tokens is not None:
            assert joint_prefix is not None
            assert joint_left_suffix is not None
            assert joint_right_suffix is not None

            combined_text = (
                "[Left branch — suffix after LCA]\n"
                + _serialize_wire_chain(joint_left_suffix)
                + "\n\n[Right branch — suffix after LCA]\n"
                + _serialize_wire_chain(joint_right_suffix)
            )
            joint_template = instantiate_fixture(
                self._fixture_plans["compact"],
                {
                    "messages_to_compact": combined_text,
                    "target_tokens": compact_target_tokens,
                    "must_keep": "",
                    "must_drop": "",
                    "compact_instruction": (
                        compact_instruction
                        or "Summarise both parallel conversation "
                        "branches into a single coherent context. "
                        "Preserve decisions and concrete facts from "
                        "both sides; call out any disagreements."
                    ),
                },
                includes=self._fixture_includes,
            )
            joint_inner = _single_node(joint_template)
            if merge_model_ref is not None:
                joint_inner.model_override = merge_model_ref
                joint_inner.resolved_model = merge_model_ref
            await self._inner.execute(
                joint_template,
                chatflow_tool_loop_budget=1,
                chatflow_auto_mode_revise_budget=0,
                chatflow_min_ground_ratio=None,
                chatflow_ground_ratio_grace_nodes=20,
                chatflow_compact_trigger_pct=None,
                disabled_tool_names=effective_disabled,
                chatflow_extra_capabilities=self._resolve_extra_capabilities(),
            )
            joint_raw = (
                (joint_inner.output_message.content or "").strip()
                if joint_inner.output_message
                else ""
            )
            if not joint_raw:
                raise RuntimeError(
                    "joint-compact worker returned empty summary"
                )

            async with runtime.lock:
                chatflow = runtime.chatflow
                if (
                    left_id not in chatflow.nodes
                    or right_id not in chatflow.nodes
                ):
                    raise KeyError(
                        "source node was removed during joint-compact "
                        f"(left={left_id!r}, right={right_id!r})"
                    )
                joint_summary = _append_branch_citation_fallback(
                    joint_raw, chatflow, [left_id, right_id]
                )
                compact_node = ChatFlowNode(
                    parent_ids=[left_id, right_id],
                    user_message=(
                        EditableText.by_user(compact_instruction)
                        if compact_instruction
                        else None
                    ),
                    agent_response=EditableText.by_agent(joint_summary),
                    workflow=joint_template,
                    compact_snapshot=CompactSnapshot(
                        summary=joint_summary,
                        preserved_messages=joint_prefix,
                        preserved_before_summary=True,
                    ),
                    status=NodeStatus.SUCCEEDED,
                    started_at=utcnow(),
                    finished_at=utcnow(),
                    entry_prompt_tokens=original_tokens,
                    output_response_tokens=_count_text_tokens(joint_summary),
                    # Joint-compact is a fresh cutoff — downstream sticky
                    # restarts empty (matches regular compact ChatNodes).
                    sticky_restored={},
                )
                chatflow.add_node(compact_node)
                compact_node_id = compact_node.id

            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=chatflow_id,
                    kind="chat.node.created",
                    node_id=compact_node_id,
                    data={
                        "parent_ids": [left_id, right_id],
                        "compact": True,
                        "merge_source": True,
                    },
                )
            )
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=chatflow_id,
                    kind="chat.node.status",
                    node_id=compact_node_id,
                    data={"status": NodeStatus.SUCCEEDED.value},
                )
            )

            # Feed the merge worker the two suffixes directly — the
            # joint summary above already captures the shared prefix
            # and the combined context, so the merge worker only
            # reconciles the post-LCA branches.
            left_summary = _serialize_wire_chain(joint_left_suffix)
            right_summary = _serialize_wire_chain(joint_right_suffix)
        else:
            from agentloom.engine.provider_context_cache import (
                lookup as _ctx_lookup,
            )

            window = (
                _ctx_lookup(merge_model_ref)
                or DEFAULT_CONTEXT_WINDOW_TOKENS
            )
            total_budget = int(window * trigger_pct)
            per_branch_budget = max(
                512,
                (total_budget - self._MERGE_PROMPT_OVERHEAD_TOKENS) // 2,
            )

            left_tokens = _estimate_tokens_from_wire(left_ctx)
            right_tokens = _estimate_tokens_from_wire(right_ctx)

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
        merge_parent_ids = (
            [compact_node_id] if compact_node_id is not None else [left_id, right_id]
        )
        async with runtime.lock:
            chatflow = runtime.chatflow
            for pid in merge_parent_ids:
                if pid not in chatflow.nodes:
                    raise KeyError(
                        f"merge parent {pid!r} missing after pre-compact"
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

            # MAX-merge sticky from all merge parents. In the
            # non-joint-compact path this picks up both branches'
            # sticky state directly; in the joint-compact path the
            # single parent is the compact_node (sticky reset to {}),
            # so the MAX is just {}.
            merged_sticky = _merge_sticky_restored(
                [
                    chatflow.nodes[pid].sticky_restored
                    for pid in merge_parent_ids
                ]
            )
            merge_node = ChatFlowNode(
                parent_ids=list(merge_parent_ids),
                user_message=(
                    EditableText.by_user(merge_instruction)
                    if merge_instruction
                    else None
                ),
                agent_response=EditableText.by_agent(""),
                workflow=templated,
                status=NodeStatus.PLANNED,
                entry_prompt_tokens=original_tokens,
                sticky_restored=merged_sticky,
            )
            chatflow.add_node(merge_node)
            merge_id = merge_node.id

        await self._bus.publish(
            WorkflowEvent(
                workflow_id=chatflow_id,
                kind="chat.node.created",
                node_id=merge_id,
                data={
                    "parent_ids": list(merge_parent_ids),
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
            frozenset(chatflow.disabled_tool_names)
            | frozenset(ws_settings.globally_disabled())
            | self._foreign_tau_tools(chatflow.id)
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
                chatflow_runtime_environment_note=self._resolve_runtime_note(chatflow),
                chatflow_tool_catalog=self._resolve_tool_catalog(chatflow),
                chatflow_max_produced_tags=chatflow.max_produced_tags,
                chatflow_max_consumed_tags=chatflow.max_consumed_tags,
                chatflow_id=chatflow.id,
                disabled_tool_names=effective_disabled,
                chatflow_extra_capabilities=self._resolve_extra_capabilities(),
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
                # The merge ChatNode is the only node downstream will
                # see — if the model didn't cite, append a structural
                # fallback pointing back at the original branches so
                # provenance survives. Joint-compact path cites the
                # same pair on the compact summary; appending again on
                # the merge reply is idempotent from the reader's POV
                # but keeps every synthesised node self-describing.
                merged_reply = _append_branch_citation_fallback(
                    merged_reply, chatflow, [left_id, right_id]
                )
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
            sibling = await self._spawn_turn_node(
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
            node = await self._spawn_turn_node(
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
            node = await self._spawn_turn_node(
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

        leaf = chatflow.get(leaf_id)

        # Failed-leaf auto-fork (2026-04-30 hotfix for issue #4 from the
        # qwen36 test batch). When the latest leaf is FAILED we cannot
        # let the pending turn sit on its queue: ``_try_consume`` early-
        # returns on non-SUCCEEDED nodes (failed nodes never consume —
        # their queue is owned by retry / delete), so the caller's
        # ``await future`` would hang indefinitely and the API
        # ``submit_lock`` it's holding never releases. Result observed
        # in production: turn 5 fails on post_judge, every subsequent
        # POST /turns 60s timeouts, the chatflow is permanently un-
        # submittable until backend restart or DELETE.
        #
        # Fix: treat a no-parent_id submit on a failed leaf as a fork
        # off the failed leaf's primary parent — semantically a fresh
        # sibling, exactly what retry_failed_node would do, and matches
        # the broader "submit must never silently hang" invariant
        # alongside the fork-semantics rule. Failed root (no parents)
        # falls back to the empty-chatflow path further up the file
        # by re-bootstrapping a fresh root.
        if leaf.status == NodeStatus.FAILED:
            target_parent_ids = list(leaf.parent_ids)
            log.warning(
                "submit on failed leaf %s — auto-forking from %s "
                "(retry semantics) instead of queueing on dead branch",
                leaf_id,
                target_parent_ids or "<root>",
            )
            node = await self._spawn_turn_node(
                runtime,
                parent_ids=target_parent_ids,
                user_message_text=pending.text,
                pending_queue=[],
                spawn_model=pending.spawn_model,
                judge_spawn_model=pending.judge_spawn_model,
                tool_call_spawn_model=pending.tool_call_spawn_model,
                originating_pending=pending,
            )
            await self._publish_node_created(runtime, node.id)
            self._launch_execute(
                runtime,
                node.id,
                consumed_pending_id=None
                if node.compact_snapshot is not None
                else pending.id,
            )
            return

        # Non-empty: drop the pending turn on the live tip. If the
        # tip is already idle, _try_consume will pop it right back
        # off and launch the child; otherwise the walk-down logic
        # will pick it up on the next transition.
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
        child = await self._spawn_turn_node(
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

    async def _spawn_turn_node(
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
        # Fetch CBIs so the summary preamble can embed a per-ancestor
        # ChatBoardItem recap when a compact cutoff is on the chain.
        # No-op when no board_writer is wired (unit-test engines) —
        # ``_fetch_chat_board_descriptions`` returns ``{}`` in that case
        # and ``_build_chat_context`` renders the bare summary.
        cbi_descriptions = await self._fetch_chat_board_descriptions(
            chatflow.id
        )
        context_wire = _build_chat_context(
            chatflow,
            parent_ids,
            chat_board_descriptions=cbi_descriptions,
        )
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
                # Strict brief sync gate (PR4 step 1a, 2026-04-21).
                # Refuse to compact while an ancestor's ChatBoardItem
                # hasn't been committed yet — we'd otherwise fold a
                # node whose recall-key is still being distilled, and
                # a later retrieval against that node would come back
                # empty. Waives the primary parent (preserved tail
                # keeps that turn verbatim; its brief is not on the
                # critical path).
                await self._await_ancestor_briefs(chatflow, parent_ids[0])
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
                        preserve_recent_turns=chatflow.compact_keep_recent_count,
                        preserve_mode=chatflow.compact_preserve_mode,
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
            # Prong 5 fact-loss fix (2026-04-30, see
            # docs/backlog-decompose-fact-loss.md): anchor the
            # outermost ChatNode's user_message verbatim onto the
            # WorkFlow so every sub_agent_delegation spawned from
            # below — at any depth — can prepend it into its own
            # judge_pre's conversation. Stops the planner brief
            # boundary from being a one-way fact-loss frontier.
            outer_user_message=user_message_text,
        )

        # Cross-turn capability accumulation (2026-04-28) — seed the
        # new turn's WorkFlow from the primary parent ChatNode's
        # WorkFlow so judge_pre starts with the standing tool surface
        # rather than re-extracting from this turn's user words alone.
        # Combined with ``_apply_judge_pre_trio``'s order-preserving
        # union, the surface grows monotonically along the chain.
        # Forks naturally diverge: each fork inherits at the fork
        # point and unions in its own per-turn additions
        # independently (the seed is a copy, not a reference). Pack /
        # compact ChatNodes whose inner WorkFlow doesn't run
        # judge_pre still propagate the seed to their downstream so
        # the chain doesn't reset across compaction boundaries.
        if parent_ids:
            primary_parent = chatflow.nodes.get(parent_ids[0])
            if primary_parent is not None and primary_parent.workflow is not None:
                inner.inheritable_tools = list(
                    primary_parent.workflow.inheritable_tools or []
                )
                inner.capabilities_origin = list(
                    primary_parent.workflow.capabilities_origin or []
                )

        if switches.judge_pre:
            # Only the pre-judge runs upfront; the rest of the chain is
            # spawned dynamically once we know the verdict.
            self._spawn_judge_pre(
                inner, user_message_text, context_wire, chatflow=chatflow
            )
        else:
            inner.add_node(
                WorkFlowNode(
                    step_kind=StepKind.DRAFT,
                    parent_ids=[],
                    input_messages=list(context_wire),
                    model_override=resolved,
                )
            )

        # Inherit the primary parent's sticky_restored map so forked
        # branches evolve independently (each fork gets its own copy
        # to mutate). ``_update_sticky_restored_for_node`` will be
        # called on this node after its turn runs; that's what
        # refreshes / decays the counters.
        inherited_sticky: dict[str, int] = {}
        if parent_ids:
            parent = chatflow.nodes.get(parent_ids[0])
            if parent is not None:
                inherited_sticky = dict(parent.sticky_restored)

        chat_node = ChatFlowNode(
            parent_ids=list(parent_ids),
            user_message=EditableText.by_user(user_message_text),
            workflow=inner,
            pending_queue=list(pending_queue),
            resolved_model=resolved,
            entry_prompt_tokens=entry_tokens,
            sticky_restored=inherited_sticky,
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
        *,
        chatflow: ChatFlow | None = None,
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
        # M7.5 §4.1: snapshot the read-only allocation onto the judge so
        # ``_spawn_recon_chain`` (and any future per-judge
        # ``resolve_for_node`` consumer) has an authoritative ceiling.
        #
        # Two source paths for the disabled set (mirrors
        # ``_spawn_judge_post``):
        # - chatflow passed explicitly (top-level turn spawn —
        #   ``_spawn_turn_node``): derive from chatflow.disabled +
        #   foreign-tau filter.
        # - chatflow=None (sub-WorkFlow spawn —
        #   ``_build_sub_workflow_for_subtask`` doesn't have chatflow
        #   in scope; bare-engine tests; etc.): fall back to the
        #   engine's per-execute ``_disabled_tool_names`` which
        #   already unions workspace + chatflow + foreign-tau at
        #   execute time. Same effective filter; sub-WorkFlow
        #   judge_pre now gets a real allocation instead of legacy
        #   None — closes the only remaining gap in the cognitive
        #   ReAct DAG production path.
        if chatflow is not None:
            node.effective_tools = self._judge_pre_effective_tools(chatflow)
        else:
            node.effective_tools = (
                self._cognitive_judge_effective_tools_from_disabled(
                    self._inner._disabled_tool_names
                )
            )
        return inner.add_node(node)

    # ---------------------------------------------------- M7.5 PR 7 recon DAG

    def _cognitive_react_enabled_for_pre(
        self,
        chatflow: ChatFlow,
        workflow: WorkFlow,
        judge_pre_node: WorkFlowNode,
    ) -> bool:
        """Decide whether to honor a ``judge_pre`` ``recon_plan``.

        Three gates, all must pass:

        - The chatflow opted in via ``cognitive_react_enabled``.
        - The tool registry exists. Without it the recon tool_calls
          would all dispatch as "tool not found" errors and the
          follow-up judge_pre would just see noise.
        - Recursion fuse (mirror of
          ``_cognitive_react_enabled_for_post``): a judge_pre whose
          parent is a TOOL_CALL is itself a recon follow-up, must
          commit to a verdict instead of recon-again. 2026-04-29
          retail evidence: without this fuse, judge_pre repeatedly
          emitted the same ``recon_plan=[get_order_details]`` after
          consuming its results — 5 rounds of redundant recon
          before the model finally gave up. Capping at one round
          per design §4.4 forces the second pass to commit.

        The judge_pre node's ``effective_tools`` (snapshotted by
        ``_spawn_judge_pre`` at allocation time, M7.5 §4.1) is the
        per-node ceiling: the recon spawn site additionally requires
        each ``recon_plan`` spec name to live on that list. Allocation
        excludes WRITE side-effect tools and chatflow-disabled tools,
        so a hallucinated ``Bash`` recon spec drops at spawn even if
        the judge_pre prompt accidentally let it through. Both
        top-level turns and sub-WorkFlow judge_pre nodes get the
        allocation now (the latter via the engine-state fallback in
        ``_spawn_judge_pre`` when chatflow isn't passed —
        ``_inner._disabled_tool_names`` is the same union the
        explicit chatflow path would compute).
        """
        if not chatflow.cognitive_react_enabled:
            return False
        if self._tools is None:
            log.warning(
                "cognitive_react_enabled=True but no tool registry; "
                "falling back to atomic judge_pre"
            )
            return False
        for pid in judge_pre_node.parent_ids or []:
            parent = workflow.nodes.get(pid)
            if parent is not None and parent.step_kind == StepKind.TOOL_CALL:
                return False
        return True

    def _cognitive_react_enabled_for_post(
        self,
        chatflow: ChatFlow,
        workflow: WorkFlow,
        judge_post_node: WorkFlowNode,
    ) -> bool:
        """Decide whether to honor a ``judge_post`` ``recon_plan``.

        Same two outer gates as the pre variant
        (``cognitive_react_enabled`` + tool registry present) plus a
        recursion fuse: a judge_post that already consumed recon
        results (i.e. its parents are TOOL_CALL nodes spawned by
        :meth:`_spawn_recon_chain_for_post`) cannot recon again. The
        guard caps the depth at one round.

        Per-judge ``effective_tools`` is enforced inside
        ``_filter_recon_plan`` at spawn time (same as pre).
        """
        if not chatflow.cognitive_react_enabled:
            return False
        if self._tools is None:
            log.warning(
                "cognitive_react_enabled=True but no tool registry; "
                "skipping judge_post recon"
            )
            return False
        for pid in judge_post_node.parent_ids or []:
            parent = workflow.nodes.get(pid)
            if parent is not None and parent.step_kind == StepKind.TOOL_CALL:
                return False
        return True

    def _filter_recon_plan(
        self,
        recon_plan: list[ReconToolCall],
        judge_node: WorkFlowNode,
        chatflow_disabled: frozenset[str],
    ) -> list[ReconToolCall]:
        """Drop unsafe / unusable specs from a judge's ``recon_plan``.

        Three cumulative gates:
        - ``chatflow_disabled`` — workspace / chatflow toggle; the
          most-specific gate.
        - registry presence — drop hallucinated names so we don't
          dispatch a "tool not found" tool_call.
        - judge ``effective_tools`` (M7.5 §4.1) — drop tools outside
          the per-judge cognitive READ ceiling. ``None`` means legacy
          fallthrough; the registry+disabled checks remain the floor.

        Pure function (logs warnings, otherwise no side effects) so the
        pre / post recon paths share the exact same filter and a
        future plan_judge / worker_judge variant just reuses it.
        """
        assert self._tools is not None
        judge_allocation: frozenset[str] | None = (
            frozenset(judge_node.effective_tools)
            if judge_node.effective_tools is not None
            else None
        )
        kept: list[ReconToolCall] = []
        for spec in recon_plan:
            if spec.name in chatflow_disabled:
                log.warning(
                    "recon: chatflow disabled tool %r — dropping spec; "
                    "judge node=%s variant=%s",
                    spec.name,
                    judge_node.id,
                    judge_node.judge_variant,
                )
                continue
            if not self._tools.has(spec.name):
                log.warning(
                    "recon: unknown tool %r — dropping spec; "
                    "judge node=%s variant=%s",
                    spec.name,
                    judge_node.id,
                    judge_node.judge_variant,
                )
                continue
            if judge_allocation is not None and spec.name not in judge_allocation:
                log.warning(
                    "recon: tool %r not in judge effective_tools "
                    "allocation (likely WRITE side_effect or workspace-"
                    "disabled) — dropping spec; judge node=%s variant=%s",
                    spec.name,
                    judge_node.id,
                    judge_node.judge_variant,
                )
                continue
            kept.append(spec)
        return kept

    def _spawn_recon_chain(
        self,
        workflow: WorkFlow,
        judge_pre_node: WorkFlowNode,
        verdict: JudgeVerdict,
        *,
        resolved_model: ProviderModelRef | None,
    ) -> bool:
        """Spawn the recon DAG for judge_pre: N tool_call children + a
        follow-up judge_pre node that consumes their outputs.

        Returns True when recon actually spawned, False when every
        spec dropped (caller is responsible for falling through to
        the regular atomic path so the planner / halt branch still
        runs — without that, the chain dies at judge_pre with no
        terminal llm_call and the ChatNode goes FAILED, observed
        2026-04-29 retail batch when every tau tool was filtered by
        the WRITE-side-effect ceiling).

        Sister of :meth:`_spawn_recon_chain_for_post`. The two share
        :meth:`_filter_recon_plan` for spec validation; both return
        bool so callers can distinguish "spawned, defer to follow-up"
        from "fell back, continue this turn's regular flow".

        The follow-up judge_pre is parented on every spawned
        tool_call so the scheduler waits for them all before re-
        running. Its ``input_messages`` reuse the original judge_pre
        seed so the conversation framing carries over; the engine's
        normal ancestor-chain context build will splice the
        tool_results in via the tool_call ancestors.
        """
        assert self._tools is not None  # enforced by _cognitive_react_enabled_for_pre
        chatflow_disabled = self._disabled_tool_names_for_workflow(workflow)
        kept = self._filter_recon_plan(
            verdict.recon_plan, judge_pre_node, chatflow_disabled
        )
        if not kept:
            log.warning(
                "recon: every spec dropped; judge_pre node=%s falling "
                "through to atomic path (caller continues to apply "
                "trio + planner spawn)",
                judge_pre_node.id,
            )
            return False

        tool_call_ids: list[str] = []
        for spec in kept:
            tc = WorkFlowNode(
                step_kind=StepKind.TOOL_CALL,
                parent_ids=[judge_pre_node.id],
                tool_name=spec.name,
                tool_args=dict(spec.args or {}),
            )
            workflow.add_node(tc)
            tool_call_ids.append(tc.id)

        templated = instantiate_fixture(
            self._fixture_plans["judge_pre"],
            {},
            includes=self._fixture_includes,
        )
        follow_up = _single_node(templated)
        # Re-use the original seed so the conversation framing is
        # consistent across the round. Tool_call ancestors splice
        # their tool_result content into the context build via the
        # standard ancestor chain — no manual injection needed.
        follow_up.parent_ids = list(tool_call_ids)
        follow_up.input_messages = list(judge_pre_node.input_messages or [])
        follow_up.model_override = resolved_model or workflow.judge_model_override
        workflow.add_node(follow_up)
        return True

    def _spawn_recon_chain_for_post(
        self,
        workflow: WorkFlow,
        judge_post_node: WorkFlowNode,
        verdict: JudgeVerdict,
        *,
        resolved_model: ProviderModelRef | None,
    ) -> bool:
        """Spawn judge_post's recon DAG: N tool_call children + a
        follow-up judge_post that re-evaluates with the recon results
        in context. Returns True when recon was actually spawned, False
        when every spec dropped (caller falls through to the original
        accept/retry/fail decision tree).

        Sister of :meth:`_spawn_recon_chain`; see that doc for the
        shared filter semantics. The post variant exists because
        judge_post is the universal exit gate and often needs to
        verify state mutations the worker claimed (e.g. retail
        ``outputs_match=True / db_hash_match=False`` symptom — agent
        said "I changed the order" but the mock DB shows otherwise).
        Verifying via ``get_order_details`` recon before issuing the
        final verdict closes that gap.

        The follow-up reuses the original judge_post's input_messages
        so the trio + upstream-summary framing carries over; the
        spawned tool_call ancestors splice their results in via the
        engine's standard context build.
        """
        assert self._tools is not None  # enforced by _cognitive_react_enabled_for_post
        chatflow_disabled = self._disabled_tool_names_for_workflow(workflow)
        kept = self._filter_recon_plan(
            verdict.recon_plan, judge_post_node, chatflow_disabled
        )
        if not kept:
            log.warning(
                "recon (post): every spec dropped; judge_post node=%s "
                "falling through to original verdict path",
                judge_post_node.id,
            )
            return False

        tool_call_ids: list[str] = []
        for spec in kept:
            tc = WorkFlowNode(
                step_kind=StepKind.TOOL_CALL,
                parent_ids=[judge_post_node.id],
                tool_name=spec.name,
                tool_args=dict(spec.args or {}),
            )
            workflow.add_node(tc)
            tool_call_ids.append(tc.id)

        templated = instantiate_fixture(
            self._fixture_plans["judge_post"],
            {
                # Re-render the prompt with empty trio/catalog params;
                # we'll overwrite input_messages below with the
                # original judge_post's seed so these placeholders
                # never reach the model. The instantiate_fixture call
                # is just to get the WorkFlowNode skeleton with the
                # right step_kind / role / judge_variant.
                "workflow_description": "",
                "workflow_inputs": "",
                "workflow_expected_outcome": "",
                "upstream_kind": "recon_followup",
                "upstream_summary": "",
                "worknode_catalog": "",
                "redo_eligible_catalog": "",
                "tool_result_ledger": "",
                "workflow_suspected_fabricated_failure": "",
            },
            includes=self._fixture_includes,
        )
        follow_up = _single_node(templated)
        follow_up.parent_ids = list(tool_call_ids)
        follow_up.judge_target_id = judge_post_node.judge_target_id
        # Re-use the original seed so the trio + upstream summary +
        # worknode catalog the original judge saw all carry over —
        # the recon results splice in via the tool_call ancestors.
        follow_up.input_messages = list(judge_post_node.input_messages or [])
        follow_up.model_override = (
            resolved_model or workflow.judge_model_override
        )
        # M7.5 §4.1 — propagate the cognitive ceiling to the follow-up
        # so a second round of recon (if the model emits one) hits the
        # same gate as the first.
        if judge_post_node.effective_tools is not None:
            follow_up.effective_tools = list(judge_post_node.effective_tools)
        workflow.add_node(follow_up)
        return True

    def _disabled_tool_names_for_workflow(
        self, workflow: WorkFlow
    ) -> frozenset[str]:
        """Best-effort lookup of the chatflow-level disabled set.

        ``WorkFlow`` doesn't carry the field — it lives on the
        enclosing ``ChatFlow``. The recon-spawn call site has the
        chatflow id but not the ChatFlow row; fetching from the DB
        here would block the post-node hook. Default to the
        registry's own ``has`` check (already done at the call site)
        and treat the disabled set as empty when we can't read it.
        """
        del workflow
        return frozenset()

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
        chatflow: ChatFlow | None = None,
    ) -> WorkFlowNode:
        """Materialize the universal-exit-gate judge_post.

        ``parent_node`` is whichever node we're routing into judge_post:
        a terminal llm_call on the happy path, or judge_pre / a future
        judge_during on a halt path. ``upstream_kind`` and
        ``upstream_summary`` give the judge enough context to write the
        user-facing message in its own voice — see judge_post.yaml.
        """
        trio = _trio_params(inner)
        # Bug A layer 1 (2026-04-30): tool-result truth ledger so
        # judge_post can cross-check worker output against engine-
        # recorded tool_result.is_error. ``parent_node`` is the
        # node we're routing FROM into judge_post (a worker draft
        # on the happy path; a judge_pre / planner_judge halt
        # node on a halt path). For halt paths there are usually no
        # ancestor tool_calls, so the ledger renders empty —
        # judge_post falls back to the existing trio-only review.
        tool_ledger = _render_tool_result_ledger(inner, parent_node)
        # Bug A layer 2 (2026-04-30): union of engine-flagged
        # fabricated-failure explanations across every WorkNode in
        # this WorkFlow — judge_post is the workflow-wide exit gate
        # so the broader scope (vs. worker_judge's single node) is
        # right.
        fabricated_flags = _render_fabricated_failure_flags(
            _aggregate_workflow_fabricated_failures(inner)
        )
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
                "tool_result_ledger": tool_ledger,
                "workflow_suspected_fabricated_failure": fabricated_flags,
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        # PR A (2026-04-21): briefs keep a single edge to their source
        # and are NOT listed here. The WorkflowEngine scheduler gates
        # judge_post on every scope=NODE brief in the WorkFlow reaching
        # a terminal status, and ``_run_judge_call`` fills in the
        # ``Layer notes`` system message by reading the MemoryBoard at
        # run time. This restores the architectural rule that a brief
        # WorkNode has exactly one edge — to its parent source.
        node.parent_ids = [parent_node.id]
        node.judge_target_id = parent_node.id
        node.input_messages = [*(node.input_messages or []), *context_wire]
        node.model_override = inner.judge_model_override
        # M7.5 §4.1 cognitive ceiling — same as judge_pre
        # (sub-task 3 of cognitive ReAct DAG productionization).
        # Snapshot the read-only allocation onto judge_post so the
        # post-recon path's _filter_recon_plan has the same gate as
        # the pre-recon path: a hallucinated WRITE recon spec drops
        # at spawn rather than dispatching as a real call.
        #
        # Two source paths for the disabled set:
        # - chatflow passed explicitly (judge_pre's hook + the
        #   easy-to-thread sites): derive from chatflow.disabled +
        #   foreign-tau filter.
        # - chatflow=None (most spawn sites — aggregator / halt
        #   helpers buried deep in the post_node_hook): fall back to
        #   the engine's per-execute ``_disabled_tool_names`` which
        #   already unions the workspace + chatflow + foreign-tau
        #   sets at execute time. Same effective filter, no need to
        #   plumb chatflow through every helper.
        if chatflow is not None:
            node.effective_tools = self._cognitive_judge_effective_tools(
                chatflow
            )
        else:
            # Fallback: the engine's per-execute disabled set is the
            # same union (workspace + chatflow + foreign-tau) the
            # explicit-chatflow path computes, just pre-baked. Bare-
            # engine tests that never set this field see the default
            # empty frozenset and get the full READ+NONE registry —
            # matches the pre-allocation behavior for those callers.
            node.effective_tools = (
                self._cognitive_judge_effective_tools_from_disabled(
                    self._inner._disabled_tool_names
                )
            )
        return inner.add_node(node)

    def _available_tools_listing(self) -> str:
        """Comma-joined tool names from the ChatFlowEngine's tool
        registry, or the literal ``"（空）"`` when no tools are
        registered. Fed to planner / plan_judge templates so they pick
        real names instead of hallucinating (2026-04-21 incident:
        planner insisted on "knowledge_search" indefinitely until the
        grounding fuse halted the WorkFlow).

        Does NOT filter by the per-chatflow disabled list — the
        WorkflowEngine enforces that at tool-dispatch time and the
        planner can revise. The goal here is to stop names from being
        invented wholesale, not to mirror the full gating logic."""
        if self._tools is None:
            return "(empty)"
        names = sorted(t.name for t in self._tools.all())
        return ", ".join(names) if names else "(empty)"

    def _foreign_tau_tools(self, chatflow_id: str) -> frozenset[str]:
        """Return tool names registered by *other* tau-bench sessions.

        τ-bench wrappers register into the global ToolRegistry under
        ``tau_<session_id[-6:]>_<name>`` per
        ``benchmarks/tau_bench/tool_source.py``. The shared registry
        means a regular (non-tau) chatflow's catalog/effective set
        would otherwise see every concurrent tau session's tools —
        observed 2026-04-26 night when M7.5 showcase chatflows had
        ``tau_*`` entries leaking in mid-batch and judge_pre tried to
        allocate them as inheritable_tools.

        Filter logic: any registered tool whose name starts with
        ``tau_`` and whose embedded prefix does NOT correspond to the
        current chatflow's id is "foreign" — exclude it. Caller
        unions this with chatflow.disabled_tool_names + workspace
        globally_disabled before resolving catalog / effective set.
        """
        if self._tools is None:
            return frozenset()
        own_prefix = f"tau_{chatflow_id[-6:]}_"
        foreign = {
            t.name
            for t in self._tools.all()
            if t.name.startswith("tau_") and not t.name.startswith(own_prefix)
        }
        return frozenset(foreign)

    def _resolve_extra_capabilities(self) -> frozenset[str]:
        """Translate workspace settings into the virtual-capability set
        the engine unions into every tool call's caller context.

        Today the only entry is the M7.5 PR 8 cross-chatflow read
        scope: when ``WorkspaceSettings.allow_cross_chatflow_lookup``
        is ``True`` the engine grants ``get_node_context.cross_chatflow``
        to every WorkNode in the workspace, so ``get_node_context``
        with ``scope='cross_chatflow'`` is admitted (otherwise that
        scope is permanently denied because no production path writes
        the cap onto a node's ``effective_tools``). Default ``False``
        keeps the pre-PR-8 boundary.
        """
        ws_settings = tenancy_runtime.get_settings(
            self._inner._tool_ctx.workspace_id
        )
        caps: set[str] = set()
        if ws_settings.allow_cross_chatflow_lookup:
            caps.add(CROSS_CHATFLOW_CAPABILITY)
        return frozenset(caps)

    def _cognitive_judge_effective_tools_from_disabled(
        self, disabled: frozenset[str]
    ) -> list[str]:
        """Compute the M7.5 §4.1 ``effective_tools`` allocation shared
        by every cognitive (judge) WorkNode that exposes a recon DAG,
        given a pre-computed disabled set.

        Cognitive nodes get ``side_effect_filter={NONE, READ}`` — they
        can read but never write. ``effective_tools`` is the
        registry-name-list expression of that ceiling, snapshotted on
        the WorkNode at spawn time so:

        - ``_spawn_recon_chain`` (pre and post variants) can validate
          ``recon_plan`` specs against this list. A hallucinated
          ``Bash`` recon spec drops at spawn instead of dispatching
          as a real WRITE call.
        - Future per-judge ``resolve_for_node`` calls (if/when judges
          start exposing tools to the LLM) read a coherent allocation
          rather than falling through to legacy.

        Excludes (caller's responsibility, baked into ``disabled``):
        - Names in ``chatflow.disabled_tool_names`` — workspace /
          chatflow toggle wins regardless of the effective_tools
          allocation. Mirrors the catalog-render filter.
        - ``foreign tau-bench`` tools (other concurrent benchmark
          sessions' wrappers leaking through the shared registry).

        Excludes here:
        - WRITE tools (``side_effect=WRITE``) — the cognitive ceiling.

        Returns ``[]`` when the registry is missing.

        judge_pre and judge_post share this exact set today (both are
        cognitive READ-only). plan_judge / worker_judge use the same
        filter when their recon path lands (sub-task follow-up).
        """
        if self._tools is None:
            return []
        out: list[str] = []
        for tool in sorted(self._tools.all(), key=lambda t: t.name):
            if tool.name in disabled:
                continue
            if tool.side_effect == SideEffect.WRITE:
                continue
            out.append(tool.name)
        return out

    def _cognitive_judge_effective_tools(
        self, chatflow: ChatFlow
    ) -> list[str]:
        """ChatFlow-keyed convenience wrapper — derives the disabled
        set from the chatflow's settings + foreign-tau filter and
        delegates."""
        disabled = (
            frozenset(chatflow.disabled_tool_names or ())
            | self._foreign_tau_tools(chatflow.id)
        )
        return self._cognitive_judge_effective_tools_from_disabled(disabled)

    def _judge_pre_effective_tools(self, chatflow: ChatFlow) -> list[str]:
        """Backward-compat wrapper kept to preserve the call site name
        used by tests + ``_spawn_judge_pre``. Delegates to the unified
        cognitive helper since judge_pre and judge_post share the
        ceiling."""
        return self._cognitive_judge_effective_tools(chatflow)

    def _resolve_tool_catalog(self, chatflow: ChatFlow) -> str:
        """Render the M7.5 inheritable-tool catalog injected before
        every cognitive (judge / planner) LLM call.

        Format — markdown so models read it as a structured list:

            ## Available tools catalog

            - Name: `Read`
              Description: Read a file from the local filesystem.
              Parameters: {"type":"object","properties":{...},"required":[...]}

            - Name: `Bash`
              Description: Execute a shell command.
              Parameters: {...}

            ...

        Sourced from the ChatFlowEngine's tool registry minus
        ``chatflow.disabled_tool_names`` (workspace toggle off → not
        a candidate for inheritable_tools). Tools are sorted by name
        for stable cache prefix across calls in the same chatflow.

        ``Parameters`` is the tool's JSON schema (compact, single line)
        so the planner can author a correct ``atomic.tool_args`` on
        the first try. Pre-2026-04-29 the catalog only carried
        ``Name`` + ``Description``; doubao-served planners hallucinated
        argument keys from intuition (e.g. wrote ``"path"`` for the
        ``Write`` tool whose actual schema requires ``"file_path"``),
        the call hit ``ToolError`` on the runtime field check, and
        the user-facing ``judge_post`` reply surfaced "调用写入工具
        出错" instead of the planted answer. With parameters now
        visible the planner sees the exact required field names + types
        + which are required vs optional. Token cost: each tool entry
        gains ~200-500 tokens of schema; for cognitive calls
        (planner / judge) that's an acceptable trade for first-try
        correctness on atomic step_kind=tool_call. Workers don't see
        this catalog — they get the full ``ToolDefinition`` injected
        into the provider's ``tools=`` slot, which already covers
        parameter shape via the structured tool-use protocol.

        Returns the empty string when the registry is missing or
        every tool is disabled. The engine treats empty as "skip the
        catalog block in the system envelope".

        Why a separate render rather than feeding into the prompt
        template: catalog content is a tool-registry derived fact
        (registration order, MCP server connect/disconnect), not
        user-editable. Threading it through the system envelope
        keeps the YAML fixtures stable and the catalog refreshes
        automatically when MCP servers (un)register.
        """
        if self._tools is None:
            return ""
        disabled = (
            frozenset(chatflow.disabled_tool_names or ())
            | self._foreign_tau_tools(chatflow.id)
        )
        rows: list[str] = []
        for tool in sorted(self._tools.all(), key=lambda t: t.name):
            if tool.name in disabled:
                continue
            description = (tool.description or "").strip().splitlines()
            first_line = description[0].strip() if description else ""
            params = getattr(tool, "parameters", None) or {}
            try:
                params_line = json.dumps(
                    params, ensure_ascii=False, separators=(",", ":")
                )
            except (TypeError, ValueError):
                params_line = "{}"
            rows.append(
                f"- Name: `{tool.name}`\n"
                f"  Description: {first_line}\n"
                f"  Parameters: {params_line}"
            )
        if not rows:
            return ""
        lang = (self._current_fixture_language or "").lower()
        heading = "## 可用工具目录" if lang.startswith("zh") else "## Available tools catalog"
        return f"{heading}\n\n" + "\n\n".join(rows)

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
                "available_tools": self._available_tools_listing(),
                "prior_plan": prior_plan,
                "critique": critique,
                "handoff_notes": handoff_notes,
            },
            includes=self._fixture_includes,
        )
        node = _single_node(templated)
        node.parent_ids = [parent_node.id]
        node.model_override = resolved_model
        # Prong 6 (2026-04-30, see docs/backlog-decompose-fact-loss.md):
        # if the WorkFlow carries an outer_user_message (set by
        # ``_spawn_turn_node`` at the top of the chain and propagated
        # through ``_build_sub_workflow_for_subtask``), prepend it to
        # the planner's input_messages as a labeled context preamble.
        # Reason: judge_pre's ``extracted_inputs`` is free-form text the
        # judge LLM authored — it may have paraphrased instead of
        # verbatim-copying the user's pasted data. The planner reading
        # only the trio loses that data. By giving every planner call
        # (including revise rounds) the outer user_message verbatim,
        # the planner can fall back to the original even when judge_pre
        # extracted a paraphrase. Same engine-mechanic family as prong 5
        # (which protects the planner→sub-WorkFlow boundary); this one
        # protects the judge_pre→planner boundary.
        if inner.outer_user_message:
            preamble = self._render_outer_user_message_preamble(
                inner.outer_user_message
            )
            existing_inputs = list(node.input_messages or [])
            # Append (not prepend) so the fixture's system → user
            # ordering stays intact. The planner reads:
            #   1. system: "you are a planner ..." (fixture)
            #   2. user: "Here is the trio. Decide ..." (fixture)
            #   3. user: "[Outer ChatFlow context] + verbatim user_message"
            #      (this preamble — added by the engine)
            # Two consecutive user messages are fine for all
            # provider transports; the labeled preamble makes the
            # role of this extra block unambiguous.
            node.input_messages = [
                *existing_inputs,
                WireMessage(role="user", content=preamble),
            ]
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
                "available_tools": self._available_tools_listing(),
                "plan_json": _render_planner_output_for_prompt(planner_node),
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
            )
            inner.add_node(delegation)
            index_to_node_id[idx] = delegation.id
            spawned.append(delegation)

        return spawned

    def _build_sub_workflow_for_subtask(
        self, parent: WorkFlow, subtask: SubTask
    ) -> WorkFlow:
        """Construct the inner WorkFlow a delegation node will execute.

        AUTO mode (so the planner pipeline can recurse). The sub's trio
        starts empty — judge_pre distills it from the subtask description
        the parent planner wrote, just like the top-level case where
        judge_pre reads the user's message. Debate budget inherited from
        the parent WorkFlow.

        **Prong 5 (2026-04-30, fact-loss fix)**: when the parent WorkFlow
        carries a non-empty ``outer_user_message`` (the originating
        ChatNode's user_message.text, propagated through every nested
        layer), the engine prepends it to the sub's judge_pre
        conversation as a "[Outer ChatFlow context]" preamble. This is
        the engine-side root-cause cure for the decompose brief boundary
        dropping literal data the planner paraphrased — sub-WorkFlows
        now have unconditional access to the user's original turn even
        when the planner-authored subtask description doesn't quote it
        verbatim. See ``docs/backlog-decompose-fact-loss.md`` prong 5.
        """
        switches = derive_switches_from_mode(ExecutionMode.AUTO_PLAN)
        sub = WorkFlow(
            execution_mode=ExecutionMode.AUTO_PLAN,
            plan_enabled=switches.plan,
            judge_pre_enabled=switches.judge_pre,
            judge_during_enabled=switches.judge_during,
            judge_post_enabled=switches.judge_post,
            debate_round_budget=parent.debate_round_budget,
            judge_retry_budget=parent.judge_retry_budget,
            judge_model_override=parent.judge_model_override,
            tool_call_model_override=parent.tool_call_model_override,
            # Inherit the MemoryBoard brief pin so nested sub-WorkFlows
            # honor the enclosing ChatFlow's brief_model.
            brief_model_override=parent.brief_model_override,
            # Propagate judge_pre's pre-scope into the sub — the sub's own
            # judge_pre will re-run and may refine, but seeding with the
            # parent's list means a first-round planner already sees the
            # right slice without waiting for the sub's judge_pre verdict.
            # M7.5 split: ``capabilities_origin`` (natural language, UI
            # provenance) and ``inheritable_tools`` (registry names, engine
            # consumed) propagate independently.
            capabilities_origin=list(parent.capabilities_origin),
            inheritable_tools=list(parent.inheritable_tools),
            # Delegation depth fuse: each sub-WorkFlow is one level
            # deeper than the planner that spawned it. When it hits
            # the budget, ``_after_planner_judge`` forces any further
            # ``decompose`` plan into an atomic worker so the tree
            # can't keep fanning out.
            delegation_depth=parent.delegation_depth + 1,
            delegation_depth_budget=parent.delegation_depth_budget,
            # Carry the originating ChatNode's user_message verbatim
            # all the way down. parent.outer_user_message was written
            # at top-level _spawn_turn_node and propagated through
            # every nesting layer so deeper subs don't lose it.
            outer_user_message=parent.outer_user_message,
        )
        # Feed the planner-authored task description to judge_pre as the
        # conversation it distills the trio from — mirrors the top-level
        # path where judge_pre reads the user's turn text. When the
        # parent WorkFlow carries an outer_user_message (the originating
        # ChatNode's user_message.text), prepend it as a labeled context
        # block so judge_pre + the planner inside this sub can fall back
        # to literal user data the planner-authored subtask description
        # may have paraphrased away.
        context_wire: list[WireMessage] = []
        if parent.outer_user_message:
            context_wire.append(
                WireMessage(
                    role="user",
                    content=self._render_outer_user_message_preamble(
                        parent.outer_user_message
                    ),
                )
            )
        context_wire.append(
            WireMessage(role="user", content=subtask.description)
        )
        self._spawn_judge_pre(sub, subtask.description, context_wire)
        return sub

    def _render_outer_user_message_preamble(self, outer_text: str) -> str:
        """Localized "[Outer ChatFlow context]" wrapper for the
        originating user_message that ``_build_sub_workflow_for_subtask``
        prepends into the sub's judge_pre conversation. Tag is in the
        workspace's fixture language so the model reads consistent
        meta-text alongside its trio."""
        lang = (self._current_fixture_language or "").lower()
        if lang.startswith("zh"):
            return (
                "[外层 ChatFlow 上下文 —— 用户在最外层 ChatNode 的原话。"
                "提供给你以防止 planner 在 brief 边界 paraphrase 丢失"
                "字面材料；当你的 subtask description 引用了"
                "\"上面提供的 X\" 类指代但你没看到具体材料时，请回到"
                "这段原文里取回原始数据，再据此完成任务。]\n\n"
                f"{outer_text}"
            )
        return (
            "[Outer ChatFlow context — the user's original turn at the "
            "outermost ChatNode. Provided so you don't lose literal "
            "data the planner above may have paraphrased. When your "
            "subtask description references something like \"the X "
            "provided above\" but you can't see X concretely, fall "
            "back to this preamble for the verbatim source.]\n\n"
            f"{outer_text}"
        )

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

    def _spawn_atomic_tool_call(
        self,
        inner: WorkFlow,
        parent_node: WorkFlowNode,
        atomic: AtomicBrief,
        *,
        resolved_model: ProviderModelRef | None,
    ) -> WorkFlowNode:
        """Materialize a ``mode=atomic, step_kind=tool_call`` plan as a
        real TOOL_CALL WorkNode + follow-up DRAFT.

        Why this exists (2026-04-29 fix): ``AtomicBrief`` carries
        first-class ``step_kind / tool_name / tool_args`` fields and
        ``plan.yaml`` explicitly tells the LLM e.g. "single
        get_node_context retrieval → ``step_kind: tool_call`` +
        ``tool_name: get_node_context`` + ``tool_args:
        {'node_id': ...}``". Pre-fix ``_after_planner_judge`` always
        called ``_spawn_worker`` for atomic plans, which only renders
        ``description / inputs / expected_outcome`` into the worker
        template. The planner's pinned ``tool_name`` + ``tool_args``
        were silently dropped — the worker had to re-derive them
        from a vague description and frequently lost the args (e.g.
        the description said "use get_node_context to verify the
        spelling" while the ``node_id`` lived only in
        ``atomic.tool_args``; the worker fell back to ``memoryboard_lookup``
        and reported "missing node_id" to the user). Observed on
        chatflow ``019dd8a8`` 2026-04-29 with qwen36-q4km, which
        actually commits to ``step_kind=tool_call`` (cloud models
        prefer ``draft`` and accidentally hide the bug).

        The new path mirrors the LLM-emitted-tool-uses pattern in
        :func:`workflow_engine._spawn_real_tool_calls_for_parent`:
        one TOOL_CALL with ``tool_name + tool_args`` verbatim, then a
        follow-up DRAFT (role=None) that the engine fills with the
        spliced tool_result via the standard ancestor walk. The
        follow-up routes to ``judge_post`` through the role-less
        DRAFT branch in the post-node hook — same shape the engine
        already takes when a worker emits tool_uses, so the rest of
        the orchestration (judge_post / aggregation / retry) stays
        unchanged.
        """
        tc = WorkFlowNode(
            step_kind=StepKind.TOOL_CALL,
            parent_ids=[parent_node.id],
            tool_name=atomic.tool_name,
            tool_args=dict(atomic.tool_args or {}),
            description=EditableText.by_agent(atomic.description),
            expected_outcome=(
                EditableText.by_agent(atomic.expected_outcome)
                if atomic.expected_outcome
                else None
            ),
            # Pin the caller capability context to exactly the tool the
            # planner authorized. Without this the parent (planner_judge)
            # is a cognitive node whose effective_tools is a read-only
            # set or None — ``_resolve_caller_effective_tools`` would
            # then return frozenset() and any tool gating on
            # ``ctx.caller_effective_tools`` (e.g.
            # ``get_node_context.cross_chatflow``) would lock out a
            # call the planner explicitly committed to. The resolver's
            # PR 472 update prefers the tool_call's own
            # effective_tools when set, so this single-element list is
            # the authoritative caller surface for this call.
            effective_tools=[atomic.tool_name],
        )
        inner.add_node(tc)
        follow_up = WorkFlowNode(
            step_kind=StepKind.DRAFT,
            parent_ids=[tc.id],
            model_override=inner.tool_call_model_override or resolved_model,
        )
        inner.add_node(follow_up)
        return follow_up

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
        # M7.5 (C) capability_request feedback loop: pre-render the
        # worker's emitted capability_request list as a string so the
        # judge fixture can drop it into the prompt. ``[]`` (empty
        # list) renders as "[]" so the LLM can see the empty marker
        # without ambiguity. Non-empty lists render as
        # ``["X", "Y"]``-style JSON for unambiguous copying.
        cap_req = worker_node.capability_request or []
        cap_req_str = json.dumps(cap_req, ensure_ascii=False)
        # Prong 2 (2026-04-30): same pattern for missing_input.
        # worker_node.missing_input was populated by the engine after
        # the worker's draft completed (see workflow_engine.py
        # _extract_missing_input).
        missing_inputs = worker_node.missing_input or []
        missing_inputs_str = json.dumps(missing_inputs, ensure_ascii=False)
        # Bug A layer 1 (2026-04-30): tool-result truth ledger.
        # Cross-checks worker narrative against engine-recorded
        # tool_result.is_error so judge can catch "Glob 失败 (lying)"
        # when the engine actually has is_error=False.
        tool_ledger = _render_tool_result_ledger(inner, worker_node)
        # Bug A layer 2 (2026-04-30): engine-pre-flagged fabricated-
        # failure explanations from the worker's own narrative scan.
        # The fixture renders these as a "must-read engine red flag"
        # section so weak judge models don't need to spot the lie
        # from the ledger alone.
        fabricated_flags = _render_fabricated_failure_flags(
            worker_node.suspected_fabricated_failure or []
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
                "worker_capability_request": cap_req_str,
                "worker_missing_input": missing_inputs_str,
                "tool_result_ledger": tool_ledger,
                "worker_suspected_fabricated_failure": fabricated_flags,
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
                    else:
                        log.warning(
                            "post_node_hook: DURING judge %s has unexpected "
                            "role=%r — no handler will run; WorkFlow may stall",
                            node.id,
                            node.role,
                        )
                    return
                if node.judge_variant == JudgeVariant.POST:
                    self._after_judge_post(
                        workflow,
                        node,
                        user_message_text=user_message_text,
                        context_wire=context_wire,
                        resolved_model=chat_node.resolved_model,
                        chatflow=chatflow,
                    )
                return

            if node.step_kind == StepKind.DELEGATE:
                self._after_delegation(
                    workflow,
                    node,
                    user_message_text=user_message_text,
                    context_wire=context_wire,
                    resolved_model=chat_node.resolved_model,
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
        # M7.5 PR 7 — cognitive ReAct DAG. When judge_pre asked for
        # recon and the chatflow opted in, branch into the DAG path:
        # spawn read-only tool_calls + a follow-up judge_pre that
        # reruns with their results. The follow-up's verdict reaches
        # this hook again (recon_plan empty by design — a single
        # round per design §4.4) and falls through to the normal
        # apply-trio + halt-or-continue branch.
        #
        # ``_spawn_recon_chain`` returns ``False`` when every spec
        # dropped (e.g. all WRITE side_effect, all chatflow-disabled).
        # In that case fall through to the atomic path below so the
        # planner / halt branch still runs — otherwise the chain
        # dies at judge_pre with no terminal llm_call and the
        # ChatNode goes FAILED.
        if (
            verdict is not None
            and verdict.recon_plan
            and self._cognitive_react_enabled_for_pre(
                chatflow, workflow, judge_pre_node
            )
        ):
            spawned = self._spawn_recon_chain(
                workflow,
                judge_pre_node,
                verdict,
                resolved_model=resolved_model,
            )
            if spawned:
                return
            # else: fall through, atomic path below applies trio +
            # spawns planner.
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
        nodes_before = len(workflow.nodes)
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
            plan = _parse_planner_output_message(planner_node.output_message)
        except PlannerParseError as exc:
            # Ops-facing hint: parse failures usually mean the model
            # picked ``mode: atomic`` (or decompose) but skipped the
            # required body. ``response_format=json_schema`` enforced at
            # provider level prevents this physically — the gate in
            # openai_compat.py only emits it when the per-model
            # ``json_mode`` is set to ``"schema"``. Surface the tip in
            # the log so anyone debugging a parse_error halt sees the
            # config knob without needing to read the source.
            log.warning(
                "planner_parse_error: workflow=%s planner=%s — %s. "
                "Tip: configure the model's ``json_mode`` to ``schema`` "
                "in its ProviderConfig to enforce the planner schema at "
                "decoding time and prevent this class of error. Eligible "
                "providers (no-tools paths): all openai_compat sub_kinds "
                "(openai_chat / volcengine / ollama / llamacpp); "
                "tools+schema coexistence currently allow-listed for "
                "openai_chat and volcengine only.",
                workflow.id,
                planner_node.id,
                exc,
            )
            planner_count = sum(
                1 for n in workflow.nodes.values()
                if n.role == WorkNodeRole.PLAN
            )
            if planner_count < 2:
                self._spawn_planner(
                    workflow,
                    planner_judge_node,
                    resolved_model=resolved_model,
                    prior_plan=_render_planner_output_for_prompt(planner_node),
                    critique=_compose_planner_retry_critique(
                        exc, planner_judge_node
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

        # Surface the planner's reasoning at INFO. The schema field's
        # main purpose is to nudge json_schema-enforced models to
        # "think first" via the field-order trick, but historically
        # the parsed value was discarded — operators couldn't see the
        # planner's framing without re-reading raw output_message.
        # Logging here keeps it debuggable without coupling to UI.
        if plan.reasoning:
            log.info(
                "planner reasoning: workflow=%s planner=%s mode=%s — %s",
                workflow.id,
                planner_node.id,
                plan.mode,
                plan.reasoning,
            )

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
                        prior_plan=_render_planner_output_for_prompt(planner_node),
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
            # Honor planner's ``step_kind=tool_call`` decision: when
            # the planner committed to a specific registered tool with
            # explicit ``tool_args``, run that tool directly instead of
            # spawning a draft worker that would have to re-derive the
            # call. The phantom-tool-name guard above already filtered
            # out unregistered names, so reaching this branch means the
            # tool is real and the planner's args are authoritative.
            # (See ``_spawn_atomic_tool_call`` for the why.)
            if (
                plan.atomic.step_kind == StepKind.TOOL_CALL
                and plan.atomic.tool_name
                and self._tools is not None
                and self._tools.has(plan.atomic.tool_name)
            ):
                self._spawn_atomic_tool_call(
                    workflow,
                    planner_judge_node,
                    plan.atomic,
                    resolved_model=resolved_model,
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
        # AUTO-mode sub-WorkFlow recursively. Depth-fused: a WorkFlow
        # at or beyond ``delegation_depth_budget`` forces any further
        # ``decompose`` plan into an atomic draft so the tree can't
        # keep fanning out (the integration-test incident 2026-04-22
        # had 62 nodes 3 tool_calls from one "DeepWiki + Cloudflare"
        # prompt). Aggregation at the layer above still happens in
        # ``_after_delegation`` once all siblings complete.
        if (
            decision == "continue"
            and plan.mode == "decompose"
            and plan.subtasks is not None
        ):
            if workflow.delegation_depth >= workflow.delegation_depth_budget:
                log.info(
                    "delegation-depth fuse: workflow=%s depth=%d budget=%d — "
                    "forcing decompose plan into atomic draft",
                    workflow.id,
                    workflow.delegation_depth,
                    workflow.delegation_depth_budget,
                )
                atomic_brief = AtomicBrief(
                    step_kind=StepKind.DRAFT,
                    description=(
                        "Handle the remaining task directly in a single "
                        "draft — delegation depth cap reached, no further "
                        "decomposition allowed."
                    ),
                    expected_outcome=(
                        "Concrete, user-ready response to the original "
                        "task description, synthesized without spawning "
                        "additional sub-workflows."
                    ),
                )
                self._spawn_worker(
                    workflow,
                    planner_judge_node,
                    atomic_brief,
                    resolved_model=resolved_model,
                )
                return
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
                prior_plan=_render_planner_output_for_prompt(planner_node),
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

        # Defence-in-depth: every branch above should have grown the DAG
        # (spawn_worker / spawn_planner / spawn_decompose_delegations /
        # spawn_judge_post via _halt_to_post_judge). If the planner_judge
        # returns without adding a single node the WorkFlow will starve
        # — the driver loop runs out of ready nodes and the chat layer
        # reports "inner workflow had no terminal llm_call". Log loudly
        # and force a halt so the user sees a real error instead of the
        # opaque one.
        if len(workflow.nodes) == nodes_before:
            log.error(
                "planner_judge %s returned without spawning anything: "
                "decision=%r plan_mode=%r verdict=%r — forcing halt_to_post_judge",
                planner_judge_node.id,
                decision,
                plan.mode,
                verdict,
            )
            self._halt_to_post_judge(
                workflow,
                parent_node=planner_judge_node,
                upstream_kind="planner_judge_fallthrough",
                upstream_summary=(
                    "internal: planner_judge handler exited without "
                    "scheduling any follow-up node — the planner pipeline "
                    "had no branch that matched its verdict. "
                    f"decision={decision!r} plan.mode={plan.mode!r}"
                ),
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

        # M7.5 (C) capability_request feedback loop. If the worker_judge
        # filled ``capability_escalation`` (because the worker emitted
        # ``<capability_request>`` markers), don't spawn a fresh worker
        # under the same plan — the worker is correctly stuck without
        # those tools. Instead:
        # 1. Widen ``WorkFlow.inheritable_tools`` to include the
        #    escalated names (union, not replace). Subsequent planner
        #    decompositions allocate ``effective_tools`` to children
        #    from this widened set.
        # 2. Spawn a fresh planner with handoff_notes citing the
        #    escalation + the prior worker's critique. The new plan
        #    can re-decompose with the now-available tools.
        # 3. The existing post-node hook chain (planner → planner_judge
        #    → worker → worker_judge) takes over; the new worker has
        #    access to the escalated tools because it sees the widened
        #    catalog through ``inheritable_tools``.
        # Cross-checked against ``_apply_judge_pre_trio`` — that helper
        # writes ``inheritable_tools`` from judge_pre's first-turn
        # extraction; widening here is the multi-turn / mid-execution
        # complement (the design doc §10.5 emergent finding called this
        # out as the missing follow-up; here it is).
        if (
            verdict is not None
            and verdict.capability_escalation
            and worker_node is not None
            and _round_index_for(workflow, worker_node) < workflow.debate_round_budget
        ):
            escalated = list(verdict.capability_escalation)
            existing = set(workflow.inheritable_tools or [])
            workflow.inheritable_tools = sorted(existing | set(escalated))
            log.info(
                "capability_escalation: widened workflow=%s inheritable_tools "
                "by %d new tool(s) — escalated=%r, total now=%d",
                workflow.id,
                len(set(escalated) - existing),
                escalated,
                len(workflow.inheritable_tools),
            )
            handoff = (
                f"Prior worker (id={worker_node.id}) emitted a "
                f"capability_request for tool(s): {', '.join(escalated)}. "
                "The engine has widened this WorkFlow's inheritable_tools "
                "to include those names. Re-plan with the widened set in "
                "mind — the new worker should use the requested tools to "
                "complete what the prior worker couldn't."
            )
            critique_text = _render_critiques(verdict)
            self._spawn_planner(
                workflow,
                worker_judge_node,
                resolved_model=resolved_model,
                prior_plan=(
                    worker_node.output_message.content
                    if worker_node.output_message
                    else ""
                ),
                critique=critique_text,
                handoff_notes=handoff,
            )
            return

        # Prong 3 (2026-04-30) missing_input feedback loop. Symmetric
        # to capability_escalation above: when worker_judge bubbles
        # ``missing_input_escalation`` (because the worker emitted
        # ``<missing_input>`` markers indicating planner brief
        # paraphrased away context), don't retry under the same plan
        # — the worker is correctly stuck without the data. Spawn a
        # fresh planner with handoff_notes describing what was
        # missing so the new brief inlines the data verbatim. Shares
        # ``debate_round_budget`` (no separate budget surface; the
        # existing fuse caps re-plan rounds).
        # See ``docs/backlog-decompose-fact-loss.md`` prong 3.
        if (
            verdict is not None
            and verdict.missing_input_escalation
            and worker_node is not None
            and _round_index_for(workflow, worker_node) < workflow.debate_round_budget
        ):
            missing = list(verdict.missing_input_escalation)
            log.info(
                "missing_input_escalation: workflow=%s worker=%s — "
                "%d gap(s) reported by worker, re-planning. gaps=%r",
                workflow.id,
                worker_node.id,
                len(missing),
                missing,
            )
            handoff = (
                f"Prior worker (id={worker_node.id}) emitted "
                f"missing_input signal(s): {'; '.join(missing)}. The "
                "previous subtask description paraphrased away material "
                "the worker needed. Re-plan with the missing material "
                "inlined VERBATIM into the new subtask description — "
                "do NOT use pointer-style references (\"based on the X "
                "above\"). The sub-WorkFlow cannot see this turn's "
                "ChatFlow conversation; the description is its only "
                "context."
            )
            critique_text = _render_critiques(verdict)
            self._spawn_planner(
                workflow,
                worker_judge_node,
                resolved_model=resolved_model,
                prior_plan=(
                    worker_node.output_message.content
                    if worker_node.output_message
                    else ""
                ),
                critique=critique_text,
                handoff_notes=handoff,
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
        resolved_model: ProviderModelRef | None = None,
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

        Prong 4 (2026-04-30, see docs/backlog-decompose-fact-loss.md):
        when ALL members of the decompose group have reached terminal
        status AND any FAILED member's sub-WorkFlow surfaced
        ``missing_input_escalation`` in its judge_post, treat the
        whole group as a missing-input signal at this layer and
        spawn a fresh planner under the owning planner_judge BEFORE
        falling through to the normal aggregator. The fresh planner
        gets handoff_notes describing what each failed sub reported
        missing, and is expected to write a new plan whose subtask
        descriptions inline the missing material verbatim. ``resolved_model``
        is required for the re-spawn — defaults to ``None`` for legacy
        callers / tests that don't trigger the re-plan branch (it
        only fires when a FAILED sub had missing_input_escalation,
        which legacy paths can't reproduce).
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
        # Aggregate once every member has reached a terminal status
        # (SUCCEEDED, FAILED, or CANCELLED) — not only when they all
        # succeeded. The 2026-04-22 self-analysis incident had a 4-member
        # decompose group where the aggregator sub-agent's pre_judge
        # halted (FAILED); the all-SUCCEEDED gate suppressed aggregation
        # and the ChatNode died with "no terminal llm_call". Let the
        # outer judge_post see the mixed outcomes via
        # _format_decompose_aggregation and decide retry / accept /
        # escalate — partial aggregate is a first-class path (M12.4d5).
        _terminal = {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}
        if not all(m.status in _terminal for m in members):
            return
        # Guard against duplicate spawning when multiple delegations
        # finish in rapid succession.
        if _decompose_group_already_aggregated(workflow, owner.id):
            return

        # Prong 4 (2026-04-30): missing_input cascade. Inspect each
        # FAILED member's sub-WorkFlow for a judge_post that bubbled
        # ``missing_input_escalation``; if any did, the failure was
        # specifically because the planner brief paraphrased away
        # context the sub-worker needed. Re-plan at this layer
        # instead of letting the aggregator judge_post emit fail —
        # the planner can write a better brief now that we know what
        # was missing.
        cascade_gaps = _collect_missing_input_from_failed_members(members)
        if (
            cascade_gaps
            and _round_index_for(workflow, owner) < workflow.debate_round_budget
        ):
            log.info(
                "delegation missing_input cascade: workflow=%s owner=%s — "
                "%d failed sub(s) reported %d unique gap(s); re-planning",
                workflow.id,
                owner.id,
                sum(1 for m in members if m.status == NodeStatus.FAILED),
                len(cascade_gaps),
            )
            handoff = (
                "One or more sub-WorkFlow delegations from your prior "
                "plan halted because their workers couldn't access "
                "context the brief paraphrased away. Reported gap(s):\n"
                + "\n".join(f"- {g}" for g in cascade_gaps)
                + "\n\nRe-plan this layer with the missing material "
                "inlined VERBATIM into the new subtask description(s). "
                "Pointer-style references (\"based on the X above\") "
                "are forbidden — sub-WorkFlows cannot see this turn's "
                "ChatFlow conversation; the description is their only "
                "context. Either (a) decompose again with verbatim "
                "data inlined per subtask, or (b) switch to atomic "
                "if the task no longer benefits from parallel split."
            )
            self._spawn_planner(
                workflow,
                owner,
                resolved_model=resolved_model,
                handoff_notes=handoff,
            )
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

    def _recover_dangling_decompose(
        self,
        workflow: WorkFlow,
        *,
        user_message_text: str,
    ) -> bool:
        """Scan *workflow* for decompose groups whose members all succeeded
        but whose aggregating judge_post is missing, and spawn it.

        Returns ``True`` if at least one aggregator was spawned; the
        caller should re-run :meth:`WorkflowEngine.execute` so the new
        node actually runs. Returns ``False`` when every decompose group
        already has its aggregator (normal happy path) or when there are
        no decompose groups at all (native_react turns).

        Why this exists: the post_node_hook that normally spawns the
        aggregator is wrapped in a blanket ``try/except`` in
        ``WorkflowEngine._run_node``. Any exception raised inside
        ``_after_delegation`` is logged but otherwise swallowed, and the
        WorkFlow settles with no judge_post — the ChatFlow layer then
        reports "inner workflow had no terminal llm_call". This recovery
        runs once after execute() returns and catches that race.
        """
        if not any(
            n.step_kind == StepKind.DELEGATE
            for n in workflow.nodes.values()
        ):
            return False

        # Find every plan_judge that owns a decompose group.
        plan_judges: list[WorkFlowNode] = [
            n
            for n in workflow.nodes.values()
            if n.step_kind == StepKind.JUDGE_CALL
            and n.role == WorkNodeRole.PLAN_JUDGE
        ]
        spawned_any = False
        _terminal = {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}
        for owner in plan_judges:
            members = _decompose_group_members(workflow, owner.id)
            if not members:
                continue
            # Same loosening as ``_after_delegation``: require every
            # member to be terminal (any of SUCCEEDED / FAILED /
            # CANCELLED), not only SUCCEEDED. _format_decompose_aggregation
            # handles mixed outcomes and judge_post can still produce a
            # partial-aggregate user-facing reply.
            if not all(m.status in _terminal for m in members):
                continue
            if _decompose_group_already_aggregated(workflow, owner.id):
                continue

            log.warning(
                "dangling decompose group recovered: workflow=%s owner=%s "
                "members=%d — spawning aggregator retroactively",
                workflow.id,
                owner.id,
                len(members),
            )
            upstream_summary = _format_decompose_aggregation(workflow, members)
            worknode_catalog = "\n".join(
                f"{m.id}: sub_agent_delegation for "
                f"{(m.description.text if m.description else '').strip() or '(no description)'}"
                for m in members
            )
            # Use the last-finishing member as parent_node for wiring
            # parity with the normal _after_delegation path; the
            # aggregator's parent_ids get overwritten to all members.
            last = max(
                members,
                key=lambda m: m.finished_at or m.updated_at or m.created_at,
            )
            try:
                aggregator = self._spawn_judge_post(
                    workflow,
                    user_message_text=user_message_text,
                    context_wire=[],
                    parent_node=last,
                    upstream_kind="decompose_aggregation",
                    upstream_summary=upstream_summary,
                    worknode_catalog=worknode_catalog,
                )
                aggregator.parent_ids = [m.id for m in members]
                spawned_any = True
            except Exception:  # noqa: BLE001 — defensive
                log.exception(
                    "dangling decompose recovery failed to spawn aggregator: "
                    "workflow=%s owner=%s",
                    workflow.id,
                    owner.id,
                )
        return spawned_any

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
        # Append upstream outputs as an additional conversation message so
        # judge_pre distills a trio that references them. The spawn-time
        # subtask description wire message stays in place at the front.
        judge_pre.input_messages = [
            *(judge_pre.input_messages or []),
            WireMessage(
                role="user",
                content=f"Upstream dependency outputs:\n\n{injected}",
            ),
        ]

    def _after_judge_post(
        self,
        workflow: WorkFlow,
        judge_post_node: WorkFlowNode,
        *,
        user_message_text: str,
        context_wire: list[WireMessage],
        resolved_model: ProviderModelRef | None,
        chatflow: ChatFlow | None = None,
    ) -> None:
        """judge_post finished. Decide what happens next:

        - ``recon_plan`` (when ``cognitive_react_enabled`` and the
          judge_post itself isn't already a recon follow-up): spawn
          read-only tool_calls + a follow-up judge_post that re-runs
          with the recon results in context. Sub-task 3 of cognitive
          ReAct DAG productionization. Targets the
          ``outputs_match=True / db_hash_match=False`` symptom on
          retail tau-bench: agent says "I changed the order" but the
          mock DB shows otherwise. judge_post calls
          ``get_order_details``, sees the discrepancy, then can issue
          a ``retry`` verdict instead of accepting.
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

        # M7.5 PR 7 sub-task 3 — cognitive ReAct DAG for judge_post.
        # When the judge asked for recon and this isn't already a
        # recon follow-up, branch into the DAG path and skip the
        # accept/retry/fail decision tree (the follow-up will re-run
        # this hook with a fresh verdict that doesn't carry recon).
        if (
            chatflow is not None
            and verdict.recon_plan
            and self._cognitive_react_enabled_for_post(
                chatflow, workflow, judge_post_node
            )
        ):
            spawned = self._spawn_recon_chain_for_post(
                workflow,
                judge_post_node,
                verdict,
                resolved_model=resolved_model,
            )
            if spawned:
                return
            # else: every spec dropped, fall through to original verdict.

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
                # Reconstruct a SubTask from the delegation's description
                # and append the judge's critique so the fresh sub-
                # WorkFlow's judge_pre + planner see what to fix.
                desc = original.description.text if original.description else ""
                critique_suffix = (
                    f"\n\n[critique from prior attempt]\n{target.critique}"
                    if target.critique
                    else ""
                )
                subtask = SubTask(
                    description=desc + critique_suffix,
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
            frozenset(chatflow.disabled_tool_names)
            | frozenset(ws_settings.globally_disabled())
            | self._foreign_tau_tools(chatflow.id)
        )
        # Compact ChatNodes (auto-inserted by the dual-track trigger OR
        # queued by an explicit user compact) run the single-shot compact
        # worker. Disable Tier 1 inside them — the worker already IS a
        # compaction — and skip the judge post-node hook (only relevant
        # for turn nodes in semi_auto / auto mode).
        is_compact_node = chat_node.compact_snapshot is not None
        # Scope the "nodes fetched via get_node_context this turn"
        # signal to this ChatNode's inner-workflow execution. contextvars
        # are task-local so concurrent sibling ChatNodes open their own
        # scopes and their accessed sets stay isolated.
        accessed_scope_cm = accessed_scope()
        accessed_this_turn = accessed_scope_cm.__enter__()
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
                chatflow_compact_keep_recent_count=chatflow.compact_keep_recent_count,
                chatflow_compact_preserve_mode=chatflow.compact_preserve_mode,
                chatflow_compact_model=chatflow.compact_model,
                chatflow_runtime_environment_note=self._resolve_runtime_note(chatflow),
                chatflow_tool_catalog=self._resolve_tool_catalog(chatflow),
                chatflow_max_produced_tags=chatflow.max_produced_tags,
                chatflow_max_consumed_tags=chatflow.max_consumed_tags,
                chatflow_id=chatflow.id,
                post_node_hook=(
                    None
                    if is_compact_node
                    else self._build_post_node_hook(chat_node, chatflow)
                ),
                disabled_tool_names=effective_disabled,
                chatflow_extra_capabilities=self._resolve_extra_capabilities(),
            )
        except Exception as exc:  # noqa: BLE001 — engine boundary
            log.exception("chat node %s inner workflow raised", node_id)
            runtime_error = f"{type(exc).__name__}: {exc}"
        finally:
            accessed_scope_cm.__exit__(None, None, None)
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

        # Dangling-decompose recovery (bug #1 from 2026-04-22 integration
        # test): post_node_hook.`_after_delegation` normally spawns the
        # aggregating judge_post when the last delegate of a decompose
        # group succeeds, but the hook's blanket try/except silently
        # swallows any exception it raises — leaving the WorkFlow ended
        # with "no terminal llm_call" and no user-facing output. Detect
        # that shape here and spawn the aggregator retroactively so the
        # turn still produces a judge_post reply instead of failing with
        # an opaque internal error.
        if runtime_error is None and not is_compact_node:
            if self._recover_dangling_decompose(
                chat_node.workflow,
                user_message_text=chat_node.user_message.text if chat_node.user_message else "",
            ):
                # A new judge_post was spawned; rerun execute so it runs.
                # The WorkflowEngine._post_node_hook is still set to fire
                # when it succeeds (for accept/retry/fail handling).
                try:
                    await self._inner.execute(
                        chat_node.workflow,
                        chatflow_runtime_environment_note=self._resolve_runtime_note(chatflow),
                chatflow_tool_catalog=self._resolve_tool_catalog(chatflow),
                chatflow_max_produced_tags=chatflow.max_produced_tags,
                chatflow_max_consumed_tags=chatflow.max_consumed_tags,
                        chatflow_id=chatflow.id,
                        disabled_tool_names=effective_disabled,
                        chatflow_extra_capabilities=self._resolve_extra_capabilities(),
                    )
                except Exception as exc:  # noqa: BLE001 — engine boundary
                    log.exception(
                        "recovery aggregator execute raised for chat node %s",
                        node_id,
                    )
                    runtime_error = f"{type(exc).__name__}: {exc}"

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
                    resolved_for_cap = None
                    if terminal is not None:
                        resolved_for_cap = (
                            terminal.resolved_model or terminal.model_override
                        )
                    summary_text = self._finalize_compact_chatnode_snapshot(
                        chatflow, chat_node, summary_text, resolved_for_cap
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
                # Defensive fallback (issue #1, 2026-04-29 qwen36
                # batch): a non-accept post_judge verdict with a
                # populated ``user_message`` is the next-best thing
                # to a real reply when the rest of the path silently
                # failed. Surfacing the judge's diagnostic gives the
                # user something actionable instead of the opaque
                # engine error.
                fallback = _last_judge_post_user_message(
                    chat_node.workflow
                )
                if fallback:
                    chat_node.agent_response = EditableText.by_agent(
                        fallback
                    )
                    chat_node.status = NodeStatus.SUCCEEDED
                    log.warning(
                        "chat_node %s had no terminal llm_call; "
                        "fell back to last post_judge user_message "
                        "(retry/fail flow likely lost its hook spawn)",
                        chat_node.id,
                    )
                else:
                    chat_node.status = NodeStatus.FAILED
                    chat_node.error = (
                        "inner workflow had no terminal llm_call — "
                        + _summarize_workflow_for_error(chat_node.workflow)
                    )
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

            # Sticky-restore update: fold this turn's get_node_context
            # hits into *this* ChatNode's own sticky_restored and decay
            # any entries that weren't re-touched. No-op on compact
            # ChatNodes (a compact is a new cutoff, sticky resets) and
            # on FAILED turns (don't let a crash evict everything
            # before the user retries). See
            # ``_update_sticky_restored_for_node`` for the rule set.
            if (
                chat_node.status == NodeStatus.SUCCEEDED
                and not is_compact_node
            ):
                _update_sticky_restored_for_node(
                    chatflow,
                    chat_node,
                    accessed_this_turn,
                    chatflow.recalled_context_sticky_turns,
                )

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
        ``board_writer``.

        Called synchronously from every ChatNode-success path: plain
        turn nodes, Tier-2 compact nodes, and manual merge nodes. Runs
        the ``chat_brief`` fixture through ``self._provider`` to distill
        the turn's user input + agent reply into a one-to-two-sentence
        description; on provider failure, missing model, or an empty
        reply, falls back to the deterministic :func:`_chat_board_description`
        code template and marks the row ``fallback=True``.

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
        # Pull ancestor produced_tags out of the existing ChatBoardItem
        # rows so the brief LLM has anchors when filling consumed_tags.
        # See ``_aggregate_active_tags`` for the dead-status filter.
        ancestral = await self._collect_chat_ancestral_active_tags(chatflow, node)
        description, produced_tags, consumed_tags, fallback = (
            await self._render_chat_board_description(
                chatflow, node, source_kind, ancestral
            )
        )
        # Drill-down pointers — collected fresh each upsert because pack
        # range / merged parents / WorkBoard membership can shift
        # between retries, and the writer is the source of truth.
        inner_chat_ids = _collect_inner_chat_ids(node)
        work_node_ids = _collect_work_node_ids(node)
        footer = _drill_down_footer(
            self._current_fixture_language,
            inner_chat_ids,
            work_node_ids,
        )
        if footer:
            description = f"{description}\n\n{footer}" if description else footer
        try:
            await writer(
                chatflow_id=chatflow.id,
                workflow_id=None,
                source_node_id=node.id,
                source_kind=source_kind,
                scope="chat",
                description=description,
                fallback=fallback,
                inner_chat_ids=inner_chat_ids,
                work_node_ids=work_node_ids,
                produced_tags=produced_tags,
                consumed_tags=consumed_tags,
            )
        except Exception:  # noqa: BLE001 — board is best-effort
            log.exception(
                "ChatBoardItem write failed for chatflow=%s node=%s "
                "kind=%s — ChatNode state is unchanged",
                chatflow.id,
                node.id,
                source_kind,
            )

    async def _collect_chat_ancestral_active_tags(
        self, chatflow: ChatFlow, node: ChatFlowNode
    ) -> list[str]:
        """Walk the primary-parent chain from root to *node*'s parent
        and return the ancestor concept anchor set.

        Implementation:
        1. Walk chain: node.parent_ids[0] → … → root, reverse to root-first.
        2. Fetch every ChatBoardItem row in this chatflow once (one SQL
           round-trip), index by source_node_id.
        3. Aggregate ``produced_tags`` from each ancestor row in
           chronological order via :func:`_aggregate_active_tags` —
           dead-status concepts (``_rejected`` / ``_deferred``) are
           filtered out.

        Best-effort: no DB session, missing rows, or fetch errors all
        return ``[]`` so the brief still runs (just without ancestor
        anchors).
        """
        if self._tool_ctx is None:
            return []
        chain = _walk_chat_chain_to_root(chatflow, node)
        if not chain:
            return []
        from agentloom.db.base import get_session_maker
        from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID
        from agentloom.db.repositories.board_item import BoardItemRepository

        workspace_id = self._tool_ctx.workspace_id or DEFAULT_WORKSPACE_ID
        try:
            async with get_session_maker()() as session:
                repo = BoardItemRepository(session, workspace_id=workspace_id)
                rows = await repo.list_by_chatflow(chatflow.id)
        except Exception:  # noqa: BLE001 — best-effort, brief still runs
            log.exception(
                "ancestral tags fetch failed for chatflow=%s — brief will "
                "run without anchors",
                chatflow.id,
            )
            return []
        produced_by_source: dict[str, list[str]] = {
            row.source_node_id: list(row.produced_tags or [])
            for row in rows
            if row.scope == "chat"
        }
        # chain is root-first; the LAST element is node's primary parent.
        chain_tags = [
            produced_by_source.get(src, []) for src in chain
        ]
        return _aggregate_active_tags(chain_tags)

    async def _render_chat_board_description(
        self,
        chatflow: ChatFlow,
        node: ChatFlowNode,
        source_kind: str,
        ancestral_tags_active: list[str],
    ) -> tuple[str, list[str], list[str], bool]:
        """Produce the ChatBoardItem distillation for *node*.

        Returns ``(description, produced_tags, consumed_tags, fallback)``.
        Tries the LLM path first (``chat_brief`` fixture +
        ``chatflow.brief_model`` or ``draft_model``, with response_format
        json_schema enforced via :func:`brief_grammar_schema`); on no
        model, empty reply, or any provider exception, returns
        :func:`_chat_board_description` with empty tag arrays and
        ``fallback=True``. The brief is best-effort — its failure must
        never break the ChatNode cascade.
        """
        from agentloom.engine.brief_parser import (
            brief_grammar_schema,
            parse_brief_output,
        )

        model_ref = chatflow.brief_model or chatflow.draft_model
        if model_ref is None:
            return _chat_board_description(node), [], [], True
        from agentloom.engine.workflow_engine import _get_brief_fixture

        try:
            plan, includes = _get_brief_fixture("chat_brief")
        except Exception:  # noqa: BLE001 — missing fixture is a build error, but the brief is best-effort
            log.exception(
                "chat_brief fixture load failed for chatflow=%s — falling "
                "back to code template",
                chatflow.id,
            )
            return _chat_board_description(node), [], [], True

        user_text = node.user_message.text if node.user_message else ""
        agent_text = node.agent_response.text if node.agent_response else ""
        try:
            brief_wf = instantiate_fixture(
                plan,
                {
                    "source_kind": source_kind,
                    "user_message": user_text,
                    "agent_response": agent_text,
                    "max_produced_tags": chatflow.max_produced_tags,
                    "max_consumed_tags": chatflow.max_consumed_tags,
                    "ancestral_tags_active": " ".join(ancestral_tags_active),
                },
                includes=includes,
            )
            inner = _single_node(brief_wf)
            assert inner.input_messages is not None
            from agentloom.engine.workflow_engine import _wire_to_provider

            messages = _wire_to_provider(list(inner.input_messages))
            model_str = (
                f"{model_ref.provider_id}:{model_ref.model_id}"
                if model_ref.provider_id
                else model_ref.model_id
            )
            response = await self._provider(
                messages,
                [],
                model_str,
                json_schema=brief_grammar_schema(),
            )
            content = (response.message.content or "").strip()
            if not content:
                return _chat_board_description(node), [], [], True
            parsed = parse_brief_output(content)
            # Apply per-chatflow caps post-parse — the prompt bounds
            # them but a stray model might overshoot. Caps are also
            # the upper bound on the indexed tag set; over-cap entries
            # are dropped silently rather than rejected.
            produced = parsed.produced_tags[: chatflow.max_produced_tags]
            consumed = parsed.consumed_tags[: chatflow.max_consumed_tags]
            return parsed.description, produced, consumed, False
        except Exception as exc:  # noqa: BLE001 — brief is best-effort
            log.warning(
                "chat_brief LLM call failed for chatflow=%s node=%s — "
                "falling back to code template: %s",
                chatflow.id,
                node.id,
                exc,
            )
            return _chat_board_description(node), [], [], True

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


def _summarize_workflow_for_error(workflow: WorkFlow) -> str:
    """Compact one-line summary of a WorkFlow's nodes for error messages.

    Used when the chat layer has to raise a "no terminal llm_call" (or
    similar) diagnostic: a bare message forces us to re-read the DB to
    know what actually happened, so embed a kind/role/status histogram
    and the ids of any FAILED nodes right in the error. Keep it short
    — this string lands in ``ChatFlowNode.error`` and the UI surfaces it
    as a toast.
    """
    counts: dict[str, int] = {}
    failed_ids: list[str] = []
    for n in workflow.nodes.values():
        kind = n.step_kind.value if n.step_kind else "?"
        role = n.role.value if n.role else "-"
        status = n.status.value if n.status else "?"
        key = f"{kind}/{role}:{status}"
        counts[key] = counts.get(key, 0) + 1
        if n.status == NodeStatus.FAILED:
            failed_ids.append(n.id)
    parts = [f"{k}×{v}" for k, v in sorted(counts.items())]
    summary = "nodes=[" + ", ".join(parts) + "]"
    if failed_ids:
        summary += f" failed_ids={failed_ids[:5]}"
    return summary


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
    # M7.5 capability model: judge_pre emits TWO lists.
    # ``extracted_capabilities`` (natural-language) →
    # ``WorkFlow.capabilities_origin``; ``extracted_inheritable_tools``
    # (registry names) → ``WorkFlow.inheritable_tools``.
    #
    # Cross-turn accumulation (2026-04-28): both fields **union** with
    # whatever's already on the WorkFlow instead of replacing. The
    # WorkFlow comes seeded from the prior turn's WorkFlow (see
    # ``_spawn_turn_node``), so a fresh extraction is additive. Why:
    # judge_pre runs against the current turn's user words alone; in
    # multi-turn auto_plan it consistently mis-judged the standing
    # tool surface as "just what's needed for this exact ask"
    # (2026-04-26 tau-bench retail batch: 3 of 3 tasks landed
    # reward=0 because turn-2 judge_pre picked 2 / 16 of the tools
    # turn-1 had granted, then later turn workers couldn't find the
    # ones turn-1 needed). Replace-semantics also clobbered any
    # mid-turn ``capability_escalation`` widening from
    # ``_after_worker_judge``. Union preserves the standing surface
    # AND lets judge_pre / capability_escalation grow it.
    #
    # Order-preserving union — keep existing order, append new
    # entries that aren't already present. Stable for prompt-cache
    # locality (the prompt's ``capabilities`` slot is rendered from
    # ``capabilities_origin`` joined with ", "; reordering would
    # blow the cache prefix on every turn).
    def _union_preserve_order(prior: list[str], new: list[str]) -> list[str]:
        seen = set(prior)
        merged = list(prior)
        for item in new:
            if item not in seen:
                merged.append(item)
                seen.add(item)
        return merged

    if verdict.extracted_capabilities:
        workflow.capabilities_origin = _union_preserve_order(
            list(workflow.capabilities_origin or []),
            list(verdict.extracted_capabilities),
        )
    if verdict.extracted_inheritable_tools:
        workflow.inheritable_tools = _union_preserve_order(
            list(workflow.inheritable_tools or []),
            list(verdict.extracted_inheritable_tools),
        )


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


def _render_tool_result_ledger(
    workflow: WorkFlow,
    target_node: WorkFlowNode,
) -> str:
    """Bug A layer 1 (2026-04-30): compact ledger of every ancestor
    tool_call's outcome, rendered into worker_judge / judge_post
    fixtures so the judge can cross-check the worker's narrative
    against engine-recorded truth.

    Format (one bullet per ancestor tool_call, in topological order):

        - Glob({"pattern": "**/*.tsx"}) → is_error=False, content="..."
        - Read({"file_path": "/x.tsx"}) → is_error=True, content="..."

    The judge prompt is updated to flag a specific failure mode:
    when the worker's output_message claims a tool failed (e.g.
    "Glob 调用失败", "未获取", "tool returned nothing") AND the
    corresponding ledger entry shows ``is_error=False`` with
    non-empty content, that's a **fabricated failure** and the
    judge should issue revise / retry rather than passing the
    lie downstream. Surfaced 2026-04-30 on chatflow ``019de131``
    T6: worker said "Glob 失败" while the engine had a successful
    tool_result with 100+ ``.tsx`` paths in content.

    Returns the empty string when the target has no ancestor
    tool_calls — the fixture renders this as the absence of any
    tool history (judges judge on the trio + worker output alone,
    same as before the ledger landed).

    Content is truncated to ~140 chars per call to keep the ledger
    cheap (a typical run with 3 tool_calls adds ~500 tokens to
    each judge prompt).
    """
    ancestor_set = set(workflow.ancestors(target_node.id))
    rows: list[str] = []
    # Iterate in node-insertion order for stable, topology-respecting
    # output (workflow.nodes is a dict preserving insertion order).
    for nid, n in workflow.nodes.items():
        if nid not in ancestor_set:
            continue
        if n.step_kind != StepKind.TOOL_CALL:
            continue
        args_str = (
            json.dumps(n.tool_args, ensure_ascii=False)
            if n.tool_args
            else "{}"
        )
        if len(args_str) > 120:
            args_str = args_str[:117] + "..."
        tr = n.tool_result
        if tr is None:
            outcome = "(not yet executed)"
        else:
            content_preview = (tr.content or "").replace("\n", " ⏎ ")
            if len(content_preview) > 140:
                content_preview = content_preview[:137] + "..."
            outcome = f"is_error={tr.is_error}, content={content_preview!r}"
        rows.append(f"- `{n.tool_name}`({args_str}) → {outcome}")
    return "\n".join(rows)


def _render_fabricated_failure_flags(entries: list[str]) -> str:
    """Bug A layer 2 (2026-04-30) renderer.

    Pre-formats the list of engine-detected fabricated-failure
    explanations into the bullet-list shape the judge fixture
    embeds. ``[]`` renders as the empty string so the fixture's
    surrounding language ("if non-empty, …") works without needing
    a separate Jinja conditional.

    Each entry was authored by ``_scan_fabricated_failure`` and is
    already a single line referencing one specific (tool_name,
    matched_phrase, content_preview) triple. We render verbatim —
    the scanner is the source of truth on phrasing.
    """
    if not entries:
        return ""
    return "\n".join(f"- {e}" for e in entries)


def _aggregate_workflow_fabricated_failures(workflow: WorkFlow) -> list[str]:
    """Union of every WorkNode's ``suspected_fabricated_failure`` list.

    Used by ``_spawn_judge_post`` because the post-judge sees the
    whole WorkFlow, not just one ancestor chain. Order is stable
    (WorkFlow.nodes preserves insertion order), de-duplicated against
    the running output. Empty list when no WorkNode in the WorkFlow
    has any flag — typical for a happy-path run.
    """
    out: list[str] = []
    seen: set[str] = set()
    for n in workflow.nodes.values():
        for desc in n.suspected_fabricated_failure or []:
            if desc not in seen:
                seen.add(desc)
                out.append(desc)
    return out


def _compose_planner_retry_critique(
    parse_exc: "PlannerParseError",
    planner_judge_node: "WorkFlowNode | None",
) -> str:
    """Build the retry critique for a planner whose output failed schema
    validation.

    Originally there were two parallel feedback channels: when plan_judge
    voted ``revise`` we passed its ``critiques`` / ``user_message`` to the
    next planner; but when the parser raised ``PlannerParseError`` first
    we used a generic "your JSON failed to parse" string and silently
    dropped plan_judge's qualitative review. For weak models that
    sometimes select ``mode=atomic`` but forget to fill the ``atomic``
    body, having both signals (the precise schema error AND any
    qualitative note the judge already wrote) maximizes the chance the
    retry succeeds. Both are appended when available; the schema error
    always leads.
    """
    parts: list[str] = [
        f"Your previous plan output failed JSON parse: {parse_exc}.",
        (
            "Reply with ONLY valid JSON matching the required schema — "
            "every required field for the chosen mode must be populated. "
            "If you pick ``mode: atomic`` you MUST also fill the "
            "``atomic`` object (step_kind/description/expected_outcome); "
            "if you pick ``mode: decompose`` you MUST also fill the "
            "``subtasks`` array."
        ),
    ]
    if planner_judge_node is not None:
        verdict = planner_judge_node.judge_verdict
        if verdict is not None:
            rendered = _render_critiques(verdict)
            if rendered:
                parts.append(
                    f"Reviewer's structured critiques (JSON list):\n{rendered}"
                )
            msg = (verdict.user_message or "").strip()
            if msg:
                parts.append(f"Reviewer's qualitative note: {msg}")
    return "\n\n".join(parts)


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


def _collect_missing_input_from_failed_members(
    members: list[WorkFlowNode],
) -> list[str]:
    """Prong 4 helper: scan FAILED ``DELEGATE`` members for
    sub-WorkFlow judge_post verdicts that bubbled
    ``missing_input_escalation``. Returns the de-duplicated, ordered
    list of all gap descriptions across every failed sub.

    A sub-WorkFlow's judge_post sits as ``step_kind=JUDGE_CALL,
    judge_variant=POST`` inside the sub. We pick the most recent one
    (largest ``finished_at`` timestamp) per sub since later
    aggregator passes can supersede earlier verdicts. Returns an
    empty list when no failed sub had a missing-input signal —
    callers fall through to the normal aggregator path.
    """
    gaps: list[str] = []
    seen: set[str] = set()
    for m in members:
        if m.status != NodeStatus.FAILED or m.sub_workflow is None:
            continue
        post_judges = [
            n
            for n in m.sub_workflow.nodes.values()
            if n.step_kind == StepKind.JUDGE_CALL
            and n.judge_variant == JudgeVariant.POST
            and n.judge_verdict is not None
        ]
        if not post_judges:
            continue
        # Latest by finished_at, falling back to insertion order if
        # finished_at is None (engine sets it on success/failure).
        latest = max(
            post_judges,
            key=lambda n: (n.finished_at is not None, n.finished_at),
        )
        verdict = latest.judge_verdict
        if verdict is None:
            continue
        for desc in verdict.missing_input_escalation or []:
            cleaned = desc.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                gaps.append(cleaned)
    return gaps


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
#: ``_redo_clone_classification``. Two variants so the retry-halt
#: message follows the workspace language (`judge_formatter` handles
#: i18n for its own prompts; keep them aligned here).
_STATUS_HUMAN = {
    "ok": "completed successfully",
    "worker_failed": "worker failed",
    "sub_pre_halt": "sub-agent refused before starting",
    "sub_judge_post_failed": "sub-agent's reviewer crashed",
    "sub_judge_post_fail": "sub-agent's reviewer returned fail",
    "sub_judge_post_retry_exhausted": "sub-agent's retry budget ran out",
    "empty": "produced no output",
}
_STATUS_HUMAN_ZH = {
    "ok": "已成功完成",
    "worker_failed": "worker 执行失败",
    "sub_pre_halt": "子代理在启动前拒绝",
    "sub_judge_post_failed": "子代理的评审器崩溃",
    "sub_judge_post_fail": "子代理的评审器判定失败",
    "sub_judge_post_retry_exhausted": "子代理的重试预算耗尽",
    "empty": "未产生任何输出",
}


def _retry_halt_is_zh() -> bool:
    """Return True when the workspace is configured for Chinese. Falls
    back to en-US if the tenancy runtime isn't initialised (tests)."""
    try:
        from agentloom import tenancy_runtime
        from agentloom.db.models.tenancy import DEFAULT_WORKSPACE_ID

        lang = tenancy_runtime.get_settings(DEFAULT_WORKSPACE_ID).language
    except Exception:
        lang = "en-US"
    return lang.lower().startswith("zh")


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

    zh = _retry_halt_is_zh()
    status_map = _STATUS_HUMAN_ZH if zh else _STATUS_HUMAN
    lines: list[str] = []
    if reason == "budget_exhausted":
        if zh:
            lines.append(
                f"我重试了 {round_index} 轮，仍未拿到干净的结果，已触达 "
                f"{budget} 轮的重试预算。当前计划的各项状态如下："
            )
        else:
            lines.append(
                f"I retried {round_index} round(s) but hit the retry budget "
                f"of {budget} without landing a clean result. Here is where "
                "each part of the plan stands:"
            )
    else:  # no_usable_targets
        if zh:
            lines.append(
                "评审器要求重试，但它指定的目标都无法重新执行。"
                "当前计划的各项状态如下："
            )
        else:
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
            status, body = _classify_sub_outcome(surviving.sub_workflow)
        else:
            status, body = _redo_clone_classification(surviving)
        phrase = status_map.get(status, status)

        if round_idx == 1:
            round_note = ""
        elif zh:
            round_note = f"（经过 {round_idx - 1} 轮重试）"
        else:
            round_note = f" (after {round_idx - 1} retry round(s))"
        # Non-ok members: surface the concrete reason (judge_pre's
        # blockers / missing_inputs / user_message, or the worker error
        # string) so the user sees WHY the layer stalled, not just a
        # generic "refused before starting". Truncated to keep the
        # overall halt message readable.
        reason = ""
        if status != "ok":
            src = ""
            if body and body.strip():
                src = body.strip()
            elif surviving.error:
                src = surviving.error
            if src:
                cutoff = 240
                snippet = src if len(src) <= cutoff else src[: cutoff].rstrip() + "…"
                reason = (f"。详情：{snippet}" if zh else f" — {snippet}")
        lines.append(f"- **{label}** — {phrase}{round_note}{reason}")

    lines.append("")
    lines.append(
        "你想怎么往下走？" if zh else "How would you like to proceed?"
    )
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


#: Literal-string sentinels that some models (notably qwen36 q4km
#: under json_mode=schema) emit instead of leaving a nullable
#: field unset. Treated as "no value" for response-derivation —
#: a 4-byte string ``"null"`` reaching the user as
#: ``agent_response`` is a UX disaster (issue #6, 2026-04-29
#: qwen36 batch: ChatNode 9bc3c73a6773 had merged_response =
#: user_message = the literal string "null", so the user got the
#: word "null" as the agent's reply). Match case-insensitively
#: and trim surrounding whitespace before the comparison.
_FAKE_NULL_TOKENS: frozenset[str] = frozenset(
    {"null", "none", "n/a", "na", "nil", "undefined"}
)


def _is_fake_null(text: str | None) -> bool:
    """True iff ``text`` is None, empty after strip, or a model-
    emitted string sentinel that should be treated as empty."""
    if text is None:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return stripped.lower() in _FAKE_NULL_TOKENS


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

    Also returns ``None`` when both fields are *fake nulls* — the
    literal string ``"null"`` / ``"none"`` / etc. that some models
    emit under json_mode instead of leaving the field unset.
    Issue #6: qwen36 q4km had a verdict with merged_response =
    user_message = ``"null"``; without this filter the user got
    that 4-byte string as the agent's reply.
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
        if not _is_fake_null(v.merged_response):
            return v.merged_response
        if not _is_fake_null(v.user_message):
            return v.user_message
        return None
    return None


def _last_judge_post_user_message(workflow: WorkFlow) -> str | None:
    """Defensive fallback used by chat finalization when the normal
    response-derivation paths all returned None and there's no
    terminal llm_call to fall back on (decompose-mode workflow whose
    aggregator hook silently failed to spawn redo clones, or any
    other path where the post_judge ran cleanly but neither
    ``_judge_post_response_text`` nor ``pending_user_prompt`` got
    populated).

    Issue #1 from the 2026-04-29 qwen36 batch (chatflow
    019dd8b9-ca89..., turn 2): post_judge produced verdict
    ``post_verdict=retry`` with ``redo_targets=[failed_delegate]``
    and a perfectly serviceable ``user_message`` ("国航数据已收集，
    东航查询失败，将重新尝试..."), but the hook never spawned the
    redo clone (silent crash or skip), the workflow ended without a
    terminal llm_call, and the user got the opaque "inner workflow
    had no terminal llm_call" error instead of the judge's
    diagnostic. This helper surfaces that diagnostic as a last-
    resort agent_response — strictly better than the engine error
    leaking to the user.

    Returns the most recent post_judge verdict's ``user_message``
    (any verdict, not just accept), or ``None`` if no post_judge
    has run or none of them have a user_message. Skips empty
    strings.
    """
    for n in reversed(list(workflow.nodes.values())):
        if (
            n.step_kind != StepKind.JUDGE_CALL
            or n.judge_variant != JudgeVariant.POST
            or n.judge_verdict is None
        ):
            continue
        msg = n.judge_verdict.user_message
        if not _is_fake_null(msg):
            return msg.strip() if msg else None
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


def _parse_planner_output_message(
    output: WireMessage,
) -> RecursivePlannerOutput:
    """Parse a planner WorkNode's frozen ``output_message`` into a
    :class:`RecursivePlannerOutput`, preferring the tool_use path
    when the model honored ``forced_tool_name="submit_plan"``.

    M7.5 PR 6 wired the planner to a single ``submit_plan`` tool;
    strong providers (anthropic / openai_chat / volcengine / ark)
    now return the structured arguments via
    ``output_message.tool_uses[0]`` and the engine never had to
    parse free-text JSON. Weak providers and adapters that drop
    tool_choice silently keep returning content-only — for those we
    fall back to the pre-PR 6 path so legacy chatflows + Ollama /
    llama.cpp keep working without configuration churn.

    Raises :class:`PlannerParseError` from whichever branch ran —
    callers handle that as a planner failure regardless of source.
    """
    if output.tool_uses:
        for tu in output.tool_uses:
            if tu.name == SUBMIT_PLAN_TOOL_NAME:
                return parse_planner_from_tool_args(dict(tu.arguments))
    return parse_recursive_planner_output(output.content or "")


def _render_planner_output_for_prompt(
    planner_node: WorkFlowNode,
) -> str:
    """Render a planner WorkNode's frozen output as the JSON string that
    the planner_judge / planner-respawn templates feed into their
    ``plan_json`` / ``prior_plan`` slots.

    PR 6 (44634dd) routed the planner via ``forced_tool_name='submit_plan'``,
    so on success ``output_message.content`` is empty and the structured
    plan lives in ``tool_uses[0].arguments``. Several call sites
    (planner_judge spawn, planner respawn after revise / parse_error /
    phantom-tool critique) used to read ``content`` directly and ended
    up feeding the empty string into the prompt — judges saw "no plan",
    new planners saw "no prior attempt", and the auto_plan loop
    revised in circles until debate budget halt'd. This helper is the
    single source of truth: parse via ``_parse_planner_output_message``
    (the same helper ``_after_planner`` consumes), then dump as
    indented JSON. Falls back to raw content on PlannerParseError so
    weak providers / adapters that drop tool_choice still surface
    something to the caller.
    """
    if planner_node.output_message is None:
        return ""
    try:
        parsed = _parse_planner_output_message(planner_node.output_message)
        return parsed.model_dump_json(indent=2)
    except PlannerParseError:
        return planner_node.output_message.content or ""


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
        plan = _parse_planner_output_message(planner.output_message)
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

    ``capabilities`` is flattened to a comma-joined string from the
    natural-language ``capabilities_origin`` (M7.5 split: ``inheritable_tools``
    is the registry-name list for engine routing, kept off the prompt
    surface to avoid token bloat). Templates referencing ``{% if
    capabilities %}`` keep working — the field still exists, just sourced
    from ``capabilities_origin`` post-rename.
    """
    return {
        "description": workflow.description.text if workflow.description else "",
        "inputs": workflow.inputs.text if workflow.inputs else "",
        "expected_outcome": (
            workflow.expected_outcome.text if workflow.expected_outcome else ""
        ),
        "capabilities": ", ".join(workflow.capabilities_origin),
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


def _chat_lca(chatflow: ChatFlow, left_id: str, right_id: str) -> str | None:
    """Return the lowest common ancestor of two ChatNodes in the DAG.

    Uses ``ChatFlow.ancestors`` which yields nodes in topological order
    (root-first). The deepest element that appears in both ancestor
    lists (plus the nodes themselves) is the LCA. Returns ``None`` if
    the branches don't share any ancestor (shouldn't happen in a
    well-formed ChatFlow — every node traces back to a root).
    """
    left_chain = set(chatflow.ancestors(left_id)) | {left_id}
    right_chain = [*chatflow.ancestors(right_id), right_id]
    lca: str | None = None
    for nid in right_chain:
        if nid in left_chain:
            lca = nid
    return lca


def _count_chat_chain_tokens(
    chatflow: ChatFlow,
    start_id: str,
    *,
    stop_at: str | None = None,
) -> int:
    """Walk ``start_id`` up via primary parent, tally user+assistant
    tokens of each frozen node along the way.

    ``stop_at`` is *exclusive* — the stop node's tokens are NOT counted.
    Passing ``stop_at=None`` walks all the way to a root-less node.
    """
    tokens = 0
    current: str | None = start_id
    while current is not None and current != stop_at:
        node = chatflow.nodes[current]
        if node.is_frozen:
            if node.user_message is not None:
                tokens += _count_text_tokens(node.user_message.text)
            tokens += _count_text_tokens(node.agent_response.text)
        current = node.parent_ids[0] if node.parent_ids else None
    return tokens


# Recognises the citation markers the planner/merge prompts ask models to
# emit — ``(nodes: abc123)`` / ``（节点: abc123）`` / ``[node:abc123]``. A
# model reply that contains any of these is assumed to carry its own
# provenance and the structural fallback stays out of the way.
_CITATION_RE = re.compile(
    r"\(\s*(?:nodes?|节点)\s*:"
    r"|（\s*(?:nodes?|节点)\s*:"
    r"|\[\s*node\s*:",
    re.IGNORECASE,
)


def _has_citation(text: str) -> bool:
    return bool(_CITATION_RE.search(text or ""))


def _branch_tail_snippet(
    chatflow: ChatFlow, node_id: str, *, char_cap: int = 240
) -> tuple[str, str] | None:
    """Return (role, short_content) for the given ChatNode's most
    representative line — prefer the assistant reply; fall back to the
    user_message if the assistant text is empty. ``None`` for root or
    unfrozen nodes."""
    node = chatflow.nodes.get(node_id)
    if node is None or not node.is_frozen:
        return None
    if node.agent_response.text:
        body = node.agent_response.text
        return ("assistant", body[:char_cap])
    if node.user_message is not None and node.user_message.text:
        return ("user", node.user_message.text[:char_cap])
    return None


def _append_branch_citation_fallback(
    reply: str, chatflow: ChatFlow, source_ids: list[str]
) -> str:
    """Append a ``[sources]`` tail referencing each source branch's
    ChatNode id when *reply* lacks any citation marker. No-op if the
    model already cited something — we don't want to double up.
    """
    if _has_citation(reply) or not source_ids:
        return reply
    lines: list[str] = []
    for nid in source_ids:
        snippet = _branch_tail_snippet(chatflow, nid)
        if snippet is None:
            lines.append(f"[node:{nid} | —]")
        else:
            role, body = snippet
            lines.append(f"[node:{nid} | {role}] {body}")
    if not lines:
        return reply
    return f"{reply.rstrip()}\n\n[sources]\n" + "\n".join(lines)


@dataclass
class MergePreview:
    """Snapshot returned by :meth:`ChatFlowEngine.preview_merge` — tells
    the UI whether the impending merge will need to compact, and what
    budget it would target."""

    lca_id: str | None
    compact_needed: bool
    suggested_target_tokens: int
    prefix_tokens: int
    left_suffix_tokens: int
    right_suffix_tokens: int
    combined_suffix_tokens: int
    effective_budget_tokens: int


def _build_chat_context(
    chatflow: ChatFlow,
    parent_ids: list[str],
    *,
    include_summary_preamble: bool = True,
    chat_board_descriptions: dict[str, str] | None = None,
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

    ``chat_board_descriptions`` (optional) maps ChatNode id →
    scope='chat' ChatBoardItem description. When supplied, the
    descriptions for ancestors *folded into the compact summary*
    (chain[0..cutoff_idx-1]) are appended to the summary preamble as
    a bulleted block so the LLM sees a per-turn recap of what was
    compressed — the chat-board equivalent of flipping through index
    cards for the summarised conversation. Ignored when there's no
    compact cutoff on the chain.
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
    # Pack substitutions computed after start_idx is finalised (see below).
    pack_subs_at: dict[int, PackSnapshot] = {}
    hidden_chain_indices: set[int] = set()
    if compact_cutoff_idx is not None:
        snap = chatflow.nodes[chain[compact_cutoff_idx]].compact_snapshot
        assert snap is not None  # loop guarantees
        cbi_block = ""
        if chat_board_descriptions:
            cbi_lines: list[str] = []
            for ancestor_id in chain[:compact_cutoff_idx]:
                desc = chat_board_descriptions.get(ancestor_id)
                if desc:
                    cbi_lines.append(f"- [{ancestor_id}] {desc}")
            if cbi_lines:
                cbi_block = (
                    "\n\n[ChatBoard | 被压缩节点逐条摘要]\n"
                    + "\n".join(cbi_lines)
                )
        summary_msg = WireMessage(
            role="user",
            content=(
                "[Prior conversation — summarized to save context]\n\n"
                f"{snap.summary}"
                f"{cbi_block}"
            ),
        )
        # preserved_before_summary=True: the snapshot's preserved list
        # is the shared *prefix* that ran temporally before the summary
        # (joint-compact merge path). Emit preserved first, then the
        # summary preamble. Default (False) keeps the historical Tier-2
        # compact shape: summary preamble first, preserved recent-tail
        # second.
        #
        # Sticky restores are NOT emitted here anymore — they're deferred
        # to the very end of the context (just before the caller appends
        # the current turn's new user message). Rationale: the prefix
        # `summary + preserved + historical nodes` is append-only across
        # turns, so keeping it stable maximises prefix-cache hits. Sticky
        # entries, by contrast, shrink/expire per-turn as the counter
        # decays, so folding them in at the tail keeps the dynamic band
        # localised and out of the cacheable prefix.
        if snap.preserved_before_summary:
            messages.extend(snap.preserved_messages)
            if include_summary_preamble:
                messages.append(summary_msg)
        else:
            if include_summary_preamble:
                messages.append(summary_msg)
            messages.extend(snap.preserved_messages)
        start_idx = compact_cutoff_idx + 1

    # Pack substitutions: for each pack ChatNode on chain[start_idx:],
    # emit its summary once at the first range index, and mark every
    # range member + the pack node itself as hidden so the emission
    # loop doesn't double-count. Pack only affects ITS OWN range
    # (unlike compact which implicitly covers everything before it);
    # pre-pack ancestors in the same chain continue to emit normally.
    for i in range(start_idx, len(chain)):
        node_i = chatflow.nodes[chain[i]]
        pack_snap = node_i.pack_snapshot
        if pack_snap is not None and pack_snap.summary:
            range_set = set(pack_snap.packed_range)
            range_indices = [
                j for j in range(start_idx, len(chain)) if chain[j] in range_set
            ]
            if range_indices:
                pack_subs_at[min(range_indices)] = pack_snap
                hidden_chain_indices.update(range_indices)
            # The pack node's own user/assistant should not emit — the
            # summary substitutes for the range and pack.agent_response
            # IS that summary.
            hidden_chain_indices.add(i)

    for i in range(start_idx, len(chain)):
        nid = chain[i]
        node = chatflow.nodes[nid]
        if not node.is_frozen:
            continue
        # Emit a pack summary at the first index of each pack's range.
        if i in pack_subs_at:
            psnap = pack_subs_at[i]
            messages.append(
                WireMessage(
                    role="user",
                    content=(
                        "[Packed range — summarized to save context]\n\n"
                        f"{psnap.summary}"
                    ),
                )
            )
            messages.extend(psnap.preserved_messages)
        # Hidden chain indices (range members + the pack node itself)
        # skip their own user/assistant emission; the summary above
        # already covers them.
        if i in hidden_chain_indices:
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
        else:
            # Greeting root: synthesize a user anchor so the wire
            # never opens with role="assistant". See
            # ``_GREETING_ANCHOR_USER_CONTENT`` for rationale.
            messages.append(
                WireMessage(
                    role="user", content=_GREETING_ANCHOR_USER_CONTENT
                )
            )
        messages.append(
            WireMessage(role="assistant", content=node.agent_response.text)
        )
    # Sticky restored recall (deferred tail placement — see comment
    # above). Emit even when there is no compact cutoff: a sticky entry
    # can only be registered once compaction has happened, but the
    # restored-context bundle still belongs at the tail regardless of
    # whether a compact ancestor is currently on the chain (a sticky
    # entry can outlive the compact it references via a fork).
    if compact_cutoff_idx is not None:
        primary_parent = chatflow.nodes.get(parent_ids[0])
        sticky_for_this_node = (
            primary_parent.sticky_restored if primary_parent is not None else {}
        )
        messages.extend(
            _render_sticky_restored_messages(chatflow, sticky_for_this_node)
        )
    return messages


def _render_sticky_restored_messages(
    chatflow: ChatFlow, sticky_restored: dict[str, int]
) -> list[WireMessage]:
    """Render the sticky-restored ChatNodes' user/assistant pairs.

    Sticky restore targets ChatNodes only — WorkNode-level ids may live
    on the accessed-signal briefly but aren't persisted in
    ``sticky_restored`` (see :func:`_update_sticky_restored_for_node`),
    so callers here should only ever see ChatNode ids. Defensive skip
    kept for robustness against stale references.

    Emitted in counter-descending order (most-recently-refreshed
    first) so the LLM sees the highest-priority restores near the top.

    Each restored pair carries an explicit time-hint header so the LLM
    doesn't mistake the block for "latest conversation" — this content
    has been *recalled* from earlier history that was already summarised
    away; treating it as present-tense breaks the narrative ordering.
    """
    out: list[WireMessage] = []
    # Sort by counter desc, then node_id for a stable order.
    for node_id, _counter in sorted(
        sticky_restored.items(), key=lambda kv: (-kv[1], kv[0])
    ):
        node = chatflow.nodes.get(node_id)
        if node is None:
            continue  # stale reference — defensive skip.
        header = (
            f"[召回早期节点 {node_id} 原文，请视作已发生过的历史上下文——"
            f"这是从此前被压缩成摘要的对话中取回的内容，不是最新一轮对话]"
        )
        user_text = node.user_message.text if node.user_message else ""
        agent_text = node.agent_response.text if node.agent_response else ""
        out.append(
            WireMessage(
                role="user",
                content=f"{header}\n\n{user_text}" if user_text else header,
            )
        )
        if agent_text:
            out.append(WireMessage(role="assistant", content=agent_text))
    return out


def build_inbound_context_segments(
    chatflow: ChatFlow,
    node_id: str,
    *,
    chat_board_descriptions: dict[str, str] | None = None,
) -> list[InboundContextSegment]:
    """Return the inbound context for *node_id* as typed segments.

    Sibling of :func:`_build_chat_context` intended for the
    ``GET .../nodes/{id}/inbound_context`` preview endpoint: the
    frontend can render each segment with its own styling (muted
    backgrounds for synthetic summary/sticky blocks, neutral for real
    ancestor turns) while still reproducing the exact wire-message
    sequence the next llm_call would consume.

    Segment order mirrors the context builder:

    1. ``summary_preamble`` + ``preserved`` (order flipped when
       ``preserved_before_summary`` is True — joint-compact merge path).
    2. ``ancestor`` pairs: user/assistant messages from frozen ChatNodes
       between the compact cutoff and *node_id* (exclusive on both
       ends). Emitted one segment per ancestor so the UI can attach
       node-level chrome.
    3. ``sticky_restored`` pairs: synthetic recall bundles, one segment
       per restored ChatNode.
    4. ``current_turn``: the previewed node's own user + assistant
       messages.

    Unfrozen ancestors contribute no segment (same rule as the context
    builder). Greeting roots are skipped since they have no
    ``user_message``; merge ChatNodes appear both as a hard stop for
    the walk AND as their own ancestor segment (the merge's synthesised
    user prompt is real context the LLM will see).
    """
    if node_id not in chatflow.nodes:
        raise KeyError(node_id)
    target = chatflow.nodes[node_id]

    # Walk the primary-parent chain, stopping at merge ChatNodes — same
    # rule as ``_build_chat_context``.
    chain: list[str] = []
    current: str | None = target.parent_ids[0] if target.parent_ids else None
    while current is not None:
        chain.append(current)
        anc = chatflow.nodes[current]
        if len(anc.parent_ids) >= 2:
            break
        current = anc.parent_ids[0] if anc.parent_ids else None
    chain.reverse()

    compact_cutoff_idx: int | None = None
    for i, nid in enumerate(chain):
        snap = chatflow.nodes[nid].compact_snapshot
        if snap is not None and snap.summary:
            compact_cutoff_idx = i

    segments: list[InboundContextSegment] = []
    start_idx = 0
    if compact_cutoff_idx is not None:
        compact_nid = chain[compact_cutoff_idx]
        snap = chatflow.nodes[compact_nid].compact_snapshot
        assert snap is not None
        cbi_block = ""
        cbi_entries: list[CbiEntry] = []
        if chat_board_descriptions:
            cbi_lines: list[str] = []
            for ancestor_id in chain[:compact_cutoff_idx]:
                desc = chat_board_descriptions.get(ancestor_id)
                if desc:
                    cbi_lines.append(f"- [{ancestor_id}] {desc}")
                    cbi_entries.append(
                        CbiEntry(node_id=ancestor_id, description=desc)
                    )
            if cbi_lines:
                cbi_block = (
                    "\n\n[ChatBoard | 被压缩节点逐条摘要]\n"
                    + "\n".join(cbi_lines)
                )
        summary_segment = InboundContextSegment(
            kind="summary_preamble",
            messages=[
                WireMessage(
                    role="user",
                    content=(
                        "[Prior conversation — summarized to save context]\n\n"
                        f"{snap.summary}"
                        f"{cbi_block}"
                    ),
                )
            ],
            source_node_id=compact_nid,
            synthetic=True,
            cbi_entries=cbi_entries or None,
        )
        preserved_segment = InboundContextSegment(
            kind="preserved",
            messages=list(snap.preserved_messages),
            source_node_id=compact_nid,
            synthetic=False,
        )
        if snap.preserved_before_summary:
            if preserved_segment.messages:
                segments.append(preserved_segment)
            segments.append(summary_segment)
        else:
            segments.append(summary_segment)
            if preserved_segment.messages:
                segments.append(preserved_segment)
        start_idx = compact_cutoff_idx + 1

    # Pack substitutions mirror ``_build_chat_context``: for each
    # post-cutoff pack ChatNode, emit one ``pack_summary`` segment at
    # the first position of its packed range, mark every range member
    # + the pack node itself as hidden so they don't also contribute
    # ``ancestor`` segments. Pack only affects its own range; pre-pack
    # ancestors in the same chain continue to emit normally (unlike
    # compact which implicitly covers everything above).
    pack_subs_at: dict[int, tuple[str, PackSnapshot]] = {}
    hidden_chain_indices: set[int] = set()
    for i in range(start_idx, len(chain)):
        node_i = chatflow.nodes[chain[i]]
        pack_snap = node_i.pack_snapshot
        if pack_snap is not None and pack_snap.summary:
            range_set = set(pack_snap.packed_range)
            range_indices = [
                j for j in range(start_idx, len(chain)) if chain[j] in range_set
            ]
            if range_indices:
                pack_subs_at[min(range_indices)] = (chain[i], pack_snap)
                hidden_chain_indices.update(range_indices)
            # The pack node's own user/assistant is never emitted as an
            # ancestor — the pack summary stands in for the whole range.
            hidden_chain_indices.add(i)

    for i in range(start_idx, len(chain)):
        nid = chain[i]
        node = chatflow.nodes[nid]
        if not node.is_frozen:
            continue
        # Emit a pack_summary segment at the first index of each pack's
        # range. Preserved messages (if any) follow as their own
        # ``preserved`` segment so the UI can style them independently.
        if i in pack_subs_at:
            pack_nid, psnap = pack_subs_at[i]
            segments.append(
                InboundContextSegment(
                    kind="pack_summary",
                    messages=[
                        WireMessage(
                            role="user",
                            content=(
                                "[Packed range — summarized to save context]"
                                f"\n\n{psnap.summary}"
                            ),
                        )
                    ],
                    source_node_id=pack_nid,
                    synthetic=True,
                )
            )
            if psnap.preserved_messages:
                segments.append(
                    InboundContextSegment(
                        kind="preserved",
                        messages=list(psnap.preserved_messages),
                        source_node_id=pack_nid,
                        synthetic=False,
                    )
                )
        if i in hidden_chain_indices:
            continue
        # Post-cutoff compact ChatNode — same skip as the context
        # builder so its summary text doesn't leak twice.
        if node.compact_snapshot is not None:
            continue
        msgs: list[WireMessage] = []
        if node.user_message is not None:
            msgs.append(
                WireMessage(role="user", content=node.user_message.text)
            )
        msgs.append(
            WireMessage(role="assistant", content=node.agent_response.text)
        )
        segments.append(
            InboundContextSegment(
                kind="ancestor",
                messages=msgs,
                source_node_id=nid,
                synthetic=False,
            )
        )

    # Sticky restored pairs are read from the target node's own dict —
    # this represents what was actually sticky-injected into *this*
    # node's LLM call (inherited-from-parent-with-decay plus any new
    # get_node_context hits folded in during the turn). The runtime
    # ``_build_chat_context`` reads the parent's dict because it is
    # called before the new ChatNode is spawned; the preview endpoint
    # has the materialised node, so it uses target's dict directly.
    # Compact ChatNodes have sticky={} by construction, so they emit
    # no sticky segments — which is correct (compact LLM calls do not
    # receive sticky-injected recalls).
    if compact_cutoff_idx is not None:
        sticky = target.sticky_restored
        for restored_id, _counter in sorted(
            sticky.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            restored = chatflow.nodes.get(restored_id)
            if restored is None:
                continue
            header = (
                f"[召回早期节点 {restored_id} 原文，请视作已发生过的历史上下文——"
                f"这是从此前被压缩成摘要的对话中取回的内容，不是最新一轮对话]"
            )
            user_text = restored.user_message.text if restored.user_message else ""
            agent_text = (
                restored.agent_response.text if restored.agent_response else ""
            )
            msgs = [
                WireMessage(
                    role="user",
                    content=f"{header}\n\n{user_text}" if user_text else header,
                )
            ]
            if agent_text:
                msgs.append(WireMessage(role="assistant", content=agent_text))
            segments.append(
                InboundContextSegment(
                    kind="sticky_restored",
                    messages=msgs,
                    source_node_id=restored_id,
                    synthetic=True,
                )
            )

    current_msgs: list[WireMessage] = []
    if target.user_message is not None:
        current_msgs.append(
            WireMessage(role="user", content=target.user_message.text)
        )
    if target.agent_response is not None and target.agent_response.text:
        current_msgs.append(
            WireMessage(role="assistant", content=target.agent_response.text)
        )
    if current_msgs:
        segments.append(
            InboundContextSegment(
                kind="current_turn",
                messages=current_msgs,
                source_node_id=target.id,
                synthetic=False,
            )
        )
    return segments


def _has_compact_ancestor(chatflow: ChatFlow, parent_ids: list[str]) -> bool:
    """True iff any ancestor on the primary-parent chain is a settled
    compact ChatNode. Sticky restore is meaningless without a compact
    (nothing's been compressed, so nothing needs restoring), so the
    update skips when this returns False."""
    if not parent_ids:
        return False
    current: str | None = parent_ids[0]
    while current is not None:
        node = chatflow.nodes[current]
        snap = node.compact_snapshot
        if snap is not None and snap.summary:
            return True
        if len(node.parent_ids) >= 2:
            return False  # merge stop, same rule as _build_chat_context
        current = node.parent_ids[0] if node.parent_ids else None
    return False


def _merge_sticky_restored(
    sources: list[dict[str, int]],
) -> dict[str, int]:
    """MAX-merge several parent sticky-restore maps into one.

    Used at merge ChatNode spawn: both branches' sticky states flow
    into the merged node, and the freshest counter on either side wins
    (content recent on one branch should survive the merge). Empty
    ``sources`` → empty result; single source → a plain copy so fork
    siblings can mutate independently.
    """
    out: dict[str, int] = {}
    for sticky in sources:
        for nid, counter in sticky.items():
            if counter > out.get(nid, 0):
                out[nid] = counter
    return out


def _update_sticky_restored_for_node(
    chatflow: ChatFlow,
    chat_node: ChatFlowNode,
    accessed_node_ids: set[str],
    counter_init: int,
) -> None:
    """Fold this turn's accessed-signal into ``chat_node.sticky_restored``
    and decay entries not re-touched.

    Rules:

    - No-op when there's no settled compact ancestor on the chain
      (nothing compressed → nothing to stick).
    - New accessed entries (filtered to ChatNode ids — WorkNode ids
      don't re-inject into the chat context, so they don't carry over)
      initialize to ``counter_init``.
    - Existing entries NOT in the accessed set decay by 1. Entries
      whose counter hits 0 drop out.
    - Existing entries that ARE in the accessed set refresh back to
      ``counter_init``.

    Mutates ``chat_node.sticky_restored`` in place (ChatFlowNode is a
    regular mutable Pydantic model, unlike CompactSnapshot).
    """
    if not _has_compact_ancestor(chatflow, chat_node.parent_ids):
        return

    accessed_chat_ids = {nid for nid in accessed_node_ids if nid in chatflow.nodes}
    old = chat_node.sticky_restored
    new_sticky: dict[str, int] = {}
    for nid, counter in old.items():
        if nid in accessed_chat_ids:
            new_sticky[nid] = counter_init  # refresh
        elif counter > 1:
            new_sticky[nid] = counter - 1  # decay
        # counter == 1 → drop (decayed to 0)
    for nid in accessed_chat_ids:
        new_sticky.setdefault(nid, counter_init)
    chat_node.sticky_restored = new_sticky


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


def _group_tagged_by_chatnode(
    tagged: list[tuple[str | None, WireMessage]],
) -> list[tuple[str | None, list[WireMessage]]]:
    """Collapse consecutive same-tag entries into per-ChatNode groups.

    Preserves chronological order. A group bundles everything that
    originated at one ChatNode (typically its user_message +
    agent_response), so downstream compact logic can treat a whole
    conversational turn as one indivisible unit — slicing a turn in
    half on a message boundary loses the user's question or strands an
    orphan reply, which hurts recall more than it saves tokens.

    ``None`` tags (synthetic preamble rows from a prior compact) are
    grouped like any other tag; adjacent ``None``s collapse into one
    synthetic group.
    """
    groups: list[tuple[str | None, list[WireMessage]]] = []
    for nid, msg in tagged:
        if groups and groups[-1][0] == nid:
            groups[-1][1].append(msg)
        else:
            groups.append((nid, [msg]))
    return groups


def _greedy_pack_chatnode_groups_within_budget(
    groups: list[tuple[str | None, list[WireMessage]]],
    budget_tokens: int,
) -> list[WireMessage]:
    """Pick the longest suffix of *groups* whose total tokens fit in
    *budget_tokens*. All-or-nothing per group — we never split a
    ChatNode so a preserved tail always contains complete turns.

    Walks groups from the newest side back; stops at the first group
    whose inclusion would overflow (can't skip middle groups — that
    would punch a hole in the conversation). Returns flat WireMessages
    in original chronological order.
    """
    if budget_tokens <= 0 or not groups:
        return []
    remaining = budget_tokens
    packed_rev: list[list[WireMessage]] = []
    for _, msgs in reversed(groups):
        group_tokens = _estimate_tokens_from_wire(msgs)
        if group_tokens > remaining:
            break
        packed_rev.append(msgs)
        remaining -= group_tokens
    out: list[WireMessage] = []
    for msgs in reversed(packed_rev):
        out.extend(msgs)
    return out


class PackRangeError(ValueError):
    """Raised when a user-supplied ``packed_range`` is invalid (empty,
    unknown id, or not contiguous along the primary-parent chain).
    Surfaced to the API handler as a 400."""


def _validate_chat_packed_range(
    chatflow: ChatFlow, packed_range: list[str]
) -> list[str]:
    """Verify the supplied ChatNode ids form a contiguous primary-parent
    chain and return them in topological order (earliest → latest).

    Rules:
      - at least one id
      - every id exists in ``chatflow``
      - for each consecutive pair ``(a, b)``, ``a`` appears in
        ``b.parent_ids`` (``a`` is a parent of ``b``)
    """
    if not packed_range:
        raise PackRangeError("packed_range is empty")
    for nid in packed_range:
        if nid not in chatflow.nodes:
            raise PackRangeError(f"packed_range id {nid!r} not in chatflow")
    for a, b in zip(packed_range, packed_range[1:]):
        if a not in chatflow.nodes[b].parent_ids:
            raise PackRangeError(
                f"packed_range not contiguous: {a!r} is not a parent of {b!r}"
            )
    return list(packed_range)


def _gather_chat_pack_range_messages(
    chatflow: ChatFlow, packed_range: list[str]
) -> list[tuple[str, WireMessage]]:
    """Collect user+assistant wire messages from the packed ChatNode
    range, each tagged with its ChatNode id.

    Nested pack / compact members substitute their summary to save
    tokens and stay consistent with how downstream readers see them —
    pack prompts shouldn't double-summarize an already-summarized block.
    Non-frozen members are skipped; a running ChatNode doesn't belong
    in pack input.
    """
    tagged: list[tuple[str, WireMessage]] = []
    for nid in packed_range:
        node = chatflow.nodes.get(nid)
        if node is None or not node.is_frozen:
            continue
        # Nested pack: fold to its summary, don't re-expand.
        if node.pack_snapshot is not None and node.pack_snapshot.summary:
            tagged.append(
                (
                    nid,
                    WireMessage(
                        role="user",
                        content=(
                            "[Prior pack — summarized]\n\n"
                            f"{node.pack_snapshot.summary}"
                        ),
                    ),
                )
            )
            continue
        # Nested compact: same — use the summary.
        if node.compact_snapshot is not None and node.compact_snapshot.summary:
            tagged.append(
                (
                    nid,
                    WireMessage(
                        role="user",
                        content=(
                            "[Prior compact — summarized]\n\n"
                            f"{node.compact_snapshot.summary}"
                        ),
                    ),
                )
            )
            continue
        if node.user_message is not None:
            tagged.append(
                (nid, WireMessage(role="user", content=node.user_message.text))
            )
        else:
            # Greeting root: synthesize a user anchor so the compact /
            # pack worker's input never starts with role="assistant".
            tagged.append(
                (
                    nid,
                    WireMessage(
                        role="user", content=_GREETING_ANCHOR_USER_CONTENT
                    ),
                )
            )
        tagged.append(
            (nid, WireMessage(role="assistant", content=node.agent_response.text))
        )
    return tagged


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
        # Pack nodes on the chain: emit the summary (as a user-role
        # preamble) instead of the pack's own user/assistant pair.
        # The compact worker sees "this range was packed — here's the
        # summary" and summarizes on top of that, which is what we want.
        if node.pack_snapshot is not None and node.pack_snapshot.summary:
            tagged.append(
                (
                    nid,
                    WireMessage(
                        role="user",
                        content=(
                            "[Prior packed range — summarized]\n\n"
                            f"{node.pack_snapshot.summary}"
                        ),
                    ),
                )
            )
            continue
        if node.user_message is not None:
            tagged.append(
                (nid, WireMessage(role="user", content=node.user_message.text))
            )
        else:
            # Greeting root: synthesize a user anchor so the compact /
            # pack worker's input never starts with role="assistant".
            tagged.append(
                (
                    nid,
                    WireMessage(
                        role="user", content=_GREETING_ANCHOR_USER_CONTENT
                    ),
                )
            )
        tagged.append(
            (nid, WireMessage(role="assistant", content=node.agent_response.text))
        )
    return tagged
