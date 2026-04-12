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
from typing import Any

from agentloom.engine.events import EventBus, WorkflowEvent
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
from agentloom.schemas.common import NodeStatus, StepKind, TokenUsage, utcnow
from agentloom.schemas.common import ToolUse as SchemaToolUse
from agentloom.schemas.workflow import WireMessage
from agentloom.tools.base import ToolContext, ToolRegistry

ProviderCall = Callable[
    [list[Message], list[ToolDefinition], str | None],
    Awaitable[ChatResponse],
]

#: Hard safety cap on the number of LLM↔tool turns the engine will
#: auto-spawn for one top-level llm_call. Shielded by this bound because
#: a malformed prompt or buggy tool could otherwise keep emitting
#: tool_uses forever. Configurable knob lands in M7.
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

    async def execute(self, workflow: WorkFlow) -> WorkFlow:
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
        """
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
                # The tool loop may have added new nodes; break to
                # recompute topological order and see them this pass.
                break

            if not progressed:
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

    async def _run_llm_call(self, workflow: WorkFlow, node: WorkFlowNode) -> None:
        """Build context from the ancestor chain + call the provider.

        Context construction rule (ADR-009 / §4.3): the ancestor chain
        only, not the full DAG. We walk the topologically ordered
        ancestors, and for each *frozen llm_call* ancestor we include
        its input_messages (once, from the root) plus the ancestor's
        output_message. For simplicity in M3, if the node carries
        explicit ``input_messages``, we use those as-is — otherwise we
        derive them from the ancestor chain.
        """
        if node.input_messages:
            messages = _wire_to_provider(node.input_messages)
        else:
            messages = _build_context_from_ancestors(workflow, node)

        if not messages:
            raise ValueError(
                f"llm_call node {node.id} has no input_messages and no "
                "ancestor context to build from"
            )

        if node.model_override:
            ref = node.model_override
            model = f"{ref.provider_id}:{ref.model_id}" if ref.provider_id else ref.model_id
        else:
            model = None

        # Expose every tool the registry considers visible under this
        # node's constraints. Empty list means "no tools" — stays
        # backward-compatible with M3 callers that don't configure a
        # registry.
        tool_defs: list[ToolDefinition] = []
        if self._tools is not None:
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

        # ------------------------------------------------------------- tool loop
        # If the model requested tool calls AND we have a registry
        # configured, auto-spawn child tool_call nodes + a follow-up
        # llm_call to feed the results back. The outer execute() loop
        # will pick up the newly-planned children on its next pass.
        if self._tools is not None and node.output_message.tool_uses:
            _assert_tool_loop_budget(workflow, node)
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


def _assert_tool_loop_budget(workflow: WorkFlow, node: WorkFlowNode) -> None:
    """Count how many llm_call ancestors exist; refuse to spawn more
    children if we've already hit the safety cap.

    The count is a cheap heuristic for "how many tool-use iterations
    have we done in this chain" — each loop turn adds exactly one
    llm_call to the ancestor chain, so len(llm ancestors) is the loop
    iteration count.
    """
    ancestors = workflow.ancestors(node.id) + [node.id]
    llm_ancestors = sum(
        1
        for nid in ancestors
        if workflow.get(nid).step_kind == StepKind.LLM_CALL
    )
    if llm_ancestors >= MAX_TOOL_LOOP_ITERATIONS:
        raise RuntimeError(
            f"tool-use loop exceeded budget ({MAX_TOOL_LOOP_ITERATIONS} iterations); "
            "aborting to protect against runaway agents"
        )


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
