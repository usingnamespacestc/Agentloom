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
  MarkerType,
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
import { PackConfirmDialog } from "@/components/PackConfirmDialog";
import { ChatFlowActiveWorkPanel } from "./ChatFlowActiveWorkPanel";
import { MemoryBoardPanel } from "./MemoryBoardPanel";
import { ModelRibbonLayer } from "./ModelRibbonLayer";
import { MODEL_KINDS, colorForModel, edgeModel } from "./effectiveModel";
import { ChatFlowNodeCard, type ChatFlowNodeData } from "./nodes/ChatFlowNodeCard";
import { ChatBriefNodeCard, type ChatBriefNodeData } from "./nodes/ChatBriefNodeCard";
import { ChatFoldNodeCard, type ChatFoldNodeData } from "./nodes/ChatFoldNodeCard";
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
  chatFold: ChatFoldNodeCard,
  stickyNote: StickyNoteNode,
};

/** Fixed vertical gap between a chat-brief's bottom edge and its source
 * ChatNode's top edge. The brief grows downward from ``srcY - GAP``
 * and its top floats upward as the content grows, so the gap is
 * constant regardless of brief height (no more overlap as briefs get
 * long). */
const CHAT_BRIEF_BOTTOM_GAP = 30;
/** Fallback brief height used until React Flow measures the actual
 * rendered card. Matches the approximate first-render height of a
 * two-line brief. */
const CHAT_BRIEF_FALLBACK_HEIGHT = 160;
/** Initial-layout stack offset used in ``buildGraph`` before the
 * component's measured-height ref takes over. Kept roughly at the old
 * value so first-frame positions still land above the source. */
