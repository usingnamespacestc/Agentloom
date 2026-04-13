"""WorkFlow and WorkFlowNode — the inner execution graph.

Each ChatFlowNode owns one WorkFlow. A WorkFlow is a DAG of WorkFlowNodes
with three kinds: llm_call, tool_call, sub_agent_delegation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from agentloom.schemas.common import (
    CycleError,
    EditableText,
    ExecutionMode,
    JudgeVariant,
    JudgeVerdict,
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

    # --- Keyframe flags (§3.4.2 / §4.9) — meaningful only while dashed ---
    is_keyframe: bool = False
    is_keyframe_locked: bool = False
    #: Snapshot of the user's original trio for unlocked keyframes, so the
    #: planner's edits can be diffed/restored. None for non-keyframe nodes
    #: and for nodes whose trio the planner has not yet touched.
    keyframe_origin_trio: dict[str, Any] | None = None

    # --- llm_call fields ---
    input_messages: list[WireMessage] | None = None
    output_message: WireMessage | None = None
    usage: TokenUsage | None = None

    # --- tool_call fields ---
    source_tool_use_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: ToolResult | None = None

    # --- judge_call fields (ADR-018) ---
    judge_variant: JudgeVariant | None = None
    #: The WorkNode id this judge is evaluating. Empty for WorkFlow-level
    #: judges (whose target is the enclosing WorkFlow itself, not a node).
    judge_target_id: NodeId | None = None
    judge_verdict: JudgeVerdict | None = None

    # --- sub_agent_delegation ---
    sub_workflow: "WorkFlow | None" = None

    @model_validator(mode="after")
    def _validate_step_kind_fields(self) -> "WorkFlowNode":
        """Only the fields belonging to the declared ``step_kind`` may be set.

        We don't *require* them all to be populated (a dashed llm_call has
        no output_message yet), but we do forbid cross-kind contamination.
        ``judge_call`` nodes share ``input_messages`` / ``output_message`` /
        ``usage`` with ``llm_call`` (they're still a model invocation under
        the hood) but additionally carry ``judge_variant`` / ``judge_verdict``.
        """
        if self.step_kind == StepKind.LLM_CALL:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("llm_call node may not carry tool_call fields")
            if self.sub_workflow is not None:
                raise ValueError("llm_call node may not carry a sub_workflow")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("llm_call node may not carry judge_call fields")
        elif self.step_kind == StepKind.TOOL_CALL:
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("tool_call node may not carry llm_call fields")
            if self.sub_workflow is not None:
                raise ValueError("tool_call node may not carry a sub_workflow")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("tool_call node may not carry judge_call fields")
        elif self.step_kind == StepKind.JUDGE_CALL:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("judge_call node may not carry tool_call fields")
            if self.sub_workflow is not None:
                raise ValueError("judge_call node may not carry a sub_workflow")
            if self.judge_variant is None:
                raise ValueError("judge_call node requires judge_variant")
        elif self.step_kind == StepKind.SUB_AGENT_DELEGATION:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("delegation node may not carry tool_call fields")
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("delegation node may not carry llm_call fields")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("delegation node may not carry judge_call fields")
        return self


class WorkFlow(BaseModel):
    """A DAG of WorkFlowNodes.

    Stored as a flat ``nodes`` map keyed by NodeId plus a list of root_ids
    (nodes with empty ``parent_ids``). Edges live on each node's
    ``parent_ids`` — no separate edge table.

    The WorkFlow carries its own **trio** (``description`` / ``inputs`` /
    ``expected_outcome``) describing the outer task it's accountable for —
    this is what ``judge_pre`` fills in and what ``judge_post`` measures
    against. See §3.3 of requirements.

    ``execution_mode`` + the four switches together determine how plan and
    judges fire. See §3.4.1.
    """

    id: NodeId = Field(default_factory=generate_node_id)
    nodes: dict[NodeId, WorkFlowNode] = Field(default_factory=dict)
    root_ids: list[NodeId] = Field(default_factory=list)

    # WorkFlow-level trio
    description: EditableText | None = None
    inputs: EditableText | None = None
    expected_outcome: EditableText | None = None

    # Execution behavior — each WorkFlow (including nested ones) picks its own
    execution_mode: ExecutionMode = ExecutionMode.DIRECT
    plan_enabled: bool = False
    judge_pre_enabled: bool = False
    judge_during_enabled: bool = False
    judge_post_enabled: bool = False

    # Per-WorkFlow budget overrides; ``None`` = inherit from ChatFlow
    tool_loop_budget: int | None = None
    auto_mode_revise_budget: int | None = None

    #: Set by the engine when a judge pass decides the WorkFlow cannot
    #: proceed without user clarification (judge_pre says non-OK, or
    #: judge_post says retry/fail). The ChatFlow layer reads this on
    #: WorkFlow completion and opens a new ChatNode whose
    #: ``agent_response`` is this prompt — all user-facing dialogue
    #: lives at the ChatFlow layer, never inside a WorkFlow (§3.5).
    pending_user_prompt: str | None = None

    @property
    def root_id(self) -> NodeId | None:
        """The single root id (§3.2 single-root decision).

        Returns the first entry of ``root_ids`` for forward-compat with
        legacy payloads that may technically have multiple roots; the
        invariant going forward is ``len(root_ids) <= 1`` on new data.
        """
        return self.root_ids[0] if self.root_ids else None

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
