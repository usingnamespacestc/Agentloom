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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  applyNodeChanges,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeMouseHandler,
  type OnNodeDrag,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTranslation } from "react-i18next";

import { layoutDag } from "./layout";
import { CanvasContextMenu, StickyNoteContextMenu } from "./CanvasContextMenu";
import { CompactConfirmDialog } from "@/components/CompactConfirmDialog";
import { ChatFlowActiveWorkPanel } from "./ChatFlowActiveWorkPanel";
import { MemoryBoardPanel } from "./MemoryBoardPanel";
import { ModelRibbonLayer } from "./ModelRibbonLayer";
import { MODEL_KINDS, colorForModel, edgeModel } from "./effectiveModel";
import { ChatFlowNodeCard, type ChatFlowNodeData } from "./nodes/ChatFlowNodeCard";
import { ChatBriefNodeCard, type ChatBriefNodeData } from "./nodes/ChatBriefNodeCard";
import { StickyNoteNode, type StickyNoteData } from "./nodes/StickyNoteNode";
import { NODE_HEIGHT } from "./layout";
import { api, type ProviderSummary } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { BoardItem, ChatFlow, ChatFlowNode, NodeId, ProviderModelRef, StickyNote } from "@/types/schema";

interface ContextMenuState {
  nodeId: string;
  x: number;
  y: number;
}

const NODE_TYPES = {
  chatflow: ChatFlowNodeCard,
  chatBrief: ChatBriefNodeCard,
  stickyNote: StickyNoteNode,
};

/** Vertical gap between a ChatNode's centre and its stacked chat-brief
 * node. ChatNode cards are taller than WorkFlow cards (two prose
 * sections + token bar) so this is a bit bigger than WorkFlow's
 * ``STACK_GAP`` in ``layout.ts``. */
const CHAT_BRIEF_STACK_OFFSET = NODE_HEIGHT + 60;

export interface ChatFlowCanvasProps {
  chatflow: ChatFlow | null;
}

