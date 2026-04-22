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
  BoardItem,
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
   * First node picked for a VSCode-compare-style merge. While set, the
   * canvas pulses that node and the context menu on every other node
   * offers "Merge with {pendingMergeFirstId}". Any non-pan interaction
   * (node click, drag, another menu item, Escape, pane click) clears
   * this back to null. Finalizing the merge also clears it.
   */
  pendingMergeFirstId: NodeId | null;

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

  /** Per-WorkNode streaming buffer, keyed by inner node id. Populated
   * by ``chat.workflow.node.token`` events and cleared on the next
   * ``running``/``succeeded``/``failed`` for that node. UI components
   * should only display this when the node's status is ``running`` —
   * once it terminates, the refreshed full payload is authoritative. */
  streamingDeltas: Record<NodeId, string>;

  /** MemoryBoardItem cache for the currently loaded ChatFlow, keyed by
   * ``source_node_id`` so the canvas bubbles can look up a node-brief
   * in O(1). Flow-briefs are keyed by their WorkFlow id (the source
   * node id of a ``scope=flow`` BoardItem is the workflow id by
   * design). Populated on ``loadChatFlow``; refreshed by
   * ``refreshBoardItems``. */
  boardItems: Record<NodeId, BoardItem>;

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
  }) => Promise<void>;

  /** Which edge is currently hovered on the ChatFlow canvas — drives
   * the model-family ribbon highlight (§4.10 rework: model lives on
   * the parent→child edge, so hover targets edges, not node badges).
   * Stored as the full {parent, child} pair because merge nodes have
   * multiple incoming edges that may carry different per-edge models. */
  hoveredEdge: { parent: NodeId; child: NodeId } | null;
  setHoveredEdge: (edge: { parent: NodeId; child: NodeId } | null) => void;

  /** Pack hover: the ChatNode ids a currently-hovered pack covers.
   * Driven by the pack ChatNode card's ``onMouseEnter/Leave``; other
   * ChatNode cards subscribe and draw a rose halo when their id is in
   * this list. Null = no pack is being hovered. Overlapping / nested
   * packs are naturally handled — only one pack's range is active at
   * a time, so hovering whichever pack you want lights up its own
   * members without conflict. */
  hoveredPackRange: NodeId[] | null;
  hoveredPackId: NodeId | null;
  setHoveredPack: (packId: NodeId | null, range: NodeId[] | null) => void;

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
  /** Atomically drill directly to a WorkNode at any nesting depth and
   * select it. ``subPath`` is the chain of ``sub_agent_delegation``
   * parent WorkNode ids between the outer workflow and the target's
   * workflow (empty for the outer workflow). Used by the active-work
   * navigator to jump across chat/sub-flow boundaries. */
  jumpToWorkNode: (
    chatNodeId: NodeId,
    subPath: NodeId[],
    workNodeId: NodeId,
  ) => void;
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
  retryNode: (nodeId: NodeId, composerModels?: ComposerModelMap | null) => Promise<void>;
  /** Cancel a RUNNING node. */
  cancelNode: (nodeId: NodeId) => Promise<void>;
  /** Re-fetch the current chatflow from the server. */
  refreshChatFlow: () => Promise<void>;

  /** Stash ``nodeId`` as the first of two source nodes in a pending
   * merge. No-op / toggle-off when called with the same id twice. */
  beginPendingMerge: (nodeId: NodeId) => void;
  /** Clear any pending-merge state. Safe to call when nothing is pending. */
  cancelPendingMerge: () => void;
  /** Fire the merge API for (pendingMergeFirstId, secondId) and clear
   * the pending state on success. Throws if no pending first node. */
  commitMergeWith: (secondId: NodeId) => Promise<void>;

  /** First-pick for pack range selection. Like ``pendingMergeFirstId``
   * but for the "two-pick range" flow: right-click ChatNode → "select
   * as pack start" → set this. Right-click another ChatNode → "pack to
   * here" → ``commitPackTo`` checks that start/end are ancestor-
   * descendant along the primary-parent chain, derives the range, and
   * hits the pack API. Null when no pack is in progress. */
  pendingPackStartId: NodeId | null;
  /** Stash ``nodeId`` as the pack start. Toggle-off when called with
   * the same id twice. */
  beginPendingPack: (nodeId: NodeId) => void;
  /** Clear any pending-pack state. */
  cancelPendingPack: () => void;
  /** Derive a primary-parent-chain range between ``pendingPackStartId``
   * and ``endId``, then hit the pack API with the supplied knobs.
   * Throws with a user-facing message if the two ids are not
   * ancestor-descendant (one must reach the other by walking
   * ``parent_ids[0]``). */
  commitPackTo: (
    endId: NodeId,
    knobs: {
      use_detailed_index?: boolean;
      preserve_last_n?: number;
      pack_instruction?: string;
      must_keep?: string;
      must_drop?: string;
      target_tokens?: number | null;
      model?: ProviderModelRef | null;
    },
  ) => Promise<void>;

  /** Apply a single SSE event to the current chatflow payload. */
  applyEvent: (event: WorkFlowEvent) => void;
  /** Override the SSE factory (for tests). */
  setSSEFactory: (factory: SSEFactory | null) => void;
  /** Refetch the ``board_items`` list for the current ChatFlow and
   * repopulate the ``boardItems`` cache. Called on ``loadChatFlow``
   * and after any SSE event hinting a brief just finished. */
  refreshBoardItems: () => Promise<void>;
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
  | "setHoveredPack"
  | "loadChatFlow"
  | "setChatFlow"
  | "selectNode"
  | "pickBranch"
  | "enterWorkflow"
  | "enterSubWorkflow"
  | "jumpToWorkNode"
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
  | "beginPendingMerge"
  | "cancelPendingMerge"
  | "commitMergeWith"
  | "beginPendingPack"
  | "cancelPendingPack"
  | "commitPackTo"
  | "applyEvent"
  | "setSSEFactory"
  | "refreshBoardItems"
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
  pendingMergeFirstId: null,
  pendingPackStartId: null,
  drillStack: [],
  workflowSelectedNodeId: null,
  workflowBranchMemory: {},
  drillDownChatNodeId: null,
  viewMode: "chatflow",
  rightPanelWidth: RIGHT_PANEL_DEFAULT,
  hoveredEdge: null,
  hoveredPackRange: null,
  hoveredPackId: null,
  sseSubscription: null,
  sseFactory: null,
  streamingDeltas: {},
  boardItems: {},
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

