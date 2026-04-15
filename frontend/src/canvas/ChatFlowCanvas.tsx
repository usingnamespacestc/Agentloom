/**
 * Top-level canvas for a ChatFlow (M8.5 horizontal).
 *
 * Subscribes to the store, lays out the current chatflow into React
 * Flow nodes + edges (horizontal flow, left→right), and forwards
 * interactions back into the store (select; drill-down via custom
 * event from the node card's ⤢ button).
 *
 * Drag support (round 2):
 * - Node positions live in local state and are updated through
 *   ``applyNodeChanges`` so React Flow's controlled-mode drag works.
 * - User-dragged positions are captured into a ref keyed by node id.
 *   On every reconcile (chatflow reload, selection change, SSE patch)
 *   we re-run ``buildGraph`` but overlay stored drag positions, so the
 *   user's manual placement sticks until they reload.
 *
 * Selection (round 2):
 * - The store is the single source of truth. We filter out React
 *   Flow's own ``select``-type changes in ``onNodesChange`` so the
 *   only way to select a node is our ``onNodeClick`` → ``selectNode``
 *   path.
 * - ``multiSelectionKeyCode={null}`` + ``selectNodesOnDrag={false}``
 *   prevent any multi-select affordance. Only one node can be
 *   highlighted at a time.
 *
 * UX rules:
 * - Users cannot draw edges between nodes.
 * - The default React Flow <Controls> "lock" button is hidden via
 *   ``showInteractive={false}`` because it confuses first-time users.
 *
 * Edges:
 * - solid dark edge for frozen → frozen transitions
 * - dashed gray edge when either endpoint is still planned
 * - purple edge into merge nodes
 * - animated flow when the target is running
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTranslation } from "react-i18next";

import { layoutDag } from "./layout";
import { ModelRibbonLayer } from "./ModelRibbonLayer";
import { MODEL_KINDS, colorForModel, edgeModel } from "./effectiveModel";
import { ChatFlowNodeCard, type ChatFlowNodeData } from "./nodes/ChatFlowNodeCard";
import { api, type ProviderSummary } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlow, ChatFlowNode, NodeId, ProviderModelRef } from "@/types/schema";

interface ContextMenuState {
  nodeId: string;
  x: number;
  y: number;
}

const NODE_TYPES = { chatflow: ChatFlowNodeCard };

export interface ChatFlowCanvasProps {
  chatflow: ChatFlow | null;
}

export function ChatFlowCanvas({ chatflow }: ChatFlowCanvasProps) {
  const { t } = useTranslation();
  const selectedNodeId = useChatFlowStore((s) => s.selectedNodeId);
  const selectNode = useChatFlowStore((s) => s.selectNode);
  const enterWorkflow = useChatFlowStore((s) => s.enterWorkflow);
  const deleteNode = useChatFlowStore((s) => s.deleteNode);
  const retryNode = useChatFlowStore((s) => s.retryNode);
  const cancelNode = useChatFlowStore((s) => s.cancelNode);
  const setHoveredEdge = useChatFlowStore((s) => s.setHoveredEdge);
  const hoveredEdge = useChatFlowStore((s) => s.hoveredEdge);

  // Cursor position for the edge-hover tooltip — only tracked while an
  // edge is hovered, so we don't pay for global mousemove the rest of
  // the time. Stored as viewport (clientX/clientY) coords because the
  // tooltip uses `position: fixed`.
  const [cursorPos, setCursorPos] = useState<{ x: number; y: number } | null>(null);
  useEffect(() => {
    if (!hoveredEdge) {
      setCursorPos(null);
      return;
    }
    const onMove = (e: MouseEvent) => setCursorPos({ x: e.clientX, y: e.clientY });
    window.addEventListener("mousemove", onMove);
    return () => window.removeEventListener("mousemove", onMove);
  }, [hoveredEdge]);

  // Context menu state
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);

  // Listen for drill-down requests from the node card's ⤢ button. We
  // use a window CustomEvent rather than passing a callback through
  // React Flow's node-data channel because data flows back out of
  // React Flow as a serialized object, not a live ref.
  useEffect(() => {
    const handler = (event: Event) => {
      const ce = event as CustomEvent<{ chatNodeId: string }>;
      if (ce.detail?.chatNodeId) enterWorkflow(ce.detail.chatNodeId);
    };
    window.addEventListener("agentloom:enter-workflow", handler);
    return () => window.removeEventListener("agentloom:enter-workflow", handler);
  }, [enterWorkflow]);

  // Delete confirmation state — null means no dialog shown.
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);

  // Listen for delete requests from the node card's ✕ button.
  useEffect(() => {
    const handler = (event: Event) => {
      const ce = event as CustomEvent<{ nodeId: string; isLeaf: boolean }>;
      if (!ce.detail?.nodeId) return;
      const { nodeId, isLeaf } = ce.detail;
      if (isLeaf) {
        void deleteNode(nodeId);
      } else {
        setPendingDeleteId(nodeId);
      }
    };
    window.addEventListener("agentloom:delete-node", handler);
    return () => window.removeEventListener("agentloom:delete-node", handler);
  }, [deleteNode]);

  // Providers list feeds the per-node context-window lookup below so the
  // TokenBar denominator matches the actual model's window (e.g. Ark's
  // 128k) instead of a hard-coded default. Fetched once per canvas
  // mount; changes to Settings won't propagate until reload, which is
  // fine — the numbers are diagnostic, not load-bearing.
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    void api
      .listProviders()
      .then((list) => {
        if (!cancelled) setProviders(list);
      })
      .catch(() => {
        // Silent — TokenBar falls back to the default window.
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const contextWindowByModel = contextWindowMap(providers);

  const [nodes, setNodes] = useState<Node<ChatFlowNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  // User-dragged positions survive graph reconciliation (SSE patches,
  // selection changes). They're cleared when a brand-new chatflow is
  // loaded (different chatflow id).
  const dragPositions = useRef<Record<string, { x: number; y: number }>>({});
  const dirtyPositions = useRef<Set<string>>(new Set());
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastChatflowId = useRef<string | null>(null);

  // Debounced save of dirty drag positions to the backend.
  const flushPositions = useCallback(() => {
    if (!chatflow || dirtyPositions.current.size === 0) return;
    const positions = [...dirtyPositions.current]
      .map((id) => {
        const pos = dragPositions.current[id];
        return pos ? { id, x: pos.x, y: pos.y } : null;
      })
      .filter(Boolean) as { id: string; x: number; y: number }[];
    dirtyPositions.current.clear();
    if (positions.length > 0) {
      void api.patchPositions(chatflow.id, positions);
    }
  }, [chatflow]);

  useEffect(() => {
    if (chatflow?.id !== lastChatflowId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      lastChatflowId.current = chatflow?.id ?? null;
    }
    const laid = buildGraph(chatflow, selectedNodeId, contextWindowByModel);
    const merged = laid.nodes.map((n) => ({
      ...n,
      position: dragPositions.current[n.id] ?? n.position,
    }));
    setNodes(merged);
    setEdges(laid.edges);
  }, [chatflow, selectedNodeId, contextWindowByModel]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    // Drop React Flow's own select events — the store is the single
    // source of truth for selection, so we never let RF track it.
    const filtered = changes.filter((c) => c.type !== "select");
    if (filtered.length === 0) return;
    for (const c of filtered) {
      if (c.type === "position" && c.position) {
        dragPositions.current[c.id] = c.position;
        dirtyPositions.current.add(c.id);
      }
    }
    setNodes((ns) => applyNodeChanges(filtered, ns) as Node<ChatFlowNodeData>[]);

    // Debounce: save 500ms after last drag movement.
    if (dirtyPositions.current.size > 0) {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPositions, 500);
    }
  }, [flushPositions]);

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    selectNode(node.id);
    setContextMenu(null);
  };

  const handleContextMenu: NodeMouseHandler = (event, node) => {
    event.preventDefault();
    setContextMenu({ nodeId: node.id, x: event.clientX, y: event.clientY });
    selectNode(node.id);
  };

  const handlePaneClick = useCallback(() => {
    setContextMenu(null);
  }, []);

  if (!chatflow) {
    return (
      <div
        data-testid="chatflow-canvas-empty"
        className="flex h-full w-full items-center justify-center text-gray-500"
      >
        {t("chatflow.select_chatflow")}
      </div>
    );
  }

  if (Object.keys(chatflow.nodes).length === 0) {
    return (
      <div
        data-testid="chatflow-canvas-empty"
        className="flex h-full w-full items-center justify-center text-gray-500"
      >
        {t("chatflow.empty")}
      </div>
    );
  }

  return (
    <div data-testid="chatflow-canvas" className="relative h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodeClick={handleNodeClick}
        onNodeContextMenu={handleContextMenu}
        onPaneClick={handlePaneClick}
        onNodesChange={onNodesChange}
        onEdgeMouseEnter={(_, edge) =>
          setHoveredEdge({ parent: edge.source, child: edge.target })
        }
        onEdgeMouseLeave={() => setHoveredEdge(null)}
        nodesDraggable
        nodesConnectable={false}
        edgesFocusable={false}
        multiSelectionKeyCode={null}
        selectNodesOnDrag={false}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
        <ModelRibbonLayer chatflow={chatflow} />
      </ReactFlow>

      {hoveredEdge && cursorPos && chatflow && (
        <EdgeModelTooltip
          chatflow={chatflow}
          edge={hoveredEdge}
          x={cursorPos.x}
          y={cursorPos.y}
        />
      )}

      {contextMenu && chatflow && (() => {
        const node = chatflow.nodes[contextMenu.nodeId];
        if (!node) return null;
        const undeletable = computeUndeletableIds(chatflow.nodes);
        const leaves = computeLeafIds(chatflow.nodes);
        const isLeaf = leaves.has(node.id);
        const canDel = !undeletable.has(node.id);

        return (
          <NodeContextMenu
            x={contextMenu.x}
            y={contextMenu.y}
            status={node.status}
            isLeaf={isLeaf}
            canDelete={canDel}
            onEnterWorkflow={() => {
              enterWorkflow(contextMenu.nodeId);
              setContextMenu(null);
            }}
            onRetry={() => {
              void retryNode(contextMenu.nodeId);
              setContextMenu(null);
            }}
            onCancel={() => {
              void cancelNode(contextMenu.nodeId);
              setContextMenu(null);
            }}
            onDelete={() => {
              if (isLeaf) {
                void deleteNode(contextMenu.nodeId);
              } else {
                setPendingDeleteId(contextMenu.nodeId);
              }
              setContextMenu(null);
            }}
            onClose={() => setContextMenu(null)}
          />
        );
      })()}

      {pendingDeleteId && (
        <ConfirmDialog
          message={t("chatflow.delete_cascade_confirm")}
          confirmLabel={t("chatflow.delete")}
          cancelLabel={t("chatflow.cancel_action")}
          onConfirm={() => {
            void deleteNode(pendingDeleteId);
            setPendingDeleteId(null);
          }}
          onCancel={() => setPendingDeleteId(null)}
        />
      )}
    </div>
  );
}

/**
 * Compute the set of node ids that cannot be deleted.
 * A node is undeletable if it is a root (the conversation anchor),
 * if it is RUNNING, or if any of its descendants is RUNNING (which
 * makes it an ancestor of a running node).
 */
