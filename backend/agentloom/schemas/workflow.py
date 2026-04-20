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
    NodeScope,
    ProviderModelRef,
    SharedNote,
    StepKind,
    StickyNote,
    TokenUsage,
    ToolConstraints,
    ToolResult,
    ToolUse,
    WorkNodeRole,
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


class CompactSnapshot(BaseModel):
    """The structured output of a :attr:`StepKind.COMPACT` WorkNode.

    Produced by the compact worker's LLM call and frozen onto the
    WorkNode. Downstream ancestor walks root here: instead of pulling
    the full pre-compact message trail, consumers read
    ``summary`` (the compressed history) plus ``preserved_messages``
    (recent turns kept verbatim).
    """

    #: Compressed-history prose. Becomes a single user-role message at the
    #: start of the downstream context. Empty string is the
    #: pre-execution stub — the engine fills this in from the compact
    #: worker's LLM output when the node completes.
    summary: str = ""
    #: Verbatim tail of the original sequence — kept so the model still has
    #: the most recent exchanges in full fidelity. Inserted after
    #: ``summary`` when reconstructing downstream context.
    preserved_messages: list[WireMessage] = Field(default_factory=list)
    #: Indices into the pre-compact message list that were folded into
    #: ``summary`` (half-open interval). ``preserved_messages`` lives at
    #: ``[end, original_len)``. Recorded for debugging / dry-run display.
    source_range: tuple[int, int] = (0, 0)
    #: Number of original messages folded into the summary (== end - start).
    dropped_count: int = 0
    #: Char-based token estimate of the pre-compact inputs.
    original_tokens: int = 0
    #: Char-based token estimate of ``summary`` + ``preserved_messages``
    #: after the compact run.
    compacted_tokens: int = 0
    #: The free-text instruction the user passed through (manual trigger
    #: or auto-trigger w/ confirmation). ``None`` for silent WorkFlow
    #: compacts that had no user interaction.
    compact_instruction: str | None = None


class MergeSnapshot(BaseModel):
    """Frozen metadata on a ChatNode whose ``parent_ids`` carry a manual merge.

    The merge node's ``agent_response.text`` *is* the synthesized reply
    (emitted by the ``merge`` builtin template); this snapshot records
    which two branches were folded together plus accounting metrics so
    the downstream context walk can stop at the merge node — same
    stop-rule as :class:`CompactSnapshot`.

    MVP merges exactly two nodes (``source_ids`` has length 2). The field
    is modelled as a list so future ≥3-way merges can reuse the shape.
    """

    source_ids: list[NodeId] = Field(default_factory=list)
    #: Optional free-text hint passed through to the merge worker.
    merge_instruction: str | None = None
    #: Char-based token estimate of the pre-merge inputs (left + right contexts).
    #: Captures the true branch sizes BEFORE any pre-compact step — so
    #: ``original_tokens >> merged_tokens`` is the real compression factor.
    original_tokens: int = 0
    #: Char-based token estimate of the merged reply.
    merged_tokens: int = 0
    #: Per-branch flags: set to ``True`` when that branch was too large
    #: to fit the merge model's context window and had to be summarized
    #: via the ``compact`` builtin template before being fed into the
    #: merge prompt. The MergeMessageBubble surfaces this so the user
    #: knows the merge saw a compacted view, not the raw branch.
    left_precompacted: bool = False
    right_precompacted: bool = False