export function ChatFlowCanvas(props: ChatFlowCanvasProps) {
  return (
    <ReactFlowProvider>
      <ChatFlowCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function ChatFlowCanvasInner({ chatflow }: ChatFlowCanvasProps) {
  const { t } = useTranslation();
  const selectedNodeId = useChatFlowStore((s) => s.selectedNodeId);
  const selectNode = useChatFlowStore((s) => s.selectNode);
  const enterWorkflow = useChatFlowStore((s) => s.enterWorkflow);
  const deleteNode = useChatFlowStore((s) => s.deleteNode);
  const retryNode = useChatFlowStore((s) => s.retryNode);
  const cancelNode = useChatFlowStore((s) => s.cancelNode);
  const setHoveredEdge = useChatFlowStore((s) => s.setHoveredEdge);
  const hoveredEdge = useChatFlowStore((s) => s.hoveredEdge);
  const pendingMergeFirstId = useChatFlowStore((s) => s.pendingMergeFirstId);
  const beginPendingMerge = useChatFlowStore((s) => s.beginPendingMerge);
  const cancelPendingMerge = useChatFlowStore((s) => s.cancelPendingMerge);
  const commitMergeWith = useChatFlowStore((s) => s.commitMergeWith);

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
  // Manual-compact dialog: when the user picks "Compact from here" in
  // the node context menu and the chatflow requires confirmation, we
  // open CompactConfirmDialog pinned to the chosen node as parent.
  const [compactDialogParentId, setCompactDialogParentId] = useState<string | null>(null);
  const reactFlow = useReactFlow();

  // Sticky notes — persisted via chatflow.sticky_notes
  const [stickyNotes, setStickyNotes] = useState<Record<string, StickyNote>>({});
  const stickyNotesRef = useRef(stickyNotes);
  useEffect(() => { stickyNotesRef.current = stickyNotes; }, [stickyNotes]);
  const isSticky = useCallback((id: string) => id in stickyNotesRef.current, []);
  const [editingStickyId, setEditingStickyId] = useState<string | null>(null);
  const [selectedStickyId, setSelectedStickyId] = useState<string | null>(null);
  const [paneMenu, setPaneMenu] = useState<{ x: number; y: number; flowX: number; flowY: number } | null>(null);
  const stickyDirty = useRef(false);
  const stickyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Sync from backend data on chatflow change
  const lastStickySourceId = useRef<string | null>(null);
  useEffect(() => {
    if (chatflow?.id !== lastStickySourceId.current) {
      lastStickySourceId.current = chatflow?.id ?? null;
      setStickyNotes(chatflow?.sticky_notes ?? {});
    }
  }, [chatflow]);

  const flushStickyNotes = useCallback((notes: Record<string, StickyNote>) => {
    if (!chatflow) return;
    void api.putStickyNotes(chatflow.id, notes);
  }, [chatflow]);

  const scheduleStickyFlush = useCallback((notes: Record<string, StickyNote>) => {
    stickyDirty.current = true;
    if (stickyTimer.current) clearTimeout(stickyTimer.current);
    stickyTimer.current = setTimeout(() => {
      stickyDirty.current = false;
      flushStickyNotes(notes);
    }, 800);
  }, [flushStickyNotes]);

  const updateStickyNote = useCallback((id: string, patch: Partial<StickyNote>) => {
    setStickyNotes((prev) => {
      const existing = prev[id];
      if (!existing) return prev;
      const next = { ...prev, [id]: { ...existing, ...patch } };
      scheduleStickyFlush(next);
      return next;
    });
  }, [scheduleStickyFlush]);

  const onNoteTitleChange = useCallback((id: string, title: string) => updateStickyNote(id, { title }), [updateStickyNote]);
  const onNoteTextChange = useCallback((id: string, text: string) => updateStickyNote(id, { text }), [updateStickyNote]);

  const onNoteDelete = useCallback((id: string) => {
    setStickyNotes((prev) => {
      const { [id]: _, ...rest } = prev;
      scheduleStickyFlush(rest);
      return rest;
    });
  }, [scheduleStickyFlush]);

  const handlePaneContextMenu = useCallback((event: MouseEvent | React.MouseEvent) => {
    event.preventDefault();
    const bounds = (event.currentTarget as HTMLElement).getBoundingClientRect();
    const flowPos = reactFlow.screenToFlowPosition({ x: event.clientX - bounds.left, y: event.clientY - bounds.top });
    setPaneMenu({ x: event.clientX, y: event.clientY, flowX: flowPos.x, flowY: flowPos.y });
  }, [reactFlow]);

  const handleInsertNote = useCallback(() => {
    if (!paneMenu) return;
    const id = `_sticky_${crypto.randomUUID()}`;
    setStickyNotes((prev) => {
      const note: StickyNote = { id, title: "Note", text: "", x: paneMenu.flowX, y: paneMenu.flowY, width: 200, height: 120 };
      const next = { ...prev, [id]: note };
      scheduleStickyFlush(next);
      return next;
    });
  }, [paneMenu, scheduleStickyFlush]);

  // Right-click menu for sticky notes (rendered at canvas level)
  const [stickyCtxMenu, setStickyCtxMenu] = useState<{ x: number; y: number; noteId: string } | null>(null);

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
  const contextWindowByModel = useMemo(
    () => contextWindowMap(providers),
    [providers],
  );

  // ChatBoardItem cache drives the synthetic chat-brief nodes stacked
  // above each ChatNode. Rebuilds the graph whenever a BoardItem is
  // written/updated (SSE refresh, compact/merge follow-ups).
  const boardItems = useChatFlowStore((s) => s.boardItems);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [nodes, setNodes] = useState<Node<any>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  // User-dragged positions survive graph reconciliation (SSE patches,
  // selection changes). They're cleared when a brand-new chatflow is
  // loaded (different chatflow id).
  const dragPositions = useRef<Record<string, { x: number; y: number }>>({});
  const dirtyPositions = useRef<Set<string>>(new Set());
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastChatflowId = useRef<string | null>(null);
  // Mid-drag, SSE-driven ``setNodes([...])`` replacements disrupt React
  // Flow's internal drag state (new Node identities per rebuild) and the
  // final position event often never makes it to ``onNodesChange``.
  // Gate the sync effect while dragging; bump ``syncTick`` on drag stop
  // to force a catch-up re-sync.
  const isDragging = useRef(false);
  const [syncTick, setSyncTick] = useState(0);

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
    if (isDragging.current) return;
    if (chatflow?.id !== lastChatflowId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      lastChatflowId.current = chatflow?.id ?? null;
    }
    const laid = buildGraph(chatflow, selectedNodeId, contextWindowByModel, boardItems);
    const merged = laid.nodes.map((n) => ({
      ...n,
      // Synthetic chat-brief nodes aren't draggable and don't persist
      // positions — they always sit above their source. Everything else
      // honours the stored drag position.
      position:
        n.type === "chatBrief"
          ? n.position
          : (dragPositions.current[n.id] ?? n.position),
    }));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const stickyNodes: Node<any>[] = Object.values(stickyNotes).map((note) => ({
      id: note.id,
      type: "stickyNote",
      position: dragPositions.current[note.id] ?? { x: note.x, y: note.y },
      selected: selectedStickyId === note.id,
      data: {
        title: note.title,
        text: note.text,
        editing: editingStickyId === note.id,
        onTitleChange: onNoteTitleChange,
        onTextChange: onNoteTextChange,
        onDelete: onNoteDelete,
        onExitEdit: () => setEditingStickyId(null),
      } satisfies StickyNoteData,
      style: { width: note.width, height: note.height },
    }));
    setNodes([...merged, ...stickyNodes]);
    setEdges(laid.edges);
  }, [chatflow, selectedNodeId, contextWindowByModel, boardItems, stickyNotes, editingStickyId, selectedStickyId, onNoteTitleChange, onNoteTextChange, onNoteDelete, syncTick]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const filtered = changes.filter((c) => c.type !== "select");
    if (filtered.length === 0) return;
    for (const c of filtered) {
      // Synthetic chat-brief nodes are view-only; skip drag-position
      // bookkeeping so we never try to PATCH a non-ChatNode id.
      const isBriefId =
        c.type === "position" && String(c.id).startsWith(CHAT_BRIEF_NODE_PREFIX);
      if (c.type === "position" && c.position && !isBriefId) {
        dragPositions.current[c.id] = c.position;
        if (isSticky(String(c.id))) {
          updateStickyNote(c.id, { x: c.position.x, y: c.position.y });
        } else {
          dirtyPositions.current.add(c.id);
        }
      }
      if (c.type === "dimensions" && c.dimensions && isSticky(String(c.id))) {
        updateStickyNote(c.id, { width: c.dimensions.width, height: c.dimensions.height });
      }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setNodes((ns) => applyNodeChanges(filtered, ns) as Node<any>[]);

    if (dirtyPositions.current.size > 0) {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPositions, 500);
    }
  }, [flushPositions, updateStickyNote, isSticky]);

  const handleNodeDragStart: OnNodeDrag = useCallback(() => {
    isDragging.current = true;
    // Dragging a node ≠ panning the page — cancels a pending merge per the
    // VSCode-compare handshake rule.
    cancelPendingMerge();
  }, [cancelPendingMerge]);

  // Fallback capture of the final drop position. ``onNodesChange``'s
  // final ``position`` event is often lost when an SSE-driven re-render
  // interrupts the drag — this runs on mouseup regardless.
  const handleNodeDragStop: OnNodeDrag = useCallback((_event, node) => {
    isDragging.current = false;
    dragPositions.current[node.id] = { x: node.position.x, y: node.position.y };
    if (isSticky(String(node.id))) {
      updateStickyNote(node.id, { x: node.position.x, y: node.position.y });
    } else {
      dirtyPositions.current.add(node.id);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPositions, 500);
    }
    setSyncTick((t) => t + 1);
  }, [flushPositions, updateStickyNote, isSticky]);

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    // Synthetic chat-brief nodes are view-only; clicking them is a
    // no-op (they don't map to any ChatNode id in the store).
    if (String(node.id).startsWith(CHAT_BRIEF_NODE_PREFIX)) return;
    if (isSticky(String(node.id))) {
      setSelectedStickyId(node.id);
    } else {
      selectNode(node.id);
      setSelectedStickyId(null);
      setEditingStickyId(null);
    }
    setContextMenu(null);
    // Left-clicking a node counts as an "operation other than panning",
    // which cancels a pending merge per the user-spec'd handshake.
    cancelPendingMerge();
  };

  const handleNodeDoubleClick: NodeMouseHandler = (_event, node) => {
    if (isSticky(String(node.id))) {
      setSelectedStickyId(node.id);
      setEditingStickyId(node.id);
    }
  };

  const handlePaneClickFull = useCallback(() => {
    setContextMenu(null);
    setPaneMenu(null);
    setStickyCtxMenu(null);
    setSelectedStickyId(null);
    setEditingStickyId(null);
    cancelPendingMerge();
  }, [cancelPendingMerge]);

  // Escape also cancels a pending merge — matches VSCode compare
  // where Esc aborts the two-step pick.
  useEffect(() => {
    if (pendingMergeFirstId === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cancelPendingMerge();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pendingMergeFirstId, cancelPendingMerge]);

  const handleContextMenu: NodeMouseHandler = (event, node) => {
    event.preventDefault();
    // Synthetic chat-brief nodes have no actions — suppress the menu.
    if (String(node.id).startsWith(CHAT_BRIEF_NODE_PREFIX)) return;
    if (isSticky(String(node.id))) {
      setStickyCtxMenu({ x: event.clientX, y: event.clientY, noteId: node.id });
    } else {
      setContextMenu({ nodeId: node.id, x: event.clientX, y: event.clientY });
      selectNode(node.id);
    }
  };

  const handlePaneClick = handlePaneClickFull;

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
        onNodeDoubleClick={handleNodeDoubleClick}
        onNodeContextMenu={handleContextMenu}
        onPaneContextMenu={handlePaneContextMenu}
        onPaneClick={handlePaneClick}
        onNodesChange={onNodesChange}
        onNodeDragStart={handleNodeDragStart}
        onNodeDragStop={handleNodeDragStop}
        onEdgeMouseEnter={(_, edge) =>
          setHoveredEdge({ parent: edge.source, child: edge.target })
        }
        onEdgeMouseLeave={() => setHoveredEdge(null)}
        nodesDraggable
        nodesConnectable={false}
        edgesFocusable={false}
        multiSelectionKeyCode={null}
        selectNodesOnDrag={false}
        zoomOnDoubleClick={false}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
        <ModelRibbonLayer chatflow={chatflow} />
        <ChatFlowActiveWorkPanel chatflow={chatflow} />
        <ChatBoardPanel boardItems={boardItems} onJump={selectNode} />
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
        // Compacting a compact node is nonsensical (it's already the
        // summary). Everything else is fair game — server rejects if
        // there's literally nothing to summarise.
        const canCompact = node.compact_snapshot == null;
        // Classify the merge-state for THIS node given the pending state.
        // - no-pending: no merge in flight — show "Select to merge"
        // - first-pending-self: this node IS the pending first — show "Cancel"
        // - first-pending-other: another node is pending — show "Merge with …"
        const mergeState: "no-pending" | "first-pending-self" | "first-pending-other" =
          pendingMergeFirstId === null
            ? "no-pending"
            : pendingMergeFirstId === contextMenu.nodeId
              ? "first-pending-self"
              : "first-pending-other";

        return (
          <NodeContextMenu
            x={contextMenu.x}
            y={contextMenu.y}
            status={node.status}
            isLeaf={isLeaf}
            canDelete={canDel}
            canCompact={canCompact}
            mergeState={mergeState}
            onSelectToMerge={() => {
              beginPendingMerge(contextMenu.nodeId);
              setContextMenu(null);
            }}
            onCommitMerge={() => {
              const secondId = contextMenu.nodeId;
              setContextMenu(null);
              void commitMergeWith(secondId);
            }}
            onCancelPendingMerge={() => {
              cancelPendingMerge();
              setContextMenu(null);
            }}
            onEnterWorkflow={() => {
              enterWorkflow(contextMenu.nodeId);
              setContextMenu(null);
              cancelPendingMerge();
            }}
            onRetry={() => {
              void retryNode(contextMenu.nodeId);
              setContextMenu(null);
              cancelPendingMerge();
            }}
            onCancel={() => {
              void cancelNode(contextMenu.nodeId);
              setContextMenu(null);
              cancelPendingMerge();
            }}
            onCompact={() => {
              const parentId = contextMenu.nodeId;
              setContextMenu(null);
              cancelPendingMerge();
              if (chatflow.compact_require_confirmation ?? true) {
                setCompactDialogParentId(parentId);
                return;
              }
              void (async () => {
                const res = await api.compactChain(chatflow.id, parentId, {});
                selectNode(res.node_id);
              })();
            }}
            onDelete={() => {
              if (isLeaf) {
                void deleteNode(contextMenu.nodeId);
              } else {
                setPendingDeleteId(contextMenu.nodeId);
              }
              setContextMenu(null);
              cancelPendingMerge();
            }}
            onClose={() => setContextMenu(null)}
          />
        );
      })()}

      {compactDialogParentId && chatflow?.nodes[compactDialogParentId] && (
        <CompactConfirmDialog
          open
          onClose={() => setCompactDialogParentId(null)}
          chatflow={chatflow}
          parentNode={chatflow.nodes[compactDialogParentId]}
          onCreated={(nodeId) => selectNode(nodeId)}
        />
      )}

      {paneMenu && (
        <CanvasContextMenu
          x={paneMenu.x}
          y={paneMenu.y}
          onInsertNote={handleInsertNote}
          onClose={() => setPaneMenu(null)}
        />
      )}
      {stickyCtxMenu && (
        <StickyNoteContextMenu
          x={stickyCtxMenu.x}
          y={stickyCtxMenu.y}
          onDelete={() => onNoteDelete(stickyCtxMenu.noteId)}
          onClose={() => setStickyCtxMenu(null)}
        />
      )}

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

