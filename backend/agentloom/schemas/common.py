"""Shared types used by both ChatFlow and WorkFlow."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

import uuid_utils
from pydantic import BaseModel, Field

NodeId = str  # UUIDv7 string; sortable by creation time


def generate_node_id() -> NodeId:
    """Return a freshly minted UUIDv7 string."""
    return str(uuid_utils.uuid7())


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class NodeStatus(str, Enum):
    """Lifecycle of a node.

    Transitions are:

        planned → running → succeeded | failed | cancelled
                        ↘ waiting_for_rate_limit → running
                        ↘ retrying → running

    ``succeeded`` and ``failed`` are terminal (frozen). See §4.1 of
    ``docs/requirements.md``.
    """

    PLANNED = "planned"
    RUNNING = "running"
    WAITING_FOR_RATE_LIMIT = "waiting_for_rate_limit"
    WAITING_FOR_USER = "waiting_for_user"  # auto-mode halt pending user resume (§3.4.1)
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}

    @property
    def is_frozen(self) -> bool:
        """Frozen nodes are immutable — see §4.1."""
        return self in {NodeStatus.SUCCEEDED, NodeStatus.FAILED}


class StepKind(str, Enum):
    """Kind of work a WorkFlowNode does."""

    DRAFT = "draft"
    TOOL_CALL = "tool_call"
    JUDGE_CALL = "judge_call"  # pre / during / post — see §3.5
    DELEGATE = "delegate"
    #: Context compaction — a single LLM call that summarizes an upstream
    #: message sequence into a :class:`CompactSnapshot`, which descendants
    #: use in place of the full ancestor trail. Auto-inserted by the
    #: engine (Tier 1) when a pending draft's estimated context exceeds
    #: the configured threshold; explicitly placed by users in ChatFlow
    #: (Tier 2). See compact design in devlog 2026-04-18 夜.
    COMPRESS = "compress"
    #: Branch merge — a single LLM call that synthesizes two ChatNode
    #: branches into one follow-up reply, recorded as a
    #: :class:`MergeSnapshot` on the resulting multi-parent ChatNode.
    #: Downstream context walks stop at the merge node exactly like they
    #: stop at a compact node.
    MERGE = "merge"
    #: MemoryBoard producer — a one-shot WorkNode that distills either
    #: another WorkNode (``scope=NODE``) or the enclosing WorkFlow
    #: (``scope=FLOW``) into a single short prose description. Auto-
    #: spawned by the engine on every non-brief / non-delegate WorkNode
    #: success and once at WorkFlow terminal time. The description is
    #: written to a ``BoardItem`` row. See MemoryBoard design 2026-04-20.
    BRIEF = "brief"


class NodeScope(str, Enum):
    """Distinguishes the two kinds of ``StepKind.BRIEF`` WorkNode.

    - ``NODE`` — brief summarizes a single source WorkNode (sibling in
      the same WorkFlow). Parent is the source node.
    - ``FLOW`` — brief summarizes the enclosing WorkFlow as a whole.
      Parents are every terminal node in the flow; the output becomes
      the WorkFlow's own WorkBoardItem so the parent layer can read it
      via the sub_agent_delegation container.
    """

    NODE = "node"
    FLOW = "flow"


class JudgeVariant(str, Enum):
    """Which of the three judge passes a ``judge_call`` WorkNode represents."""

    PRE = "pre"
    DURING = "during"
    POST = "post"


class WorkNodeRole(str, Enum):
    """Structural role in the recursive planner model — see §3.4.3 / ADR-024.

    Orthogonal to ``StepKind``: ``role`` answers "what is this node's
    purpose in the planning model?", ``step_kind`` answers "what
    mechanical invocation does it perform?". The engine interprets
    ``role`` only when the WorkFlow's execution mode is ``semi_auto`` or
    ``auto``; in ``direct`` mode and on legacy nodes ``role`` is None and
    only ``step_kind`` is consulted.
    """

    PRE_JUDGE = "pre_judge"          # judge_call — frame the task, decide feasibility
    PLAN = "plan"                    # draft — atomic-or-decompose decision
    PLAN_JUDGE = "plan_judge"        # judge_call — review the plan, debate
    WORKER = "worker"                # draft OR tool_call — execute atomic task
    WORKER_JUDGE = "worker_judge"    # judge_call — review the worker's draft, debate
    POST_JUDGE = "post_judge"        # judge_call — verify success, roll up children, handoff


class ExecutionMode(str, Enum):
    """How autonomous a WorkFlow's execution is — see §3.4.1."""

    NATIVE_REACT = "native_react"  # pure ReAct, no plan phase
    SEMI_AUTO = "semi_auto"        # plan + modal gates; keyframes allowed
    AUTO_PLAN = "auto_plan"        # end-to-end until halt condition