function computeUndeletableIds(
  nodes: Record<string, ChatFlowNode>,
): Set<string> {
  const undeletable = new Set<string>();

  // Roots are the conversation's anchor — never deletable.
  for (const [id, node] of Object.entries(nodes)) {
    if (node.parent_ids.length === 0) undeletable.add(id);
  }

  // First, find all running nodes.
  const runningIds: string[] = [];
  for (const [id, node] of Object.entries(nodes)) {
    if (node.status === "running") {
      runningIds.push(id);
      undeletable.add(id);
    }
  }

  // For each running node, walk up the ancestor chain and mark undeletable.
  for (const rid of runningIds) {
    const stack = [...nodes[rid].parent_ids];
    while (stack.length > 0) {
      const pid = stack.pop()!;
      if (undeletable.has(pid)) continue;
      undeletable.add(pid);
      const parent = nodes[pid];
      if (parent) stack.push(...parent.parent_ids);
    }
  }

  return undeletable;
}

function computeLeafIds(nodes: Record<string, ChatFlowNode>): Set<string> {
  const hasChild = new Set<string>();
  for (const node of Object.values(nodes)) {
    for (const pid of node.parent_ids) {
      hasChild.add(pid);
    }
  }
  const leaves = new Set<string>();
  for (const id of Object.keys(nodes)) {
    if (!hasChild.has(id)) leaves.add(id);
  }
  return leaves;
}