/**
 * Chain-context tokens the *next* turn will consume as input if it
 * forks off this ChatNode. Composed as ``entry_prompt_tokens`` (what
 * this turn saw going in) + ``output_response_tokens`` (what this
 * turn contributed as ``agent_response``, which every descendant
 * pays for via ``_build_chat_context``).
 *
 * While the turn is still running ``output_response_tokens`` is
 * ``null``; we show just the entry value so the bar doesn't jump
 * mid-flight. Once the turn finishes the card grows by the output
 * size — which is what the next turn's first WorkNode will see.
 *
 * Legacy fallback (node predates ``entry_prompt_tokens``): use the
 * prompt_tokens of the first root WorkNode to execute (judge_pre in
 * semi_auto / auto, or the initial llm_call in direct mode). That
 * node's prompt is the closest proxy for "what the LLM saw entering
 * this turn" and keeps monotonic growth along the chain. Picking
 * ``max(prompt_tokens)`` would overshoot because judge_post sees the
 * accumulated tool-loop output and blows past the true entry value —
 * producing the inverted display where a legacy ancestor shows more
 * tokens than a freshly-stamped leaf.
 */
function nodeTokens(node: ChatFlowNode): number {
  if (node.entry_prompt_tokens != null) {
    return node.entry_prompt_tokens + (node.output_response_tokens ?? 0);
  }
  for (const rid of node.workflow.root_ids ?? []) {
    const wn = node.workflow.nodes[rid];
    if (wn?.usage) return wn.usage.prompt_tokens;
  }
  let min = Infinity;
  for (const wn of Object.values(node.workflow.nodes)) {
    if (!wn.usage) continue;
    if (wn.usage.prompt_tokens < min) min = wn.usage.prompt_tokens;
  }
  return min === Infinity ? 0 : min;
}