class WorkFlowNode(NodeBase):
    """A single step inside a WorkFlow.

    Invariant: exactly one of the step_kind-specific sub-objects is
    populated, matching ``step_kind``. This is validated at construction.
    """

    step_kind: StepKind
    #: Structural role in the recursive planner model (§3.4.3, ADR-024).
    #: Orthogonal to ``step_kind``. ``None`` for direct-mode nodes and
    #: legacy (pre-recursive-planner) nodes; the engine reads this field
    #: only when the enclosing WorkFlow runs in ``semi_auto`` or ``auto``.
    role: WorkNodeRole | None = None
    #: MemoryBoard brief scope. Populated only when ``step_kind ==
    #: StepKind.BRIEF`` — NODE means the brief summarizes a single
    #: source WorkNode; FLOW means it summarizes the enclosing WorkFlow.
    #: Must be None for every other step_kind. See MemoryBoard design.
    scope: NodeScope | None = None
    tool_constraints: ToolConstraints | None = None
    #: Pin for the model this specific WorkNode's LLM call uses. Set by
    #: the engine at spawn time (from the enclosing ChatNode's
    #: ``resolved_model``) and propagated across retries/tool-call
    #: follow-ups. Not user-facing — ChatFlow-level model selection
    #: happens in the composer (§4.10 rework).
    model_override: ProviderModelRef | None = None

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

    # --- compact ---
    #: Set by the engine when a COMPACT node finishes. Downstream ancestor
    #: walks root at this snapshot instead of the pre-compact message
    #: chain. ``None`` for pending compact nodes and for non-compact kinds.
    compact_snapshot: CompactSnapshot | None = None

    #: If this node is a redo clone spawned by a judge_post ``retry``
    #: verdict, the id of the node it was cloned from (worker or
    #: delegation). Lets the re-aggregation walk the retry lineage so
    #: later rounds can carry forward the latest surviving version of
    #: each round-1 subtask — siblings that succeeded in earlier rounds
    #: still belong in the picture. ``None`` for non-clones.
    redo_source_id: NodeId | None = None

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
            if self.compact_snapshot is not None:
                raise ValueError("llm_call node may not carry compact fields")
        elif self.step_kind == StepKind.TOOL_CALL:
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("tool_call node may not carry llm_call fields")
            if self.sub_workflow is not None:
                raise ValueError("tool_call node may not carry a sub_workflow")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("tool_call node may not carry judge_call fields")
            if self.compact_snapshot is not None:
                raise ValueError("tool_call node may not carry compact fields")
        elif self.step_kind == StepKind.JUDGE_CALL:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("judge_call node may not carry tool_call fields")
            if self.sub_workflow is not None:
                raise ValueError("judge_call node may not carry a sub_workflow")
            if self.judge_variant is None:
                raise ValueError("judge_call node requires judge_variant")
            if self.compact_snapshot is not None:
                raise ValueError("judge_call node may not carry compact fields")
        elif self.step_kind == StepKind.SUB_AGENT_DELEGATION:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("delegation node may not carry tool_call fields")
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("delegation node may not carry llm_call fields")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("delegation node may not carry judge_call fields")
            if self.compact_snapshot is not None:
                raise ValueError("delegation node may not carry compact fields")
        elif self.step_kind == StepKind.COMPACT:
            # Compact nodes share llm_call's input/output/usage shape
            # (they're a single LLM invocation under the hood) and
            # additionally carry ``compact_snapshot`` as the structured
            # parse of the worker's JSON output.
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("compact node may not carry tool_call fields")
            if self.sub_workflow is not None:
                raise ValueError("compact node may not carry a sub_workflow")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("compact node may not carry judge_call fields")
        elif self.step_kind == StepKind.BRIEF:
            # Brief nodes are llm_call-shaped: they carry input_messages /
            # output_message / usage and nothing else. ``scope`` must
            # be populated to distinguish node-brief from flow-brief.
            if self.scope is None:
                raise ValueError("brief node requires scope (node or flow)")
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("brief node may not carry tool_call fields")
            if self.sub_workflow is not None:
                raise ValueError("brief node may not carry a sub_workflow")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("brief node may not carry judge_call fields")
            if self.compact_snapshot is not None:
                raise ValueError("brief node may not carry compact fields")

        # Non-brief nodes may not carry a scope — scope is a brief-only
        # marker. Prevents accidental contamination across kinds.
        if self.step_kind != StepKind.BRIEF and self.scope is not None:
            raise ValueError(
                f"scope is only valid on brief nodes, got step_kind={self.step_kind.value}"
            )

        # ADR-024: role and step_kind are orthogonal but constrained to a
        # whitelist of compatible pairings. ``role=None`` is always valid
        # (direct mode / legacy nodes); a non-null role must match.
        if self.role is not None:
            allowed_kinds = _ROLE_TO_STEP_KINDS.get(self.role, set())
            if self.step_kind not in allowed_kinds:
                raise ValueError(
                    f"role={self.role.value} requires step_kind in "
                    f"{{{', '.join(sorted(k.value for k in allowed_kinds))}}}, "
                    f"got step_kind={self.step_kind.value}"
                )
        return self