const CHAT_BRIEF_INITIAL_OFFSET = NODE_HEIGHT + 60;

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
  const pendingPackStartId = useChatFlowStore((s) => s.pendingPackStartId);
  const beginPendingPack = useChatFlowStore((s) => s.beginPendingPack);
  const cancelPendingPack = useChatFlowStore((s) => s.cancelPendingPack);
  const foldChatNode = useChatFlowStore((s) => s.foldChatNode);
  const unfoldChatNode = useChatFlowStore((s) => s.unfoldChatNode);
  const setFoldPosition = useChatFlowStore((s) => s.setFoldPosition);
  const foldPositions = useChatFlowStore((s) => s.foldPositions);

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
  // Pack two-pick range selection resolves to this pair and opens
  // the PackConfirmDialog. Derived by beginPendingPack (start) +
  // the "pack to here" menu item (end).
  const [packDialogPair, setPackDialogPair] = useState<{
    startId: string;
    endId: string;
  } | null>(null);
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
  const foldedChatNodeIds = useChatFlowStore((s) => s.foldedChatNodeIds);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [nodes, setNodes] = useState<Node<any>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  // User-dragged positions survive graph reconciliation (SSE patches,
  // selection changes). They're cleared when a brand-new chatflow is
  // loaded (different chatflow id).
  const dragPositions = useRef<Record<string, { x: number; y: number }>>({});
  const dirtyPositions = useRef<Set<string>>(new Set());
  /** Last measured height of each chat-brief, keyed by brief id. Drives
   * the bubble's vertical position so its **bottom** (not top) stays
   * ``CHAT_BRIEF_BOTTOM_GAP`` above the source's top, regardless of how
   * tall the brief text becomes. */
  const briefHeights = useRef<Record<string, number>>({});
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

  // Emergency sync flush for pagehide / tab switch: the 500ms debounce
  // above batches drags nicely during interactive use, but if the user
  // reloads the tab (or hits browser back) inside that window, the PATCH
  // never goes out and the drag is lost on the next load. fetch with
  // ``keepalive: true`` is guaranteed to complete even as the page is
  // unloading, so flush synchronously here without touching the debounce.
  // navigator.sendBeacon is the canonical tool for this but only speaks
  // POST; our positions endpoint is PATCH.
  useEffect(() => {
    const emergencyFlush = () => {
      if (!chatflow || dirtyPositions.current.size === 0) return;
      const positions = [...dirtyPositions.current]
        .map((id) => {
          const pos = dragPositions.current[id];
          return pos ? { id, x: pos.x, y: pos.y } : null;
        })
        .filter(Boolean) as { id: string; x: number; y: number }[];
      if (positions.length === 0) return;
      dirtyPositions.current.clear();
      try {
        fetch(`/api/chatflows/${chatflow.id}/positions`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ positions }),
          keepalive: true,
        });
      } catch {
        // The unload path is best-effort; a failed fetch here isn't
        // actionable and there's no UI left to surface it on.
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") emergencyFlush();
    };
    window.addEventListener("pagehide", emergencyFlush);
    window.addEventListener("beforeunload", emergencyFlush);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("pagehide", emergencyFlush);
      window.removeEventListener("beforeunload", emergencyFlush);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [chatflow]);

  useEffect(() => {
    if (isDragging.current) return;
    if (chatflow?.id !== lastChatflowId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      briefHeights.current = {};
      lastChatflowId.current = chatflow?.id ?? null;
    }
    const laid = buildGraph(
      chatflow,
      selectedNodeId,
      contextWindowByModel,
      boardItems,
      foldedChatNodeIds,
      foldPositions,
    );
    // Two-pass position merge: first resolve each real ChatNode's
    // effective position (drag override wins over laid-out), then snap
    // every brief to ``source.effective + offset`` so briefs behave
    // like bubbles attached to their parent — one-to-one, always in
    // the same relative slot.
    const effectivePos = new Map<string, { x: number; y: number }>();
    for (const n of laid.nodes) {
      if (n.type === "chatBrief") continue;
      effectivePos.set(n.id, dragPositions.current[n.id] ?? n.position);
    }
    const merged = laid.nodes.map((n) => {
      if (n.type === "chatBrief") {
        const srcId = (n.data as ChatBriefNodeData).sourceNodeId;
        const srcPos = effectivePos.get(srcId);
        if (srcPos) {
          const h = briefHeights.current[n.id] ?? CHAT_BRIEF_FALLBACK_HEIGHT;
          return {
            ...n,
            position: { x: srcPos.x, y: srcPos.y - CHAT_BRIEF_BOTTOM_GAP - h },
          };
        }
        return n;
      }
      return { ...n, position: effectivePos.get(n.id) ?? n.position };
    });
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
  }, [chatflow, selectedNodeId, contextWindowByModel, boardItems, foldedChatNodeIds, foldPositions, stickyNotes, editingStickyId, selectedStickyId, onNoteTitleChange, onNoteTextChange, onNoteDelete, syncTick]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const filtered = changes.filter((c) => c.type !== "select");
    if (filtered.length === 0) return;
    for (const c of filtered) {
      // Synthetic chat-brief nodes are computed from their source
      // position each tick — skip drag bookkeeping entirely.
      // Synthetic chat-fold nodes are user-draggable but ephemeral:
      // record into ``dragPositions.current`` so the new position
      // sticks across rebuilds, but skip ``dirtyPositions`` so we
      // never try to PATCH a non-ChatNode id.
      if (c.type === "position" && c.position) {
        const cid = String(c.id);
        const isBriefId = cid.startsWith(CHAT_BRIEF_NODE_PREFIX);
        const isFoldId = cid.startsWith(CHAT_FOLD_NODE_PREFIX);
        if (!isBriefId) {
          dragPositions.current[c.id] = c.position;
          if (isSticky(cid)) {
            updateStickyNote(c.id, { x: c.position.x, y: c.position.y });
          } else if (!isFoldId) {
            dirtyPositions.current.add(c.id);
          }
        }
      }
      if (c.type === "dimensions" && c.dimensions) {
        const id = String(c.id);
        if (id.startsWith(CHAT_BRIEF_NODE_PREFIX)) {
          // Brief's rendered height changed — record it and bump the
          // sync tick so the next layout pass repositions the brief
          // with its bottom ``CHAT_BRIEF_BOTTOM_GAP`` above the source.
          const prev = briefHeights.current[id];
          if (prev !== c.dimensions.height) {
            briefHeights.current[id] = c.dimensions.height;
            setSyncTick((t) => t + 1);
          }
        } else if (isSticky(id)) {
          updateStickyNote(c.id, { width: c.dimensions.width, height: c.dimensions.height });
        }
      }
    }
    // Keep briefs glued to their source while the source is being dragged:
    // React Flow only emits position changes for the dragged node, so
    // without this we'd get a laggy brief that snaps only on drag-stop.
    const sourceMoves = new Map<string, { x: number; y: number }>();
    for (const c of filtered) {
      if (c.type === "position" && c.position) {
        sourceMoves.set(String(c.id), c.position);
      }
    }
    setNodes((ns) => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const after = applyNodeChanges(filtered, ns) as Node<any>[];
      if (sourceMoves.size === 0) return after;
      return after.map((n) => {
        if (n.type !== "chatBrief") return n;
        const srcId = (n.data as ChatBriefNodeData).sourceNodeId;
        const src = sourceMoves.get(srcId);
        if (!src) return n;
        const h =
          n.measured?.height ??
          briefHeights.current[n.id] ??
          CHAT_BRIEF_FALLBACK_HEIGHT;
        return {
          ...n,
          position: { x: src.x, y: src.y - CHAT_BRIEF_BOTTOM_GAP - h },
        };
      });
    });

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
    const nid = String(node.id);
    const isBrief = nid.startsWith(CHAT_BRIEF_NODE_PREFIX);
    const isFold = nid.startsWith(CHAT_FOLD_NODE_PREFIX);
    // Record the final drop for every draggable node except briefs
    // (briefs recompute from source each tick — storing their
    // position would clobber the follow-the-source behaviour).
    if (!isBrief) {
      dragPositions.current[node.id] = { x: node.position.x, y: node.position.y };
    }
    if (isSticky(nid)) {
      updateStickyNote(node.id, { x: node.position.x, y: node.position.y });
    } else if (isFold) {
      // Fold nodes: persist to the store (→ localStorage) so the
      // placement survives refresh. Host id = fold id stripped of
      // the synthetic prefix.
      const hostId = nid.slice(CHAT_FOLD_NODE_PREFIX.length);
      setFoldPosition(hostId, {
        x: node.position.x,
        y: node.position.y,
      });
    } else if (!isBrief) {
      // Real ChatNode: PATCH back to the server through dirty queue.
      dirtyPositions.current.add(node.id);
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPositions, 500);
    }
    setSyncTick((t) => t + 1);
  }, [flushPositions, updateStickyNote, isSticky, setFoldPosition]);

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    // Synthetic chat-brief / chat-fold nodes are view-only; clicking
    // them is a no-op (they don't map to any ChatNode id in the store).
    if (String(node.id).startsWith(CHAT_BRIEF_NODE_PREFIX)) return;
    if (String(node.id).startsWith(CHAT_FOLD_NODE_PREFIX)) return;
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
    // Synthetic fold nodes expose only one action: unfold. Route
    // through the store directly instead of popping the full node
    // menu so the user isn't offered irrelevant items (retry / merge /
    // compact). The host id lives on the fold's data payload.
    if (String(node.id).startsWith(CHAT_FOLD_NODE_PREFIX)) {
      const hostId = (node.data as ChatFoldNodeData | undefined)?.hostId;
      if (hostId) unfoldChatNode(hostId);
      return;
    }
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
        <ChatBoardPanel
          chatflow={chatflow}
          boardItems={boardItems}
          foldedChatNodeIds={foldedChatNodeIds}
          onJump={selectNode}
        />
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
        const packState: "no-pending" | "first-pending-self" | "first-pending-other" =
          pendingPackStartId === null
            ? "no-pending"
            : pendingPackStartId === contextMenu.nodeId
              ? "first-pending-self"
              : "first-pending-other";
        // Fold is offered on pack / compact hosts only — regular turn
        // nodes have no well-defined range to collapse. The text flips
        // based on whether THIS host is currently folded.
        const isFoldableHost =
          node.pack_snapshot != null || node.compact_snapshot != null;
        const foldState: "none" | "fold" | "unfold" = !isFoldableHost
          ? "none"
          : foldedChatNodeIds.has(contextMenu.nodeId)
            ? "unfold"
            : "fold";

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
            packState={packState}
            onPackStart={() => {
              beginPendingPack(contextMenu.nodeId);
              setContextMenu(null);
            }}
            onPackToHere={() => {
              const endId = contextMenu.nodeId;
              const startId = pendingPackStartId;
              setContextMenu(null);
              if (startId) {
                setPackDialogPair({ startId, endId });
              }
            }}
            onCancelPendingPack={() => {
              cancelPendingPack();
              setContextMenu(null);
            }}
            foldState={foldState}
            onFold={() => {
              foldChatNode(contextMenu.nodeId);
              setContextMenu(null);
            }}
            onUnfold={() => {
              unfoldChatNode(contextMenu.nodeId);
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

      {packDialogPair && chatflow && (
        <PackConfirmDialog
          open
          onClose={() => setPackDialogPair(null)}
          chatflow={chatflow}
          startId={packDialogPair.startId}
          endId={packDialogPair.endId}
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

/** Sort BoardItems oldest-first so the list reads top→bottom in the
 * same direction the canvas flows — later turns sit at the bottom,
 * matching the user's reading order. */
function sortByCreatedAsc(a: BoardItem, b: BoardItem): number {
  const ta = a.created_at ? Date.parse(a.created_at) : 0;
  const tb = b.created_at ? Date.parse(b.created_at) : 0;
  return ta - tb;
}

/** ChatFlow-layer MemoryBoard panel — lists scope='chat' briefs and
 * jumps to the source ChatNode on click. Route-B semantics: mirror
 * the canvas fold state. Any item whose ``source_node_id`` is hidden
 * by a currently-active fold is filtered out — fold IS the viewpoint
 * gesture, panel follows. */
function ChatBoardPanel({
  chatflow,
  boardItems,
  foldedChatNodeIds,
  onJump,
}: {
  chatflow: ChatFlow;
  boardItems: Record<NodeId, BoardItem>;
  foldedChatNodeIds: Set<NodeId>;
  onJump: (nodeId: NodeId) => void;
}) {
  const { t } = useTranslation();
  const hiddenSet = useMemo(
    () => computeFoldProjection(chatflow, foldedChatNodeIds).hidden,
    [chatflow, foldedChatNodeIds],
  );
  const items = useMemo(
    () =>
      Object.values(boardItems)
        .filter((item) => item.scope === "chat")
        .filter((item) => !hiddenSet.has(item.source_node_id))
        .sort(sortByCreatedAsc),
    [boardItems, hiddenSet],
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
/** Prefix for synthetic fold node IDs. Each effective fold injects
 * one ``chatFold`` rfNode (id = prefix + hostId). Kept distinct from
 * real ChatNode ids so store lookups never collide. */
export const CHAT_FOLD_NODE_PREFIX = "_chat_fold_";

/** Fold-state projection. For each hidden ChatNode, records the
 * synthetic fold node id that represents it on the canvas. The fold
 * node is positioned upstream of its host (a pack / compact ChatNode);
 * the host itself stays visible and retains its own card.
 * ``lastMemberByFold`` lets edge re-route tell apart "fork from the
 * last range member" (attach to host via fold's right handle) from
 * "fork from an earlier member" (attach via fold's top handle).
 * ``nestedInnerByOuter`` records strict-nested fold pairs so the
 * rendering layer can style the crossing edge as a containment link. */
export interface FoldProjection {
  hidden: Set<NodeId>;
  /** hidden ChatNode id → synthetic fold node id. */
  foldByHidden: Map<NodeId, NodeId>;
  /** synthetic fold node id → the host (compact/pack) ChatNode id. */
  hostByFold: Map<NodeId, NodeId>;
  /** synthetic fold node id → how many ChatNodes it currently hides. */
  countByFold: Map<NodeId, number>;
  /** synthetic fold node id → the last range member (the one that's
   * adjacent to the host in the original chain). Used to route
   * "boundary forks" to fold.right and "interior forks" to fold.top. */
  lastMemberByFold: Map<NodeId, NodeId>;
  /** outer fold id → set of inner fold ids whose range sits as a
   * convex prefix/suffix of the outer's walk. Each inner fold gets
   * its own rfNode and claims its own range; the chain-continuation
   * edge crossing the outer/inner boundary is the visual containment
   * link and is styled distinctly (dashed + muted) in buildGraph. */
  nestedInnersByOuter: Map<NodeId, Set<NodeId>>;
}

/** Return ``true`` iff the members of ``innerRange`` occupy a
 * contiguous PREFIX or SUFFIX of ``outerWalk`` — i.e. the "outer
 * minus inner" members are still contiguous on the walk. If inner
 * lives in the middle of the walk, subtracting it leaves two disjoint
 * outer segments, and split attribution would then project a
 * directed cycle into the graph (chain edges cross outer↔inner twice
 * with opposite directions). This function is the cycle-safety gate. */
function innerOccupiesEndOfOuter(
  outerWalk: NodeId[],
  innerRange: Set<NodeId>,
): boolean {
  if (innerRange.size === 0) return true;
  let firstInner = -1;
  let lastInner = -1;
  for (let i = 0; i < outerWalk.length; i++) {
    if (innerRange.has(outerWalk[i])) {
      if (firstInner === -1) firstInner = i;
      lastInner = i;
    }
  }
  if (firstInner === -1) return true; // inner not in walk (shouldn't happen if ⊆)
  // Inner slots must be contiguous in the walk.
  for (let i = firstInner; i <= lastInner; i++) {
    if (!innerRange.has(outerWalk[i])) return false;
  }
  // And the contiguous inner block must touch an end (prefix or suffix).
  return firstInner === 0 || lastInner === outerWalk.length - 1;
}

/** Returns the walk order this fold's range was computed from, in the
 * same sequence (host.parent_ids[0] as walk[0], walking up). For pack
 * this mirrors ``packed_range`` REVERSED so position 0 is nearest the
 * host (matching compact's walk[0] = immediate parent). Used by
 * ``innerOccupiesEndOfOuter`` so two fold walks can be compared on
 * the same axis. */
function foldWalkOrder(
  chatflow: ChatFlow,
  hostId: NodeId,
  range: Set<NodeId>,
): NodeId[] {
  const host = chatflow.nodes[hostId];
  if (!host) return [];
  if (host.pack_snapshot) {
    // packed_range is [oldest, …, newest-before-host]; walk[0] wants
    // "nearest host" so reverse.
    const reversed = [...host.pack_snapshot.packed_range].reverse();
    return reversed.filter((nid) => range.has(nid));
  }
  // compact (or any other chain host): walk primary-parent up. Stop at
  // merge boundaries (same rule as _build_chat_context).
  const out: NodeId[] = [];
  let current: NodeId | undefined = host.parent_ids[0];
  while (current !== undefined) {
    if (!range.has(current)) break;
    out.push(current);
    const anc: ChatFlowNode | undefined = chatflow.nodes[current];
    if (!anc) break;
    if (anc.parent_ids.length >= 2) break;
    current = anc.parent_ids[0];
  }
  return out;
}

/** Compute the fold projection. Pack ranges are contiguous primary-
 * parent chains; compact ranges walk primary-parent up to (but not
 * across) a merge. A fold is only *effective* when the host itself is
 * visible — if packA is inside packB's range and both are folded,
 * packA drops out of attribution.
 *
 * Attribution strategy:
 * - **Strict nested**: inner.range ⊆ outer.range, inner.host ∉
 *   outer.range, and inner occupies a convex prefix/suffix of outer's
 *   walk. Split attribution — outer claims outer.range \ inner.range,
 *   inner claims its full range. Both folds emit rfNodes; the
 *   outer→inner crossing edge visualises containment.
 * - **Otherwise (partial overlap, non-convex inner-in-middle, or no
 *   nesting)**: largest-range-wins greedy. Avoids cycles from DAG
 *   quotient by non-convex classes (see
 *   ``feedback_dag_projection_cycles.md``).
 *
 * Post-attribution: drop any fold whose effective claim came out
 * empty (partial-overlap loser). Those would render as orphan fold
 * cards with no in/out edges otherwise. */
export function computeFoldProjection(
  chatflow: ChatFlow,
  foldedChatNodeIds: Set<NodeId>,
): FoldProjection {
  // Pass 1: raw range per fold.
  const rangesByFold = new Map<NodeId, Set<NodeId>>();
  for (const foldId of foldedChatNodeIds) {
    const node = chatflow.nodes[foldId];
    if (!node) continue;
    if (node.pack_snapshot) {
      rangesByFold.set(foldId, new Set(node.pack_snapshot.packed_range));
      continue;
    }
    if (node.compact_snapshot) {
      const ancestors = new Set<NodeId>();
      let current: NodeId | undefined = node.parent_ids[0];
      while (current !== undefined) {
        const anc: ChatFlowNode | undefined = chatflow.nodes[current];
        if (!anc) break;
        if (anc.parent_ids.length >= 2) break;
        ancestors.add(current);
        current = anc.parent_ids[0];
      }
      rangesByFold.set(foldId, ancestors);
    }
  }

  // Pass 2: raw hidden union — folds whose host is inside another
  // fold's range become ineffective (the outer fold absorbs them).
  const rawHidden = new Set<NodeId>();
  for (const range of rangesByFold.values()) {
    for (const id of range) rawHidden.add(id);
  }
  const effective = new Map<NodeId, Set<NodeId>>();
  for (const [hostId, range] of rangesByFold) {
    if (!rawHidden.has(hostId)) effective.set(hostId, range);
  }

  // Pass 3: detect strict-nested pairs (inner ⊆ outer, inner.host not
  // in outer.range, inner's range occupies a convex prefix/suffix of
  // outer's walk). For each inner, pick the SMALLEST outer containing
  // it (most specific) — so chains of nesting attribute inside-out.
  const innerToOuter = new Map<NodeId, NodeId>();
  const effectiveEntries = [...effective.entries()];
  for (const [innerHost, innerRange] of effectiveEntries) {
    let chosenOuter: NodeId | null = null;
    let chosenOuterSize = Infinity;
    for (const [outerHost, outerRange] of effectiveEntries) {
      if (outerHost === innerHost) continue;
      if (innerRange.size >= outerRange.size) continue;
      // innerRange ⊆ outerRange
      let isSubset = true;
      for (const n of innerRange) {
        if (!outerRange.has(n)) {
          isSubset = false;
          break;
        }
      }
      if (!isSubset) continue;
      // Inner host must remain visible (i.e., not part of outer's range).
      if (outerRange.has(innerHost)) continue;
      // Inner must occupy a convex end of outer's walk (prefix or suffix).
      const outerWalk = foldWalkOrder(chatflow, outerHost, outerRange);
      if (!innerOccupiesEndOfOuter(outerWalk, innerRange)) continue;
      // Prefer the smallest qualifying outer so nested chains work.
      if (outerRange.size < chosenOuterSize) {
        chosenOuter = outerHost;
        chosenOuterSize = outerRange.size;
      }
    }
    if (chosenOuter !== null) innerToOuter.set(innerHost, chosenOuter);
  }
  const outerToInners = new Map<NodeId, Set<NodeId>>();
  for (const [inner, outer] of innerToOuter) {
    if (!outerToInners.has(outer)) outerToInners.set(outer, new Set());
    outerToInners.get(outer)!.add(inner);
  }

  // Pass 4: compute each fold's effective claim.
  //
  // For outer folds that have inner folds: claim = range \ (∪ inner
  // ranges). Because inner's members overlap subsequent larger outers
  // only in exactly the "contained" region, this subtraction preserves
  // convexity of outer's claim and recursion works naturally — deeper
  // inners claim first, shallower outers claim the remainder.
  //
  // For orphan folds with no nesting: fall back to largest-first
  // greedy on the remaining hidden nodes.
  const claimByFold = new Map<NodeId, Set<NodeId>>();
  // First: every fold with a nesting relationship gets its baseline
  // claim computed (range minus its direct inners' ranges).
  for (const [hostId, range] of effective) {
    const directInners = outerToInners.get(hostId);
    if (!directInners) {
      claimByFold.set(hostId, new Set(range));
      continue;
    }
    const claim = new Set(range);
    for (const innerHost of directInners) {
      const innerRange = effective.get(innerHost);
      if (!innerRange) continue;
      for (const n of innerRange) claim.delete(n);
    }
    claimByFold.set(hostId, claim);
  }

  // Second: resolve overlaps BETWEEN claims (e.g. two sibling folds
  // that are both inside the same outer and partially overlap each
  // other, or two non-nested folds with partial overlap). Use largest-
  // first greedy on claim size to break ties deterministically.
  const foldByHidden = new Map<NodeId, NodeId>();
  const sortedClaims = [...claimByFold.entries()].sort((a, b) => {
    const diff = b[1].size - a[1].size;
    if (diff !== 0) return diff;
    return a[0].localeCompare(b[0]);
  });
  for (const [hostId, claim] of sortedClaims) {
    const foldId = `${CHAT_FOLD_NODE_PREFIX}${hostId}`;
    for (const id of claim) {
      if (!foldByHidden.has(id)) foldByHidden.set(id, foldId);
    }
  }

  // Pass 5: assemble output. Drop folds whose effective claim came out
  // empty (nothing got attributed to them — would render as orphan).
  const hostByFold = new Map<NodeId, NodeId>();
  const countByFold = new Map<NodeId, number>();
  const lastMemberByFold = new Map<NodeId, NodeId>();
  const nestedInnersByOuter = new Map<NodeId, Set<NodeId>>();
  const claimedByFold = new Map<NodeId, Set<NodeId>>();
  for (const [hiddenId, foldId] of foldByHidden) {
    if (!claimedByFold.has(foldId)) claimedByFold.set(foldId, new Set());
    claimedByFold.get(foldId)!.add(hiddenId);
  }
  for (const [hostId] of effective) {
    const foldId = `${CHAT_FOLD_NODE_PREFIX}${hostId}`;
    const claim = claimedByFold.get(foldId);
    if (!claim || claim.size === 0) continue; // orphan — don't emit rfNode
    hostByFold.set(foldId, hostId);
    countByFold.set(foldId, claim.size);
    const hostNode = chatflow.nodes[hostId];
    const lastMember = hostNode?.parent_ids[0];
    if (lastMember && claim.has(lastMember)) {
      lastMemberByFold.set(foldId, lastMember);
    }
  }
  // Translate outer→inner map from host ids to fold node ids. Only keep
  // pairs where BOTH folds survived the empty-claim filter.
  for (const [outerHost, inners] of outerToInners) {
    const outerFoldId = `${CHAT_FOLD_NODE_PREFIX}${outerHost}`;
    if (!hostByFold.has(outerFoldId)) continue;
    const innerSet = new Set<NodeId>();
    for (const innerHost of inners) {
      const innerFoldId = `${CHAT_FOLD_NODE_PREFIX}${innerHost}`;
      if (hostByFold.has(innerFoldId)) innerSet.add(innerFoldId);
    }
    if (innerSet.size > 0) nestedInnersByOuter.set(outerFoldId, innerSet);
  }

  const hidden = new Set(foldByHidden.keys());
  return {
    hidden,
    foldByHidden,
    hostByFold,
    countByFold,
    lastMemberByFold,
    nestedInnersByOuter,
  };
}

export function buildGraph(
  chatflow: ChatFlow | null,
  selectedNodeId: string | null,
  contextWindowByModel: Record<string, number> = {},
  boardItems: Record<NodeId, BoardItem> = {},
  foldedChatNodeIds: Set<NodeId> = new Set(),
  foldPositions: Record<NodeId, { x: number; y: number }> = {},
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
): { nodes: Node<any>[]; edges: Edge[] } {
  if (!chatflow) return { nodes: [], edges: [] };
  const fold = computeFoldProjection(chatflow, foldedChatNodeIds);
  const laidOut = layoutDag<ChatFlowNode>(chatflow.nodes, chatflow.root_ids);
  const undeletable = computeUndeletableIds(chatflow.nodes);
  const leaves = computeLeafIds(chatflow.nodes);
  const ctxTokens = computeContextTokens(chatflow.nodes);
  const rootSet = new Set(chatflow.root_ids);
  // Parents of pack ChatNodes need a bottom source handle; every other
  // node suppresses it to avoid a visible-but-unconnected dot.
  const parentsOfPack = new Set<NodeId>();
  for (const n of Object.values(chatflow.nodes)) {
    if (n.pack_snapshot == null) continue;
    const pid = n.parent_ids[0];
    if (pid) parentsOfPack.add(pid);
  }
  // Pack ChatNodes drop **below** their parent (the last packed node)
  // instead of flowing to the right. We override layoutDag's output
  // here: same x as parent, y offset by one node-height + gap so the
  // edge bottom→top edge has somewhere to land. Only kicks in when
  // the pack has no persisted position (first render / user hasn't
  // dragged it).
  const PACK_BELOW_GAP = 60;
  const autoLayoutPosById = new Map<NodeId, { x: number; y: number }>();
  for (const { node, position } of laidOut) {
    autoLayoutPosById.set(node.id, position);
  }
  const packOverride = new Map<NodeId, { x: number; y: number }>();
  for (const node of Object.values(chatflow.nodes)) {
    if (node.pack_snapshot == null) continue;
    if (node.position_x != null && node.position_y != null) continue;
    const parentId = node.parent_ids[0];
    if (!parentId) continue;
    const parentPos = autoLayoutPosById.get(parentId);
    if (!parentPos) continue;
    // ``NODE_HEIGHT`` in layout.ts is 120; reuse a conservative 160
    // here to account for the pack card's summary body stretching
    // the height.
    packOverride.set(node.id, {
      x: parentPos.x,
      y: parentPos.y + 160 + PACK_BELOW_GAP,
    });
  }
  const chatNodePositions = new Map<NodeId, { x: number; y: number }>();
  const rfNodes: Node<ChatFlowNodeData>[] = [];
  for (const { node, position } of laidOut) {
    // Hidden nodes disappear from the canvas — their content is
    // represented by the synthetic fold node injected below.
    if (fold.hidden.has(node.id)) continue;
    const pos =
      node.position_x != null && node.position_y != null
        ? { x: node.position_x, y: node.position_y }
        : packOverride.get(node.id) ?? position;
    chatNodePositions.set(node.id, pos);
    const isRoot = rootSet.has(node.id);
    rfNodes.push({
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
        hasPackChild: parentsOfPack.has(node.id),
      },
      selectable: false,
    });
  }

  // Synthetic fold nodes — one per active fold. Each sits upstream of
  // its host (compact/pack), occupying the slot the first hidden
  // ancestor used to take. Host itself stays visible; fold absorbs
  // the range on both canvas and edge routing.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const foldNodes: Node<any>[] = [];
  const FOLD_WIDTH = 160;
  const FOLD_GAP = 40;
  for (const [foldId, hostId] of fold.hostByFold) {
    const hostPos = chatNodePositions.get(hostId);
    if (!hostPos) continue;
    const host = chatflow.nodes[hostId];
    const hostKind: "compact" | "pack" = host?.pack_snapshot
      ? "pack"
      : "compact";
    // Position: persisted user-drag (from the store's foldPositions
    // map, keyed by host id) wins; else default to directly left of
    // the host card with a small gap. When the host is a pack (drops
    // below its parent), we anchor at the host's y so the fold still
    // reads as "upstream of pack".
    const foldPos = foldPositions[hostId] ?? {
      x: hostPos.x - FOLD_WIDTH - FOLD_GAP,
      y: hostPos.y,
    };
    foldNodes.push({
      id: foldId,
      type: "chatFold",
      position: foldPos,
      data: {
        hostId,
        hostKind,
        foldedCount: fold.countByFold.get(foldId) ?? 0,
      } satisfies ChatFoldNodeData,
      // Selection stays off (right-click is the only interaction), but
      // drag is allowed so users can reposition the fold to declutter
      // crowded upstream layouts. Position is ephemeral — stored in
      // ``dragPositions.current`` without hitting the backend.
      selectable: false,
      draggable: true,
    });
  }

  // Edge re-route through the synthetic fold nodes. The category of an
  // edge (src→dst) in the original graph is resolved to three cases:
  //
  //   - Both endpoints hidden: drop (range-internal).
  //   - Src visible, dst hidden: ``src → fold`` landing on the fold's
  //     LEFT handle (``fold-input``). There is only one such edge per
  //     fold — the chain's entry into the range.
  //   - Src hidden, dst visible: route via the fold host's source
  //     handle, chosen by edge semantics:
  //     * ``dst`` is a pack ChatNode → ``fold-output-bottom`` +
  //       ``main-target-top`` (preserves the pack-below visual).
  //     * ``src`` is the range's LAST MEMBER (the one adjacent to the
  //       host in the original chain) → ``fold-output-right``,
  //       including the edge into the host itself.
  //     * ``src`` is an EARLIER range member → ``fold-output-top``,
  //       signalling "emerged from inside".
  //   - Both hidden but in different folds: route fold_src →
  //     fold_dst with default side handles (rare, only arises when
  //     two non-overlapping folds share an edge crossing them).
  const rerouted = new Map<string, Edge>();
  for (const { node } of laidOut) {
    for (const parentId of node.parent_ids) {
      if (!(parentId in chatflow.nodes)) continue;
      const parent = chatflow.nodes[parentId];
      const isMerge = node.parent_ids.length >= 2;
      const isPackChild = node.pack_snapshot != null;
      const isDashed =
        !parent.status || parent.status === "planned" || node.status === "planned";
      const edgeColor = isPackChild
        ? "#f43f5e" // rose-500
        : isMerge
          ? "#a855f7"
          : isDashed
            ? "#9ca3af"
            : "#374151";
      const srcFold = fold.foldByHidden.get(parentId);
      const dstFold = fold.foldByHidden.get(node.id);
      const srcId = srcFold ?? parentId;
      const dstId = dstFold ?? node.id;
      if (srcId === dstId) continue; // internal edge
      const key = `${srcId}->${dstId}`;
      if (rerouted.has(key)) continue; // dedupe parallel rails

      // Resolve handle names based on which endpoint got re-routed and
      // the edge's semantic category (pack-below, boundary fork, or
      // interior fork).
      let sourceHandle: string | undefined;
      let targetHandle: string | undefined;
      if (srcFold && !dstFold) {
        // Hidden → visible: emerging from a fold.
        const lastMember = fold.lastMemberByFold.get(srcFold);
        const hostOfSrcFold = fold.hostByFold.get(srcFold);
        if (dstId === hostOfSrcFold) {
          // Edge into this fold's own host: always the boundary /
          // "right-forward" route, even if the host happens to be a
          // pack (its target handle is its normal left-side one; the
          // visual pack-below drop is achieved by the fold sitting
          // upstream, not by re-routing into the pack's top handle).
          sourceHandle = "fold-output-right";
          targetHandle = undefined;
        } else if (isPackChild) {
          // Pack hanging off a hidden range member (NOT the host) —
          // drop it below the fold so the existing pack-below visual
          // convention is preserved.
          sourceHandle = "fold-output-bottom";
          targetHandle = "main-target-top";
        } else if (parentId === lastMember) {
          // Boundary fork from the last range member — sibling to the host.
          sourceHandle = "fold-output-right";
          targetHandle = undefined;
        } else {
          // Interior fork from an earlier member.
          sourceHandle = "fold-output-top";
          targetHandle = undefined;
        }
      } else if (!srcFold && dstFold) {
        // Visible → hidden: entering a fold from upstream.
        sourceHandle = undefined;
        targetHandle = "fold-input";
      } else if (srcFold && dstFold) {
        // Hidden → hidden across two folds. If these are a nested
        // outer → inner pair detected by the projection, this edge IS
        // the visual containment link: route into the inner fold's
        // left (input) handle and style it distinctly below via
        // ``isContainment``. Otherwise (rare) fall back to default
        // side handles.
        const nestedInners = fold.nestedInnersByOuter.get(srcFold);
        if (nestedInners && nestedInners.has(dstFold)) {
          sourceHandle = undefined;
          targetHandle = "fold-input";
        } else {
          sourceHandle = undefined;
          targetHandle = undefined;
        }
      } else {
        // Verbatim edge: keep the existing pack-drop / default handles.
        sourceHandle = isPackChild ? "main-source-bottom" : "main-source";
        targetHandle = isPackChild ? "main-target-top" : "main-target";
      }

      const isReroutedEdge = srcId !== parentId || dstId !== node.id;
      // Containment edge = outer fold → inner fold of a nested pair.
      // Style it muted + dashed so the user reads it as "entering a
      // nested fold" rather than a regular chain continuation.
      const isContainment =
        srcFold !== undefined
        && dstFold !== undefined
        && (fold.nestedInnersByOuter.get(srcFold)?.has(dstFold) ?? false);
      const effectiveColor = isContainment ? "#94a3b8" /* slate-400 */ : edgeColor;
      rerouted.set(key, {
        id: key,
        source: srcId,
        target: dstId,
        sourceHandle,
        targetHandle,
        animated: node.status === "running" && !isReroutedEdge,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: effectiveColor,
          width: 16,
          height: 16,
        },
        style: {
          stroke: effectiveColor,
          strokeDasharray: isDashed || isContainment ? "6 4" : undefined,
          strokeWidth: isMerge ? 2.5 : 1.5,
        },
      });
    }
  }

  // Synthetic chat-brief nodes stacked above each visible ChatNode
  // that has a ``scope='chat'`` BoardItem. Skip briefs for hidden
  // sources — their fold node already stands in for them.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const briefNodes: Node<any>[] = [];
  for (const id of Object.keys(chatflow.nodes)) {
    if (fold.hidden.has(id)) continue;
    const bi = boardItems[id];
    if (!bi || bi.scope !== "chat") continue;
    const srcPos = chatNodePositions.get(id);
    if (!srcPos) continue;
    const briefId = `${CHAT_BRIEF_NODE_PREFIX}${id}`;
    briefNodes.push({
      id: briefId,
      type: "chatBrief",
      position: { x: srcPos.x, y: srcPos.y - CHAT_BRIEF_INITIAL_OFFSET },
      data: { sourceNodeId: id } satisfies ChatBriefNodeData,
      selectable: false,
      draggable: false,
    });
  }
  return {
    nodes: [...rfNodes, ...foldNodes, ...briefNodes],
    edges: [...rerouted.values()],
  };
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
  packState,
  onPackStart,
  onPackToHere,
  onCancelPendingPack,
  foldState,
  onFold,
  onUnfold,
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
  packState:
    | "no-pending"
    | "first-pending-self"
    | "first-pending-other";
  onPackStart: () => void;
  onPackToHere: () => void;
  onCancelPendingPack: () => void;
  /** ``"none"`` = this node isn't a pack/compact host (hide fold items);
   * ``"fold"`` = pack/compact currently NOT folded (show "Fold range");
   * ``"unfold"`` = currently folded (show "Unfold range"). */
  foldState: "none" | "fold" | "unfold";
  onFold: () => void;
  onUnfold: () => void;
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

  // Pack (two-step range selection, mirrors merge's handshake).
  if (packState === "no-pending") {
    items.push({ label: t("chatflow.ctx_pack_start"), onClick: onPackStart });
  } else if (packState === "first-pending-self") {
    items.push({
      label: t("chatflow.ctx_cancel_pending_pack"),
      onClick: onCancelPendingPack,
    });
  } else {
    items.push({
      label: t("chatflow.ctx_pack_to_here"),
      onClick: onPackToHere,
    });
  }

  // Fold / Unfold — only on pack or compact ChatNodes.
  if (foldState === "fold") {
    items.push({ label: t("chatflow.ctx_fold_range"), onClick: onFold });
  } else if (foldState === "unfold") {
    items.push({ label: t("chatflow.ctx_unfold_range"), onClick: onUnfold });
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