/**
 * Per-node context-token map. No parent walk needed — each ChatNode's
 * first worknode prompt_tokens already accumulates ancestor history.
 */
function computeContextTokens(
  nodes: Record<string, ChatFlowNode>,
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [id, n] of Object.entries(nodes)) out[id] = nodeTokens(n);
  return out;
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

/** Prefix for synthetic chat-brief React Flow node ids. Keeps them
 * outside the ChatNode id-space so selection / drag-position / delete
 * handlers can cheaply skip them. */
export const CHAT_BRIEF_NODE_PREFIX = "_chat_brief_";

/** Sort BoardItems newest-first; undefined ``created_at`` sorts last. */
function sortByCreatedDesc(a: BoardItem, b: BoardItem): number {
  const ta = a.created_at ? Date.parse(a.created_at) : 0;
  const tb = b.created_at ? Date.parse(b.created_at) : 0;
  return tb - ta;
}

/** ChatFlow-layer MemoryBoard panel — lists scope='chat' briefs and
 * jumps to the source ChatNode on click. */
function ChatBoardPanel({
  boardItems,
  onJump,
}: {
  boardItems: Record<NodeId, BoardItem>;
  onJump: (nodeId: NodeId) => void;
}) {
  const { t } = useTranslation();
  const items = useMemo(
    () =>
      Object.values(boardItems)
        .filter((item) => item.scope === "chat")
        .sort(sortByCreatedDesc),
    [boardItems],
  );
  return (
    <MemoryBoardPanel
      testId="chatflow-memoryboard-panel"
      title={t("chatflow.memoryboard_panel_title")}
      emptyText={t("chatflow.memoryboard_panel_empty")}
      fallbackLabel={t("chatflow.chat_brief_fallback_badge")}
      items={items}
      onItemClick={(item) => onJump(item.source_node_id)}
    />
  );
}