_ROLE_TO_STEP_KINDS: dict[WorkNodeRole, set[StepKind]] = {
    WorkNodeRole.PRE_JUDGE: {StepKind.JUDGE_CALL},
    WorkNodeRole.PLANNER: {StepKind.LLM_CALL},
    WorkNodeRole.PLANNER_JUDGE: {StepKind.JUDGE_CALL},
    # Worker is the only role with mechanical flexibility — atomic tasks
    # may be a model call or a direct tool invocation. ``sub_agent_delegation``
    # is explicitly *not* allowed: a worker by definition does not decompose.
    WorkNodeRole.WORKER: {StepKind.LLM_CALL, StepKind.TOOL_CALL},
    WorkNodeRole.WORKER_JUDGE: {StepKind.JUDGE_CALL},
    WorkNodeRole.POST_JUDGE: {StepKind.JUDGE_CALL},
}


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
    execution_mode: ExecutionMode = ExecutionMode.NATIVE_REACT
    plan_enabled: bool = False
    judge_pre_enabled: bool = False
    judge_during_enabled: bool = False
    judge_post_enabled: bool = False

    # Per-WorkFlow budget overrides; ``None`` = inherit from ChatFlow
    tool_loop_budget: int | None = None
    auto_mode_revise_budget: int | None = None
    #: Hard cap on planner↔planner_judge / worker↔worker_judge debate
    #: rounds before forcing convergence (§3.4.5). Ignored in
    #: ``native_react`` mode where there are no judges.
    debate_round_budget: int = 3
    #: Hard cap on judge_post retry rounds before the engine stops re-
    #: spawning redo clones and halts to the user. ``-1`` = unlimited
    #: (the engine keeps retrying as long as the judge votes ``retry``
    #: with redo_targets). Distinct from ``debate_round_budget`` —
    #: debate rounds are the planner/worker inner convergence loop,
    #: retry rounds are the judge_post outer "fix these subtasks" loop.
    judge_retry_budget: int = 3

    #: Per-call-type model pins, snapshotted from the enclosing ChatFlow
    #: at WorkFlow creation. The engine stamps every judge_call node's
    #: ``model_override`` from ``judge_model_override`` (so judges run on
    #: the chatflow's chosen judge model regardless of the main turn
    #: model), and tool-call follow-up llm_calls from
    #: ``tool_call_model_override``. ``None`` means "no per-kind pin —
    #: fall back to the node's own ``model_override``" (which is itself
    #: the ChatNode's resolved_model). Sub-WorkFlows inherit these too,
    #: so the whole nested tree honors the same defaults.
    judge_model_override: ProviderModelRef | None = None
    tool_call_model_override: ProviderModelRef | None = None
    #: Snapshotted MemoryBoard brief pin. The engine stamps every
    #: ``StepKind.BRIEF`` WorkNode's ``model_override`` from this field
    #: so brief runs go to the ChatFlow's brief_model regardless of the
    #: main turn model. ``None`` → brief falls back to the per-node
    #: ``model_override`` (which is itself the ChatNode's resolved_model).
    brief_model_override: ProviderModelRef | None = None

    #: Set by the engine when a judge pass decides the WorkFlow cannot
    #: proceed without user clarification (judge_pre says non-OK, or
    #: judge_post says retry/fail). The ChatFlow layer reads this on
    #: WorkFlow completion and opens a new ChatNode whose
    #: ``agent_response`` is this prompt — all user-facing dialogue
    #: lives at the ChatFlow layer, never inside a WorkFlow (§3.5).
    pending_user_prompt: str | None = None

    #: Layer-local blackboard. Engine appends a one-line summary when a
    #: WorkNode succeeds so siblings / aggregating judges get a layer
    #: picture without pulling every full output into context. Full
    #: content stays on the WorkNode itself; consumers that need it
    #: pull explicitly via prompt params. NOT shared across nested
    #: WorkFlows — each layer has its own (§3.4.6).
    shared_notes: list[SharedNote] = Field(default_factory=list)
    sticky_notes: dict[str, StickyNote] = Field(default_factory=dict)

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