/** Sum total_tokens across all WorkNodes in a ChatNode's workflow. */
function nodeTokens(node: ChatFlowNode): number {
  let sum = 0;
  for (const wn of Object.values(node.workflow.nodes)) {
    if (wn.usage) sum += wn.usage.total_tokens;
  }
  return sum;
}

/**
 * For each ChatNode, compute accumulated context tokens from root to
 * that node (following parent_ids[0] as the primary path).
 */
function computeContextTokens(
  nodes: Record<string, ChatFlowNode>,
): Record<string, number> {
  const cache: Record<string, number> = {};

  function walk(id: string): number {
    if (id in cache) return cache[id];
    const node = nodes[id];
    if (!node) return 0;
    const own = nodeTokens(node);
    const parentId = node.parent_ids[0] ?? null;
    const inherited = parentId ? walk(parentId) : 0;
    cache[id] = inherited + own;
    return cache[id];
  }

  for (const id of Object.keys(nodes)) walk(id);
  return cache;
}

/** Look up the context window of a node's resolved model, falling back
 * to the chatflow's default model if the node was spawned before a
 * resolved_model snapshot existed. Returns ``null`` when no window is
 * configured — callers can treat that as "use the default". */
function resolveContextWindow(
  ref: ProviderModelRef | null | undefined,
  defaultRef: ProviderModelRef | null | undefined,
  byModel: Record<string, number>,
): number | null {
  const picks = [ref, defaultRef];
  for (const p of picks) {
    if (!p) continue;
    const key = `${p.provider_id}:${p.model_id}`;
    if (key in byModel) return byModel[key];
  }
  return null;
}

