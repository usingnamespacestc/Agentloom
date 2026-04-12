"""WorkFlow and WorkFlowNode — the inner execution graph.

Each ChatFlowNode owns one WorkFlow. A WorkFlow is a DAG of WorkFlowNodes
with three kinds: llm_call, tool_call, sub_agent_delegation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from agentloom.schemas.common import (
    CycleError,
    NodeBase,
    NodeHasReferencesError,
    NodeId,
    StepKind,
    TokenUsage,
    ToolConstraints,
    ToolResult,
    ToolUse,
    generate_node_id,
)


class WireMessage(BaseModel):
    """A single chat message for an llm_call input/output. Mirrors the shape
    in ``agentloom.providers.types`` but is import-independent to avoid a
    layering cycle between schemas and providers."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_uses: list[ToolUse] = Field(default_factory=list)
    tool_use_id: str | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


class WorkFlowNode(NodeBase):
    """A single step inside a WorkFlow.

    Invariant: exactly one of the step_kind-specific sub-objects is
    populated, matching ``step_kind``. This is validated at construction.
    """

    step_kind: StepKind
    tool_constraints: ToolConstraints | None = None

    # --- llm_call fields ---
    input_messages: list[WireMessage] | None = None
    output_message: WireMessage | None = None
    usage: TokenUsage | None = None

    # --- tool_call fields ---
    source_tool_use_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: ToolResult | None = None

    # --- sub_agent_delegation ---
    sub_workflow: "WorkFlow | None" = None

    @model_validator(mode="after")
    def _validate_step_kind_fields(self) -> "WorkFlowNode":
        """Only the fields belonging to the declared ``step_kind`` may be set.

        We don't *require* them all to be populated (a dashed llm_call has
        no output_message yet), but we do forbid cross-kind contamination.
        """
        if self.step_kind == StepKind.LLM_CALL:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("llm_call node may not carry tool_call fields")
            if self.sub_workflow is not None:
                raise ValueError("llm_call node may not carry a sub_workflow")
        elif self.step_kind == StepKind.TOOL_CALL:
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("tool_call node may not carry llm_call fields")
            if self.sub_workflow is not None:
                raise ValueError("tool_call node may not carry a sub_workflow")
        elif self.step_kind == StepKind.SUB_AGENT_DELEGATION:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("delegation node may not carry tool_call fields")
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("delegation node may not carry llm_call fields")
        return self


class WorkFlow(BaseModel):
    """A DAG of WorkFlowNodes.

    Stored as a flat ``nodes`` map keyed by NodeId plus a list of root_ids
    (nodes with empty ``parent_ids``). Edges live on each node's
    ``parent_ids`` — no separate edge table.
    """

    id: NodeId = Field(default_factory=generate_node_id)
    nodes: dict[NodeId, WorkFlowNode] = Field(default_factory=dict)
    root_ids: list[NodeId] = Field(default_factory=list)

    # ----------------------------------------------------------- construction

    def add_node(self, node: WorkFlowNode) -> WorkFlowNode:
        """Add a node. Rejects cycles and dangling parent references."""
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id {node.id}")
        for p in node.parent_ids:
            if p not in self.nodes:
                raise ValueError(f"parent {p!r} not in workflow")
        # Cycle check: would adding this node introduce a cycle?
        # Since parent_ids reference existing nodes and this node is new,
        # no cycle is possible. Re-parenting (below) is where cycles can form.
        self.nodes[node.id] = node
        if not node.parent_ids:
            self.root_ids.append(node.id)
        return node

    def remove_node(self, node_id: NodeId) -> None:
        """Remove a node. Rejects if any other node lists it as parent."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        for other in self.nodes.values():
            if node_id in other.parent_ids:
                raise NodeHasReferencesError(
                    f"node {node_id} still referenced by {other.id}"
                )
        del self.nodes[node_id]
        if node_id in self.root_ids:
            self.root_ids.remove(node_id)

    def ancestors(self, node_id: NodeId) -> list[NodeId]:
        """Topologically ordered ancestor chain, root first."""
        if node_id not in self.nodes:
            raise KeyError(node_id)
        seen: set[NodeId] = set()
        order: list[NodeId] = []

        def visit(nid: NodeId) -> None:
            if nid in seen:
                return
            seen.add(nid)
            for p in self.nodes[nid].parent_ids:
                visit(p)
            order.append(nid)

        for p in self.nodes[node_id].parent_ids:
            visit(p)
        return order

    def topological_order(self) -> list[NodeId]:
        """Kahn's algorithm. Raises CycleError if the graph is not a DAG."""
        incoming: dict[NodeId, set[NodeId]] = {nid: set(n.parent_ids) for nid, n in self.nodes.items()}
        ready = [nid for nid, deps in incoming.items() if not deps]
        order: list[NodeId] = []
        while ready:
            ready.sort()  # deterministic for tests
            nid = ready.pop(0)
            order.append(nid)
            for other_id, deps in incoming.items():
                if nid in deps:
                    deps.remove(nid)
                    if not deps and other_id not in order and other_id not in ready:
                        ready.append(other_id)
        if len(order) != len(self.nodes):
            raise CycleError(f"cycle detected in workflow {self.id}")
        return order

    def get(self, node_id: NodeId) -> WorkFlowNode:
        return self.nodes[node_id]


# Resolve forward reference
WorkFlowNode.model_rebuild()
WorkFlow.model_rebuild()