/** Pure function so it can be unit-tested without rendering React Flow. */
export function buildGraph(
  chatflow: ChatFlow | null,
  selectedNodeId: string | null,
  contextWindowByModel: Record<string, number> = {},
  boardItems: Record<NodeId, BoardItem> = {},
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): { nodes: Node<any>[]; edges: Edge[] } {
  if (!chatflow) return { nodes: [], edges: [] };
  const laidOut = layoutDag<ChatFlowNode>(chatflow.nodes, chatflow.root_ids);
  const undeletable = computeUndeletableIds(chatflow.nodes);
  const leaves = computeLeafIds(chatflow.nodes);
  const ctxTokens = computeContextTokens(chatflow.nodes);
  const rootSet = new Set(chatflow.root_ids);
  const chatNodePositions = new Map<NodeId, { x: number; y: number }>();
  const rfNodes: Node<ChatFlowNodeData>[] = laidOut.map(({ node, position }) => {
    // Prefer server-persisted position over auto-layout.
    const pos =
      node.position_x != null && node.position_y != null
        ? { x: node.position_x, y: node.position_y }
        : position;
    chatNodePositions.set(node.id, pos);
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
          chatflow.draft_model,
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
        sourceHandle: "main-source",
        targetHandle: "main-target",
        animated: node.status === "running",
        style: {
          stroke: isMerge ? "#a855f7" : isDashed ? "#9ca3af" : "#374151",
          strokeDasharray: isDashed ? "6 4" : undefined,
          strokeWidth: isMerge ? 2.5 : 1.5,
        },
      });
    }
  }

  // Synthetic chat-brief nodes stacked above each ChatNode that has a
  // ``scope='chat'`` BoardItem. The brief text lives on the BoardItem,
  // not on a ChatNode — so these are view-only, don't exist in
  // ``chatflow.nodes``, aren't draggable, and aren't selectable.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const briefNodes: Node<any>[] = [];
  for (const id of Object.keys(chatflow.nodes)) {
    const bi = boardItems[id];
    if (!bi || bi.scope !== "chat") continue;
    const srcPos = chatNodePositions.get(id);
    if (!srcPos) continue;
    const briefId = `${CHAT_BRIEF_NODE_PREFIX}${id}`;
    briefNodes.push({
      id: briefId,
      type: "chatBrief",
      position: { x: srcPos.x, y: srcPos.y - CHAT_BRIEF_STACK_OFFSET },
      data: { sourceNodeId: id } satisfies ChatBriefNodeData,
      selectable: false,
      draggable: false,
    });
    rfEdges.push({
      id: `brief->${id}`,
      source: id,
      target: briefId,
      sourceHandle: "brief-source",
      targetHandle: "brief-target",
      style: { stroke: "#a5b4fc", strokeWidth: 1.25 },
    });
  }
  return { nodes: [...rfNodes, ...briefNodes], edges: rfEdges };
}

