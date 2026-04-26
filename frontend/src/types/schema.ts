/**
 * TypeScript mirrors of the backend Pydantic schemas.
 *
 * These types are intentionally structural (not branded) and match the
 * JSON shape that `GET /api/chatflows/{id}` returns. Keep in sync with
 * `backend/agentloom/schemas/common.py`, `chatflow.py`, `workflow.py`.
 *
 * Rule: every field added on the backend must be added here *before*
 * any UI reads it, so TypeScript catches schema drift at build time.
 */

export type NodeId = string;

export const NODE_STATUSES = [
  "planned",
  "running",
  "waiting_for_rate_limit",
  "waiting_for_user",
  "succeeded",
  "failed",
  "retrying",
  "cancelled",
] as const;

export type NodeStatus = (typeof NODE_STATUSES)[number];

export const STEP_KINDS = [
  "draft",
  "tool_call",
  "judge_call",
  "delegate",
  "compress",
  "merge",
  "brief",
] as const;

export type StepKind = (typeof STEP_KINDS)[number];

export type JudgeVariant = "pre" | "during" | "post";

/**
 * Structural role in the recursive planner model (§3.4.4 / ADR-024).
 * Orthogonal to ``StepKind``. ``null`` for direct-mode and legacy nodes.
 */
export const WORK_NODE_ROLES = [
  "pre_judge",
  "plan",
  "plan_judge",
  "worker",
  "worker_judge",
  "post_judge",
] as const;

export type WorkNodeRole = (typeof WORK_NODE_ROLES)[number];

export const EXECUTION_MODES = ["native_react", "semi_auto", "auto_plan"] as const;
export type ExecutionMode = (typeof EXECUTION_MODES)[number];

export interface Critique {
  issue: string;
  severity: "blocker" | "concern" | "nit";
  evidence: string;
}

export interface Issue {
  location: NodeId;
  expected: string;
  actual: string;
  reproduction: string;
}

export interface RedoTarget {
  node_id: NodeId;
  critique: string;
}

export interface JudgeVerdict {
  // pre
  feasibility: "ok" | "risky" | "infeasible" | null;
  blockers: string[];
  missing_inputs: string[];
  // during
  critiques: Critique[];
  during_verdict: "continue" | "revise" | "halt" | null;
  // post
  post_verdict: "accept" | "retry" | "fail" | null;
  issues: Issue[];
  // Universal exit-gate prose. Written by judge_post (Option B) when
  // the workflow halts; the ChatFlow layer surfaces it as the agent's
  // reply. Null on accept paths.
  user_message: string | null;
  /** Synthesized output for accept on a decompose layer; becomes that
   * layer's effective output (§3.4.6). Null on atomic layers. */
  merged_response?: string | null;
  /** Nodes the judge wants re-run before the layer completes. */
  redo_targets?: RedoTarget[];
}

export type EditProvenance = "pure_user" | "pure_agent" | "mixed" | "unset";

export interface EditableText {
  text: string;
  provenance: EditProvenance;
  updated_at: string;
}

export interface ProviderModelRef {
  provider_id: string;
  model_id: string;
}

export interface ToolConstraints {
  allow: string[];
  deny: string[];
}

