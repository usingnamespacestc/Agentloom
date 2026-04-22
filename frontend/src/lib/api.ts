/**
 * Thin fetch wrapper for the Agentloom REST surface.
 *
 * Vite proxies `/api/*` to `http://localhost:8000` in dev (see
 * `vite.config.ts`) so callers can use relative URLs without caring
 * about CORS. Errors are converted to `ApiError` so the UI can tell
 * HTTP failures apart from network failures.
 */

import type { BoardItem, ChatFlow, ChatFlowSummary, ExecutionMode, Folder, InboundContextResponse, PendingTurn, PendingTurnSource, ProviderModelRef, StickyNote } from "@/types/schema";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly url: string,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(
  url: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new ApiError(
      response.status,
      url,
      `${response.status} ${response.statusText}: ${body}`,
    );
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export interface CreateChatFlowResponse {
  id: string;
}

export interface SubmitTurnResponse {
  node_id: string;
  status: string;
  agent_response: string;
}

/** Drop any ProviderModelRef that doesn't carry both provider_id and
 * model_id as non-empty strings. A partial ref would serialize to a body
 * missing a required field and 422 the backend (Pydantic requires both).
 * Centralized here so every turn/retry/queue endpoint is covered. */
function sanitizeRef(ref: ProviderModelRef | null | undefined): ProviderModelRef | null {
  if (!ref) return null;
  if (typeof ref.provider_id !== "string" || !ref.provider_id) return null;
  if (typeof ref.model_id !== "string" || !ref.model_id) return null;
  return { provider_id: ref.provider_id, model_id: ref.model_id };
}

export const api = {
  listChatFlows: () => request<ChatFlowSummary[]>("/api/chatflows"),

  createChatFlow: (title?: string) =>
    request<CreateChatFlowResponse>("/api/chatflows", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null }),
    }),

  getChatFlow: (id: string) => request<ChatFlow>(`/api/chatflows/${id}`),

  /** List every MemoryBoardItem attached to a ChatFlow. The frontend
   * filters the flat list by ``scope`` / ``source_node_id`` client-side
   * when rendering the node-brief bubble and flow-brief banner. */
  listBoardItems: (id: string) =>
    request<{ items: BoardItem[] }>(`/api/chatflows/${id}/board_items`),

  patchChatFlow: (
    id: string,
    patch: {
      title?: string | null;
      description?: string | null;
      tags?: string[];
      draft_model?: ProviderModelRef | null;
      default_judge_model?: ProviderModelRef | null;
      default_tool_call_model?: ProviderModelRef | null;
      brief_model?: ProviderModelRef | null;
      default_execution_mode?: ExecutionMode;
      judge_retry_budget?: number;
      min_ground_ratio?: number | null;
      ground_ratio_grace_nodes?: number;
      disabled_tool_names?: string[];
      compact_trigger_pct?: number | null;
      compact_target_pct?: number;
      compact_keep_recent_count?: number;
      compact_preserve_mode?: "by_count" | "by_budget";
      recalled_context_sticky_turns?: number;
      compact_model?: ProviderModelRef | null;
      compact_require_confirmation?: boolean;
      chatnode_compact_trigger_pct?: number | null;
      chatnode_compact_target_pct?: number;
    },
  ) =>
    request<{ ok: boolean }>(`/api/chatflows/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  deleteChatFlow: (id: string) =>
    request<{ ok: boolean }>(`/api/chatflows/${id}`, { method: "DELETE" }),

  moveChatFlowToFolder: (chatflowId: string, folderId: string | null) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/folder`, {
      method: "PATCH",
      body: JSON.stringify({ folder_id: folderId }),
    }),

  submitTurn: (
    id: string,
    text: string,
    parentId?: string,
    spawnModel?: ProviderModelRef | null,
    judgeSpawnModel?: ProviderModelRef | null,
    toolCallSpawnModel?: ProviderModelRef | null,
  ) =>
    request<SubmitTurnResponse>(`/api/chatflows/${id}/turns`, {
      method: "POST",
      body: JSON.stringify({
        text,
        parent_id: parentId ?? null,
        spawn_model: sanitizeRef(spawnModel),
        judge_spawn_model: sanitizeRef(judgeSpawnModel),
        tool_call_spawn_model: sanitizeRef(toolCallSpawnModel),
      }),
    }),

  enqueueTurn: (
    chatflowId: string,
    nodeId: string,
    text: string,
    source: PendingTurnSource = "web",
    spawnModel?: ProviderModelRef | null,
    judgeSpawnModel?: ProviderModelRef | null,
    toolCallSpawnModel?: ProviderModelRef | null,
  ) =>
    request<PendingTurn>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/queue`, {
      method: "POST",
      body: JSON.stringify({
        text,
        source,
        spawn_model: sanitizeRef(spawnModel),
        judge_spawn_model: sanitizeRef(judgeSpawnModel),
        tool_call_spawn_model: sanitizeRef(toolCallSpawnModel),
      }),
    }),

  patchQueueItem: (chatflowId: string, nodeId: string, itemId: string, text: string) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/queue/${itemId}`, {
      method: "PATCH",
      body: JSON.stringify({ text }),
    }),

  deleteQueueItem: (chatflowId: string, nodeId: string, itemId: string) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/queue/${itemId}`, {
      method: "DELETE",
    }),

  reorderQueue: (chatflowId: string, nodeId: string, itemIds: string[]) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/queue/reorder`, {
      method: "POST",
      body: JSON.stringify({ item_ids: itemIds }),
    }),

  deleteNode: (chatflowId: string, nodeId: string) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}`, {
      method: "DELETE",
    }),

  retryNode: (
    chatflowId: string,
    nodeId: string,
    spawnModel?: ProviderModelRef | null,
    judgeSpawnModel?: ProviderModelRef | null,
    toolCallSpawnModel?: ProviderModelRef | null,
  ) =>
    request<{ node_id: string }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/retry`, {
      method: "POST",
      body: JSON.stringify({
        spawn_model: sanitizeRef(spawnModel),
        judge_spawn_model: sanitizeRef(judgeSpawnModel),
        tool_call_spawn_model: sanitizeRef(toolCallSpawnModel),
      }),
    }),

  cancelNode: (chatflowId: string, nodeId: string) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/cancel`, {
      method: "POST",
    }),

  /** Segmented inbound-context preview for a ChatNode. Backs the
   * right-pane conversation display when a compact ancestor or sticky
   * recall changes what the next llm_call would actually consume
   * versus a naive walk up ``parent_ids``. */
  getInboundContext: (chatflowId: string, nodeId: string) =>
    request<InboundContextResponse>(
      `/api/chatflows/${chatflowId}/nodes/${nodeId}/inbound_context`,
    ),

  /** Tier 2 manual compact. ``nodeId`` is the parent the compact
   * should hang off; the engine walks the chain up-to-and-including
   * that node and produces a new compact ChatNode as its child. */
  compactChain: (
    chatflowId: string,
    nodeId: string,
    body: {
      compact_instruction?: string | null;
      must_keep?: string;
      must_drop?: string;
      preserve_recent_turns?: number | null;
      target_tokens?: number | null;
      model?: ProviderModelRef | null;
    },
  ) =>
    request<{ node_id: string; status: string }>(
      `/api/chatflows/${chatflowId}/nodes/${nodeId}/compact`,
      {
        method: "POST",
        body: JSON.stringify({
          compact_instruction: body.compact_instruction ?? null,
          must_keep: body.must_keep ?? "",
          must_drop: body.must_drop ?? "",
          preserve_recent_turns: body.preserve_recent_turns ?? null,
          target_tokens: body.target_tokens ?? null,
          model: sanitizeRef(body.model ?? null),
        }),
      },
    ),

  /** Manual branch merge. Folds two ChatNode branches into a single
   * synthesized reply; the new node's parent_ids are [left_id, right_id]. */
  mergeChain: (
    chatflowId: string,
    body: {
      left_id: string;
      right_id: string;
      merge_instruction?: string | null;
      model?: ProviderModelRef | null;
    },
  ) =>
    request<{ node_id: string; status: string }>(
      `/api/chatflows/${chatflowId}/merge`,
      {
        method: "POST",
        body: JSON.stringify({
          left_id: body.left_id,
          right_id: body.right_id,
          merge_instruction: body.merge_instruction ?? null,
          model: sanitizeRef(body.model ?? null),
        }),
      },
    ),

  patchPositions: (chatflowId: string, positions: { id: string; x: number; y: number }[]) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/positions`, {
      method: "PATCH",
      body: JSON.stringify({ positions }),
    }),

  patchWorkflowPositions: (
    chatflowId: string,
    chatNodeId: string,
    positions: { id: string; x: number; y: number }[],
  ) =>
    request<{ ok: boolean }>(
      `/api/chatflows/${chatflowId}/nodes/${chatNodeId}/workflow/positions`,
      {
        method: "PATCH",
        body: JSON.stringify({ positions }),
      },
    ),

  putStickyNotes: (chatflowId: string, notes: Record<string, StickyNote>) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/sticky-notes`, {
      method: "PUT",
      body: JSON.stringify({ notes }),
    }),

  putWorkflowStickyNotes: (
    chatflowId: string,
    chatNodeId: string,
    notes: Record<string, StickyNote>,
    subPath: string[] = [],
  ) =>
    request<{ ok: boolean }>(
      `/api/chatflows/${chatflowId}/nodes/${chatNodeId}/workflow/sticky-notes`,
      { method: "PUT", body: JSON.stringify({ notes, sub_path: subPath }) },
    ),

  // ---- folders ----
  listFolders: () => request<Folder[]>("/api/folders"),

  createFolder: (name: string, parentId?: string | null) =>
    request<{ id: string; name: string }>("/api/folders", {
      method: "POST",
      body: JSON.stringify({ name, parent_id: parentId ?? null }),
    }),

  renameFolder: (id: string, name: string) =>
    request<{ ok: boolean }>(`/api/folders/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),

  moveFolder: (id: string, parentId: string | null) =>
    request<{ ok: boolean }>(`/api/folders/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ parent_id: parentId }),
    }),

  deleteFolder: (id: string) =>
    request<{ ok: boolean; deleted_chatflows: string[] }>(`/api/folders/${id}`, {
      method: "DELETE",
    }),

  // ---- providers ----
  listProviders: () => request<ProviderSummary[]>("/api/providers"),

  createProvider: (body: CreateProviderBody) =>
    request<{ id: string; friendly_name: string }>("/api/providers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getProvider: (id: string) => request<ProviderDetail>(`/api/providers/${id}`),

  patchProvider: (id: string, patch: Partial<CreateProviderBody>) =>
    request<{ ok: boolean }>(`/api/providers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  deleteProvider: (id: string) =>
    request<{ ok: boolean }>(`/api/providers/${id}`, { method: "DELETE" }),

  testProvider: (id: string) =>
    request<{ ok: boolean; models?: string[]; error?: string }>(
      `/api/providers/${id}/test`,
      { method: "POST", body: JSON.stringify({}) },
    ),

  discoverModels: (id: string) =>
    request<{ models: ModelInfoDTO[] }>(`/api/providers/${id}/models`, {
      method: "POST",
    }),

  // ---- mcp servers ----
  listMCPServers: () => request<MCPServerState[]>("/api/mcp-servers"),

  createMCPServer: (body: CreateMCPServerBody) =>
    request<MCPServerState>("/api/mcp-servers", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  patchMCPServer: (id: string, patch: PatchMCPServerBody) =>
    request<MCPServerState>(`/api/mcp-servers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  deleteMCPServer: (id: string) =>
    request<{ ok: boolean }>(`/api/mcp-servers/${id}`, { method: "DELETE" }),

  reconnectMCPServer: (id: string) =>
    request<MCPServerState>(`/api/mcp-servers/${id}/reconnect`, {
      method: "POST",
    }),

  // ---- tools ----
  listTools: () => request<ToolDTO[]>("/api/tools"),

  // ---- workspace settings ----
  getWorkspaceSettings: () =>
    request<WorkspaceSettingsDTO>("/api/workspace/settings"),

  patchWorkspaceSettings: (patch: {
    tool_states?: Record<string, ToolState>;
    language?: WorkspaceLanguage;
  }) =>
    request<WorkspaceSettingsDTO>("/api/workspace/settings", {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),

  /** SSE URL for a chatflow — pass to `new EventSource(...)`. */
  eventsUrl: (id: string) => `/api/chatflows/${id}/events`,
};

// ---- tool + workspace-settings types ----

export interface ToolDTO {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
}

export type ToolState = "default_allow" | "available" | "disabled";

export type WorkspaceLanguage = "en-US" | "zh-CN";

export interface WorkspaceSettingsDTO {
  tool_states: Record<string, ToolState>;
  language: WorkspaceLanguage;
}

// ---- mcp types ----

export type MCPServerKind = "http" | "stdio";

export interface MCPServerState {
  id: string;
  server_id: string;
  friendly_name: string;
  kind: MCPServerKind;
  enabled: boolean;
  url: string | null;
  command: string | null;
  is_connected: boolean;
  tool_count: number;
  tool_names: string[];
  last_error: string | null;
}

export interface CreateMCPServerBody {
  server_id: string;
  friendly_name: string;
  kind: MCPServerKind;
  url?: string | null;
  headers?: Record<string, string>;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
  enabled?: boolean;
}

export interface PatchMCPServerBody {
  friendly_name?: string;
  enabled?: boolean;
  url?: string | null;
  headers?: Record<string, string>;
  command?: string | null;
  args?: string[];
  env?: Record<string, string>;
}

// ---- provider types ----

export type JsonMode = "schema" | "object" | "none";

export type ProviderSubKind =
  | "openai_chat"
  | "ollama"
  | "volcengine"
  | "llamacpp"
  | "anthropic";

/** Mirror of agentloom.schemas.provider.SUB_KIND_PARAM_WHITELIST. */
export const SUB_KIND_PARAM_WHITELIST: Record<ProviderSubKind, ReadonlySet<keyof ModelInfoDTO>> = {
  openai_chat: new Set([
    "temperature",
    "top_p",
    "max_output_tokens",
    "presence_penalty",
    "frequency_penalty",
  ]),
  ollama: new Set([
    "temperature",
    "top_p",
    "top_k",
    "max_output_tokens",
    "repetition_penalty",
    "num_ctx",
  ]),
  volcengine: new Set([
    "temperature",
    "top_p",
    "max_output_tokens",
    "presence_penalty",
    "frequency_penalty",
    "thinking_enabled",
  ]),
  llamacpp: new Set([
    "temperature",
    "top_p",
    "top_k",
    "max_output_tokens",
    "repetition_penalty",
  ]),
  anthropic: new Set([
    "temperature",
    "top_p",
    "top_k",
    "max_output_tokens",
    "thinking_budget_tokens",
  ]),
};

export interface ModelInfoDTO {
  id: string;
  context_window: number | null;
  max_output_tokens: number | null;
  supports_tools: boolean;
  supports_streaming: boolean;
  pinned: boolean;
  json_mode?: JsonMode | null;
  temperature?: number | null;
  top_p?: number | null;
  top_k?: number | null;
  presence_penalty?: number | null;
  frequency_penalty?: number | null;
  repetition_penalty?: number | null;
  num_ctx?: number | null;
  thinking_budget_tokens?: number | null;
  thinking_enabled?: boolean | null;
}

export interface ProviderSummary {
  id: string;
  friendly_name: string;
  provider_kind: string;
  provider_sub_kind?: ProviderSubKind | null;
  base_url: string;
  available_models: ModelInfoDTO[];
  api_key_source: string;
  api_key_env_var: string | null;
  rate_limit_bucket: string | null;
  json_mode?: JsonMode;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProviderDetail extends ProviderSummary {
  extra_headers: Record<string, string>;
}

export interface CreateProviderBody {
  friendly_name: string;
  provider_kind: string;
  provider_sub_kind?: ProviderSubKind | null;
  base_url: string;
  api_key_source?: string;
  api_key_env_var?: string | null;
  api_key_inline?: string | null;
  available_models?: ModelInfoDTO[];
  rate_limit_bucket?: string | null;
  json_mode?: JsonMode;
}
