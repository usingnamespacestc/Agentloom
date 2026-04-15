/**
 * Single source of truth for the canvas UI.
 *
 * M8.5 round 2:
 * - The path shown in the Conversation view terminates strictly at the
 *   selected node. There's no "extend past selected to latest leaf"
 *   fallback anymore.
 * - Branch memory is keyed by branch-root id (= a node that has
 *   siblings), not by fork id. Each key stores the endpoint the user
 *   last landed on while that branch was active. When the user later
 *   switches back to that branch, we restore that precise endpoint.
 * - ``selectNode`` walks ancestors of the new endpoint and refreshes
 *   branch memory entries along the way. ``pickBranch(forkId, childId)``
 *   looks up the remembered endpoint for ``childId`` (if any) and
 *   selects it; otherwise it just selects the child itself.
 *
 * The same shape applies to the drill-down WorkFlow via a parallel set
 * of fields (``workflowBranchMemory``, ``selectWorkflowNode``,
 * ``pickWorkflowBranch``).
 *
 * ADR-013 note: the store never *reorders* nodes. It only replaces the
 * whole ChatFlow payload on reload or status-patches individual nodes
 * on SSE events. The layout and path layers are the only sorters.
 */

import { create } from "zustand";

import { findLatestLeafId } from "@/canvas/pathUtils";
import { api, ApiError } from "@/lib/api";
import { subscribeEvents, type SSEFactory, type SSESubscription } from "@/lib/sse";
import type { ComposerModelMap } from "@/store/preferencesStore";
import type {
  ChatFlow,
  ChatFlowNode,
  ChatFlowSummary,
  ExecutionMode,
  Folder,
  NodeId,
  NodeStatus,
  PendingTurn,
  ProviderModelRef,
  WorkFlowEvent,
  WorkFlowNode,
} from "@/types/schema";

export type LoadState = "idle" | "loading" | "ready" | "error";
export type ViewMode = "chatflow" | "workflow";

/**
 * One level of nested-WorkFlow drill-in (§3.4.3 nesting).
 *
 *   - ``chatnode``   — the outer WorkFlow attached to a ChatFlowNode.
 *                      Always the first frame on the stack.
 *   - ``subworkflow`` — a ``sub_workflow`` attached to a
 *                      ``sub_agent_delegation`` WorkNode in the PREVIOUS
 *                      frame's WorkFlow. Always frame[≥1].
 */
export type DrillFrame =
  | { kind: "chatnode"; chatNodeId: NodeId }
  | { kind: "subworkflow"; parentWorkNodeId: NodeId };

export const RIGHT_PANEL_MIN = 320;
export const RIGHT_PANEL_MAX = 900;
const RIGHT_PANEL_DEFAULT = 440;

export interface ChatFlowStoreState {
  /** Sidebar: list of chatflow summaries. */
  chatflowList: ChatFlowSummary[];
  /** Sidebar: list of folders. */
  folderList: Folder[];
  /** Whether the sidebar list is loading. */
  listLoading: boolean;
  /** Whether the sidebar is collapsed. */
  sidebarCollapsed: boolean;

  chatflow: ChatFlow | null;
  loadState: LoadState;
  errorMessage: string | null;

  /** IDs of optimistic nodes not yet confirmed by the server. */
  _optimisticIds: Set<string>;

  /** Which node is selected in the ChatFlow canvas. */
  selectedNodeId: NodeId | null;
  /**
   * Branch memory for the ChatFlow: branch-root id → remembered
   * endpoint id. "Branch root" = a node whose parent has >1 children.
   */
  branchMemory: Record<NodeId, NodeId>;

  /**
   * Stack of drill-in frames (§3.4.3). Empty = chatflow view; first
   * frame is always a ChatNode entry; later frames are nested
   * sub-WorkFlows reached through ``sub_agent_delegation`` WorkNodes.
   */
  drillStack: DrillFrame[];
  /** Selection inside the *top* drill frame's WorkFlow. */
  workflowSelectedNodeId: NodeId | null;
  /** Branch memory for the top drill frame's WorkFlow. Reset on push/pop. */
  workflowBranchMemory: Record<NodeId, NodeId>;