class EditProvenance(str, Enum):
    """Who authored an editable field. See §4.4 of requirements."""

    PURE_USER = "pure_user"  # green
    PURE_AGENT = "pure_agent"  # blue
    MIXED = "mixed"  # purple
    UNSET = "unset"


class EditableText(BaseModel):
    """Rich text field with edit provenance.

    MVP stores attribution at field-level granularity (one tag per field).
    Character-level spans are a v2+ concern — see ADR-015 (implicit) in
    requirements §4.4.
    """

    text: str = ""
    provenance: EditProvenance = EditProvenance.UNSET
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def by_user(cls, text: str) -> "EditableText":
        return cls(text=text, provenance=EditProvenance.PURE_USER)

    @classmethod
    def by_agent(cls, text: str) -> "EditableText":
        return cls(text=text, provenance=EditProvenance.PURE_AGENT)

    def edited_by_user(self, new_text: str) -> "EditableText":
        """Return a new EditableText after a user edit.

        If we were pure_agent, we become mixed. If pure_user or unset, we
        remain pure_user. Mixed stays mixed.
        """
        new_prov: EditProvenance
        if self.provenance == EditProvenance.PURE_AGENT:
            new_prov = EditProvenance.MIXED
        else:
            new_prov = EditProvenance.PURE_USER
        return EditableText(text=new_text, provenance=new_prov, updated_at=utcnow())

    def edited_by_agent(self, new_text: str) -> "EditableText":
        """Return a new EditableText after an auto-planner edit."""
        new_prov: EditProvenance
        if self.provenance == EditProvenance.PURE_USER:
            new_prov = EditProvenance.MIXED
        else:
            new_prov = EditProvenance.PURE_AGENT
        return EditableText(text=new_text, provenance=new_prov, updated_at=utcnow())


class ProviderModelRef(BaseModel):
    """A reference to a specific model on a specific configured provider."""

    provider_id: str
    model_id: str


class ToolConstraints(BaseModel):
    """Per-node allow/deny list for tools.

    Wildcards use glob-like syntax handled at resolve time:

        "Bash"               -> all bash commands
        "Bash(git *)"        -> bash starting with "git "
        "McpTool(tavily, *)" -> any tavily mcp tool
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class NodeBase(BaseModel):
    """Fields common to both ChatFlowNode and WorkFlowNode.

    The **planning trio** (``description`` / ``inputs`` / ``expected_outcome``)
    gives every node a consistent shape — what to do, what it needs, what
    success looks like. Judges read and write the trio.
    """

    id: NodeId = Field(default_factory=generate_node_id)
    parent_ids: list[NodeId] = Field(default_factory=list)
    description: EditableText = Field(default_factory=EditableText)
    inputs: EditableText | None = None
    expected_outcome: EditableText | None = None
    status: NodeStatus = NodeStatus.PLANNED
    #: The model this node actually ran with, set at spawn time from the
    #: composer's choice (if the user picked one) or inherited from the
    #: primary parent's ``resolved_model``. Immutable after spawn — edits
    #: to an ancestor never rewrite history. Conceptually this is the
    #: model carried by the incoming edge (parent→this); we store it on
    #: the child since Agentloom's DAG has no first-class edge objects.
    #: ``None`` means "not yet resolved" (still PLANNED in certain
    #: bootstrap paths, or pre-existing nodes from before this field
    #: existed — UI falls back to the chatflow default).
    resolved_model: ProviderModelRef | None = None
    locked: bool = False
    error: str | None = None

    # Canvas position — persisted so layouts survive page reload.
    position_x: float | None = None
    position_y: float | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_dashed(self) -> bool:
        """Pre-execution placeholder — opposite of frozen/solid."""
        return self.status in {NodeStatus.PLANNED, NodeStatus.WAITING_FOR_RATE_LIMIT}

    @property
    def is_frozen(self) -> bool:
        return self.status.is_frozen

    def require_mutable(self) -> None:
        """Raise if the node is frozen. Call at the top of any mutation path."""
        if self.is_frozen:
            raise FrozenNodeError(f"Node {self.id} is frozen ({self.status.value})")
        if self.locked:
            # Note: locked nodes CAN be user-edited; engine/auto-planner must
            # check this separately. This helper is the frozen-guard only.
            pass


class FrozenNodeError(Exception):
    """Raised when mutating a frozen node. See §4.1."""


class CycleError(Exception):
    """Raised when an edge would create a DAG cycle."""


class NodeHasReferencesError(Exception):
    """Raised when deleting a node that is still referenced by children."""


class ToolUse(BaseModel):
    """An assistant request to call a tool (stored on llm_call output)."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Result of a tool invocation (stored on tool_call output)."""

    content: str
    is_error: bool = False
    attachments: list[str] = Field(default_factory=list)  # blob refs


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0


class Critique(BaseModel):
    """One item from a ``judge_during`` pass. See ADR-020."""

    issue: str
    severity: Literal["blocker", "concern", "nit"] = "concern"
    evidence: str = ""