/** Build ``"provider_id:model_id" → context_window`` from a providers
 * list, skipping models that don't declare a window. */
export function contextWindowMap(
  providers: ProviderSummary[],
): Record<string, number> {
  const map: Record<string, number> = {};
  for (const p of providers) {
    for (const m of p.available_models) {
      if (m.context_window != null) {
        map[`${p.id}:${m.id}`] = m.context_window;
      }
    }
  }
  return map;
}

/** Pure function so it can be unit-tested without rendering React Flow. */
export function buildGraph(
  chatflow: ChatFlow | null,
  selectedNodeId: string | null,
  contextWindowByModel: Record<string, number> = {},
): { nodes: Node<ChatFlowNodeData>[]; edges: Edge[] } {
  if (!chatflow) return { nodes: [], edges: [] };
  const laidOut = layoutDag<ChatFlowNode>(chatflow.nodes, chatflow.root_ids);
  const undeletable = computeUndeletableIds(chatflow.nodes);
  const leaves = computeLeafIds(chatflow.nodes);
  const ctxTokens = computeContextTokens(chatflow.nodes);
  const rootSet = new Set(chatflow.root_ids);
  const rfNodes: Node<ChatFlowNodeData>[] = laidOut.map(({ node, position }) => {
    // Prefer server-persisted position over auto-layout.
    const pos =
      node.position_x != null && node.position_y != null
        ? { x: node.position_x, y: node.position_y }
        : position;
    const isRoot = rootSet.has(node.id);
    return {
      id: node.id,
      type: "chatflow",
      position: pos,
      data: {
        node,
        isSelected: node.id === selectedNodeId,
        canDelete: !undeletable.has(node.id),
        isLeaf: leaves.has(node.id),
        isRoot,
        contextTokens: ctxTokens[node.id] ?? 0,
        maxContextTokens: resolveContextWindow(
          node.resolved_model,
          chatflow.default_model,
          contextWindowByModel,
        ),
      },
      selectable: false,
    };
  });

  const rfEdges: Edge[] = [];
  for (const { node } of laidOut) {
    for (const parentId of node.parent_ids) {
      if (!(parentId in chatflow.nodes)) continue;
      const parent = chatflow.nodes[parentId];
      const isMerge = node.parent_ids.length >= 2;
      const isDashed = !parent.status || parent.status === "planned" || node.status === "planned";
      rfEdges.push({
        id: `${parentId}->${node.id}`,
        source: parentId,
        target: node.id,
        animated: node.status === "running",
        style: {
          stroke: isMerge ? "#a855f7" : isDashed ? "#9ca3af" : "#374151",
          strokeDasharray: isDashed ? "6 4" : undefined,
          strokeWidth: isMerge ? 2.5 : 1.5,
        },
      });
    }
  }
  return { nodes: rfNodes, edges: rfEdges };
}