  /** Convenience for legacy callers: the original ChatNode the user
   * drilled into (= ``drillStack[0].chatNodeId``), or null in chatflow
   * view. Computed from ``drillStack`` so updates are atomic. */
  drillDownChatNodeId: NodeId | null;
  /** ``"chatflow"`` when ``drillStack`` is empty, ``"workflow"`` otherwise.
   * Computed from ``drillStack``. */
  viewMode: ViewMode;

  /** ConversationView width in px — user-draggable. */
  rightPanelWidth: number;

  sseSubscription: SSESubscription | null;
  sseFactory: SSEFactory | null;

  /** Fetch the chatflow list and folders for the sidebar. */
  fetchChatFlowList: () => Promise<void>;
  /** Create a new chatflow and switch to it. */
  createChatFlow: (title?: string) => Promise<void>;
  /** Delete a chatflow entirely. */
  deleteChatFlow: (id: string) => Promise<void>;
  /** Toggle sidebar collapsed state. */
  toggleSidebar: () => void;
  /** Create a new folder (optionally nested under parentId). */
  createFolder: (name: string, parentId?: string | null) => Promise<void>;
  /** Rename a folder. */
  renameFolder: (id: string, name: string) => Promise<void>;
  /** Delete a folder and all chatflows inside it. */
  deleteFolder: (id: string) => Promise<void>;
  /** Move a folder under another folder (null = root). */
  moveFolder: (folderId: string, parentId: string | null) => Promise<void>;
  /** Move a chatflow into a folder (null = unfiled). */
  moveChatFlowToFolder: (chatflowId: string, folderId: string | null) => Promise<void>;
  /** Update title / description / tags / default model on the current chatflow. */
  patchChatFlow: (patch: {
    title?: string | null;
    description?: string | null;
    tags?: string[];
    default_model?: ProviderModelRef | null;
    default_judge_model?: ProviderModelRef | null;
    default_tool_call_model?: ProviderModelRef | null;
    default_execution_mode?: ExecutionMode;
    judge_retry_budget?: number;
    disabled_tool_names?: string[];
  }) => Promise<void>;

  /** Which edge is currently hovered on the ChatFlow canvas — drives
   * the model-family ribbon highlight (§4.10 rework: model lives on
   * the parent→child edge, so hover targets edges, not node badges).
   * Stored as the full {parent, child} pair because merge nodes have
   * multiple incoming edges that may carry different per-edge models. */
  hoveredEdge: { parent: NodeId; child: NodeId } | null;
  setHoveredEdge: (edge: { parent: NodeId; child: NodeId } | null) => void;

  /** Load a chatflow from the server and subscribe to its events. */
  loadChatFlow: (id: string) => Promise<void>;
  /** Manually inject a chatflow (used by tests and by a create handler
   * that can seed one without a second round-trip). */
  setChatFlow: (chat: ChatFlow | null) => void;
  /**
   * Select a node on the ChatFlow canvas. ``null`` clears. Walks
   * ancestors of the new endpoint and refreshes ``branchMemory``
   * entries for every branch-root ancestor (so later switching back
   * to that branch restores this endpoint).
   */
  selectNode: (nodeId: NodeId | null) => void;
  /**
   * Switch to a different child at a fork. Looks up the remembered
   * endpoint for ``childId`` (if one exists) and selects it; otherwise
   * selects the child itself. ``forkId`` is unused today but kept in
   * the signature to keep the caller code expressive.
   */
  pickBranch: (forkId: NodeId, childId: NodeId) => void;

