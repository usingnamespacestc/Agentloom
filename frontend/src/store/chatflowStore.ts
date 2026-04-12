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
import type {
  ChatFlow,
  ChatFlowNode,
  ChatFlowSummary,
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

  /** Drill-down: which ChatNode we entered. null means we're in chatflow view. */
  viewMode: ViewMode;
  drillDownChatNodeId: NodeId | null;
  workflowSelectedNodeId: NodeId | null;
  /** Same concept as ``branchMemory``, but inside a drilled-down WorkFlow. */
  workflowBranchMemory: Record<NodeId, NodeId>;

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
  }) => Promise<void>;

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

  /** Enter the workflow drill-down view for a specific ChatNode. */
  enterWorkflow: (chatNodeId: NodeId) => void;
  /** Leave the workflow drill-down and return to ChatFlow canvas. */
  exitWorkflow: () => void;
  /** Select a node inside the currently drilled-down WorkFlow. */
  selectWorkflowNode: (nodeId: NodeId | null) => void;
  /** Switch to a different child at a WorkFlow fork (branch memory aware). */
  pickWorkflowBranch: (forkId: NodeId, childId: NodeId) => void;

  /** Update the right panel width (clamped to [MIN, MAX]). */
  setRightPanelWidth: (width: number) => void;

  /** Send a user turn (enqueue or immediate submit depending on state). */
  sendTurn: (text: string, parentId?: string) => Promise<void>;
  /** Enqueue a pending turn on a specific node. */
  enqueueTurn: (nodeId: NodeId, text: string) => Promise<void>;
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
  | "loadChatFlow"
  | "setChatFlow"
  | "selectNode"
  | "pickBranch"
  | "enterWorkflow"
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
  viewMode: "chatflow",
  drillDownChatNodeId: null,
  workflowSelectedNodeId: null,
  workflowBranchMemory: {},
  rightPanelWidth: RIGHT_PANEL_DEFAULT,
  sseSubscription: null,
  sseFactory: null,
};

function autoLeafForChatFlow(chat: ChatFlow | null): NodeId | null {
  if (!chat) return null;
  return findLatestLeafId<ChatFlowNode>({ nodes: chat.nodes, rootIds: chat.root_ids });
}

function autoLeafForWorkFlow(chat: ChatFlow | null, chatNodeId: NodeId | null): NodeId | null {
  if (!chat || !chatNodeId) return null;
  const node = chat.nodes[chatNodeId];
  if (!node) return null;
  return findLatestLeafId<WorkFlowNode>({
    nodes: node.workflow.nodes,
    rootIds: node.workflow.root_ids,
  });
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
          { onEvent: (evt) => get().applyEvent(evt) },
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
    set({
      viewMode: "workflow",
      drillDownChatNodeId: chatNodeId,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
    });
    const leaf = autoLeafForWorkFlow(chat, chatNodeId);
    if (leaf) get().selectWorkflowNode(leaf);
  },

  exitWorkflow() {
    set({
      viewMode: "chatflow",
      drillDownChatNodeId: null,
      workflowSelectedNodeId: null,
      workflowBranchMemory: {},
    });
  },

  selectWorkflowNode(nodeId) {
    const chat = get().chatflow;
    const drillId = get().drillDownChatNodeId;
    if (!nodeId || !chat || !drillId) {
      set({ workflowSelectedNodeId: nodeId });
      return;
    }
    const wfNodes = chat.nodes[drillId]?.workflow.nodes;
    if (!wfNodes || !wfNodes[nodeId]) {
      set({ workflowSelectedNodeId: nodeId });
      return;
    }
    const nextMemory = { ...get().workflowBranchMemory };
    rememberBranchEndpoints(wfNodes, nodeId, nextMemory);
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

  async sendTurn(text, parentId) {
    const chat = get().chatflow;
    if (!chat) return;

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
    const optimisticId = `_opt_${Date.now()}`;
    const now = new Date().toISOString();
    const optimistic: ChatFlowNode = {
      id: optimisticId,
      parent_ids: [targetId],
      description: { text: "", provenance: "unset", updated_at: now },
      expected_outcome: null,
      status: "running",
      model_override: null,
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
    api.submitTurn(chat.id, text, targetId).catch(() => {
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

  async enqueueTurn(nodeId, text) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.enqueueTurn(chat.id, nodeId, text);
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
