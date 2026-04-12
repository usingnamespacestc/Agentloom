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
import { ChatFlowNodeCard, type ChatFlowNodeData } from "./nodes/ChatFlowNodeCard";
import { api } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlow, ChatFlowNode } from "@/types/schema";

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
    const laid = buildGraph(chatflow, selectedNodeId);
    const merged = laid.nodes.map((n) => ({
      ...n,
      position: dragPositions.current[n.id] ?? n.position,
    }));
    setNodes(merged);
    setEdges(laid.edges);
  }, [chatflow, selectedNodeId]);

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
  };

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
        onNodesChange={onNodesChange}
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
      </ReactFlow>

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
 * A node is undeletable if it is RUNNING, or if any of its
 * descendants is RUNNING (which makes it an ancestor of a running node).
 */
function computeUndeletableIds(
  nodes: Record<string, ChatFlowNode>,
): Set<string> {
  const undeletable = new Set<string>();

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

/** Pure function so it can be unit-tested without rendering React Flow. */
export function buildGraph(
  chatflow: ChatFlow | null,
  selectedNodeId: string | null,
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
    return {
      id: node.id,
      type: "chatflow",
      position: pos,
      data: {
        node,
        isSelected: node.id === selectedNodeId,
        canDelete: !undeletable.has(node.id),
        isLeaf: leaves.has(node.id),
        isRoot: rootSet.has(node.id),
        contextTokens: ctxTokens[node.id] ?? 0,
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