  /** Enter the outer workflow drill-down view for a specific ChatNode.
   * Resets the stack to a single ``chatnode`` frame. */
  enterWorkflow: (chatNodeId: NodeId) => void;
  /** Push a nested ``subworkflow`` frame for ``parentWorkNodeId`` (a
   * ``sub_agent_delegation`` WorkNode in the current top frame). The
   * caller is responsible for verifying that node has a ``sub_workflow``. */
  enterSubWorkflow: (parentWorkNodeId: NodeId) => void;
  /** Pop the topmost drill frame. If the stack becomes empty, returns
   * to the ChatFlow view. */
  popDrill: () => void;
  /** Truncate the stack to ``length`` frames (used by breadcrumb clicks).
   * ``length === 0`` returns to the ChatFlow view. */
  truncateDrillStack: (length: number) => void;
  /** Leave the workflow drill-down entirely and return to ChatFlow canvas. */
  exitWorkflow: () => void;
  /** Select a node inside the currently drilled-down WorkFlow. */
  selectWorkflowNode: (nodeId: NodeId | null) => void;
  /** Switch to a different child at a WorkFlow fork (branch memory aware). */
  pickWorkflowBranch: (forkId: NodeId, childId: NodeId) => void;

  /** Update the right panel width (clamped to [MIN, MAX]). */
  setRightPanelWidth: (width: number) => void;

  /** Send a user turn (enqueue or immediate submit depending on state). */
  sendTurn: (
    text: string,
    parentId?: string,
    composerModels?: ComposerModelMap | null,
  ) => Promise<void>;
  /** Enqueue a pending turn on a specific node. */
  enqueueTurn: (
    nodeId: NodeId,
    text: string,
    composerModels?: ComposerModelMap | null,
  ) => Promise<void>;
  /** Delete a pending queue item. */
  deleteQueueItem: (nodeId: NodeId, itemId: string) => Promise<void>;
  /** Delete a FAILED node. */
  deleteNode: (nodeId: NodeId) => Promise<void>;
  /** Retry a FAILED node. */
  retryNode: (nodeId: NodeId) => Promise<void>;
  /** Cancel a RUNNING node. */
  cancelNode: (nodeId: NodeId) => Promise<void>;
  /** Re-fetch the current chatflow from the server. */
  refreshChatFlow: () => Promise<void>;

  /** Apply a single SSE event to the current chatflow payload. */
  applyEvent: (event: WorkFlowEvent) => void;
  /** Override the SSE factory (for tests). */
  setSSEFactory: (factory: SSEFactory | null) => void;
  /** Close any live subscription and reset state. */
  reset: () => void;
}

const INITIAL: Omit<
  ChatFlowStoreState,
  | "fetchChatFlowList"
  | "createChatFlow"
  | "deleteChatFlow"
  | "toggleSidebar"
  | "createFolder"
  | "renameFolder"
  | "deleteFolder"
  | "moveFolder"
  | "moveChatFlowToFolder"
  | "patchChatFlow"
  | "setHoveredEdge"
  | "loadChatFlow"
  | "setChatFlow"
  | "selectNode"
  | "pickBranch"
  | "enterWorkflow"
  | "enterSubWorkflow"
  | "popDrill"
  | "truncateDrillStack"
  | "exitWorkflow"
  | "selectWorkflowNode"
  | "pickWorkflowBranch"
  | "setRightPanelWidth"
  | "sendTurn"
  | "enqueueTurn"
  | "deleteQueueItem"
  | "deleteNode"
  | "retryNode"
  | "cancelNode"
  | "refreshChatFlow"
  | "applyEvent"
  | "setSSEFactory"
  | "reset"
> = {
  chatflowList: [],
  folderList: [],
  listLoading: false,
  sidebarCollapsed: false,
  chatflow: null,
  loadState: "idle",
  errorMessage: null,
  _optimisticIds: new Set<string>(),
  selectedNodeId: null,
  branchMemory: {},
  drillStack: [],
  workflowSelectedNodeId: null,
  workflowBranchMemory: {},
  drillDownChatNodeId: null,
  viewMode: "chatflow",
  rightPanelWidth: RIGHT_PANEL_DEFAULT,
  hoveredEdge: null,
  sseSubscription: null,
  sseFactory: null,
};