export interface ToolUse {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResult {
  content: string;
  is_error: boolean;
  attachments: string[];
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_tokens: number;
  reasoning_tokens: number;
}

export interface WireMessage {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  tool_uses: ToolUse[];
  tool_use_id: string | null;
  extras: Record<string, unknown>;
}

export interface NodeBaseFields {
  id: NodeId;
  parent_ids: NodeId[];
  description: EditableText;
  inputs: EditableText | null;
  expected_outcome: EditableText | null;
  status: NodeStatus;
  /** Snapshot of the model carried by the incoming edge (parent→this),
   * stamped at spawn from the composer's pick or inherited from the
   * primary parent. Immutable after spawn — edits to an ancestor never
   * rewrite history. Null for legacy rows that predate this field. */
  resolved_model: ProviderModelRef | null;
  locked: boolean;
  error: string | null;
  position_x: number | null;
  position_y: number | null;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface WorkFlowNode extends NodeBaseFields {
  step_kind: StepKind;
  /** Structural role in the recursive planner flow. ``null`` outside
   * semi_auto/auto modes. See §3.4.4. */
  role: WorkNodeRole | null;
  /** MemoryBoard brief scope: ``"node"`` or ``"flow"``. Populated
   * only on BRIEF WorkNodes; null for every other kind. */
  scope?: "node" | "flow" | null;
  tool_constraints: ToolConstraints | null;
  /** Pin for this WorkNode's LLM call. Set by the engine at spawn time
   * from the enclosing ChatNode's resolved_model and propagated across
   * retries. Not user-facing. */
  model_override: ProviderModelRef | null;

  // draft
  input_messages?: WireMessage[] | null;
  output_message?: WireMessage | null;
  usage?: TokenUsage | null;

  // tool_call
  source_tool_use_id?: string | null;
  tool_name?: string | null;
  tool_args?: Record<string, unknown> | null;
  tool_result?: ToolResult | null;

  // delegate
  sub_workflow?: WorkFlow | null;

  // judge_call (ADR-018)
  judge_variant?: JudgeVariant | null;
  judge_target_id?: NodeId | null;
  judge_verdict?: JudgeVerdict | null;

  // compress (Tier 1)
  compact_snapshot?: CompactSnapshot | null;
}

export interface StickyNote {
  id: string;
  title: string;
  text: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface WorkFlow {
  id: NodeId;
  nodes: Record<NodeId, WorkFlowNode>;
  root_ids: NodeId[];
  /** Execution mode stamped at spawn time — survives later changes to
   * the ChatFlow's ``default_execution_mode`` so past ChatNodes keep
   * their original visual identity. */
  execution_mode?: ExecutionMode;
  /** Judge model stamped at submit time from the composer / ChatFlow
   * default. Source of truth for which model actually ran the inner
   * judges (``default_judge_model`` on ChatFlow is only the default
   * for *new* turns and may drift after submit). */
  judge_model_override?: ProviderModelRef | null;
  /** Tool-call follow-up draft model stamped at submit time. */
  tool_call_model_override?: ProviderModelRef | null;
  /**
   * Set by the engine when a judge decides the WorkFlow cannot proceed
   * without user clarification. The ChatFlow layer renders this as the
   * agent's next turn (§3.5).
   */
  pending_user_prompt?: string | null;
  /** Hard cap on plan↔plan_judge / worker↔worker_judge debate
   * rounds before forcing convergence (§3.4.5). */
  debate_round_budget?: number;
  /** Resources / tools / skills judge_pre pre-scoped for this
   * WorkFlow (e.g. ``["web_search", "code_execution"]``). Planner
   * and downstream workers read this to select the relevant slice
   * of their tool pool. Empty = no pre-scope, worker sees its full
   * pool. Plain list of snake_case tokens; not an EditableText
   * because the user rarely hand-edits the tool-slice. */
  capabilities?: string[];
  sticky_notes?: Record<string, StickyNote>;
}

export type PendingTurnSource = "web" | "discord" | "feishu" | "api" | "test";

export interface PendingTurn {
  id: string;
  text: string;
  source: PendingTurnSource;
  on_upstream_failure: "discard" | "continue";
  created_at: string;
  /** Composer's model pick for the turn this PendingTurn represents.
   * Travels with the queued turn so the choice survives the chain walk. */
  spawn_model: ProviderModelRef | null;
}

/**
 * Mirror of ``agentloom.schemas.workflow.CompactSnapshot``.
 * Populated on WorkFlowNodes with ``step_kind="compress"`` (Tier 1)
 * and on ChatFlowNodes that serve as compact points for their
 * subtree (Tier 2). ``summary`` empty means the snapshot is stubbed
 * but the compact worker hasn't finished yet.
 */
export interface CompactSnapshot {
  summary: string;
  preserved_messages: WireMessage[];
  preserved_before_summary: boolean;
}

/**
 * Mirror of ``agentloom.schemas.workflow.PackSnapshot``.
 * Populated on ChatFlowNodes that serve as mid-chain pack points.
 * Unlike compact (implicit root→leaf), ``packed_range`` carries the
 * explicit ChatNode id range the pack covers. ``summary`` empty
 * means the pack worker hasn't finished yet.
 */
export interface PackSnapshot {
  summary: string;
  packed_range: NodeId[];
  use_detailed_index: boolean;
  preserve_last_n: number;
  preserved_messages: WireMessage[];
}

/**
 * Mirror of ``agentloom.schemas.chatflow.InboundContextSegment``.
 *
 * Kinds:
 * - ``summary_preamble`` / ``sticky_restored``: synthetic blocks the
 *   engine constructs; UI renders them in a muted style without node
 *   id / token / brief chrome.
 * - ``preserved``: verbatim tail carried by the compact snapshot.
 * - ``ancestor`` / ``current_turn``: real ChatNode user/assistant pairs.
 */
export type InboundContextSegmentKind =
  | "summary_preamble"
  | "preserved"
  | "ancestor"
  | "pack_summary"
  | "sticky_restored"
  | "current_turn";

/**
 * Mirror of ``agentloom.schemas.chatflow.CbiEntry`` — structured pre-
 * compact ChatBoard-item bullets folded into a ``summary_preamble``
 * segment. The segment's text mirrors these verbatim for LLM
 * consumption; the structured list lets the UI render clickable per-
 * node bullets without re-parsing the marker string.
 */
export interface CbiEntry {
  node_id: string;
  description: string;
}

export interface InboundContextSegment {
  kind: InboundContextSegmentKind;
  messages: WireMessage[];
  source_node_id: string | null;
  synthetic: boolean;
  cbi_entries: CbiEntry[] | null;
}

export interface InboundContextResponse {
  segments: InboundContextSegment[];
}

export interface ChatFlowNode extends NodeBaseFields {
  user_message: EditableText | null;
  agent_response: EditableText;
  workflow: WorkFlow;
  pending_queue: PendingTurn[];
  /** Tier 2 marker — see backend schema. A populated snapshot means
   * this ChatNode is a compact point: ``agent_response.text`` holds
   * the summary prose and downstream context builds root here. */
  compact_snapshot: CompactSnapshot | null;
  /** Mid-chain pack marker. When non-null, this ChatNode is a pack
   * snapshot point over the explicit ``pack_snapshot.packed_range``
   * list; downstream context builds substitute the summary for the
   * range, pre-range ancestors remain visible as usual. Mutually
   * exclusive with ``compact_snapshot``. */
  pack_snapshot?: PackSnapshot | null;
  /** Tokens in this node's chain context at spawn (``_build_chat_context``
   * output + this turn's user message). Stamped once by the engine; the
   * canvas TokenBar reads this for monotonic context-growth display.
   * ``null`` on legacy nodes — UI falls back to the old first/last-worknode
   * heuristic in that case. */
  entry_prompt_tokens: number | null;
  /** Tokens in ``agent_response.text`` — what this turn will contribute
   * to every descendant's chain context. Stamped once when the turn
   * finalises. ``null`` while the turn is still running, on failed
   * turns before a response was written, and on legacy nodes. The
   * canvas adds this to ``entry_prompt_tokens`` so the card shows the
   * next turn's entry size, not this turn's. */
  output_response_tokens: number | null;
}

/** Lightweight summary returned by GET /api/chatflows (list). */
export interface ChatFlowSummary {
  id: string;
  title: string | null;
  description: string | null;
  tags: string[];
  folder_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface Folder {
  id: string;
  parent_id: string | null;
  name: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface ChatFlow {
  id: NodeId;
  title: string | null;
  description: string | null;
  tags: string[];
  nodes: Record<NodeId, ChatFlowNode>;
  root_ids: NodeId[];
  /** Renamed from ``default_model`` in the MemoryBoard PR (2026-04-20).
   * The backend migration rewrote existing rows to use the new key;
   * any payload still carrying ``default_model`` is translated on the
   * Pydantic side. */
  draft_model: ProviderModelRef | null;
  /** Per-call-type overrides of ``draft_model``. Judge calls use
   * ``default_judge_model`` when set; tool-call follow-up drafts use
   * ``default_tool_call_model``. ``null`` means "fall back to the main
   * turn model" — same model as the user's primary draft. */
  default_judge_model: ProviderModelRef | null;
  default_tool_call_model: ProviderModelRef | null;
  /** MemoryBoard brief model pin. When set, BRIEF WorkNodes (the
   * per-node + per-WorkFlow summaries the engine writes into the
   * MemoryBoard) route through this model regardless of the main turn
   * model. ``null`` disables MemoryBoard writing entirely — the
   * engine skips brief auto-spawn so the ChatFlow is zero-cost.
   * Invariant: ``brief_model``'s context_window must be >=
   * ``draft_model``'s. */
  brief_model: ProviderModelRef | null;
  default_execution_mode: ExecutionMode;
  /** User-editable anti-hallucination guidance prepended to every
   * tool-bearing LLM call. ``null`` = use the workspace-language
   * default (zh/en); ``""`` = explicit opt-out of static framing.
   * Engine still appends a dynamic OS / shell / cwd hint either way.
   * Optional in TS so legacy chatflow fixtures pre-2026-04-25 can omit
   * the field; production payloads always include it. */
  runtime_environment_note?: string | null;
  /** Hard cap on judge_post retry rounds. ``-1`` means unlimited. */
  judge_retry_budget: number;
  /**
   * Planner-grounding fuse. Halts this ChatFlow (each recursive
   * delegate level independently) when the fraction of
   * completed ``tool_call`` leaves drops below ``min_ground_ratio``
   * after ``ground_ratio_grace_nodes`` leaves have accumulated. ``null``
   * disables the check. Default 5% / 20. See backend §5.4.
   */
  min_ground_ratio: number | null;
  ground_ratio_grace_nodes: number;
  /**
   * Per-ChatFlow tool denylist — tool names hidden from every LLM call
   * and refused at execute-time. Covers both built-ins (``Bash``,
   * ``Read``, ...) and MCP tools (``mcp__<server>__<tool>``). Empty =
   * no per-chatflow filter on top of the workspace defaults.
   */
  disabled_tool_names: string[];
  /**
   * Tier 1 pre-draft auto-compact threshold: when the pending
   * message-list footprint crosses this fraction of the target
   * model's context window, the engine inserts a compact WorkNode
   * before the call. ``null`` disables Tier 1 entirely. Default 0.7.
   */
  compact_trigger_pct: number | null;
  /**
   * Target footprint for Tier 1 summaries, as a fraction of the
   * target model's context window. Default 0.5.
   */
  compact_target_pct: number;
  /**
   * Trailing messages kept verbatim on the downstream side of a
   * compact. Smaller = more aggressive; larger = more fidelity. Only
   * consulted when ``compact_preserve_mode === "by_count"``.
   */
  compact_keep_recent_count: number;
  /**
   * Strategy for deciding the verbatim tail on a compact.
   * ``by_count`` keeps the last N messages (and lets the summary run
   * uncapped); ``by_budget`` greedy-packs the tail into whatever
   * is left of ``target_pct × context_window`` after the summary's
   * tokens are subtracted. Applies to both tiers. Default
   * ``by_count``.
   */
  compact_preserve_mode: "by_count" | "by_budget";
  /**
   * Counter-init for sticky-restore: how many turns a recalled
   * ChatNode/WorkNode stays in context after ``get_node_context``
   * pulls it back. Decremented by 1 each turn that doesn't re-touch
   * the entry; at 0 it drops out and falls back to the MemoryBoard-
   * reference form. Independent of ``compact_preserve_mode``.
   * Default 3.
   */
  recalled_context_sticky_turns: number;
  /** Optional pin for the compact worker itself. ``null`` = inherit. */
  compact_model: ProviderModelRef | null;
  /** Whether explicit UI-driven compacts open a confirmation dialog. */
  compact_require_confirmation: boolean;
  /**
   * ChatFlow-layer auto-compact trigger (dual-track — runs in addition
   * to the WorkFlow Tier 1 trigger above). Evaluated at ChatNode spawn:
   * when the prospective chain context crosses this fraction of the
   * turn model's context window, the engine inserts a compact ChatNode
   * before the new turn. ``null`` disables the layer. Default 0.6.
   */
  chatnode_compact_trigger_pct: number | null;
  /**
   * Target footprint for ChatNode-level compact summaries, as a fraction
   * of the turn model's context window. Default 0.4.
   */
  chatnode_compact_target_pct: number;
  /** Hard ceiling on ``produced_tags`` that ``chat_brief`` /
   * ``node_brief`` may emit per BoardItem. Concept anchors beyond this
   * are dropped. Default 10. */
  max_produced_tags: number;
  /** Hard ceiling on ``consumed_tags`` per BoardItem. Default 8. */
  max_consumed_tags: number;
  sticky_notes?: Record<string, StickyNote>;
  created_at: string;
}

/**
 * SSE event shape emitted by the backend EventBus. Mirrors
 * `agentloom.engine.events.WorkflowEvent`.
 */
export interface WorkFlowEvent {
  kind: string;
  workflow_id: string;
  node_id: string | null;
  data: Record<string, unknown>;
  at: string;
}

/** One row from the ``board_items`` table — MemoryBoardItem in the
 * PR-1 design doc. The frontend caches these per-ChatFlow and filters
 * by ``scope`` / ``source_node_id`` client-side when rendering the
 * floating node-brief bubble and the flow-brief top banner. */
export interface BoardItem {
  id: string;
  chatflow_id: string;
  workflow_id: string | null;
  source_node_id: string;
  source_kind: string;
  scope: "chat" | "node" | "flow";
  description: string;
  fallback: boolean;
  created_at: string | null;
  /** Drill-down ChatNode ids this item folds over. Populated for pack
   * (``packed_range``), merge (parent ids), and compact (single-hop
   * upstream parent — drill recurses through the next layer's own
   * ``inner_chat_ids``). Empty on plain turn rows. */
  inner_chat_ids?: NodeId[];
  /** Drill-down WorkNode ids inside this ChatNode's WorkFlow that
   * carry their own WorkBoardItem. Lets a downstream agent (including
   * one in a different ChatNode's WorkFlow) pull a specific WorkNode's
   * content via ``get_node_context`` without re-reading the full chain. */
  work_node_ids?: NodeId[];
  /** Concept anchors this brief introduces (e.g. ``plan_x``,
   * ``plan_x_rejected``). Used by ``memoryboard_lookup`` for tag-based
   * retrieval in addition to free-text search. */
  produced_tags?: string[];
  /** Concept anchors this brief touches without minting (downstream
   * references to upstream produced_tags). */
  consumed_tags?: string[];
}