// ---------------------------------------------------------------- Context menu

function NodeContextMenu({
  x,
  y,
  status,
  isLeaf,
  canDelete,
  canCompact,
  onEnterWorkflow,
  onRetry,
  onCancel,
  onCompact,
  onDelete,
  onClose,
  mergeState,
  onSelectToMerge,
  onCommitMerge,
  onCancelPendingMerge,
}: {
  x: number;
  y: number;
  status: string;
  isLeaf: boolean;
  canDelete: boolean;
  canCompact: boolean;
  onEnterWorkflow: () => void;
  onRetry: () => void;
  onCancel: () => void;
  onCompact: () => void;
  onDelete: () => void;
  onClose: () => void;
  mergeState: "none" | "first-pending-self" | "first-pending-other" | "no-pending";
  onSelectToMerge: () => void;
  onCommitMerge: () => void;
  onCancelPendingMerge: () => void;
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

  // Merge (VSCode compare-style two-step handshake).
  if (mergeState === "no-pending") {
    items.push({
      label: t("chatflow.ctx_select_to_merge"),
      onClick: onSelectToMerge,
    });
  } else if (mergeState === "first-pending-self") {
    items.push({
      label: t("chatflow.ctx_cancel_pending_merge"),
      onClick: onCancelPendingMerge,
    });
  } else if (mergeState === "first-pending-other") {
    items.push({
      label: t("chatflow.ctx_merge_with_pending"),
      onClick: onCommitMerge,
    });
  }

  // Compact — hide on compact nodes themselves.
  if (canCompact) {
    items.push({ label: t("chatflow.ctx_compact"), onClick: onCompact });
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