function autoLeafForChatFlow(chat: ChatFlow | null): NodeId | null {
  if (!chat) return null;
  return findLatestLeafId<ChatFlowNode>({ nodes: chat.nodes, rootIds: chat.root_ids });
}

function autoLeafForWorkFlow(chat: ChatFlow | null, stack: DrillFrame[]): NodeId | null {
  const wf = resolveDrilledWorkflow(chat, stack);
  if (!wf) return null;
  return findLatestLeafId<WorkFlowNode>({
    nodes: wf.nodes,
    rootIds: wf.root_ids,
  });
}

/**
 * Walk ``drillStack`` against ``chatflow`` and return the WorkFlow at
 * the top frame, or ``null`` if any frame fails to resolve (deleted
 * node, missing sub_workflow, …).
 */
export function resolveDrilledWorkflow(
  chat: ChatFlow | null,
  stack: DrillFrame[],
): import("@/types/schema").WorkFlow | null {
  if (!chat || stack.length === 0) return null;
  let wf: import("@/types/schema").WorkFlow | null = null;
  for (const frame of stack) {
    if (frame.kind === "chatnode") {
      const cn = chat.nodes[frame.chatNodeId];
      if (!cn) return null;
      wf = cn.workflow;
    } else {
      if (!wf) return null;
      const wn: import("@/types/schema").WorkFlowNode | undefined =
        wf.nodes[frame.parentWorkNodeId];
      if (!wn || !wn.sub_workflow) return null;
      wf = wn.sub_workflow;
    }
  }
  return wf;
}

/**
 * Walk ancestors of ``endpoint`` via ``parent_ids[0]``. For every
 * ancestor that has >1 siblings (i.e. is a branch root), write
 * ``memory[ancestor] = endpoint``. Mutates ``memory`` in place.
 */
function rememberBranchEndpoints(
  nodes: Record<NodeId, { parent_ids: NodeId[] }>,
  endpoint: NodeId,
  memory: Record<NodeId, NodeId>,
): void {
  const guard = new Set<NodeId>();
  let cursor: NodeId | null = endpoint;
  while (cursor !== null && !guard.has(cursor)) {
    guard.add(cursor);
    const node: { parent_ids: NodeId[] } | undefined = nodes[cursor];
    if (!node) break;
    const parentId: NodeId | null = node.parent_ids[0] ?? null;
    if (parentId !== null && nodes[parentId]) {
      if (countChildren(nodes, parentId) > 1) {
        memory[cursor] = endpoint;
      }
      cursor = parentId;
    } else {
      cursor = null;
    }
  }
}

function countChildren(
  nodes: Record<NodeId, { parent_ids: NodeId[] }>,
  parentId: NodeId,
): number {
  let n = 0;
  for (const node of Object.values(nodes)) {
    if (node.parent_ids.includes(parentId)) n++;
  }
  return n;
}

type SetFn = (partial: Partial<ChatFlowStoreState>) => void;
type GetFn = () => ChatFlowStoreState;

function removeOptimistic(get: GetFn, set: SetFn, optId: string): void {
  const latest = get().chatflow;
  const optIds = get()._optimisticIds;
  if (!latest || !latest.nodes[optId]) return;
  const { [optId]: _, ...rest } = latest.nodes;
  const nextOpts = new Set(optIds);
  nextOpts.delete(optId);
  const cleaned = { ...latest, nodes: rest };
  set({ chatflow: cleaned, _optimisticIds: nextOpts });
  const leaf = autoLeafForChatFlow(cleaned);
  if (leaf) get().selectNode(leaf);
}

