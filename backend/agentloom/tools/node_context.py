"""``get_node_context`` tool — fetch another node's input/output by id.

Looks up *any* node id (ChatNode or WorkNode) within the caller's
workspace via the ``node_index`` side table, then loads the containing
ChatFlow and renders the node's context.

Return shape depends on the node kind:

- ChatNode: just that turn's ``user_message`` and ``agent_response``.
  Ancestor ChatNodes are *not* included — the caller can chain calls if
  it needs them. ``parent_ids`` is returned so the caller can walk
  upstream one id at a time.
- WorkNode: ``input_messages`` + ``output_message`` + the enclosing
  WorkFlow's trio (description / inputs / expected_outcome), plus
  tool_call fields when applicable. Step-kind-specific fields only
  appear when populated. ``parent_ids`` is returned for upstream walks.

Large WireMessage chains can blow up tool_result, so the tool truncates
the JSON body at ``max_bytes`` (default 50 KiB), appending a
``"... (truncated)"`` marker instead.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from agentloom.db.base import get_session_maker
from agentloom.db.models.node_index import NodeIndexRow
from agentloom.db.repositories.chatflow import ChatFlowRepository
from agentloom.schemas.chatflow import ChatFlowNode
from agentloom.schemas.common import ToolResult
from agentloom.schemas.workflow import WorkFlow, WorkFlowNode
from agentloom.tools.base import (
    SideEffect,
    Tool,
    ToolContext,
    ToolError,
    record_accessed_node_id,
)

_DEFAULT_MAX_BYTES = 50_000
_MIN_MAX_BYTES = 1_000
_MAX_MAX_BYTES = 500_000

#: Virtual capability bit gating the cross-chatflow scope. Lives on a
#: WorkNode's ``effective_tools`` alongside real registry names (e.g.
#: ``["get_node_context", "get_node_context.cross_chatflow"]``); the
#: tool registry's ``resolve_for_node`` skips entries containing ``.``
#: so this bit doesn't pollute the LLM-facing tool definitions. The
#: tool itself reads it from ``ctx.caller_effective_tools`` at execute
#: time to authorize cross-chatflow lookups.
CROSS_CHATFLOW_CAPABILITY = "get_node_context.cross_chatflow"

_SCOPE_SELF = "self_chatflow"
_SCOPE_CROSS = "cross_chatflow"
_VALID_SCOPES = (_SCOPE_SELF, _SCOPE_CROSS)


class GetNodeContextTool(Tool):
    name = "get_node_context"
    side_effect = SideEffect.READ
    description = (
        "Fetch another node's raw context by node id. For a ChatNode, "
        "returns just that turn's user_message and agent_response (no "
        "ancestors). For a WorkNode, returns the node's input_messages "
        "and output_message, plus its enclosing WorkFlow's trio "
        "(description / inputs / expected_outcome) and any tool_call "
        "fields. Both kinds also return parent_ids so you can walk "
        "upstream one hop at a time. Search scope defaults to the "
        "current ChatFlow; pass scope='cross_chatflow' (and hold the "
        "matching virtual capability) to look up nodes in sibling "
        "ChatFlows. Oversized responses are truncated at max_bytes "
        "(default ~50 KiB)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "Id of the ChatNode or WorkNode to fetch.",
            },
            "scope": {
                "type": "string",
                "enum": list(_VALID_SCOPES),
                "description": (
                    "Where to search. 'self_chatflow' (default) restricts "
                    "the lookup to nodes in the caller's own ChatFlow — "
                    "always allowed. 'cross_chatflow' allows targets in "
                    "any ChatFlow under the same workspace, but requires "
                    "the calling WorkNode to hold the virtual capability "
                    f"`{CROSS_CHATFLOW_CAPABILITY}` in its effective_tools."
                ),
                "default": _SCOPE_SELF,
            },
            "max_bytes": {
                "type": "integer",
                "description": (
                    "Cap on the JSON response size. Larger payloads get "
                    "their input_messages truncated tail-first. Default "
                    f"{_DEFAULT_MAX_BYTES}, clamped to "
                    f"[{_MIN_MAX_BYTES}, {_MAX_MAX_BYTES}]."
                ),
                "default": _DEFAULT_MAX_BYTES,
            },
        },
        "required": ["node_id"],
    }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        node_id = args.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ToolError("get_node_context: 'node_id' must be a non-empty string")
        raw_scope = args.get("scope", _SCOPE_SELF)
        if raw_scope not in _VALID_SCOPES:
            raise ToolError(
                f"get_node_context: 'scope' must be one of {_VALID_SCOPES}, "
                f"got {raw_scope!r}"
            )
        scope: str = raw_scope
        # M7.5 PR 8 capability gate. Cross-chatflow lookups are gated
        # on the virtual ``get_node_context.cross_chatflow`` bit in the
        # caller's effective_tools. The bit is set by the planner's
        # capability allocation (or inherited via ``inheritable_tools``)
        # — by default an aggregator only sees its own chatflow, which
        # matches the pre-M7.5 behavior for the common in-chatflow
        # sibling-pull case. Bare-test path (no caller context) leaves
        # ``caller_effective_tools`` empty and falls open: tests would
        # otherwise need to thread the bit through every fixture.
        if scope == _SCOPE_CROSS and ctx.caller_chatflow_id is not None:
            if CROSS_CHATFLOW_CAPABILITY not in ctx.caller_effective_tools:
                raise ToolError(
                    f"get_node_context: scope='cross_chatflow' requires the "
                    f"virtual capability {CROSS_CHATFLOW_CAPABILITY!r} on "
                    f"the calling WorkNode's effective_tools (currently "
                    f"holds {sorted(ctx.caller_effective_tools)})"
                )
        raw_cap = args.get("max_bytes", _DEFAULT_MAX_BYTES)
        try:
            max_bytes = int(raw_cap)
        except (TypeError, ValueError):
            raise ToolError(
                f"get_node_context: 'max_bytes' must be an integer, got {raw_cap!r}"
            ) from None
        max_bytes = max(_MIN_MAX_BYTES, min(max_bytes, _MAX_MAX_BYTES))

        async with get_session_maker()() as session:
            stmt = select(NodeIndexRow).where(
                NodeIndexRow.node_id == node_id,
                NodeIndexRow.workspace_id == ctx.workspace_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                raise ToolError(
                    f"get_node_context: no node with id {node_id!r} in workspace "
                    f"{ctx.workspace_id!r}"
                )

            # Default scope keeps the caller in its own chatflow. We
            # only enforce this when the caller context is populated —
            # bare tests / ad-hoc tool runs without a chatflow id fall
            # open, same as the cross-chatflow capability check above.
            if (
                scope == _SCOPE_SELF
                and ctx.caller_chatflow_id is not None
                and row.chatflow_id != ctx.caller_chatflow_id
            ):
                raise ToolError(
                    f"get_node_context: node {node_id!r} lives in chatflow "
                    f"{row.chatflow_id!r}, not the caller's "
                    f"{ctx.caller_chatflow_id!r}. Pass "
                    f"scope='cross_chatflow' (with the matching "
                    f"capability) to read across chatflows."
                )

            repo = ChatFlowRepository(session, workspace_id=ctx.workspace_id)
            chatflow = await repo.get(row.chatflow_id)

        if row.kind == "chatnode":
            chat_node = chatflow.nodes.get(node_id)
            if chat_node is None:
                raise ToolError(
                    f"get_node_context: node_index says {node_id} lives in "
                    f"chatflow {row.chatflow_id} but it isn't there — stale index?"
                )
            body = _render_chatnode(node_id, row.chatflow_id, chat_node)
        elif row.kind == "worknode":
            located = _locate_worknode(chatflow, node_id)
            if located is None:
                raise ToolError(
                    f"get_node_context: node_index says {node_id} lives in "
                    f"chatflow {row.chatflow_id} but it isn't there — stale index?"
                )
            workflow, worknode = located
            body = _render_worknode(node_id, row.chatflow_id, workflow, worknode)
        else:
            raise ToolError(
                f"get_node_context: unknown kind {row.kind!r} on index row"
            )

        payload = _serialize(body, max_bytes)
        # Record the used-signal so the engine can refresh / extend the
        # sticky-restore counter for this source node on the current
        # ChatNode's CompactSnapshot. Only successful lookups count;
        # the not-found path above already raised.
        record_accessed_node_id(ctx, node_id)
        return ToolResult(content=payload)


def _render_chatnode(
    node_id: str, chatflow_id: str, node: ChatFlowNode
) -> dict[str, Any]:
    return {
        "kind": "chatnode",
        "node_id": node_id,
        "chatflow_id": chatflow_id,
        "parent_ids": list(node.parent_ids),
        "user_message": node.user_message.text if node.user_message else None,
        "agent_response": node.agent_response.text if node.agent_response else None,
    }


def _render_worknode(
    node_id: str,
    chatflow_id: str,
    workflow: WorkFlow,
    node: WorkFlowNode,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kind": "worknode",
        "node_id": node_id,
        "chatflow_id": chatflow_id,
        "workflow_id": workflow.id,
        "parent_ids": list(node.parent_ids),
        "step_kind": node.step_kind.value if node.step_kind else None,
        "role": node.role.value if node.role else None,
        "enclosing_description": (
            workflow.description.text if workflow.description else None
        ),
        "enclosing_inputs": workflow.inputs.text if workflow.inputs else None,
        "enclosing_expected_outcome": (
            workflow.expected_outcome.text if workflow.expected_outcome else None
        ),
    }
    if node.input_messages is not None:
        body["input_messages"] = [m.model_dump(mode="json") for m in node.input_messages]
    if node.output_message is not None:
        body["output_message"] = node.output_message.model_dump(mode="json")
    if node.tool_name is not None:
        body["tool_name"] = node.tool_name
    if node.tool_args is not None:
        body["tool_args"] = node.tool_args
    if node.tool_result is not None:
        body["tool_result"] = node.tool_result.model_dump(mode="json")
    return body


def _locate_worknode(
    chatflow: Any, node_id: str
) -> tuple[WorkFlow, WorkFlowNode] | None:
    """Walk every ChatNode's attached WorkFlow (and sub-WorkFlows) to
    find the WorkFlow containing *node_id* — the index tells us the
    chatflow but not which nested WorkFlow the node lives in."""
    for chat_node in chatflow.nodes.values():
        workflow = chat_node.workflow
        if workflow is None:
            continue
        found = _walk_workflow(workflow, node_id)
        if found is not None:
            return found
    return None


def _walk_workflow(
    workflow: WorkFlow, node_id: str
) -> tuple[WorkFlow, WorkFlowNode] | None:
    node = workflow.nodes.get(node_id)
    if node is not None:
        return workflow, node
    for child in workflow.nodes.values():
        sub = child.sub_workflow
        if sub is None:
            continue
        found = _walk_workflow(sub, node_id)
        if found is not None:
            return found
    return None


def _serialize(body: dict[str, Any], max_bytes: int) -> str:
    """Dump *body* as JSON, shrinking ``input_messages`` tail-first if
    the rendered text exceeds *max_bytes*. Falls back to a crude byte
    truncation if even an empty message list still overflows."""
    text = json.dumps(body, ensure_ascii=False)
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    messages = body.get("input_messages")
    if isinstance(messages, list) and messages:
        truncated = list(messages)
        dropped = 0
        while truncated and len(
            json.dumps({**body, "input_messages": truncated, "truncation": {
                "dropped_input_messages": dropped
            }}, ensure_ascii=False).encode("utf-8")
        ) > max_bytes:
            truncated.pop()
            dropped += 1
        if dropped > 0:
            body = {
                **body,
                "input_messages": truncated,
                "truncation": {"dropped_input_messages": dropped},
            }
            text = json.dumps(body, ensure_ascii=False)
            if len(text.encode("utf-8")) <= max_bytes:
                return text

    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = b"... (truncated)"
    cap = max(0, max_bytes - len(marker))
    cut = encoded[:cap]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    return cut.decode("utf-8", errors="ignore") + marker.decode("utf-8")
