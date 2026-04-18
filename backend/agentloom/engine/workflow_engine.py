"""In-memory WorkFlow engine — M3 scope (llm_call only).

Design notes:
- Operates on ``schemas.WorkFlow`` Pydantic objects.
- Execution order is Kahn's topological sort (deterministic tie-break).
- Per-node status transitions: planned → running → succeeded | failed.
- ``succeeded`` and ``failed`` are frozen from this point (§4.1, ADR-003).
- Tool calls and sub-agent delegation are explicitly NOT supported here;
  they land in M6. An encountered ``tool_call`` node is marked FAILED
  with a descriptive error so the engine stays honest about its scope.
- The engine takes a *provider callable* as a constructor arg rather
  than owning the adapter directly. Tests inject a stub; production
  wires in an OpenAI-compat adapter.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

from agentloom.engine.events import EventBus, WorkflowEvent
from agentloom.engine.judge_formatter import (
    format_ground_ratio_halt_prompt,
    format_judge_post_prompt,
    format_revise_budget_halt_prompt,
    judge_post_needs_user_input,
)
from agentloom.engine.judge_parser import (
    JudgeParseError,
    judge_verdict_tool_def,
    parse_judge_from_tool_args,
    parse_judge_verdict,
)
from agentloom.engine.model_resolution import effective_model_for
from agentloom.engine.recursive_planner_parser import (
    PlannerParseError,
    RecursivePlannerOutput,
    parse_recursive_planner_output,
)
from agentloom.providers.types import (
    AssistantMessage,
    ChatResponse,
    Message,
    SystemMessage,
    ToolDefinition,
    ToolMessage,
    UserMessage,
)
from agentloom.providers.types import ToolUse as ProviderToolUse
from agentloom.schemas import WorkFlow, WorkFlowNode
from agentloom.schemas.common import (
    EditableText,
    EditProvenance,
    JudgeVariant,
    NodeStatus,
    ProviderModelRef,
    SharedNote,
    StepKind,
    TokenUsage,
    WorkNodeRole,
    utcnow,
)
from agentloom.schemas.common import ToolUse as SchemaToolUse
from agentloom.schemas.workflow import CompactSnapshot, WireMessage
from agentloom.tools.base import ToolContext, ToolRegistry

#: Provider call surface — the engine never instantiates an adapter
#: directly, the caller injects a closure. ``on_token`` is the
#: streaming hook: when supplied, the closure should run the provider
#: with stream=true and forward each fragment via the callback so the
#: engine can republish a live preview to the bus. ``None`` keeps the
#: legacy non-streaming behavior so test doubles don't have to
#: implement streaming.
ProviderCall = Callable[
    ...,
    Awaitable[ChatResponse],
]
TokenCallback = Callable[[str], Awaitable[None]]

#: Callback fired after a node transitions to SUCCEEDED. The hook is
#: free to mutate ``workflow`` (typically: add new nodes that the next
#: ``execute()`` iteration will pick up). Used to keep the inner DAG
#: dynamic — e.g. judge_pre's verdict decides whether the WorkFlow
#: continues with an llm_call or routes straight to judge_post.
PostNodeHook = Callable[[WorkFlow, WorkFlowNode], None]

#: Resolve a model's context window in tokens. ChatFlowEngine wires
#: this to a closure that reads ``ModelInfo.context_window`` from the
#: provider registry. ``None`` on either the callable or its result
#: means "unknown"; the engine falls back to
#: :data:`DEFAULT_CONTEXT_WINDOW_TOKENS`.
ContextWindowLookup = Callable[[ProviderModelRef | None], int | None]

#: Sentinel so we can distinguish ``chatflow_tool_loop_budget=None``
#: ("chatflow exists and says unlimited") from "no chatflow was passed
#: at all".
_UNSET: object = object()


#: Fallback tool-loop budget used when no ChatFlow/WorkFlow context is
#: provided (e.g. engine tests that exercise a bare WorkFlow). Real
#: traffic resolves the budget via
#: ``workflow.tool_loop_budget ?? chatflow.tool_loop_budget`` — see
#: ``_effective_tool_loop_budget`` below. ``None`` on either layer
#: means "unlimited"; this default exists only so standalone callers
#: aren't implicitly unlimited.
MAX_TOOL_LOOP_ITERATIONS = 12


# --------------------------------------------------------------- compact (Tier 1)

#: Default per-ChatFlow compact trigger threshold. Pre-llm_call the
#: engine estimates the message-list token footprint (char-based); if
#: ``estimate / context_window >= TRIGGER_PCT`` a compact WorkNode is
#: inserted before the call runs. Chatflow settings will override this
#: per-flow in step 5.
DEFAULT_COMPACT_TRIGGER_PCT = 0.7
#: Default target footprint for the compact summary. Fed to the compact
#: worker as ``target_tokens = context_window * TARGET_PCT``.
DEFAULT_COMPACT_TARGET_PCT = 0.5
#: Default number of trailing messages kept verbatim on the downstream
#: side of a compact. Smaller = more aggressive compaction, larger =
#: more fidelity. Chatflow settings will override per-flow in step 5.
DEFAULT_PRESERVE_RECENT_TURNS = 3
#: Fallback context window (in tokens) used when the model's actual
#: ``context_window`` is unknown. Matches the frontend's
#: ``DEFAULT_MAX_CONTEXT_TOKENS`` so UI bar and engine threshold agree.
DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000


class _CompactRequested(Exception):
    """Raised inside ``_invoke_and_freeze`` when the pending message
    list exceeds the compact trigger. Caught by ``_run_node`` which
    un-winds the current node back to ``planned`` so the execute loop
    picks up the freshly-inserted compact WorkNode on its next pass.
    """


def _estimate_tokens_from_provider_messages(messages: list[Message]) -> int:
    """Char-based token estimate (chars/4) for a provider-side message
    list. Matches the shape of the inputs ``_invoke_and_freeze`` has
    already built, so the trigger fires on exactly what would hit the
    wire. Good enough for a gate — not a substitute for tiktoken.
    """
    chars = 0
    for m in messages:
        content = getattr(m, "content", None) or ""
        chars += len(content)
        tool_uses = getattr(m, "tool_uses", None)
        if tool_uses:
            for tu in tool_uses:
                chars += len(tu.name)
                import json as _json
                chars += len(_json.dumps(tu.arguments, default=str))
    return chars // 4


def _estimate_tokens_from_wire(messages: list[WireMessage]) -> int:
    """Sibling of :func:`_estimate_tokens_from_provider_messages` for
    schema-side ``WireMessage`` lists. Used by snapshot accounting."""
    chars = 0
    for w in messages:
        chars += len(w.content or "")
        for tu in w.tool_uses:
            chars += len(tu.name)
            import json as _json
            chars += len(_json.dumps(tu.arguments, default=str))
    return chars // 4


_COMPACT_FIXTURE_CACHE: tuple[dict[str, Any], dict[str, str]] | None = None


def _get_compact_fixture() -> tuple[dict[str, Any], dict[str, str]]:
    """Return ``(plan_dict, include_fragments)`` for ``compact.yaml``.

    Loaded once at first use and cached. Fails loudly if the fixture
    is missing because Tier 1 can't function without it — the compact
    worker has no fallback prompt.
    """
    global _COMPACT_FIXTURE_CACHE
    if _COMPACT_FIXTURE_CACHE is None:
        from agentloom.templates.loader import fragments_as_texts, load_fixtures

        templates, fragments = load_fixtures()
        compact = next(
            (f for f in templates if f.builtin_id == "compact"), None
        )
        if compact is None:
            raise RuntimeError(
                "compact.yaml fixture missing — required for Tier 1 auto-compact"
            )
        _COMPACT_FIXTURE_CACHE = (compact.plan, fragments_as_texts(fragments))
    return _COMPACT_FIXTURE_CACHE


def _compact_description_text() -> EditableText:
    """Placeholder description for engine-inserted compact nodes. Kept
    in one spot so the UI and tests share the same string."""
    return EditableText(
        text="Compact (auto-inserted)",
        provenance=EditProvenance.PURE_AGENT,
    )


def _parent_is_fresh_compact(workflow: WorkFlow, node: WorkFlowNode) -> bool:
    """True iff ``node`` has a direct parent that is a COMPACT WorkNode
    with a settled snapshot. Used to break the Tier 1 loop: after a
    compact finishes, the node it was inserted for re-runs exactly
    once with the summarized context — it must not trigger another
    compact even if the summary + preserved tail still overflow.
    """
    for pid in node.parent_ids:
        parent = workflow.get(pid)
        if (
            parent.step_kind == StepKind.COMPACT
            and parent.compact_snapshot is not None
            and parent.compact_snapshot.summary
        ):
            return True
    return False


def _render_messages_to_compact(messages: list[Message]) -> str:
    """Serialize a provider-message list into the ``[role] body`` shape
    the compact worker's prompt expects.
    """
    lines: list[str] = []
    for m in messages:
        if isinstance(m, SystemMessage):
            lines.append(f"[system] {m.content}")
        elif isinstance(m, UserMessage):
            lines.append(f"[user] {m.content}")
        elif isinstance(m, AssistantMessage):
            body = m.content or ""
            if m.tool_uses:
                import json as _json
                parts = [
                    f"{tu.name}({_json.dumps(tu.arguments, default=str)})"
                    for tu in m.tool_uses
                ]
                body = (body + "\n" if body else "") + "tool_uses: " + "; ".join(parts)
            lines.append(f"[assistant] {body}")
        elif isinstance(m, ToolMessage):
            lines.append(f"[tool:{m.tool_use_id}] {m.content}")
    return "\n".join(lines)


class WorkflowEngine:
    def __init__(
        self,
        provider_call: ProviderCall,
        event_bus: EventBus,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
        *,
        context_window_lookup: ContextWindowLookup | None = None,
        compact_trigger_pct: float = DEFAULT_COMPACT_TRIGGER_PCT,
        compact_target_pct: float = DEFAULT_COMPACT_TARGET_PCT,
        compact_preserve_recent_turns: int = DEFAULT_PRESERVE_RECENT_TURNS,
        compact_model: ProviderModelRef | None = None,
    ) -> None:
        self._provider_call = provider_call
        self._bus = event_bus
        self._tools = tool_registry
        self._tool_ctx = tool_context or ToolContext()
        #: Resolves a model's context_window in tokens. ``None`` means
        #: "no lookup plumbed in" — the engine treats every model as
        #: having :data:`DEFAULT_CONTEXT_WINDOW_TOKENS`.
        self._context_window_lookup = context_window_lookup
        #: Compact Tier 1 parameters. Chatflow-level overrides flow in
        #: via the ChatFlowEngine wiring at construction time.
        self._compact_trigger_pct = compact_trigger_pct
        self._compact_target_pct = compact_target_pct
        self._compact_preserve_recent_turns = compact_preserve_recent_turns
        self._compact_model = compact_model
        #: Per-``execute()`` filter — tool names hidden from the LLM and
        #: refused if invoked. Populated from the chatflow's
        #: ``disabled_tool_names`` list by ChatFlowEngine. Empty
        #: frozenset means "no extra filter on top of constraints".
        self._disabled_tool_names: frozenset[str] = frozenset()
        #: Resolved once per ``execute()`` call; read by
        #: :func:`_assert_tool_loop_budget`. ``None`` means unlimited.
        self._effective_budget: int | None = MAX_TOOL_LOOP_ITERATIONS
        #: Resolved once per ``execute()`` call — the cap on
        #: ``judge_during.during_verdict == "revise"`` seen in this run
        #: before auto-mode halts. ``None`` means unlimited (§5.3 FR-PL-7).
        self._effective_revise_budget: int | None = None
        #: Revise counter for *this* ``execute()`` invocation. Nested
        #: sub_agent_delegation will spin up its own engine, so each
        #: recursion level counts independently.
        self._revise_count: int = 0
        #: Per-``execute()`` hook fired on every node success. Lets the
        #: caller (typically ChatFlowEngine) grow the DAG dynamically —
        #: e.g. spawn judge_post once judge_pre/llm_call has settled.
        self._post_node_hook: PostNodeHook | None = None
        #: Resolved once per ``execute()``. ``None`` means the
        #: planner-grounding fuse is disabled for this run.
        self._effective_min_ground_ratio: float | None = None
        #: Minimum completed leaves before the grounding fuse arms.
        self._effective_ground_ratio_grace: int = 20

    async def execute(
        self,
        workflow: WorkFlow,
        *,
        chatflow_tool_loop_budget: int | None | object = _UNSET,
        chatflow_auto_mode_revise_budget: int | None | object = _UNSET,
        chatflow_min_ground_ratio: float | None | object = _UNSET,
        chatflow_ground_ratio_grace_nodes: int | object = _UNSET,
        post_node_hook: PostNodeHook | None = None,
        disabled_tool_names: frozenset[str] | None = None,
    ) -> WorkFlow:
        """Run every planned node in topological order. Mutates and
        returns the workflow.

        Already-frozen nodes are skipped (they belong to a prior
        execution). A node whose ancestors include a failed node is
        also skipped — failure does not cascade execution, but we do
        not run downstream nodes whose context is broken.

        The tool-use loop (M6) can add new nodes to ``workflow``
        mid-execution: when an llm_call emits ``tool_uses`` we
        auto-spawn child tool_call nodes + a follow-up llm_call. We
        handle this by recomputing the order after each step and
        running any newly-planned node we haven't seen yet.

        ``chatflow_tool_loop_budget`` lets the caller (typically
        ``ChatFlowEngine``) hand in the outer ChatFlow's budget so the
        resolution ``workflow.tool_loop_budget ?? chatflow.tool_loop_budget``
        can finish. ``None`` explicitly means "unlimited"; the
        ``_UNSET`` sentinel means "no chatflow context" and falls back
        to :data:`MAX_TOOL_LOOP_ITERATIONS`.
        """
        self._effective_budget = _effective_tool_loop_budget(
            workflow.tool_loop_budget, chatflow_tool_loop_budget
        )
        self._effective_revise_budget = _effective_revise_budget(
            workflow.auto_mode_revise_budget, chatflow_auto_mode_revise_budget
        )
        self._effective_min_ground_ratio = (
            None if chatflow_min_ground_ratio is _UNSET else chatflow_min_ground_ratio  # type: ignore[assignment]
        )
        self._effective_ground_ratio_grace = (
            20 if chatflow_ground_ratio_grace_nodes is _UNSET else chatflow_ground_ratio_grace_nodes  # type: ignore[assignment]
        )
        self._revise_count = 0
        self._post_node_hook = post_node_hook
        self._disabled_tool_names = disabled_tool_names or frozenset()
        broken: set[str] = set()
        done: set[str] = set()

        # Parallel-ready scheduling: each outer pass collects every node
        # whose parents are all in ``done`` and runs the batch
        # concurrently via ``asyncio.gather``. The tool loop and
        # ``post_node_hook`` can mutate the DAG inside ``_run_node`` —
        # any nodes they add land in the next pass's ready set after
        # ``topological_order()`` is recomputed.
        while True:
            order = workflow.topological_order()
            ready: list[WorkFlowNode] = []
            for node_id in order:
                if node_id in done:
                    continue
                node = workflow.get(node_id)

                if node.is_frozen:
                    done.add(node_id)
                    continue

                if any(p in broken for p in node.parent_ids):
                    node.status = NodeStatus.CANCELLED
                    node.error = "skipped: ancestor failed"
                    done.add(node_id)
                    continue

                # Only schedule once every parent has finished this run.
                # Parents that are still planned/running in a later
                # batch will let this node appear in a future pass.
                if not all(p in done for p in node.parent_ids):
                    continue

                ready.append(node)

            if not ready:
                break

            await asyncio.gather(
                *(self._run_node(workflow, n) for n in ready)
            )
            for n in ready:
                if n.status == NodeStatus.FAILED:
                    broken.add(n.id)
                if n.status == NodeStatus.PLANNED:
                    # Tier 1 compact deferred this node — leave it out
                    # of ``done`` so the next topological pass picks
                    # it up again once its new compact parent runs.
                    continue
                done.add(n.id)

            # Planner-grounding fuse: once enough leaves have resolved,
            # require tool_calls to occupy at least ``min_ground_ratio``
            # of them. Catches runaway planner/judge churn that never
            # lands a real action (see §5.4 / 2026-04-17 incident).
            if (
                workflow.pending_user_prompt is None
                and self._effective_min_ground_ratio is not None
            ):
                leaves, tools = _compute_ground_ratio(workflow)
                if (
                    leaves >= self._effective_ground_ratio_grace
                    and tools / leaves < self._effective_min_ground_ratio
                ):
                    workflow.pending_user_prompt = format_ground_ratio_halt_prompt(
                        leaves=leaves,
                        tools=tools,
                        min_ratio=self._effective_min_ground_ratio,
                    )
                    log.info(
                        "ground-ratio fuse halt: workflow=%s leaves=%d tools=%d ratio=%.3f threshold=%.3f",
                        workflow.id,
                        leaves,
                        tools,
                        tools / leaves,
                        self._effective_min_ground_ratio,
                    )

            # If a judge pass decided the WorkFlow must bounce back
            # to the ChatFlow layer for user clarification, stop
            # running — remaining planned nodes stay dashed, and
            # the ChatFlow engine opens a new ChatNode whose
            # agent_response is the pending prompt.
            if workflow.pending_user_prompt is not None:
                break

        await self._bus.publish(
            WorkflowEvent(workflow_id=workflow.id, kind="workflow.completed")
        )
        return workflow

    def _token_callback(
        self, workflow: WorkFlow, node: WorkFlowNode
    ) -> TokenCallback:
        """Build the per-token publish closure handed to the provider.

        Each fragment becomes a ``node.token`` event on the bus. The
        chatflow_engine relay re-publishes it as
        ``chat.workflow.node.token`` so the frontend can render a
        live preview while a slow model (e.g. local 27B Ollama
        loading from cold) is still generating.
        """
        wf_id = workflow.id
        node_id = node.id

        async def publish(piece: str) -> None:
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=wf_id,
                    kind="node.token",
                    node_id=node_id,
                    data={"delta": piece},
                )
            )

        return publish

    async def _forward_sub_events(
        self,
        sub_id: str,
        parent_id: str,
        queue: asyncio.Queue[WorkflowEvent | None],
    ) -> None:
        """Re-publish ``sub_id``-scoped events under ``parent_id``.

        Preserves ``kind``, ``node_id``, and ``data`` — only the
        ``workflow_id`` changes. Drops ``workflow.completed`` so each
        sub-WorkFlow's internal completion doesn't look like the
        outer run's completion to downstream subscribers.
        """
        async for event in self._bus.drain(sub_id, queue):
            if event.kind == "workflow.completed":
                continue
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=parent_id,
                    kind=event.kind,
                    node_id=event.node_id,
                    data=event.data,
                )
            )

    async def _run_node(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        node.status = NodeStatus.RUNNING
        node.started_at = utcnow()
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=workflow.id,
                kind="node.running",
                node_id=node.id,
                data={"step_kind": node.step_kind.value},
            )
        )

        try:
            if node.step_kind == StepKind.LLM_CALL:
                await self._run_llm_call(workflow, node)
            elif node.step_kind == StepKind.TOOL_CALL:
                await self._run_tool_call(workflow, node)
            elif node.step_kind == StepKind.JUDGE_CALL:
                await self._run_judge_call(workflow, node)
            elif node.step_kind == StepKind.SUB_AGENT_DELEGATION:
                await self._run_sub_agent_delegation(workflow, node)
            elif node.step_kind == StepKind.COMPACT:
                await self._run_compact(workflow, node)
            else:  # pragma: no cover — enum exhaustiveness
                raise ValueError(f"unknown step_kind {node.step_kind}")
        except _CompactRequested:
            # Tier 1 pre-call check spliced a compact WorkNode in front
            # of this one. _insert_compact_worknode already reset the
            # node to PLANNED and added the compact as a new parent;
            # we just need to emit a bus event so subscribers know
            # this run got deferred, then unwind without the
            # "node.failed" treatment.
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=workflow.id,
                    kind="node.compact_deferred",
                    node_id=node.id,
                    data={"compact_parent": node.parent_ids[0]},
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 — engine boundary
            node.status = NodeStatus.FAILED
            node.error = f"{type(exc).__name__}: {exc}"
            node.finished_at = utcnow()
            await self._bus.publish(
                WorkflowEvent(
                    workflow_id=workflow.id,
                    kind="node.failed",
                    node_id=node.id,
                    data={"error": node.error},
                )
            )
        else:
            # A handler may have already marked the node terminal (e.g.
            # ``_run_sub_agent_delegation`` flips to FAILED when it
            # absorbs a sub-layer halt). Don't overwrite that decision.
            if node.status == NodeStatus.RUNNING:
                node.status = NodeStatus.SUCCEEDED
                node.finished_at = utcnow()
                _append_shared_note(workflow, node)
                await self._bus.publish(
                    WorkflowEvent(
                        workflow_id=workflow.id,
                        kind="node.succeeded",
                        node_id=node.id,
                        data={"usage": node.usage.model_dump() if node.usage else None},
                    )
                )
            elif node.status == NodeStatus.FAILED:
                await self._bus.publish(
                    WorkflowEvent(
                        workflow_id=workflow.id,
                        kind="node.failed",
                        node_id=node.id,
                        data={"error": node.error},
                    )
                )

        # Let the caller grow the DAG before the next iteration picks
        # up the new nodes (Option B: judge_pre / llm_call completion
        # decides whether to spawn judge_post or an llm_call follow-up).
        # The hook also fires for FAILED nodes so post_judge crashes can
        # be retried — the hook itself filters which kinds it acts on.
        if self._post_node_hook is not None:
            self._post_node_hook(workflow, node)

    async def _invoke_and_freeze(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        *,
        expose_tools: bool,
        override_tools: list[ToolDefinition] | None = None,
        extra: dict[str, Any] | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> None:
        """Shared provider-call path for ``llm_call`` and ``judge_call``.

        Builds the message context from ``node.input_messages`` or, if
        empty, the ancestor chain; resolves the effective model via
        :func:`effective_model_for`; optionally exposes tool definitions
        (llm_call only — judges don't get tools, see ADR-020); invokes
        the provider and freezes ``output_message`` / ``usage`` onto
        the node. Does **not** spawn a tool loop — that's the caller's
        choice.
        """
        if node.input_messages:
            messages = _wire_to_provider(node.input_messages)
        else:
            messages = _build_context_from_ancestors(workflow, node)

        if not messages:
            raise ValueError(
                f"{node.step_kind.value} node {node.id} has no input_messages "
                "and no ancestor context to build from"
            )

        ref = effective_model_for(workflow, node.id)
        if ref is not None:
            model = f"{ref.provider_id}:{ref.model_id}" if ref.provider_id else ref.model_id
        else:
            model = None

        # Tier 1 compact check — only for ancestor-built contexts and
        # only for the kinds of calls that would be *growing* context.
        # Compact calls themselves are exempt (they're the recovery
        # path; they'd trigger recursion). Judge / planner / worker
        # nodes that were spawned with explicit ``input_messages`` are
        # also skipped: their prompt is template-driven and we have no
        # obvious place to splice in a summary without breaking the
        # template contract. We also exempt nodes whose direct parent
        # is a freshly-settled compact — that's the node the compact
        # was inserted for, and its re-run IS the "compacted version".
        # Re-triggering here would loop indefinitely on pathologically
        # long preserved tails.
        if (
            node.step_kind != StepKind.COMPACT
            and node.input_messages is None
            and not _parent_is_fresh_compact(workflow, node)
            and self._needs_compact(messages, ref)
        ):
            self._insert_compact_worknode(workflow, node, messages, ref)
            raise _CompactRequested(node.id)

        # Expose every tool the registry considers visible under this
        # node's constraints. Empty list means "no tools" — stays
        # backward-compatible with M3 callers that don't configure a
        # registry. Judges never see tools even if a registry exists.
        tool_defs: list[ToolDefinition] = []
        if override_tools is not None:
            tool_defs = override_tools
        elif expose_tools and self._tools is not None:
            tool_defs = [
                ToolDefinition(**d)
                for d in self._tools.definitions_for_constraints(node.tool_constraints)
                if d["name"] not in self._disabled_tool_names
            ]

        response = await self._provider_call(
            messages,
            tool_defs,
            model,
            on_token=self._token_callback(workflow, node),
            extra=extra,
            json_schema=json_schema,
        )

        # Freeze the result on the node.
        assistant = response.message
        node.output_message = WireMessage(
            role="assistant",
            content=assistant.content or "",
            tool_uses=[
                SchemaToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                for tu in assistant.tool_uses
            ],
            extras=dict(assistant.extras) if assistant.extras else {},
        )
        if node.input_messages is None:
            node.input_messages = _provider_to_wire(messages)
        node.usage = TokenUsage(**response.usage.model_dump()) if response.usage else None

    async def _run_llm_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Run an llm_call node: invoke the provider, freeze the output,
        and spawn a tool-use loop if the model requested one.

        Context construction rule (ADR-009 / §4.3): the ancestor chain
        only, not the full DAG. We walk topologically and include each
        frozen llm_call ancestor's input (seed) plus its output_message;
        tool_call ancestors contribute a ``tool`` message. If the node
        carries explicit ``input_messages``, we use those as-is.
        """
        # Planner nodes emit a JSON object matching RecursivePlannerOutput.
        # When the downstream provider supports structured output, we
        # pass the Pydantic-derived schema so the wire layer can enforce
        # it (Ollama format:, OpenAI response_format json_schema, etc.).
        # Adapters whose json_mode resolves to "object" will get a plain
        # json_object shape; "none" falls through to prompt-only.
        # Planner nodes must NOT expose tools: the openai_compat adapter
        # silently drops ``response_format`` when ``tools`` is non-empty,
        # so if we expose tools the json_schema enforcement is lost and
        # models fall back to markdown-fenced JSON (which the parser then
        # has to heuristically unwrap). Planners are pure "decide how to
        # decompose" nodes — they never actually call a tool — so tools
        # can safely be suppressed on this path.
        is_planner = node.role == WorkNodeRole.PLANNER
        planner_schema: dict[str, Any] | None = (
            RecursivePlannerOutput.model_json_schema() if is_planner else None
        )
        await self._invoke_and_freeze(
            workflow, node, expose_tools=not is_planner, json_schema=planner_schema
        )
        assert node.output_message is not None

        # ------------------------------------------------------------- tool loop
        # If the model requested tool calls AND we have a registry
        # configured, auto-spawn child tool_call nodes + a follow-up
        # llm_call to feed the results back. The outer execute() loop
        # will pick up the newly-planned children on its next pass.
        if self._tools is not None and node.output_message.tool_uses:
            _assert_tool_loop_budget(workflow, node, self._effective_budget)
            _spawn_tool_loop_children(workflow, node)

    # --------------------------------------------------------------- compact

    def _context_window_for(self, ref: ProviderModelRef | None) -> int:
        """Resolve the model's context window in tokens, falling back
        to :data:`DEFAULT_CONTEXT_WINDOW_TOKENS` when the lookup isn't
        plumbed in or returns ``None``.
        """
        if self._context_window_lookup is not None:
            resolved = self._context_window_lookup(ref)
            if resolved is not None and resolved > 0:
                return resolved
        return DEFAULT_CONTEXT_WINDOW_TOKENS

    def _needs_compact(
        self,
        messages: list[Message],
        ref: ProviderModelRef | None,
    ) -> bool:
        """Return True iff the estimated footprint of *messages*
        crosses the configured fraction of the target model's context
        window. Pure read — never mutates engine or workflow state.
        """
        estimated = _estimate_tokens_from_provider_messages(messages)
        ctx = self._context_window_for(ref)
        threshold = int(ctx * self._compact_trigger_pct)
        return estimated >= threshold

    def _insert_compact_worknode(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        messages: list[Message],
        ref: ProviderModelRef | None,
    ) -> WorkFlowNode:
        """Splice a COMPACT WorkNode in front of *node* and re-parent
        *node* onto it.

        - Carves off the last ``compact_preserve_recent_turns`` provider
          messages as verbatim tail.
        - Serializes the remaining head into the compact worker's
          ``messages_to_compact`` param.
        - Instantiates :file:`compact.yaml` to borrow the rendered
          prompt, then builds a single ``StepKind.COMPACT`` node with
          that prompt and a pre-populated snapshot holding the
          preserved tail + accounting.
        - ``node.parent_ids`` become ``[compact.id]``; the compact
          inherits ``node``'s prior parents. The engine will pick up
          the compact on its next ready pass; when the compact
          finishes, *node* will run again and build its context from
          the snapshot.
        """
        from agentloom.templates.instantiate import instantiate_fixture

        keep = max(0, min(len(messages), self._compact_preserve_recent_turns))
        head = messages[:-keep] if keep else messages
        tail = messages[-keep:] if keep else []
        head_serialized = _render_messages_to_compact(head)
        original_tokens = _estimate_tokens_from_provider_messages(messages)
        ctx = self._context_window_for(ref)
        target_tokens = max(256, int(ctx * self._compact_target_pct))

        compact_plan, includes = _get_compact_fixture()
        compact_wf = instantiate_fixture(
            compact_plan,
            {
                "messages_to_compact": head_serialized,
                "target_tokens": target_tokens,
                "must_keep": "",
                "must_drop": "",
                "compact_instruction": "",
            },
            includes=includes,
        )
        # The compact plan ships as a single-node WorkFlow; we borrow
        # its fully-rendered input_messages (system + user prompt).
        (inner,) = compact_wf.nodes.values()
        assert inner.input_messages is not None
        prompt = list(inner.input_messages)

        preserved_wire = _provider_to_wire(tail)
        snapshot = CompactSnapshot(
            summary="",  # filled in by _run_compact after the LLM call
            preserved_messages=preserved_wire,
            source_range=(0, len(head)),
            dropped_count=len(head),
            original_tokens=original_tokens,
            compacted_tokens=0,
            compact_instruction=None,
        )

        compact_model = self._compact_model or node.model_override or ref
        compact_node = WorkFlowNode(
            step_kind=StepKind.COMPACT,
            parent_ids=list(node.parent_ids),
            description=_compact_description_text(),
            input_messages=prompt,
            compact_snapshot=snapshot,
            model_override=compact_model,
            resolved_model=compact_model,
        )
        workflow.add_node(compact_node)
        if compact_node.id in workflow.root_ids and compact_node.parent_ids:
            # add_node appended to root_ids under the assumption of an
            # empty parent list; revert since we carry real parents.
            workflow.root_ids.remove(compact_node.id)
        node.parent_ids = [compact_node.id]
        # Reset the pending node so execute() re-schedules it after the
        # compact finishes.
        node.status = NodeStatus.PLANNED
        node.started_at = None
        log.info(
            "compact inserted: workflow=%s pending_node=%s compact=%s "
            "original_tokens=%d target_tokens=%d preserved=%d",
            workflow.id,
            node.id,
            compact_node.id,
            original_tokens,
            target_tokens,
            len(tail),
        )
        return compact_node

    async def _run_compact(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Run a COMPACT WorkNode: invoke the provider on the rendered
        prompt the engine pre-filled and splice the model's summary
        into ``compact_snapshot``.
        """
        await self._invoke_and_freeze(workflow, node, expose_tools=False)
        assert node.output_message is not None
        summary = (node.output_message.content or "").strip()
        if node.compact_snapshot is None:
            # Shouldn't happen — _insert_compact_worknode always
            # pre-populates. Be defensive so a malformed node fails
            # loudly rather than silently producing an empty snapshot.
            raise RuntimeError(
                f"compact node {node.id} missing pre-populated snapshot"
            )
        compacted_tokens = len(summary) // 4 + _estimate_tokens_from_wire(
            node.compact_snapshot.preserved_messages
        )
        node.compact_snapshot = node.compact_snapshot.model_copy(
            update={"summary": summary, "compacted_tokens": compacted_tokens}
        )

    async def _run_sub_agent_delegation(
        self, workflow: WorkFlow, node: WorkFlowNode
    ) -> None:
        """Execute the delegation's sub-WorkFlow recursively.

        Spawns a fresh :class:`WorkflowEngine` for the recursive
        ``execute()`` so per-call state (budgets, revise counter,
        disabled-tool filter, post-node hook) lives on its own instance
        rather than on ``self``. This is what lets sibling
        sub_agent_delegations run concurrently under ``asyncio.gather``
        without clobbering each other's counters — a single engine's
        save/restore pattern is not safe across parallel awaits. Same
        provider, bus, tool registry, and tool context are shared; the
        outer-resolved budgets are passed as the inner's "chatflow
        defaults" so a sub-WorkFlow without its own override inherits
        the running effective values.

        SSE forwarding: the sub engine publishes its node events on
        ``sub.id`` (its own ``workflow_id``), but the ChatFlow-level
        relay only subscribes to the outermost WorkFlow's id. Without
        a forwarder the frontend would see nothing inside any
        ``sub_agent_delegation`` — pre/planner/judge/etc. would all
        be invisible until the next full-snapshot refresh. We open a
        subscription to ``sub.id`` and re-publish every event under
        the outer ``workflow.id``. Nested delegations chain through:
        sub_2 → sub_1 → outer → ChatFlow relay. ``workflow.completed``
        is dropped so only the outermost completion reaches the
        ChatFlow layer.
        """
        sub = node.sub_workflow
        if sub is None:
            raise ValueError(
                f"sub_agent_delegation {node.id} has no sub_workflow"
            )

        sub_engine = WorkflowEngine(
            self._provider_call,
            self._bus,
            self._tools,
            self._tool_ctx,
            context_window_lookup=self._context_window_lookup,
            compact_trigger_pct=self._compact_trigger_pct,
            compact_target_pct=self._compact_target_pct,
            compact_preserve_recent_turns=self._compact_preserve_recent_turns,
            compact_model=self._compact_model,
        )
        forward_queue = self._bus.open_subscription(sub.id)
        forward_task = asyncio.create_task(
            self._forward_sub_events(sub.id, workflow.id, forward_queue),
            name=f"forward-{sub.id}",
        )
        try:
            await sub_engine.execute(
                sub,
                chatflow_tool_loop_budget=self._effective_budget,
                chatflow_auto_mode_revise_budget=self._effective_revise_budget,
                chatflow_min_ground_ratio=self._effective_min_ground_ratio,
                chatflow_ground_ratio_grace_nodes=self._effective_ground_ratio_grace,
                post_node_hook=self._post_node_hook,
                disabled_tool_names=self._disabled_tool_names,
            )
        finally:
            # execute() itself publishes ``workflow.completed`` on
            # ``sub.id`` at the end. Signal end-of-stream so the
            # forwarder drains the tail (including that completed
            # event, which it filters out) and exits naturally. A
            # cancel() here would race the last-batch events and
            # drop them.
            await self._bus.close(sub.id)
            try:
                await forward_task
            except Exception:  # noqa: BLE001 — forwarder must not raise into run loop
                pass

        # Absorb sub-layer halt signals into this delegation node
        # instead of bubbling. The outer ChatNode-level judge is the
        # sole user-facing halt authority (Phase 1 of the 2026-04-14
        # redesign). The delegation node is marked FAILED so the outer
        # aggregating judge_post sees a structured failure via
        # ``_classify_sub_outcome`` / ``_format_decompose_aggregation``
        # and can choose to partial-aggregate, retry, or escalate.
        # ``sub.pending_user_prompt`` is cleared so only the outermost
        # WorkFlow may carry a user-facing prompt.
        if sub.pending_user_prompt is not None:
            log.info(
                "sub-WorkFlow halt bubbling up: parent=%s sub=%s node=%s",
                workflow.id,
                sub.id,
                node.id,
            )
            node.error = f"sub-WorkFlow halted: {sub.pending_user_prompt}"
            node.status = NodeStatus.FAILED
            node.finished_at = utcnow()
            sub.pending_user_prompt = None

    async def _run_tool_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Execute a single tool_call node. Requires a registry."""
        if self._tools is None:
            raise RuntimeError(
                "tool_call node encountered but engine has no tool_registry"
            )
        if not node.tool_name:
            raise ValueError(f"tool_call node {node.id} has no tool_name")
        if node.tool_name in self._disabled_tool_names:
            # Defensive: the LLM never sees disabled tools in the prompt,
            # but a hallucinated tool_use still lands here. Surface a
            # normal tool failure so the model can apologize on retry.
            from agentloom.schemas.common import ToolResult

            node.tool_result = ToolResult(
                content=(
                    f"tool {node.tool_name!r} is not enabled for this chatflow"
                ),
                is_error=True,
            )
            return
        result = await self._tools.execute(
            node.tool_name,
            dict(node.tool_args or {}),
            self._tool_ctx,
            constraints=node.tool_constraints,
        )
        node.tool_result = result

    async def _run_judge_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Invoke the LLM exactly like an llm_call, then parse the raw
        assistant reply into a :class:`JudgeVerdict` that matches the
        node's declared ``judge_variant`` (ADR-018).

        Parse failures surface as a failed node — the outer ``_run_node``
        marks the status and the raw output is already on
        ``output_message`` for the user to inspect or re-run. The engine
        never silently accepts malformed judge output.

        ``judge_during`` runs in **monitoring mode** for MVP (ADR-020):
        the verdict is written to the node but does not interrupt the
        surrounding WorkFlow. Auto-mode halts on `revise` exhaustion
        and semi_auto's user-driven gates live at the ChatFlow layer.
        """
        if node.judge_variant is None:
            raise ValueError(f"judge_call node {node.id} missing judge_variant")

        tool_def = judge_verdict_tool_def(node.judge_variant)
        await self._invoke_and_freeze(
            workflow,
            node,
            expose_tools=False,
            override_tools=[tool_def],
        )
        assert node.output_message is not None

        # Prefer tool_use arguments (structured); fall back to content parsing.
        tool_uses = node.output_message.tool_uses or []
        judge_tool = next((tu for tu in tool_uses if tu.name == "judge_verdict"), None)

        try:
            if judge_tool is not None:
                node.judge_verdict = parse_judge_from_tool_args(
                    dict(judge_tool.arguments), node.judge_variant,
                )
            else:
                node.judge_verdict = parse_judge_verdict(
                    node.output_message.content, node.judge_variant,
                )
        except JudgeParseError as first_exc:
            try:
                await self._retry_judge_parse(workflow, node, first_exc)
            except JudgeParseError as retry_exc:
                raise RuntimeError(
                    f"judge parse failed after retry: first={first_exc}; "
                    f"retry={retry_exc}"
                ) from retry_exc

        # Option B: judge_post is the WorkFlow's universal exit gate —
        # only it writes ``pending_user_prompt``. judge_pre's verdict
        # is consumed by the post-node hook (set by ChatFlowEngine),
        # which decides whether to spawn an llm_call or route straight
        # to a halt-mode judge_post. judge_during stays monitoring-only
        # except for the auto-mode revise budget halt below.
        verdict = node.judge_verdict
        if node.judge_variant == JudgeVariant.POST and judge_post_needs_user_input(verdict):
            # Retry + redo_targets is the hook's responsibility: the
            # post-node hook re-spawns the targeted nodes and schedules
            # re-aggregation. Only if the hook decides the retry budget
            # is exhausted (or redo_targets is empty) does
            # ``pending_user_prompt`` get set — by the hook itself.
            if verdict.post_verdict == "retry" and verdict.redo_targets:
                pass
            else:
                workflow.pending_user_prompt = format_judge_post_prompt(verdict)
        elif node.judge_variant == JudgeVariant.DURING and verdict.during_verdict == "revise":
            # Monitoring mode (ADR-020) — the WorkFlow keeps running on
            # a single "revise", but auto-mode maintains a running count
            # of revises across this execute() call. Once the count
            # exceeds the budget we halt and bounce back to the user
            # (§5.3 FR-PL-7).
            self._revise_count += 1
            budget = self._effective_revise_budget
            if budget is not None and self._revise_count > budget:
                log.info(
                    "revise-budget halt: workflow=%s revise_count=%d budget=%d",
                    workflow.id,
                    self._revise_count,
                    budget,
                )
                workflow.pending_user_prompt = format_revise_budget_halt_prompt(
                    revise_count=self._revise_count,
                    budget=budget,
                    latest_verdict=verdict,
                )

    async def _retry_judge_parse(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        first_exc: JudgeParseError,
    ) -> None:
        """Re-invoke the judge with a JSON-discipline reminder.

        On success, overwrites ``node.output_message`` and sets
        ``node.judge_verdict``. On failure, re-raises
        :class:`JudgeParseError` for the caller to surface.
        """
        assert node.output_message is not None  # _run_judge_call guarantees
        assert node.judge_variant is not None
        first_raw = node.output_message.content

        # Build retry context: original input + the bad response + a
        # terse corrective user message. Keeps the token cost small and
        # shows the model exactly what it emitted.
        base_messages = _wire_to_provider(node.input_messages or [])
        retry_messages: list[Message] = [
            *base_messages,
            AssistantMessage(content=first_raw),
            UserMessage(
                content=(
                    f"Your previous reply failed JSON parse: {first_exc}. "
                    "Reply with ONLY a valid JSON object matching the "
                    "required schema — no prose, no code fences, all "
                    "string values quoted."
                )
            ),
        ]

        ref = effective_model_for(workflow, node.id)
        model = (
            f"{ref.provider_id}:{ref.model_id}" if ref and ref.provider_id
            else (ref.model_id if ref else None)
        )
        tool_def = judge_verdict_tool_def(node.judge_variant)
        response = await self._provider_call(
            retry_messages,
            [tool_def],
            model,
            on_token=self._token_callback(workflow, node),
        )
        assistant = response.message
        node.output_message = WireMessage(
            role="assistant",
            content=assistant.content or "",
            tool_uses=[
                SchemaToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                for tu in assistant.tool_uses
            ],
            extras=dict(assistant.extras) if assistant.extras else {},
        )
        # Accumulate usage — the retry is real provider cost the user
        # should see reflected on the node.
        if response.usage is not None:
            retry_usage = TokenUsage(**response.usage.model_dump())
            if node.usage is None:
                node.usage = retry_usage
            else:
                node.usage = TokenUsage(
                    prompt_tokens=node.usage.prompt_tokens + retry_usage.prompt_tokens,
                    completion_tokens=node.usage.completion_tokens + retry_usage.completion_tokens,
                    total_tokens=node.usage.total_tokens + retry_usage.total_tokens,
                    cached_tokens=node.usage.cached_tokens + retry_usage.cached_tokens,
                    reasoning_tokens=node.usage.reasoning_tokens + retry_usage.reasoning_tokens,
                )

        retry_tool_uses = node.output_message.tool_uses or []
        retry_judge_tool = next((tu for tu in retry_tool_uses if tu.name == "judge_verdict"), None)
        if retry_judge_tool is not None:
            node.judge_verdict = parse_judge_from_tool_args(
                dict(retry_judge_tool.arguments), node.judge_variant,
            )
        else:
            node.judge_verdict = parse_judge_verdict(
                node.output_message.content, node.judge_variant,
            )


#: Cap on a single SharedNote's summary length. Picked at "fits in a
#: single line of typical model context" — long enough to identify the
#: output, short enough that ~50 notes still cost a manageable amount
#: of tokens in a layer-wide injection.
_SHARED_NOTE_SUMMARY_MAX = 200


def _summarize_for_shared_note(node: WorkFlowNode) -> str | None:
    """Pull a one-line summary out of a freshly-succeeded WorkNode.

    Read by judge_post (only consumer today) to evaluate sibling state
    and target redo on specific node ids. Summaries are role-aware so
    each entry actually carries information judge_post can act on:

    - judge_call: verdict label + the human ask (user_message) /
      blockers / critique count / redo target count, whichever apply.
      Lets judge_post see *why* a prior judge said what it did.
    - llm_call planner: parsed plan shape (``atomic <step_kind>`` or
      ``decompose N subtasks``), not the raw JSON. The previous
      "first line of output" gave back ``{`` because JSON starts with
      a brace.
    - llm_call worker / aggregator / other: first substantive line of
      the markdown output (skips blank lines and lone braces).
    - tool_call: ``tool_name → first-line-of-result``.
    - sub_agent_delegation: best-effort one-liner from the sub-WorkFlow's
      effective output (judge_post merged_response if present, else
      latest worker draft).

    Returns ``None`` when there's nothing meaningful to record yet —
    callers skip the append in that case.
    """
    if node.step_kind == StepKind.JUDGE_CALL and node.judge_verdict is not None:
        return _summarize_judge(node)
    if node.step_kind == StepKind.LLM_CALL and node.output_message is not None:
        return _summarize_llm(node)
    if node.step_kind == StepKind.TOOL_CALL and node.tool_result is not None:
        return _truncate_one_line(
            f"{node.tool_name or 'tool'} → {node.tool_result.content or '(empty)'}"
        )
    if node.step_kind == StepKind.SUB_AGENT_DELEGATION and node.sub_workflow is not None:
        body = _sub_workflow_summary(node)
        return _truncate_one_line(body) if body else None
    return None


def _summarize_judge(node: WorkFlowNode) -> str:
    v = node.judge_verdict
    assert v is not None  # caller guards
    if v.during_verdict:
        verdict_label = v.during_verdict
    elif v.post_verdict:
        verdict_label = v.post_verdict
    elif v.feasibility:
        verdict_label = v.feasibility
    else:
        verdict_label = "verdict"
    variant_label = node.judge_variant.value if node.judge_variant else "judge"
    extras: list[str] = []
    if v.user_message:
        extras.append(_truncate_one_line(v.user_message))
    if v.blockers:
        extras.append("blockers: " + "; ".join(v.blockers))
    if v.missing_inputs:
        extras.append("missing: " + ", ".join(v.missing_inputs))
    if v.critiques:
        extras.append(f"{len(v.critiques)} critiques")
    if v.redo_targets:
        extras.append(f"redo {len(v.redo_targets)}")
    suffix = " — " + " | ".join(extras) if extras else ""
    return _truncate_one_line(f"{variant_label}: {verdict_label}{suffix}")


def _summarize_llm(node: WorkFlowNode) -> str:
    assert node.output_message is not None  # caller guards
    content = node.output_message.content
    if node.role == WorkNodeRole.PLANNER:
        try:
            plan = parse_recursive_planner_output(content)
        except PlannerParseError:
            return _truncate_one_line(f"plan: parse-error — {content}")
        if plan.mode == "atomic" and plan.atomic is not None:
            return _truncate_one_line(
                f"plan: atomic {plan.atomic.step_kind.value} — {plan.atomic.description}"
            )
        if plan.mode == "decompose" and plan.subtasks:
            heads = ", ".join(st.description for st in plan.subtasks[:3])
            more = "" if len(plan.subtasks) <= 3 else f" (+{len(plan.subtasks) - 3} more)"
            return _truncate_one_line(
                f"plan: decompose {len(plan.subtasks)} — {heads}{more}"
            )
        return _truncate_one_line(f"plan: infeasible — {plan.reason or ''}")
    return _first_substantive_line(content)


def _first_substantive_line(text: str) -> str:
    """First non-empty, non-syntactic line — skips blank lines and lone
    braces / brackets so JSON-shaped outputs don't degenerate to ``{``.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line in {"{", "}", "[", "]"}:
            continue
        if len(line) <= _SHARED_NOTE_SUMMARY_MAX:
            return line
        return line[: _SHARED_NOTE_SUMMARY_MAX - 1] + "…"
    return ""


def _truncate_one_line(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else ""
    if len(first_line) <= _SHARED_NOTE_SUMMARY_MAX:
        return first_line
    return first_line[: _SHARED_NOTE_SUMMARY_MAX - 1] + "…"


def _sub_workflow_summary(node: WorkFlowNode) -> str:
    """Best-effort one-liner for a delegation node.

    Walks the sub-WorkFlow looking for the most informative output:
    judge_post's ``merged_response`` (decompose aggregation) →
    judge_post's ``user_message`` (halt path) → most recent worker
    draft → empty. Kept inline (rather than calling chatflow_engine's
    full ``_classify_sub_outcome``) to avoid an import cycle and
    because the blackboard only needs a single line, not a structured
    classification.
    """
    sub = node.sub_workflow
    if sub is None:
        return ""
    for n in reversed(list(sub.nodes.values())):
        if n.step_kind != StepKind.JUDGE_CALL or n.judge_variant != JudgeVariant.POST:
            continue
        v = n.judge_verdict
        if v is None:
            continue
        if v.merged_response:
            return v.merged_response
        if v.user_message:
            return v.user_message
        break
    for n in reversed(list(sub.nodes.values())):
        if n.step_kind == StepKind.LLM_CALL and n.output_message is not None:
            return n.output_message.content
    return ""


def _append_shared_note(workflow: WorkFlow, node: WorkFlowNode) -> None:
    """Append a one-line note for a freshly-succeeded WorkNode.

    Engine-side hook — runs unconditionally on success; templates
    decide whether to render the note list. The note carries
    ``author_node_id`` so any consumer can look the full output back
    up via ``workflow.nodes[id]``.
    """
    summary = _summarize_for_shared_note(node)
    if summary is None or summary == "":
        return
    kind = (
        "judge_verdict" if node.step_kind == StepKind.JUDGE_CALL else "node_succeeded"
    )
    workflow.shared_notes.append(
        SharedNote(
            author_node_id=node.id,
            role=node.role,
            kind=kind,
            summary=summary,
        )
    )


def _assert_tool_loop_budget(
    workflow: WorkFlow,
    node: WorkFlowNode,
    effective_budget: int | None,
) -> None:
    """Count how many llm_call ancestors exist; refuse to spawn more
    children if we've already hit the safety cap.

    The count is a cheap heuristic for "how many tool-use iterations
    have we done in this chain" — each loop turn adds exactly one
    llm_call to the ancestor chain, so len(llm ancestors) is the loop
    iteration count.

    ``effective_budget=None`` means unlimited (this check becomes a
    no-op). See :func:`_effective_tool_loop_budget`.
    """
    if effective_budget is None:
        return
    ancestors = workflow.ancestors(node.id) + [node.id]
    llm_ancestors = sum(
        1
        for nid in ancestors
        if workflow.get(nid).step_kind == StepKind.LLM_CALL
    )
    if llm_ancestors >= effective_budget:
        raise RuntimeError(
            f"tool-use loop exceeded budget ({effective_budget} iterations); "
            "aborting to protect against runaway agents"
        )


def _compute_ground_ratio(workflow: WorkFlow) -> tuple[int, int]:
    """Count this WorkFlow's *completed* leaves for the grounding fuse.

    Returns ``(leaves, tool_calls)`` where ``leaves`` is the number of
    terminal-status non-``sub_agent_delegation`` nodes and
    ``tool_calls`` is the subset of those whose step_kind is
    ``tool_call``.

    Why local-only (no recursion into sub_workflows)? Each recursive
    engine level already runs the check on its own WorkFlow — so a
    sub_agent_delegation whose inner tree is churning halts *inside*
    itself and bubbles up via the existing sub-halt mechanism. The
    outer level's count correctly ignores delegation containers
    (they're not leaves) so a healthy parent that's only dispatching
    to children never trips the fuse.
    """
    leaves = 0
    tools = 0
    for node in workflow.nodes.values():
        if node.step_kind == StepKind.SUB_AGENT_DELEGATION:
            continue
        if node.status not in (NodeStatus.SUCCEEDED, NodeStatus.FAILED):
            continue
        leaves += 1
        if node.step_kind == StepKind.TOOL_CALL:
            tools += 1
    return leaves, tools


def _effective_revise_budget(
    workflow_budget: int | None,
    chatflow_budget: int | None | object,
) -> int | None:
    """Resolve ``workflow.auto_mode_revise_budget ?? chatflow.auto_mode_revise_budget``.

    Mirror of :func:`_effective_tool_loop_budget` but with a different
    fallback: when no ChatFlow context is provided (bare engine test),
    auto-mode defaults to **unlimited** rather than a numeric cap. The
    WorkFlow/ChatFlow-level defaults (``3``) only apply when the caller
    actually hands them in.
    """
    if workflow_budget is not None:
        return workflow_budget
    if chatflow_budget is _UNSET:
        return None  # unlimited for bare engine callers
    assert chatflow_budget is None or isinstance(chatflow_budget, int)
    return chatflow_budget


def _effective_tool_loop_budget(
    workflow_budget: int | None,
    chatflow_budget: int | None | object,
) -> int | None:
    """Resolve ``workflow.tool_loop_budget ?? chatflow.tool_loop_budget``.

    - If the WorkFlow set its own budget, that wins (``None`` on the
      WorkFlow means "inherit from ChatFlow").
    - Else use the ChatFlow's budget. ``None`` there explicitly means
      "unlimited".
    - ``chatflow_budget=_UNSET`` means the caller didn't provide a
      ChatFlow context at all (e.g. a test invoking the engine
      directly); fall back to :data:`MAX_TOOL_LOOP_ITERATIONS` so
      those callers still get a safety cap.
    """
    if workflow_budget is not None:
        return workflow_budget
    if chatflow_budget is _UNSET:
        return MAX_TOOL_LOOP_ITERATIONS
    assert chatflow_budget is None or isinstance(chatflow_budget, int)
    return chatflow_budget


def _spawn_tool_loop_children(workflow: WorkFlow, parent_llm: WorkFlowNode) -> None:
    """Given an llm_call that just emitted tool_uses, append:

    1. One ``tool_call`` WorkFlowNode per tool_use, all as children of
       ``parent_llm``.
    2. One follow-up ``llm_call`` node whose parents are every one of
       the tool_calls just added. Its ``input_messages`` is None so the
       engine will derive context from the ancestor chain on its next
       execute pass.

    Nothing here runs the children — the outer execute() loop picks
    them up on the next iteration.
    """
    assert parent_llm.output_message is not None
    tool_call_ids: list[str] = []
    for tu in parent_llm.output_message.tool_uses:
        tc = WorkFlowNode(
            step_kind=StepKind.TOOL_CALL,
            parent_ids=[parent_llm.id],
            source_tool_use_id=tu.id,
            tool_name=tu.name,
            tool_args=dict(tu.arguments),
            tool_constraints=parent_llm.tool_constraints,
        )
        workflow.add_node(tc)
        tool_call_ids.append(tc.id)

    # Tool-call follow-up llm_calls honor the chatflow-level
    # ``default_tool_call_model`` when the WorkFlow carries one (see
    # WorkFlow.tool_call_model_override). Falls back to the parent
    # llm_call's pin so direct-mode chats keep their existing behavior.
    follow_up = WorkFlowNode(
        step_kind=StepKind.LLM_CALL,
        parent_ids=tool_call_ids,
        tool_constraints=parent_llm.tool_constraints,
        model_override=workflow.tool_call_model_override or parent_llm.model_override,
    )
    workflow.add_node(follow_up)


def _wire_to_provider(wires: list[WireMessage]) -> list[Message]:
    """Translate the schema-side WireMessage list into the provider-facing
    Message union. Preserves order (KV cache contract, ADR-013)."""
    out: list[Message] = []
    for w in wires:
        if w.role == "system":
            out.append(SystemMessage(content=w.content))
        elif w.role == "user":
            out.append(UserMessage(content=w.content))
        elif w.role == "assistant":
            out.append(
                AssistantMessage(
                    content=w.content,
                    tool_uses=[
                        ProviderToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                        for tu in w.tool_uses
                    ],
                )
            )
        elif w.role == "tool":
            # M3 does not execute tools, but the mapping exists for
            # symmetry — M6 will use it.
            from agentloom.providers.types import ToolMessage

            out.append(ToolMessage(tool_use_id=w.tool_use_id or "", content=w.content))
        else:  # pragma: no cover
            raise ValueError(f"unknown wire role {w.role}")
    return out


def _provider_to_wire(messages: list[Message]) -> list[WireMessage]:
    out: list[WireMessage] = []
    for m in messages:
        extras = dict(m.extras) if m.extras else {}
        if isinstance(m, SystemMessage):
            out.append(WireMessage(role="system", content=m.content, extras=extras))
        elif isinstance(m, UserMessage):
            out.append(WireMessage(role="user", content=m.content, extras=extras))
        elif isinstance(m, AssistantMessage):
            out.append(
                WireMessage(
                    role="assistant",
                    content=m.content or "",
                    tool_uses=[
                        SchemaToolUse(id=tu.id, name=tu.name, arguments=dict(tu.arguments))
                        for tu in m.tool_uses
                    ],
                    extras=extras,
                )
            )
        else:
            # ToolMessage
            out.append(
                WireMessage(
                    role="tool",
                    content=m.content,
                    tool_use_id=getattr(m, "tool_use_id", None),
                    extras=extras,
                )
            )
    return out


def _build_context_from_ancestors(workflow: WorkFlow, node: WorkFlowNode) -> list[Message]:
    """Topologically walk ancestors and reconstruct the OpenAI-style
    message list the upstream provider expects.

    Rules:
    - The first llm_call's ``input_messages`` provides the seed
      (system/user turns).
    - Every subsequent llm_call contributes its ``output_message``
      (assistant turn, possibly with tool_uses).
    - Every frozen tool_call contributes a ``tool`` message carrying
      ``tool_use_id = source_tool_use_id`` and the result string.
    - If a COMPACT ancestor with a settled snapshot exists, everything
      before (and including) it is replaced by a single user message
      holding the summary prose plus the snapshot's preserved recent
      turns. Later ancestors layer on top as usual.
    """
    ancestors = workflow.ancestors(node.id)

    # Find the latest (most-recent-topologically) compact ancestor whose
    # snapshot is settled. ``ancestors`` is in topological order, so the
    # last match wins.
    compact_cutoff_idx: int | None = None
    for i, aid in enumerate(ancestors):
        a = workflow.get(aid)
        if (
            a.step_kind == StepKind.COMPACT
            and a.compact_snapshot is not None
            and a.compact_snapshot.summary
        ):
            compact_cutoff_idx = i

    messages: list[Message] = []
    seen_input = False
    start_idx = 0

    if compact_cutoff_idx is not None:
        snap = workflow.get(ancestors[compact_cutoff_idx]).compact_snapshot
        assert snap is not None  # loop guarantees
        messages.append(
            UserMessage(
                content=(
                    "[Prior conversation — summarized to save context]\n\n"
                    f"{snap.summary}"
                )
            )
        )
        messages.extend(_wire_to_provider(snap.preserved_messages))
        seen_input = True  # summary stands in for the original seed
        start_idx = compact_cutoff_idx + 1

    for aid in ancestors[start_idx:]:
        a = workflow.get(aid)
        if a.step_kind == StepKind.LLM_CALL:
            if not seen_input and a.input_messages:
                messages.extend(_wire_to_provider(a.input_messages))
                seen_input = True
            if a.output_message is not None:
                messages.extend(_wire_to_provider([a.output_message]))
        elif a.step_kind == StepKind.TOOL_CALL:
            if a.tool_result is not None and a.source_tool_use_id is not None:
                messages.append(
                    ToolMessage(
                        tool_use_id=a.source_tool_use_id,
                        content=a.tool_result.content,
                    )
                )
    return messages