class Issue(BaseModel):
    """One item from a ``judge_post`` FAIL/RETRY pass. See ADR-018.

    ``location`` points at a WorkNode id so the canvas can jump/highlight
    exactly where the issue was found.
    """

    location: NodeId
    expected: str
    actual: str
    reproduction: str = ""


class RedoTarget(BaseModel):
    """A node a judge wants re-run, with the critique that motivates it.

    Used by judges (currently judge_post — see ADR-024 §3.4.6) to bounce
    work back into the chain rather than halting. ``node_id`` may point
    at any ancestor in the same WorkFlow:

    - a planner → re-run the planning step (engine spawns a fresh
      planner sibling threading the critique)
    - a sub_agent_delegation → re-run that sub-WorkFlow with the
      critique as additional context
    - a worker → spawn a fresh worker round (within debate budget)

    The orchestrator decides the spawn shape from ``node_id``'s role;
    the judge only declares "this needs another pass, here's why".
    """

    node_id: NodeId
    critique: str


class JudgeVerdict(BaseModel):
    """Structured parse of a ``judge_call`` WorkNode's output.

    Exactly the subset of fields relevant to ``judge_variant`` is populated;
    other fields are left at their defaults. Parsing failures surface as a
    ``failed`` judge_call with the raw output preserved on
    ``output_message`` (not here).
    """

    # --- judge_pre ---
    feasibility: Literal["ok", "risky", "infeasible"] | None = None
    blockers: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    #: judge_pre additionally distills the conversation into the
    #: WorkFlow trio (description / inputs / expected_outcome) so the
    #: planner downstream doesn't have to re-parse the full transcript.
    #: The engine writes these onto ``WorkFlow.description`` / ``.inputs``
    #: / ``.expected_outcome`` before spawning the planner. All three
    #: are optional: an empty string means "judge_pre couldn't extract
    #: a clean value", which the planner treats as infeasible.
    extracted_description: str | None = None
    extracted_inputs: str | None = None
    extracted_expected_outcome: str | None = None

    # --- judge_during ---
    critiques: list[Critique] = Field(default_factory=list)
    during_verdict: Literal["continue", "revise", "halt"] | None = None

    # --- judge_post ---
    post_verdict: Literal["accept", "retry", "fail"] | None = None
    issues: list[Issue] = Field(default_factory=list)
    #: User-facing prose written by judge_post when the WorkFlow halts.
    #: judge_post is the universal exit gate (Option B) — it decides
    #: what the user sees regardless of whether the halt came from a
    #: judge_pre veto, a judge_during retry-budget exhaustion, a node
    #: error, or its own retry/fail verdict. Only meaningful when
    #: ``post_verdict != "accept"``; for accept the terminal llm_call's
    #: own output reaches the user untouched.
    user_message: str | None = None
    #: Synthesized output for an ``accept`` judge_post on an aggregating
    #: layer (one whose plan was ``decompose``). The merged text becomes
    #: that layer's effective output — at the top it surfaces as
    #: ``ChatNode.agent_response``; in a nested sub-WorkFlow it becomes
    #: the parent ``sub_agent_delegation``'s output. ``None`` when the
    #: layer was atomic (the worker's own output already serves).
    merged_response: str | None = None
    #: Nodes the judge wants re-run before the layer can be considered
    #: done. Empty on ``accept``; populated on ``retry`` / ``fail`` when
    #: the issue is fixable in-chain rather than user-facing. The
    #: orchestrator inspects each target's role to pick the right
    #: re-spawn shape (re-plan / re-run sub-WorkFlow / fresh worker).
    redo_targets: list[RedoTarget] = Field(default_factory=list)


class SharedNote(BaseModel):
    """One entry in a WorkFlow's blackboard.

    Engine appends a note when a WorkNode succeeds: a one-line summary
    so downstream siblings / aggregators get a layer-wide picture
    without pulling every full output into context. Full content lives
    on the WorkNode itself (``workflow.nodes[author_node_id]``); callers
    that need it pull it explicitly via prompt params.

    Notes do NOT cross layer boundaries — each sub-WorkFlow has its own
    blackboard. Information that needs to flow between layers travels
    through ``sub_agent_delegation`` inputs / outputs explicitly.
    """

    author_node_id: NodeId
    role: WorkNodeRole | None = None
    kind: Literal["node_succeeded", "judge_verdict"] = "node_succeeded"
    summary: str
    at: datetime = Field(default_factory=utcnow)


class StickyNote(BaseModel):
    """User-created canvas sticky note, persisted with the ChatFlow."""

    id: NodeId = Field(default_factory=generate_node_id)
    title: str = "Note"
    text: str = ""
    x: float = 0.0
    y: float = 0.0
    width: float = 200.0
    height: float = 120.0
