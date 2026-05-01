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
    """The structured output of a :attr:`StepKind.COMPRESS` WorkNode.

    Produced by the compact worker's LLM call and frozen onto the
    WorkNode. Downstream ancestor walks root here: instead of pulling
    the full pre-compact message trail, consumers read
    ``summary`` (the compressed history) plus ``preserved_messages``
    (recent turns kept verbatim).
    """

    summary: str = ""
    preserved_messages: list[WireMessage] = Field(default_factory=list)
    #: True when ``preserved_messages`` hold the *shared prefix* that
    #: came *before* the summary in temporal order — the joint-compact
    #: merge path writes a ChatNode whose summary folds both sibling
    #: branches' suffixes, but the root-→LCA prefix is what we want
    #: downstream readers to see first, not last. Default False keeps
    #: the historical Tier-2 compact semantics (preserved_messages are
    #: the recent-tail carried past the cutoff).
    preserved_before_summary: bool = False


class PackSnapshot(BaseModel):
    """Structured output of a "pack" ChatNode — ChatFlow-layer topic
    packaging, symmetric with but not identical to
    :class:`CompactSnapshot`.

    Where compact implicitly covers a root→leaf ChatNode prefix, pack
    covers an **arbitrary contiguous range** of ChatNodes chosen by
    the user and recorded explicitly in ``packed_range``. The pack
    ChatNode's parent is ``packed_range[-1]`` (the last packed
    ChatNode) and its ``agent_response.text`` carries ``summary``.

    From the pack ChatNode itself and every descendant downstream of
    it, ancestor walks in ``_build_chat_context`` substitute
    ``summary`` for every ChatNode in ``packed_range`` and stop
    walking past the pack. From the pre-pack siblings and the global
    canvas the range remains fully visible as if pack never ran.

    Pack is nestable: a member of ``packed_range`` may itself be a
    pack ChatNode, resolved recursively at walk time.

    ``use_detailed_index`` / ``preserve_last_n`` are the per-
    invocation knobs the user can twist; their defaults match
    compact-all-on so pack run with all-defaults looks like a
    mid-graph compact.
    """

    summary: str = ""
    #: ChatNode ids covered by this pack, in topological order along
    #: the primary-parent chain — first element is the earliest
    #: packed ChatNode, last element is ``parent_ids[0]`` of the pack
    #: ChatNode itself. Non-empty at commit time.
    packed_range: list[NodeId] = Field(default_factory=list)
    #: When True (default), each packed ChatNode also keeps its own
    #: ChatBoardItem so downstream refs can cite members individually.
    #: When False, only pack's own item is emitted and members collapse
    #: into one monolithic summary for citation purposes.
    use_detailed_index: bool = True
    #: Number of most-recent ChatNodes inside the range to keep
    #: verbatim (as ``preserved_messages``) instead of folding into
    #: ``summary``. 0 = no preserved tail (typical for a mid-graph
    #: pack). Compact's analogous knob lives on ChatFlowSettings;
    #: pack's is per-invocation because pack is always user-initiated.
    preserve_last_n: int = 0
    #: Verbatim tail messages carried past the pack cutoff, matching
    #: ``preserve_last_n``. Empty when ``preserve_last_n == 0``.
    preserved_messages: list[WireMessage] = Field(default_factory=list)


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
    #: M7.5 capability model — registry tool names this node is
    #: permitted to call. ``None`` means "fall back to legacy
    #: ``tool_constraints`` + chatflow disabled list" (the pre-M7.5
    #: behavior; PR 3's engine filter checks this distinction). ``[]``
    #: means an explicit empty whitelist (e.g. monitoring nodes that
    #: shouldn't call any tool). PR 1 only adds the field; consumer
    #: filtering lands in PR 3.
    effective_tools: list[str] | None = None
    #: M7.5 capability model — registry tool names this node is allowed
    #: to **delegate to children**, i.e. the ceiling for any subtask
    #: ``effective_tools`` it spawns. Only meaningful for ``PRE_JUDGE``
    #: (writes the WorkFlow's overall ceiling), ``PLAN`` (re-distributes
    #: to subtasks), and ``DELEGATE`` (re-distributes to sub_workflow
    #: roots). All other roles leave this ``None``.
    inheritable_tools: list[str] | None = None
    #: M7.5 capability_request signal slot — when an execution node
    #: discovers it needs a tool that isn't in its ``effective_tools``,
    #: it emits the missing tool name(s) here instead of silently
    #: failing. judge_during / monitoring scans for non-empty lists and
    #: bubbles them to a re-plan request via ``JudgeVerdict.
    #: capability_escalation``. PR 5 implements the read+propagate path.
    capability_request: list[str] = Field(default_factory=list)
    #: Prong 2 (2026-04-30) signal slot symmetric to
    #: ``capability_request``: when a worker discovers that the
    #: planner-authored brief paraphrased away some piece of context
    #: it needs (e.g. specific table schema, code block, URL, exact
    #: number), it emits ``<missing_input>concise description</missing_input>``
    #: in its draft. The engine extracts the markers into this list
    #: at LLM-call completion time. ``_spawn_worker_judge`` /
    #: ``_spawn_judge_post`` render it into the judge fixture so the
    #: judge can bubble the entries to ``JudgeVerdict.missing_input_escalation``,
    #: which the missing-input feedback path (prong 3) reads to
    #: spawn a fresh planner with handoff_notes describing what was
    #: missing — instead of going straight to retry / fail.
    missing_input: list[str] = Field(default_factory=list)
    #: Bug A layer 2 (2026-04-30) — engine-detected fabricated tool
    #: failure. The layer-1 truth ledger (``_render_tool_result_ledger``)
    #: gives the judge a flat list of ancestor tool_call outcomes and
    #: asks the judge to spot mismatches between worker narrative and
    #: ``is_error`` truth; layer 2 closes the same loop on the engine
    #: side so it doesn't depend on the judge correctly executing the
    #: prompted cross-check. After a worker draft completes the engine
    #: scans its narrative for failure-claiming phrases ("调用失败",
    #: "tool returned nothing", "didn't get the data", etc.) located
    #: within a tight window of an ancestor tool_call's name; for any
    #: such match where that ancestor recorded ``is_error=False`` with
    #: non-empty content, one human-readable explanation is appended
    #: here. The judge fixture renders the field as an authoritative
    #: red flag separate from the ledger so weak judge models can't
    #: miss it. Empty list = no fabricated-failure suspicion (most
    #: turns).
    suspected_fabricated_failure: list[str] = Field(default_factory=list)
    #: Pin for the model this specific WorkNode's LLM call uses. Set by
    #: the engine at spawn time (from the enclosing ChatNode's
    #: ``resolved_model``) and propagated across retries/tool-call
    #: follow-ups. Not user-facing — ChatFlow-level model selection
    #: happens in the composer (§4.10 rework).
    model_override: ProviderModelRef | None = None

    # --- Keyframe flags (§3.4.2 / §4.9) — meaningful only while dashed ---
    is_keyframe: bool = False
    is_keyframe_locked: bool = False
    #: Schema slot for the unlocked-keyframe diff/restore feature: when
    #: the planner edits an unlocked keyframe's trio, the original was
    #: meant to be saved here so the canvas could show "restore to
    #: original" + diff. **Currently neither written nor read** — the
    #: feature was never wired up. Field is preserved (instead of
    #: removed) so a future implementation has a stable JSON key on
    #: persisted payloads. Keyframe LOCK enforcement (the
    #: ``is_keyframe_locked=True`` invariant) is implemented and lives
    #: in :mod:`agentloom.engine.keyframe_validator` — that path does
    #: not use this field.
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
        if self.step_kind == StepKind.DRAFT:
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
        elif self.step_kind == StepKind.DELEGATE:
            if self.tool_name or self.tool_args or self.tool_result:
                raise ValueError("delegation node may not carry tool_call fields")
            if self.input_messages or self.output_message or self.usage:
                raise ValueError("delegation node may not carry llm_call fields")
            if self.judge_variant or self.judge_verdict:
                raise ValueError("delegation node may not carry judge_call fields")
            if self.compact_snapshot is not None:
                raise ValueError("delegation node may not carry compact fields")
        elif self.step_kind == StepKind.COMPRESS:
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
    WorkNodeRole.PLAN: {StepKind.DRAFT},
    WorkNodeRole.PLAN_JUDGE: {StepKind.JUDGE_CALL},
    # Worker is the only role with mechanical flexibility — atomic tasks
    # may be a model call or a direct tool invocation. ``sub_agent_delegation``
    # is explicitly *not* allowed: a worker by definition does not decompose.
    WorkNodeRole.WORKER: {StepKind.DRAFT, StepKind.TOOL_CALL},
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
    #: M7.5 capability model — the registry tool names planner is
    #: permitted to authorize for downstream subtasks (the "ceiling"
    #: that worker ``effective_tools`` cannot exceed). Written by
    #: ``_apply_judge_pre_trio`` from ``JudgeVerdict.extracted_inheritable_tools``
    #: before the planner runs. Empty = judge_pre didn't scope OR a
    #: pre-M7.5 chatflow that hasn't run a fresh judge_pre — engine
    #: PR 3 fallback is to treat empty as "registry full set" so legacy
    #: chatflows keep current behavior. Planner reads this to constrain
    #: each subtask's ``effective_tools`` allocation.
    inheritable_tools: list[str] = Field(default_factory=list)
    #: Natural-language capability provenance — kept for UI display,
    #: human review, and debugging. Pre-M7.5 this field was named
    #: ``capabilities`` and stored e.g. ["web_search", "code_execution"];
    #: M7.5 keeps that semantic but renames + pairs it with the
    #: registry-name list above. Pydantic validator below accepts the
    #: legacy ``capabilities`` JSON key as an alias on ingest so old
    #: persisted chatflows decode cleanly. Empty = judge_pre didn't
    #: emit a natural-language list.
    capabilities_origin: list[str] = Field(default_factory=list)

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

    #: Auto-mode recursion depth. 0 = outermost ChatNode-level WorkFlow;
    #: each ``sub_agent_delegation`` sub-WorkFlow is depth+1. The
    #: planner-judge pipeline uses this as a fuse: when depth hits
    #: ``MAX_DELEGATION_DEPTH`` (set on WorkFlow.delegation_depth_budget)
    #: a ``decompose`` plan is forced into ``atomic`` so the sub never
    #: fans out another layer of delegates. Added 2026-04-22 after
    #: integration tests showed auto_plan decomposing 62+ node trees
    #: on mildly-multipart prompts.
    delegation_depth: int = 0
    #: Maximum recursive planner delegation depth. Hitting this cap
    #: degrades the plan to atomic so the worker runs directly. Keep
    #: small — one layer of fan-out is usually enough for multi-part
    #: prompts; deeper trees are almost always over-decomposition.
    delegation_depth_budget: int = 2

    #: Verbatim copy of the **outer ChatNode's user_message.text** that
    #: anchors this WorkFlow tree. Set by ``_spawn_turn_node`` at the
    #: top of the chain and propagated through every
    #: ``_build_sub_workflow_for_subtask`` so nested sub-WorkFlows
    #: never lose access to what the user actually wrote — the engine
    #: prepends it to each sub-WorkFlow's judge_pre input_messages
    #: as a "[Outer ChatFlow context]" preamble. Empty string for
    #: legacy WorkFlows / pre-2026-04-30 payloads + bare-engine
    #: tests; in those cases the engine simply skips the injection
    #: and falls back to pre-fix behavior. See
    #: ``docs/backlog-decompose-fact-loss.md`` prong 5 for the
    #: design rationale (decompose subtask description was
    #: paraphrasing the user's data and sub-WorkFlows had no way
    #: to recover it; carrying the outer user_message here gives
    #: them an unconditional fallback channel).
    outer_user_message: str = ""

    sticky_notes: dict[str, StickyNote] = Field(default_factory=dict)

    @property
    def root_id(self) -> NodeId | None:
        """The single root id (§3.2 single-root decision).

        Returns the first entry of ``root_ids`` for forward-compat with
        legacy payloads that may technically have multiple roots; the
        invariant going forward is ``len(root_ids) <= 1`` on new data.
        """
        return self.root_ids[0] if self.root_ids else None

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_capabilities(cls, data: Any) -> Any:
        """Backwards-compat for the M7.5 capability model rename.

        Pre-M7.5 the natural-language capability list lived on the
        ``capabilities`` field. M7.5 splits that into:

        - ``inheritable_tools`` (NEW, registry tool names — engine
          consumed)
        - ``capabilities_origin`` (RENAMED from ``capabilities``,
          natural-language provenance — UI / human review)

        Persisted JSON from older chatflows still carries the
        ``capabilities`` key. This validator translates it to
        ``capabilities_origin`` on ingest. Legacy data has no
        ``inheritable_tools`` — engine PR 3 fallback treats empty as
        "registry full set" so old chatflows keep current behavior
        until the next judge_pre run repopulates both fields.
        """
        if isinstance(data, dict):
            if "capabilities" in data and "capabilities_origin" not in data:
                data = {**data, "capabilities_origin": data["capabilities"]}
            if "capabilities" in data:
                data = {k: v for k, v in data.items() if k != "capabilities"}
        return data

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