export const useChatFlowStore = create<ChatFlowStoreState>((set, get) => ({
  ...INITIAL,

  async fetchChatFlowList() {
    set({ listLoading: true });
    try {
      const [list, folders] = await Promise.all([
        api.listChatFlows(),
        api.listFolders(),
      ]);
      set({ chatflowList: list, folderList: folders, listLoading: false });
    } catch {
      set({ listLoading: false });
    }
  },

  async createChatFlow(title) {
    const { id } = await api.createChatFlow(title);
    await get().fetchChatFlowList();
    await get().loadChatFlow(id);
  },

  async deleteChatFlow(id) {
    await api.deleteChatFlow(id);
    const current = get().chatflow;
    if (current?.id === id) {
      get().reset();
    }
    await get().fetchChatFlowList();
  },

  toggleSidebar() {
    set({ sidebarCollapsed: !get().sidebarCollapsed });
  },

  async createFolder(name, parentId) {
    await api.createFolder(name, parentId);
    await get().fetchChatFlowList();
  },

  async renameFolder(id, name) {
    await api.renameFolder(id, name);
    await get().fetchChatFlowList();
  },

  async deleteFolder(id) {
    const current = get().chatflow;
    const result = await api.deleteFolder(id);
    // If the active chatflow was inside this folder, reset.
    if (current && result.deleted_chatflows?.includes(current.id)) {
      get().reset();
    }
    await get().fetchChatFlowList();
  },

  async moveFolder(folderId, parentId) {
    await api.moveFolder(folderId, parentId);
    await get().fetchChatFlowList();
  },

  async moveChatFlowToFolder(chatflowId, folderId) {
    await api.moveChatFlowToFolder(chatflowId, folderId);
    await get().fetchChatFlowList();
  },

  setHoveredEdge(edge) {
    set({ hoveredEdge: edge });
  },

  async patchChatFlow(patch) {
    const cf = get().chatflow;
    if (!cf) return;
    await api.patchChatFlow(cf.id, patch);
    // Optimistic local update so the header reflects changes immediately.
    const updated = { ...cf };
    if ("title" in patch) updated.title = patch.title ?? null;
    if ("description" in patch) updated.description = patch.description ?? null;
    if ("tags" in patch) updated.tags = patch.tags ?? [];
    if ("default_model" in patch) updated.default_model = patch.default_model ?? null;
    if ("default_judge_model" in patch)
      updated.default_judge_model = patch.default_judge_model ?? null;
    if ("default_tool_call_model" in patch)
      updated.default_tool_call_model = patch.default_tool_call_model ?? null;
    if ("default_execution_mode" in patch && patch.default_execution_mode !== undefined) {
      updated.default_execution_mode = patch.default_execution_mode;
    }
    if ("judge_retry_budget" in patch && patch.judge_retry_budget !== undefined) {
      updated.judge_retry_budget = patch.judge_retry_budget;
    }
    if ("disabled_tool_names" in patch && patch.disabled_tool_names !== undefined) {
      updated.disabled_tool_names = patch.disabled_tool_names;
    }
    set({ chatflow: updated as typeof cf });
    // Refresh sidebar list too (title may have changed).
    void get().fetchChatFlowList();
  },

  async loadChatFlow(id) {
    const previous = get().sseSubscription;
    if (previous) previous.close();

    set({
      loadState: "loading",
      errorMessage: null,
      selectedNodeId: null,
      branchMemory: {},
      drillStack: [],
      viewMode: "chatflow",
      drillDownChatNodeId: null,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
      sseSubscription: null,
    });

    try {
      const chat = await api.getChatFlow(id);
      set({ chatflow: chat, loadState: "ready" });
      const leaf = autoLeafForChatFlow(chat);
      if (leaf) get().selectNode(leaf);

      const factory = get().sseFactory;
      if (factory) {
        const sub = subscribeEvents(
          api.eventsUrl(id),
          {
            onEvent: (evt) => get().applyEvent(evt),
            // Reconnect-safe: backend doesn't tag SSE events with
            // ``id:`` so missed events during a disconnect window
            // can't be replayed. Re-fetch full state on every open
            // so chat.turn.completed (and the agent_response payload
            // it brings) can't be silently lost.
            onOpen: () => {
              if (get().chatflow?.id !== id) return;
              if (get()._optimisticIds.size > 0) return;
              void get().refreshChatFlow();
            },
          },
          factory,
        );
        set({ sseSubscription: sub });
      }
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.message
          : err instanceof Error
            ? err.message
            : String(err);
      set({ loadState: "error", errorMessage: message, chatflow: null });
    }
  },

  setChatFlow(chat) {
    set({
      chatflow: chat,
      loadState: chat ? "ready" : "idle",
      errorMessage: null,
      selectedNodeId: null,
      branchMemory: {},
      drillStack: [],
      viewMode: "chatflow",
      drillDownChatNodeId: null,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
    });
    const leaf = autoLeafForChatFlow(chat);
    if (leaf) get().selectNode(leaf);
  },

  selectNode(nodeId) {
    const chat = get().chatflow;
    if (!nodeId || !chat || !chat.nodes[nodeId]) {
      set({ selectedNodeId: nodeId });
      return;
    }
    const nextMemory = { ...get().branchMemory };
    rememberBranchEndpoints(chat.nodes, nodeId, nextMemory);
    set({ selectedNodeId: nodeId, branchMemory: nextMemory });
  },

  pickBranch(_forkId, childId) {
    const target = get().branchMemory[childId] ?? childId;
    get().selectNode(target);
  },

  enterWorkflow(chatNodeId) {
    const chat = get().chatflow;
    const stack: DrillFrame[] = [{ kind: "chatnode", chatNodeId }];
    set({
      drillStack: stack,
      viewMode: "workflow",
      drillDownChatNodeId: chatNodeId,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
    });
    const leaf = autoLeafForWorkFlow(chat, stack);
    if (leaf) get().selectWorkflowNode(leaf);
  },

  enterSubWorkflow(parentWorkNodeId) {
    const chat = get().chatflow;
    const current = get().drillStack;
    if (current.length === 0) return; // can't push below an empty stack
    const next: DrillFrame[] = [
      ...current,
      { kind: "subworkflow", parentWorkNodeId },
    ];
    // Validate: the parentWorkNodeId must resolve to a WorkNode with
    // a sub_workflow in the *current* top frame's WorkFlow. If it
    // doesn't, drop the push silently — the canvas will just keep
    // showing the current level. A noisier error would only surface
    // race conditions where the user clicked into a node the engine
    // just rewrote out from under them.
    const validated = resolveDrilledWorkflow(chat, next);
    if (!validated) return;
    set({
      drillStack: next,
      viewMode: "workflow",
      drillDownChatNodeId: next[0].kind === "chatnode" ? next[0].chatNodeId : null,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
    });
    const leaf = autoLeafForWorkFlow(chat, next);
    if (leaf) get().selectWorkflowNode(leaf);
  },

  popDrill() {
    const stack = get().drillStack;
    if (stack.length === 0) return;
    const next = stack.slice(0, -1);
    get().truncateDrillStack(next.length);
  },

  truncateDrillStack(length) {
    const stack = get().drillStack.slice(0, Math.max(0, length));
    const chat = get().chatflow;
    set({
      drillStack: stack,
      viewMode: stack.length === 0 ? "chatflow" : "workflow",
      drillDownChatNodeId:
        stack.length > 0 && stack[0].kind === "chatnode"
          ? stack[0].chatNodeId
          : null,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
    });
    if (stack.length > 0) {
      const leaf = autoLeafForWorkFlow(chat, stack);
      if (leaf) get().selectWorkflowNode(leaf);
    }
  },

  exitWorkflow() {
    get().truncateDrillStack(0);
  },

  selectWorkflowNode(nodeId) {
    const chat = get().chatflow;
    const stack = get().drillStack;
    const wf = resolveDrilledWorkflow(chat, stack);
    if (!nodeId || !wf || !wf.nodes[nodeId]) {
      set({ workflowSelectedNodeId: nodeId });
      return;
    }
    const nextMemory = { ...get().workflowBranchMemory };
    rememberBranchEndpoints(wf.nodes, nodeId, nextMemory);
    set({ workflowSelectedNodeId: nodeId, workflowBranchMemory: nextMemory });
  },

  pickWorkflowBranch(_forkId, childId) {
    const target = get().workflowBranchMemory[childId] ?? childId;
    get().selectWorkflowNode(target);
  },

  setRightPanelWidth(width) {
    const clamped = Math.max(RIGHT_PANEL_MIN, Math.min(RIGHT_PANEL_MAX, Math.round(width)));
    set({ rightPanelWidth: clamped });
  },

  async sendTurn(text, parentId, composerModels) {
    const chat = get().chatflow;
    if (!chat) return;

    const llmModel = composerModels?.llm ?? null;
    const judgeModel = composerModels?.judge ?? null;
    const toolCallModel = composerModels?.tool_call ?? null;

    // Use the explicit parent (= selected node in the UI). This is
    // how forks work: if the user selected a non-leaf node, the new
    // turn branches off that node instead of appending to the latest
    // leaf. Only fall back to the latest leaf when no parent given.
    const targetId = parentId ?? findLatestLeafId<ChatFlowNode>({
      nodes: chat.nodes,
      rootIds: chat.root_ids,
    });
    if (!targetId) return;

    // 1. Optimistic node — appears immediately with "running" status.
    //    Stamp resolved_model with the composer's llm pick so the
    //    ribbon layer colors the optimistic node correctly until the
    //    real one arrives via SSE.
    const optimisticId = `_opt_${Date.now()}`;
    const now = new Date().toISOString();
    const optimistic: ChatFlowNode = {
      id: optimisticId,
      parent_ids: [targetId],
      description: { text: "", provenance: "unset", updated_at: now },
      inputs: null,
      expected_outcome: null,
      status: "running",
      resolved_model: llmModel,
      locked: false,
      error: null,
      position_x: null,
      position_y: null,
      created_at: now,
      updated_at: now,
      started_at: now,
      finished_at: null,
      user_message: { text, provenance: "pure_user", updated_at: now },
      agent_response: { text: "", provenance: "unset", updated_at: now },
      workflow: { id: `wf-${optimisticId}`, root_ids: [], nodes: {} },
      pending_queue: [],
    };
    const nextOptIds = new Set(get()._optimisticIds);
    nextOptIds.add(optimisticId);
    set({
      _optimisticIds: nextOptIds,
      chatflow: {
        ...chat,
        nodes: { ...chat.nodes, [optimisticId]: optimistic },
      },
    });
    get().selectNode(optimisticId);

    // 2. Fire submitTurn (supports parent_id for fork semantics).
    //    Don't await the response — it blocks until the LLM finishes.
    //    SSE events will drive all UI updates. We just need the server
    //    to receive the request and create the real node.
    api
      .submitTurn(chat.id, text, targetId, llmModel, judgeModel, toolCallModel)
      .catch(() => {
        // If the request itself fails (network error, 4xx), clean up.
        removeOptimistic(get, set, optimisticId);
      });

    // 3. Give the server a moment to create the node, then refresh
    //    to replace the optimistic node with the real one. SSE
    //    chat.node.created can't fire yet (optimistic IDs suppress
    //    SSE-triggered refreshes), so we drive it ourselves.
    await new Promise((r) => setTimeout(r, 300));
    await get().refreshChatFlow();

    // 4. Focus the newly-created child of ``targetId`` (the server's
    //    real node that replaced our optimistic one). refreshChatFlow's
    //    generic fallback picks the global default-walk leaf, which
    //    lands on the wrong branch when the user forked off a non-latest
    //    branch — override it explicitly.
    const fresh = get().chatflow;
    if (fresh) {
      let newest: ChatFlowNode | null = null;
      for (const n of Object.values(fresh.nodes)) {
        if (!n.parent_ids.includes(targetId)) continue;
        if (!newest || n.created_at > newest.created_at) newest = n;
      }
      if (newest) get().selectNode(newest.id);
    }
  },

  async enqueueTurn(nodeId, text, composerModels) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.enqueueTurn(
      chat.id,
      nodeId,
      text,
      "web",
      composerModels?.llm ?? null,
      composerModels?.judge ?? null,
      composerModels?.tool_call ?? null,
    );
    await get().refreshChatFlow();
  },

  async deleteQueueItem(nodeId, itemId) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.deleteQueueItem(chat.id, nodeId, itemId);
    await get().refreshChatFlow();
  },

  async deleteNode(nodeId) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.deleteNode(chat.id, nodeId);
    await get().refreshChatFlow();
  },

  async retryNode(nodeId) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.retryNode(chat.id, nodeId);
    await get().refreshChatFlow();
  },

  async cancelNode(nodeId) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.cancelNode(chat.id, nodeId);
    await get().refreshChatFlow();
  },

  async refreshChatFlow() {
    const chat = get().chatflow;
    if (!chat) return;
    try {
      const fresh = await api.getChatFlow(chat.id);
      const selected = get().selectedNodeId;

      // Clear all optimistic nodes — the server state is now
      // authoritative. Real nodes that replaced them are in ``fresh``.
      set({ chatflow: fresh, _optimisticIds: new Set() });

      // If the selected node no longer exists (optimistic ID or
      // deleted node), jump to the latest real leaf.
      if (selected && !fresh.nodes[selected]) {
        const leaf = autoLeafForChatFlow(fresh);
        if (leaf) get().selectNode(leaf);
      }
    } catch {
      // Refresh failed — stale state is acceptable, SSE will reconcile.
    }
  },

  applyEvent(event) {
    const chat = get().chatflow;
    if (!chat) return;

    const kind = event.kind;
    const data = event.data ?? {};

    // Structure-changing events: reload the full chatflow from the
    // server — BUT skip if there are optimistic nodes in flight.
    // sendTurn's own refreshChatFlow (after enqueue resolves) will
    // reconcile; firing here would race it and flash the optimistic
    // node away before the real one arrives.
    if (
      kind === "chat.node.created" ||
      kind === "chat.node.deleted" ||
      kind === "chat.turn.completed" ||
      kind.startsWith("chat.workflow.node.")
    ) {
      if (get()._optimisticIds.size === 0) {
        void get().refreshChatFlow();
      }
      return;
    }

    if (!event.node_id) return;
    const targetId = event.node_id;

    // Status patch — works for both outer chatflow nodes and inner workflow nodes.
    if (kind === "chat.node.status" || kind === "chat.turn.started") {
      const status = (data.status as NodeStatus) ?? null;
      if (!status) return;

      let patched = false;
      const nextNodes: Record<NodeId, ChatFlowNode> = { ...chat.nodes };

      for (const [cnid, cnode] of Object.entries(chat.nodes)) {
        if (cnid === targetId) {
          nextNodes[cnid] = withStatus(cnode, status);
          patched = true;
          continue;
        }
        const innerNodes = cnode.workflow.nodes;
        if (targetId in innerNodes) {
          const innerPatched = {
            ...innerNodes,
            [targetId]: {
              ...innerNodes[targetId],
              status,
            },
          };
          nextNodes[cnid] = {
            ...cnode,
            workflow: { ...cnode.workflow, nodes: innerPatched },
          };
          patched = true;
        }
      }

      if (patched) {
        set({ chatflow: { ...chat, nodes: nextNodes } });
      }
      return;
    }

    // Queue update — patch the pending_queue array on the target node.
    if (kind === "chat.node.queue.updated") {
      const queue = (data.pending_queue as PendingTurn[]) ?? [];
      const node = chat.nodes[targetId];
      if (!node) return;
      set({
        chatflow: {
          ...chat,
          nodes: {
            ...chat.nodes,
            [targetId]: { ...node, pending_queue: queue },
          },
        },
      });
    }
  },

  setSSEFactory(factory) {
    set({ sseFactory: factory });
  },

  reset() {
    const sub = get().sseSubscription;
    if (sub) sub.close();
    set({ ...INITIAL });
  },
}));

function withStatus(node: ChatFlowNode, status: NodeStatus | null): ChatFlowNode {
  if (!status) return node;
  return { ...node, status };
}
