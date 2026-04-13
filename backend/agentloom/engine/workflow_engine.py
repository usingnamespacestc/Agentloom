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

from collections.abc import Awaitable, Callable

from agentloom.engine.events import EventBus, WorkflowEvent
from agentloom.engine.judge_formatter import (
    format_judge_post_prompt,
    format_judge_pre_prompt,
    format_revise_budget_halt_prompt,
    judge_post_needs_user_input,
    judge_pre_needs_user_input,
)
from agentloom.engine.judge_parser import JudgeParseError, parse_judge_verdict
from agentloom.engine.model_resolution import effective_model_for
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
from agentloom.schemas.common import JudgeVariant, NodeStatus, StepKind, TokenUsage, utcnow
from agentloom.schemas.common import ToolUse as SchemaToolUse
from agentloom.schemas.workflow import WireMessage
from agentloom.tools.base import ToolContext, ToolRegistry

ProviderCall = Callable[
    [list[Message], list[ToolDefinition], str | None],
    Awaitable[ChatResponse],
]

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


class WorkflowEngine:
    def __init__(
        self,
        provider_call: ProviderCall,
        event_bus: EventBus,
        tool_registry: ToolRegistry | None = None,
        tool_context: ToolContext | None = None,
    ) -> None:
        self._provider_call = provider_call
        self._bus = event_bus
        self._tools = tool_registry
        self._tool_ctx = tool_context or ToolContext()
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

    async def execute(
        self,
        workflow: WorkFlow,
        *,
        chatflow_tool_loop_budget: int | None | object = _UNSET,
        chatflow_auto_mode_revise_budget: int | None | object = _UNSET,
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
        self._revise_count = 0
        broken: set[str] = set()
        done: set[str] = set()

        while True:
            order = workflow.topological_order()
            progressed = False
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

                await self._run_node(workflow, node)
                if node.status == NodeStatus.FAILED:
                    broken.add(node.id)
                done.add(node_id)
                progressed = True
                # If a judge pass decided the WorkFlow must bounce back
                # to the ChatFlow layer for user clarification, stop
                # running — remaining planned nodes stay dashed, and
                # the ChatFlow engine opens a new ChatNode whose
                # agent_response is the pending prompt.
                if workflow.pending_user_prompt is not None:
                    break
                # The tool loop may have added new nodes; break to
                # recompute topological order and see them this pass.
                break

            if not progressed or workflow.pending_user_prompt is not None:
                break

        await self._bus.publish(
            WorkflowEvent(workflow_id=workflow.id, kind="workflow.completed")
        )
        return workflow

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
                raise NotImplementedError(
                    "sub_agent_delegation is not implemented yet"
                )
            else:  # pragma: no cover — enum exhaustiveness
                raise ValueError(f"unknown step_kind {node.step_kind}")
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
            return

        node.status = NodeStatus.SUCCEEDED
        node.finished_at = utcnow()
        await self._bus.publish(
            WorkflowEvent(
                workflow_id=workflow.id,
                kind="node.succeeded",
                node_id=node.id,
                data={"usage": node.usage.model_dump() if node.usage else None},
            )
        )

    async def _invoke_and_freeze(
        self,
        workflow: WorkFlow,
        node: WorkFlowNode,
        *,
        expose_tools: bool,
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

        # Expose every tool the registry considers visible under this
        # node's constraints. Empty list means "no tools" — stays
        # backward-compatible with M3 callers that don't configure a
        # registry. Judges never see tools even if a registry exists.
        tool_defs: list[ToolDefinition] = []
        if expose_tools and self._tools is not None:
            tool_defs = [
                ToolDefinition(**d)
                for d in self._tools.definitions_for_constraints(node.tool_constraints)
            ]

        response = await self._provider_call(messages, tool_defs, model)

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
        await self._invoke_and_freeze(workflow, node, expose_tools=True)
        assert node.output_message is not None

        # ------------------------------------------------------------- tool loop
        # If the model requested tool calls AND we have a registry
        # configured, auto-spawn child tool_call nodes + a follow-up
        # llm_call to feed the results back. The outer execute() loop
        # will pick up the newly-planned children on its next pass.
        if self._tools is not None and node.output_message.tool_uses:
            _assert_tool_loop_budget(workflow, node, self._effective_budget)
            _spawn_tool_loop_children(workflow, node)

    async def _run_tool_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Execute a single tool_call node. Requires a registry."""
        if self._tools is None:
            raise RuntimeError(
                "tool_call node encountered but engine has no tool_registry"
            )
        if not node.tool_name:
            raise ValueError(f"tool_call node {node.id} has no tool_name")
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

        # Same context/provider path as llm_call, but never exposes
        # tools to the judge — judges must respond with structured
        # JSON, not by asking to call a tool.
        await self._invoke_and_freeze(workflow, node, expose_tools=False)
        assert node.output_message is not None
        try:
            node.judge_verdict = parse_judge_verdict(
                node.output_message.content,
                node.judge_variant,
            )
        except JudgeParseError as exc:
            # Reraise as a plain exception so the outer _run_node marks
            # FAILED. The raw output_message is already on the node.
            raise RuntimeError(f"judge parse failed: {exc}") from exc

        # If the verdict requires user clarification, stash a rendered
        # prompt on the WorkFlow — the outer execute() loop will halt,
        # and the ChatFlow engine will open a new ChatNode whose
        # agent_response is this prompt. judge_during is monitoring-
        # mode only in MVP (ADR-020) — it never sets this field.
        verdict = node.judge_verdict
        if node.judge_variant == JudgeVariant.PRE and judge_pre_needs_user_input(verdict):
            workflow.pending_user_prompt = format_judge_pre_prompt(verdict)
        elif node.judge_variant == JudgeVariant.POST and judge_post_needs_user_input(verdict):
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
                workflow.pending_user_prompt = format_revise_budget_halt_prompt(
                    revise_count=self._revise_count,
                    budget=budget,
                    latest_verdict=verdict,
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

    follow_up = WorkFlowNode(
        step_kind=StepKind.LLM_CALL,
        parent_ids=tool_call_ids,
        tool_constraints=parent_llm.tool_constraints,
        model_override=parent_llm.model_override,
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
    """
    ancestors = workflow.ancestors(node.id)
    messages: list[Message] = []
    seen_input = False
    for aid in ancestors:
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
