/**
 * Thin fetch wrapper for the Agentloom REST surface.
 *
 * Vite proxies `/api/*` to `http://localhost:8000` in dev (see
 * `vite.config.ts`) so callers can use relative URLs without caring
 * about CORS. Errors are converted to `ApiError` so the UI can tell
 * HTTP failures apart from network failures.
 */

import type { ChatFlow, ChatFlowSummary, Folder, PendingTurn, PendingTurnSource } from "@/types/schema";

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

export const api = {
  listChatFlows: () => request<ChatFlowSummary[]>("/api/chatflows"),

  createChatFlow: (title?: string) =>
    request<CreateChatFlowResponse>("/api/chatflows", {
      method: "POST",
      body: JSON.stringify({ title: title ?? null }),
    }),

  getChatFlow: (id: string) => request<ChatFlow>(`/api/chatflows/${id}`),

  patchChatFlow: (id: string, patch: { title?: string | null; description?: string | null; tags?: string[] }) =>
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

  submitTurn: (id: string, text: string, parentId?: string) =>
    request<SubmitTurnResponse>(`/api/chatflows/${id}/turns`, {
      method: "POST",
      body: JSON.stringify({
        text,
        parent_id: parentId ?? null,
      }),
    }),

  enqueueTurn: (chatflowId: string, nodeId: string, text: string, source: PendingTurnSource = "web") =>
    request<PendingTurn>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/queue`, {
      method: "POST",
      body: JSON.stringify({ text, source }),
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

  retryNode: (chatflowId: string, nodeId: string) =>
    request<{ node_id: string }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/retry`, {
      method: "POST",
    }),

  cancelNode: (chatflowId: string, nodeId: string) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/nodes/${nodeId}/cancel`, {
      method: "POST",
    }),

  patchPositions: (chatflowId: string, positions: { id: string; x: number; y: number }[]) =>
    request<{ ok: boolean }>(`/api/chatflows/${chatflowId}/positions`, {
      method: "PATCH",
      body: JSON.stringify({ positions }),
    }),

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

  /** SSE URL for a chatflow — pass to `new EventSource(...)`. */
  eventsUrl: (id: string) => `/api/chatflows/${id}/events`,
};

// ---- provider types ----

export interface ModelInfoDTO {
  id: string;
  context_window: number | null;
  max_output_tokens: number | null;
  supports_tools: boolean;
  supports_streaming: boolean;
  pinned: boolean;
}

export interface ProviderSummary {
  id: string;
  friendly_name: string;
  provider_kind: string;
  base_url: string;
  available_models: ModelInfoDTO[];
  api_key_source: string;
  api_key_env_var: string | null;
  rate_limit_bucket: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProviderDetail extends ProviderSummary {
  extra_headers: Record<string, string>;
}

export interface CreateProviderBody {
  friendly_name: string;
  provider_kind: string;
  base_url: string;
  api_key_source?: string;
  api_key_env_var?: string | null;
  api_key_inline?: string | null;
  available_models?: ModelInfoDTO[];
  rate_limit_bucket?: string | null;
}