// ---------------------------------------------------------------- Context menu

function NodeContextMenu({
  x,
  y,
  status,
  isLeaf,
  canDelete,
  onEnterWorkflow,
  onRetry,
  onCancel,
  onDelete,
  onClose,
}: {
  x: number;
  y: number;
  status: string;
  isLeaf: boolean;
  canDelete: boolean;
  onEnterWorkflow: () => void;
  onRetry: () => void;
  onCancel: () => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();

  const items: { label: string; onClick: () => void; danger?: boolean }[] = [];

  // Always show "Enter workflow"
  items.push({ label: t("chatflow.ctx_enter_workflow"), onClick: onEnterWorkflow });

  // Status-based actions
  if (status === "failed") {
    items.push({ label: t("chatflow.ctx_retry"), onClick: onRetry });
  }
  if (status === "running") {
    items.push({ label: t("chatflow.ctx_cancel"), onClick: onCancel });
  }

  // Delete
  if (canDelete) {
    items.push({
      label: isLeaf ? t("chatflow.ctx_delete") : t("chatflow.ctx_delete_cascade"),
      onClick: onDelete,
      danger: true,
    });
  }

  return (
    <div
      className="fixed inset-0 z-50"
      onClick={onClose}
      onContextMenu={(e) => { e.preventDefault(); onClose(); }}
    >
      <div
        className="absolute min-w-[160px] rounded-lg border border-gray-200 bg-white py-1 shadow-lg"
        style={{ left: x, top: y }}
        onClick={(e) => e.stopPropagation()}
      >
        {items.map((item, i) => (
          <button
            key={i}
            type="button"
            onClick={item.onClick}
            className={[
              "block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-50",
              item.danger ? "text-red-500 hover:bg-red-50" : "text-gray-700",
            ].join(" ")}
          >
            {item.label}
          </button>
        ))}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Confirm dialog

function ConfirmDialog({
  message,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
}: {
  message: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      data-testid="confirm-dialog-overlay"
      className="absolute inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onCancel}
    >
      <div
        className="w-80 rounded-lg border border-gray-200 bg-white p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="mb-4 text-sm text-gray-700">{message}</p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            data-testid="confirm-dialog-cancel"
            onClick={onCancel}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            data-testid="confirm-dialog-confirm"
            onClick={onConfirm}
            className="rounded bg-red-500 px-3 py-1.5 text-xs text-white hover:bg-red-600"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * Tooltip that appears next to the cursor while an edge is hovered,
 * listing the model used on that edge for each ModelKind. Today there
 * is only one row (`llm`); future per-kind models slot in by extending
 * `MODEL_KINDS` in `effectiveModel.ts`.
 *
 * Positioning is `position: fixed` in viewport coords so the tooltip
 * doesn't get clipped by React Flow's overflow-hidden viewport. The
 * 12px offset keeps it from sitting under the cursor and intercepting
 * the next mouseleave.
 */
function EdgeModelTooltip({
  chatflow,
  edge,
  x,
  y,
}: {
  chatflow: ChatFlow;
  edge: { parent: NodeId; child: NodeId };
  x: number;
  y: number;
}) {
  const { t } = useTranslation();
  const rows = MODEL_KINDS.map((kind) => {
    const m = edgeModel(chatflow, edge.parent, edge.child, kind);
    return {
      kind,
      label: t(`composer_model.kind_${kind}`),
      modelLabel: m ? m.model_id : t("composer_model.button_inherit"),
      color: colorForModel(m),
    };
  });

  return (
    <div
      data-testid="edge-model-tooltip"
      className="pointer-events-none fixed z-50 rounded-md border border-gray-200 bg-white px-2.5 py-1.5 text-[11px] shadow-lg"
      style={{ left: x + 12, top: y + 12 }}
    >
      {rows.map((row) => (
        <div key={row.kind} className="flex items-center gap-1.5 whitespace-nowrap">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: row.color }}
          />
          <span className="text-gray-500">{row.label}</span>
          <span className="font-mono text-gray-800">{row.modelLabel}</span>
        </div>
      ))}
    </div>
  );
}