let _refreshInFlight: Promise<void> | null = null;
let _refreshRequested = false;

async function _doRefreshOnce(get: GetFn, set: SetFn): Promise<void> {
  const chat = get().chatflow;
  if (!chat) return;
  try {
    const fresh = await api.getChatFlow(chat.id);
    if (get().chatflow?.id !== fresh.id) return;
    const selected = get().selectedNodeId;
    set({ chatflow: fresh, _optimisticIds: new Set() });
    if (selected && !fresh.nodes[selected]) {
      const leaf = autoLeafForChatFlow(fresh);
      if (leaf) get().selectNode(leaf);
    }
    // A refresh implies the workflow graph changed; a brief may have
    // just landed. Piggyback a board_items fetch so the canvas bubbles
    // update in the same reconcile pass.
    void get().refreshBoardItems();
  } catch {
    // Refresh failed — stale state is acceptable, next SSE event will retry.
  }
}

function runCoalescedRefresh(get: GetFn, set: SetFn): Promise<void> {
  if (_refreshInFlight) {
    _refreshRequested = true;
    return _refreshInFlight;
  }
  const run = async () => {
    try {
      do {
        _refreshRequested = false;
        await _doRefreshOnce(get, set);
      } while (_refreshRequested);
    } finally {
      _refreshInFlight = null;
    }
  };
  _refreshInFlight = run();
  return _refreshInFlight;
}

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

  setHoveredPack(packId, range) {
    set({ hoveredPackId: packId, hoveredPackRange: range });
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
    if ("draft_model" in patch) updated.draft_model = patch.draft_model ?? null;
    if ("default_judge_model" in patch)
      updated.default_judge_model = patch.default_judge_model ?? null;
    if ("default_tool_call_model" in patch)
      updated.default_tool_call_model = patch.default_tool_call_model ?? null;
    if ("brief_model" in patch) updated.brief_model = patch.brief_model ?? null;
    if ("default_execution_mode" in patch && patch.default_execution_mode !== undefined) {
      updated.default_execution_mode = patch.default_execution_mode;
    }
    if ("judge_retry_budget" in patch && patch.judge_retry_budget !== undefined) {
      updated.judge_retry_budget = patch.judge_retry_budget;
    }
    if ("min_ground_ratio" in patch) {
      updated.min_ground_ratio = patch.min_ground_ratio ?? null;
    }
    if (
      "ground_ratio_grace_nodes" in patch
      && patch.ground_ratio_grace_nodes !== undefined
    ) {
      updated.ground_ratio_grace_nodes = patch.ground_ratio_grace_nodes;
    }
    if ("disabled_tool_names" in patch && patch.disabled_tool_names !== undefined) {
      updated.disabled_tool_names = patch.disabled_tool_names;
    }
    if ("compact_trigger_pct" in patch) {
      updated.compact_trigger_pct = patch.compact_trigger_pct ?? null;
    }
    if ("compact_target_pct" in patch && patch.compact_target_pct !== undefined) {
      updated.compact_target_pct = patch.compact_target_pct;
    }
    if (
      "compact_keep_recent_count" in patch
      && patch.compact_keep_recent_count !== undefined
    ) {
      updated.compact_keep_recent_count = patch.compact_keep_recent_count;
    }
    if (
      "compact_preserve_mode" in patch
      && patch.compact_preserve_mode !== undefined
    ) {
      updated.compact_preserve_mode = patch.compact_preserve_mode;
    }
    if (
      "recalled_context_sticky_turns" in patch
      && patch.recalled_context_sticky_turns !== undefined
    ) {
      updated.recalled_context_sticky_turns = patch.recalled_context_sticky_turns;
    }
    if ("compact_model" in patch) {
      updated.compact_model = patch.compact_model ?? null;
    }
    if (
      "compact_require_confirmation" in patch
      && patch.compact_require_confirmation !== undefined
    ) {
      updated.compact_require_confirmation = patch.compact_require_confirmation;
    }
    if ("chatnode_compact_trigger_pct" in patch) {
      updated.chatnode_compact_trigger_pct = patch.chatnode_compact_trigger_pct ?? null;
    }
    if (
      "chatnode_compact_target_pct" in patch
      && patch.chatnode_compact_target_pct !== undefined
    ) {
      updated.chatnode_compact_target_pct = patch.chatnode_compact_target_pct;
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
      set({ chatflow: chat, loadState: "ready", boardItems: {} });
      const leaf = autoLeafForChatFlow(chat);
      if (leaf) get().selectNode(leaf);
      // Fetch the MemoryBoard cache in the background — canvas bubbles
      // appear as soon as the response returns, but we don't block the
      // main load on it.
      void get().refreshBoardItems();

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

  jumpToWorkNode(chatNodeId, subPath, workNodeId) {
    const chat = get().chatflow;
    const stack: DrillFrame[] = [
      { kind: "chatnode", chatNodeId },
      ...subPath.map((id) => ({ kind: "subworkflow" as const, parentWorkNodeId: id })),
    ];
    const wf = resolveDrilledWorkflow(chat, stack);
    if (!wf || !wf.nodes[workNodeId]) return;
    set({
      drillStack: stack,
      viewMode: "workflow",
      drillDownChatNodeId: chatNodeId,
      workflowSelectedNodeId: workNodeId,
      workflowBranchMemory: {},
    });
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
      compact_snapshot: null,
      entry_prompt_tokens: null,
      output_response_tokens: null,
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

  async retryNode(nodeId, composerModels) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.retryNode(
      chat.id,
      nodeId,
      composerModels?.llm ?? null,
      composerModels?.judge ?? null,
      composerModels?.tool_call ?? null,
    );
    await get().refreshChatFlow();
  },

  async cancelNode(nodeId) {
    const chat = get().chatflow;
    if (!chat) return;
    await api.cancelNode(chat.id, nodeId);
    await get().refreshChatFlow();
  },

  beginPendingMerge(nodeId) {
    const current = get().pendingMergeFirstId;
    if (current === nodeId) {
      // Re-picking the same node cancels — matches VSCode compare where
      // the second right-click of the same file un-selects it.
      set({ pendingMergeFirstId: null });
      return;
    }
    set({ pendingMergeFirstId: nodeId });
  },

  cancelPendingMerge() {
    if (get().pendingMergeFirstId !== null) {
      set({ pendingMergeFirstId: null });
    }
  },

  async commitMergeWith(secondId) {
    const chat = get().chatflow;
    const firstId = get().pendingMergeFirstId;
    if (!chat) throw new Error("no chatflow loaded");
    if (firstId === null) throw new Error("no pending first merge node");
    if (firstId === secondId) throw new Error("cannot merge a node with itself");
    // Optimistic cancel first — whatever the server returns, the
    // pending handshake is done and should not leak to the next
    // interaction.
    set({ pendingMergeFirstId: null });
    const res = await api.mergeChain(chat.id, {
      left_id: firstId,
      right_id: secondId,
    });
    await get().refreshChatFlow();
    get().selectNode(res.node_id);
  },

  beginPendingPack(nodeId) {
    const current = get().pendingPackStartId;
    if (current === nodeId) {
      // Same node twice cancels (mirrors merge's toggle-off).
      set({ pendingPackStartId: null });
      return;
    }
    set({ pendingPackStartId: nodeId });
  },

  cancelPendingPack() {
    if (get().pendingPackStartId !== null) {
      set({ pendingPackStartId: null });
    }
  },

  async commitPackTo(endId, knobs) {
    const chat = get().chatflow;
    const startId = get().pendingPackStartId;
    if (!chat) throw new Error("no chatflow loaded");
    if (startId === null) throw new Error("no pending pack start");
    if (startId === endId) throw new Error("cannot pack a node with itself");

    // Derive the primary-parent-chain range. Try both directions:
    // walk up from endId until we hit startId, else walk up from
    // startId until we hit endId. If neither walk reaches the other,
    // the two nodes are not ancestor-descendant on the primary
    // chain — the pack backend would reject this anyway, but we
    // surface a clearer error up-front.
    const walkUpTo = (from: NodeId, target: NodeId): NodeId[] | null => {
      const range: NodeId[] = [];
      const guard = new Set<NodeId>();
      let cur: NodeId | null = from;
      while (cur !== null && !guard.has(cur)) {
        guard.add(cur);
        range.unshift(cur);
        if (cur === target) return range;
        const parents: NodeId[] = chat.nodes[cur]?.parent_ids ?? [];
        cur = parents.length > 0 ? parents[0] : null;
      }
      return null;
    };

    let range = walkUpTo(endId, startId);
    // If the user picked in reverse order (start is actually the
    // newer node), flip and try the other direction.
    if (range === null) {
      const reversed = walkUpTo(startId, endId);
      if (reversed !== null) {
        range = reversed;
      }
    }
    if (range === null) {
      throw new Error(
        "pack range invalid: the two ChatNodes must be ancestor and " +
          "descendant along the primary-parent chain",
      );
    }
    // Clear pending state up-front; API errors shouldn't leave the UI
    // stuck in "pending pack" mode.
    set({ pendingPackStartId: null });
    const res = await api.packChain(chat.id, {
      packed_range: range,
      use_detailed_index: knobs.use_detailed_index,
      preserve_last_n: knobs.preserve_last_n,
      pack_instruction: knobs.pack_instruction,
      must_keep: knobs.must_keep,
      must_drop: knobs.must_drop,
      target_tokens: knobs.target_tokens,
      model: knobs.model,
    });
    await get().refreshChatFlow();
    get().selectNode(res.node_id);
  },

  async refreshChatFlow() {
    // SSE fires many ``chat.workflow.node.*`` events in quick
    // succession during a retry or turn run. Each event used to
    // trigger a fresh ``api.getChatFlow`` fetch; in flight the fetches
    // would race and sometimes commit an older snapshot on top of a
    // newer one — leaving the UI stuck on "only judge_pre" until the
    // user manually refreshed. Coalesce: at most one fetch in flight
    // at a time, and chain exactly one follow-up if more calls arrive.
    // The returned promise resolves only after the whole chain
    // completes, so callers awaiting this after a mutation still see
    // the post-mutation state.
    return runCoalescedRefresh(get, set);
  },

  applyEvent(event) {
    const chat = get().chatflow;
    if (!chat) return;

    const kind = event.kind;
    const data = event.data ?? {};

    // The whole chatflow was deleted (by this tab or elsewhere). Drop
    // local state so the canvas stops painting the last-known node
    // status as still-running, close the SSE stream, and refresh the
    // sidebar so the row disappears.
    if (kind === "chat.deleted") {
      get().sseSubscription?.close();
      get().setChatFlow(null);
      void get().fetchChatFlowList();
      return;
    }

    // High-frequency streaming token events: append to the per-node
    // buffer, do NOT trigger a server refresh (one fragment per
    // chunk, so a long generation can fire 100+ events). The UI
    // reads ``streamingDeltas[node_id]`` for the live preview while
    // the node is RUNNING; once it terminates, the refresh fired
    // by ``running``/``succeeded``/``failed`` brings in authoritative
    // text and the buffer is cleared.
    if (kind === "chat.workflow.node.token" && event.node_id) {
      const delta = typeof data.delta === "string" ? data.delta : "";
      if (!delta) return;
      const current = get().streamingDeltas;
      set({
        streamingDeltas: {
          ...current,
          [event.node_id]: (current[event.node_id] ?? "") + delta,
        },
      });
      return;
    }

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
      // Drain stale streaming buffer for this worknode so the next
      // run starts from empty (and the now-authoritative server
      // payload fully owns the rendered text).
      if (
        event.node_id &&
        (kind === "chat.workflow.node.running" ||
          kind === "chat.workflow.node.succeeded" ||
          kind === "chat.workflow.node.failed")
      ) {
        const buf = get().streamingDeltas;
        if (event.node_id in buf) {
          const next = { ...buf };
          delete next[event.node_id];
          set({ streamingDeltas: next });
        }
      }
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

  async refreshBoardItems() {
    const chat = get().chatflow;
    if (!chat) return;
    try {
      const res = await api.listBoardItems(chat.id);
      // ``scope=node``: source_node_id is the source WorkNode's id.
      // ``scope=flow``: source_node_id is the enclosing WorkFlow's id.
      // ``scope=chat``: source_node_id is the source ChatNode's id (PR 3).
      // All three share the same id-space (UUIDv7), so a flat map
      // keyed by source_node_id is unambiguous.
      const next: Record<NodeId, BoardItem> = {};
      for (const item of res.items) {
        next[item.source_node_id] = item;
      }
      // Keep the store write scoped to the still-current ChatFlow: if
      // the user switched away during the fetch, don't overwrite the
      // cache for a different ChatFlow's bubbles.
      if (get().chatflow?.id === chat.id) {
        set({ boardItems: next });
      }
    } catch {
      // Fail open: absent bubbles aren't worth surfacing a toast.
    }
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
