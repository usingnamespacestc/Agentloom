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
  "succeeded",
  "failed",
  "retrying",
  "cancelled",
] as const;

export type NodeStatus = (typeof NODE_STATUSES)[number];

export const STEP_KINDS = [
  "llm_call",
  "tool_call",
  "sub_agent_delegation",
] as const;

export type StepKind = (typeof STEP_KINDS)[number];

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
  expected_outcome: EditableText | null;
  status: NodeStatus;
  model_override: ProviderModelRef | null;
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
  tool_constraints: ToolConstraints | null;

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
}

export interface WorkFlow {
  id: NodeId;
  nodes: Record<NodeId, WorkFlowNode>;
  root_ids: NodeId[];
}

export type PendingTurnSource = "web" | "discord" | "feishu" | "api" | "test";

export interface PendingTurn {
  id: string;
  text: string;
  source: PendingTurnSource;
  on_upstream_failure: "discard" | "continue";
  created_at: string;
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
  default_chat_model: ProviderModelRef | null;
  default_work_model: ProviderModelRef | null;
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
