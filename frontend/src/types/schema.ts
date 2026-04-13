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
  "llm_call",
  "tool_call",
  "judge_call",
  "sub_agent_delegation",
] as const;

export type StepKind = (typeof STEP_KINDS)[number];

export type JudgeVariant = "pre" | "during" | "post";

/**
 * Structural role in the recursive planner model (§3.4.4 / ADR-024).
 * Orthogonal to ``StepKind``. ``null`` for direct-mode and legacy nodes.
 */
export const WORK_NODE_ROLES = [
  "pre_judge",
  "planner",
  "planner_judge",
  "worker",
  "worker_judge",
  "post_judge",
] as const;

export type WorkNodeRole = (typeof WORK_NODE_ROLES)[number];

export const EXECUTION_MODES = ["direct", "semi_auto", "auto"] as const;
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
  tool_constraints: ToolConstraints | null;
  /** Pin for this WorkNode's LLM call. Set by the engine at spawn time
   * from the enclosing ChatNode's resolved_model and propagated across
   * retries. Not user-facing. */
  model_override: ProviderModelRef | null;

  // llm_call
  input_messages?: WireMessage[] | null;
  output_message?: WireMessage | null;
  usage?: TokenUsage | null;

  // tool_call
  source_tool_use_id?: string | null;
  tool_name?: string | null;
  tool_args?: Record<string, unknown> | null;
  tool_result?: ToolResult | null;

  // sub_agent_delegation
  sub_workflow?: WorkFlow | null;

  // judge_call (ADR-018)
  judge_variant?: JudgeVariant | null;
  judge_target_id?: NodeId | null;
  judge_verdict?: JudgeVerdict | null;
}

export interface WorkFlow {
  id: NodeId;
  nodes: Record<NodeId, WorkFlowNode>;
  root_ids: NodeId[];
  /**
   * Set by the engine when a judge decides the WorkFlow cannot proceed
   * without user clarification. The ChatFlow layer renders this as the
   * agent's next turn (§3.5).
   */
  pending_user_prompt?: string | null;
  /** Hard cap on planner↔planner_judge / worker↔worker_judge debate
   * rounds before forcing convergence (§3.4.5). */
  debate_round_budget?: number;
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

export interface ChatFlowNode extends NodeBaseFields {
  user_message: EditableText | null;
  agent_response: EditableText;
  workflow: WorkFlow;
  pending_queue: PendingTurn[];
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
  default_model: ProviderModelRef | null;
  default_execution_mode: ExecutionMode;
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
