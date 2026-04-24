/**
 * Main-area canvas for the drill-down view of a WorkFlow DAG.
 *
 * Renders the workflow with the same horizontal layout and drag /
 * single-selection semantics as ``ChatFlowCanvas``. Selection feeds
 * into ``workflowSelectedNodeId`` in the store so the right-side
 * ConversationView can show a matching I/O + detail panel.
 *
 * Drives off whatever WorkFlow the drill-stack resolves to (§3.4.3) —
 * the outer workflow attached to a ChatNode, or a nested ``sub_workflow``
 * inside a ``sub_agent_delegation`` WorkNode. Position persistence is
 * only wired for the outer workflow today; nested sub-workflow drag
 * positions live in memory until a backend endpoint exists for them.
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
import { contextWindowMap } from "./ChatFlowCanvas";
import { MemoryBoardPanel } from "./MemoryBoardPanel";
import { StickyNoteNode, type StickyNoteData } from "./nodes/StickyNoteNode";
import { WorkFlowNodeCard, type WorkFlowNodeData } from "./nodes/WorkFlowNodeCard";
import { api } from "@/lib/api";
import type { ProviderSummary } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { BoardItem, NodeId, StickyNote, WorkFlow, WorkFlowNode } from "@/types/schema";

const NODE_TYPES = { workflow: WorkFlowNodeCard, stickyNote: StickyNoteNode };

/** Fixed vertical gap between a brief WorkNode's bottom edge and its
 * source WorkNode's top edge. The brief's top floats upward as content
 * grows so the gap stays constant (no overlap on long briefs). */
const WORK_BRIEF_BOTTOM_GAP = 30;
/** Fallback height used until React Flow measures the rendered brief
 * card. Briefs in the WorkFlow lane are typically shorter than ChatFlow
 * briefs so the fallback is a bit smaller. */
const WORK_BRIEF_FALLBACK_HEIGHT = 130;

export interface WorkFlowCanvasProps {
  workflow: WorkFlow | null;
  /** ChatNode that owns the drill-stack root. Used as the persistence
   * key for outer-workflow drag positions. ``null`` while the stack is
   * empty. */
  outerChatNodeId: NodeId | null;
  /** Sub-path of WorkNode IDs for nested sub-workflows (frames after the
   * first chatnode frame in the drill stack). Empty for the outer workflow. */
  subPath: string[];
}

export function WorkFlowCanvas(props: WorkFlowCanvasProps) {
  return (
    <ReactFlowProvider>
      <WorkFlowCanvasInner {...props} />
    </ReactFlowProvider>
  );
}

function WorkFlowCanvasInner({ workflow, outerChatNodeId, subPath }: WorkFlowCanvasProps) {
  const { t } = useTranslation();
  const chatflowId = useChatFlowStore((s) => s.chatflow?.id ?? null);
  const workflowSelectedNodeId = useChatFlowStore((s) => s.workflowSelectedNodeId);
  const selectWorkflowNode = useChatFlowStore((s) => s.selectWorkflowNode);
  const boardItems = useChatFlowStore((s) => s.boardItems);
  const reactFlow = useReactFlow();

  // Sticky notes — persisted via workflow.sticky_notes
  const [stickyNotes, setStickyNotes] = useState<Record<string, StickyNote>>({});
  const stickyNotesRef = useRef(stickyNotes);
  useEffect(() => { stickyNotesRef.current = stickyNotes; }, [stickyNotes]);
  const isSticky = useCallback((id: string) => id in stickyNotesRef.current, []);
  const [editingStickyId, setEditingStickyId] = useState<string | null>(null);
  const [selectedStickyId, setSelectedStickyId] = useState<string | null>(null);
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; flowX: number; flowY: number } | null>(null);
  const stickyDirty = useRef(false);
  const stickyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const lastStickySourceId = useRef<string | null>(null);
  useEffect(() => {
    if (workflow?.id !== lastStickySourceId.current) {
      lastStickySourceId.current = workflow?.id ?? null;
      setStickyNotes(workflow?.sticky_notes ?? {});
    }
  }, [workflow]);

  const flushStickyNotes = useCallback((notes: Record<string, StickyNote>) => {
    if (!chatflowId || !outerChatNodeId) return;
    void api.putWorkflowStickyNotes(chatflowId, outerChatNodeId, notes, subPath);
  }, [chatflowId, outerChatNodeId, subPath]);

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
    setCtxMenu({ x: event.clientX, y: event.clientY, flowX: flowPos.x, flowY: flowPos.y });
  }, [reactFlow]);

  const handleInsertNote = useCallback(() => {
    if (!ctxMenu) return;
    const id = `_sticky_${crypto.randomUUID()}`;
    setStickyNotes((prev) => {
      const note: StickyNote = { id, title: "Note", text: "", x: ctxMenu.flowX, y: ctxMenu.flowY, width: 200, height: 120 };
      const next = { ...prev, [id]: note };
      scheduleStickyFlush(next);
      return next;
    });
  }, [ctxMenu, scheduleStickyFlush]);

  // Right-click menu for sticky notes (rendered at canvas level, not inside the node)
  const [stickyCtxMenu, setStickyCtxMenu] = useState<{ x: number; y: number; noteId: string } | null>(null);
  const handleNodeContextMenu: NodeMouseHandler = useCallback((event, node) => {
    if (!isSticky(String(node.id))) return;
    event.preventDefault();
    setStickyCtxMenu({ x: event.clientX, y: event.clientY, noteId: node.id });
  }, [isSticky]);

  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  useEffect(() => {
    let cancelled = false;
    api.listProviders().then((list) => {
      if (!cancelled) setProviders(list);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);
  const ctxWindowByModel = useMemo(() => contextWindowMap(providers), [providers]);

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [nodes, setNodes] = useState<Node<any>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const dragPositions = useRef<Record<string, { x: number; y: number }>>({});
  const dirtyPositions = useRef<Set<string>>(new Set());
  /** Per-brief source link + horizontal offset from layout. The vertical
   * offset is computed dynamically from the brief's measured height so
   * the brief's **bottom** stays ``WORK_BRIEF_BOTTOM_GAP`` above its
   * source's top, no matter how long the brief grows. */
  const briefOffsets = useRef<Record<string, { sourceId: string; dx: number }>>(
    {},
  );
  /** Measured height of each brief WorkNode, keyed by node id. Updated
   * from React Flow ``dimensions`` changes in ``onNodesChange``. */
  const briefHeights = useRef<Record<string, number>>({});
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastWorkflowId = useRef<string | null>(null);
  // Mid-drag, SSE-driven ``setNodes([...])`` replacements disrupt React
  // Flow's drag state; gate the sync effect and bump ``syncTick`` on
  // drag stop to catch up.
  const isDragging = useRef(false);
  const [syncTick, setSyncTick] = useState(0);

  const flushPositions = useCallback(() => {
    if (!chatflowId || !outerChatNodeId || dirtyPositions.current.size === 0) return;
    const positions = [...dirtyPositions.current]
      .map((id) => {
        const pos = dragPositions.current[id];
        return pos ? { id, x: pos.x, y: pos.y } : null;
      })
      .filter(Boolean) as { id: string; x: number; y: number }[];
    dirtyPositions.current.clear();
    if (positions.length > 0) {
      void api.patchWorkflowPositions(chatflowId, outerChatNodeId, positions, subPath);
    }
  }, [chatflowId, outerChatNodeId, subPath]);

  // Emergency sync flush on pagehide / tab-switch — mirrors the guard
  // in ChatFlowCanvas. The 500ms debounce during drag is fine for
  // interactive batching but leaves a window where a reload drops the
  // drag. fetch with keepalive completes during unload.
  useEffect(() => {
    const emergencyFlush = () => {
      if (!chatflowId || !outerChatNodeId || dirtyPositions.current.size === 0) {
        return;
      }
      const positions = [...dirtyPositions.current]
        .map((id) => {
          const pos = dragPositions.current[id];
          return pos ? { id, x: pos.x, y: pos.y } : null;
        })
        .filter(Boolean) as { id: string; x: number; y: number }[];
      if (positions.length === 0) return;
      dirtyPositions.current.clear();
      try {
        fetch(
          `/api/chatflows/${chatflowId}/nodes/${outerChatNodeId}/workflow/positions`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ positions, sub_path: subPath }),
            keepalive: true,
          },
        );
      } catch {
        // best-effort on unload
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
  }, [chatflowId, outerChatNodeId, subPath]);

  useEffect(() => {
    if (isDragging.current) return;
    if (workflow?.id !== lastWorkflowId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      briefOffsets.current = {};
      briefHeights.current = {};
      lastWorkflowId.current = workflow?.id ?? null;
    }
    const laid = buildWorkflowGraph(workflow, workflowSelectedNodeId, ctxWindowByModel);
    // Brief WorkNodes (step_kind === "brief") are bubbles attached to
    // their source: capture the laid-out delta so they glide with the
    // source as it moves, and drop any stale drag positions on them so
    // a legacy drag can't leave the brief stranded.
    const effectivePos = new Map<string, { x: number; y: number }>();
    for (const n of laid.nodes) {
      if (n.data.node.step_kind === "brief") continue;
      effectivePos.set(n.id, dragPositions.current[n.id] ?? n.position);
    }
    for (const n of laid.nodes) {
      if (n.data.node.step_kind !== "brief") continue;
      const srcId = n.data.node.parent_ids[0];
      const srcLaid = laid.nodes.find((m) => m.id === srcId);
      if (!srcLaid) continue;
      briefOffsets.current[n.id] = {
        sourceId: srcId,
        dx: n.position.x - srcLaid.position.x,
      };
      delete dragPositions.current[n.id];
    }
    const merged = laid.nodes.map((n) => {
      if (n.data.node.step_kind === "brief") {
        const off = briefOffsets.current[n.id];
        const srcPos = off ? effectivePos.get(off.sourceId) : undefined;
        if (off && srcPos) {
          const h = briefHeights.current[n.id] ?? WORK_BRIEF_FALLBACK_HEIGHT;
          return {
            ...n,
            position: { x: srcPos.x + off.dx, y: srcPos.y - WORK_BRIEF_BOTTOM_GAP - h },
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
  }, [workflow, workflowSelectedNodeId, ctxWindowByModel, stickyNotes, editingStickyId, selectedStickyId, onNoteTitleChange, onNoteTextChange, onNoteDelete, syncTick]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const filtered = changes.filter((c) => c.type !== "select");
    if (filtered.length === 0) return;
    for (const c of filtered) {
      if (c.type === "position" && c.position) {
        // Briefs aren't draggable (set in buildWorkflowGraph), but a
        // programmatic change could still arrive for one — never
        // persist a brief's position so it can't drift from its source.
        if (c.id in briefOffsets.current) continue;
        dragPositions.current[c.id] = c.position;
        if (isSticky(String(c.id))) {
          updateStickyNote(c.id, { x: c.position.x, y: c.position.y });
        } else {
          dirtyPositions.current.add(c.id);
        }
      }
      if (c.type === "dimensions" && c.dimensions) {
        const id = String(c.id);
        if (id in briefOffsets.current) {
          // Brief's rendered height changed — record it and bump the
          // sync tick so the next layout pass repositions it with the
          // bottom-gap formula.
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
    // Glide briefs with their source during a live drag: build a map
    // of (sourceId → newPosition) from the incoming changes and apply
    // each brief's stored offset so it tracks the cursor frame-by-frame.
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
        const off = briefOffsets.current[n.id];
        if (!off) return n;
        const src = sourceMoves.get(off.sourceId);
        if (!src) return n;
        const h =
          n.measured?.height ??
          briefHeights.current[n.id] ??
          WORK_BRIEF_FALLBACK_HEIGHT;
        return {
          ...n,
          position: { x: src.x + off.dx, y: src.y - WORK_BRIEF_BOTTOM_GAP - h },
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
  }, []);

  const handleNodeDragStop: OnNodeDrag = useCallback((_event, node) => {
    isDragging.current = false;
    // Briefs ride their source — don't record or persist their position.
    if (node.id in briefOffsets.current) {
      setSyncTick((t) => t + 1);
      return;
    }
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
    if (isSticky(String(node.id))) {
      setSelectedStickyId(node.id);
    } else {
      selectWorkflowNode(node.id);
      setSelectedStickyId(null);
      setEditingStickyId(null);
    }
  };

  const handleNodeDoubleClick: NodeMouseHandler = (_event, node) => {
    if (isSticky(String(node.id))) {
      setSelectedStickyId(node.id);
      setEditingStickyId(node.id);
    }
  };

  if (!workflow) {
    return (
      <div
        data-testid="workflow-canvas-empty"
        className="flex h-full w-full items-center justify-center text-gray-500"
      >
        {t("workflow.no_selection")}
      </div>
    );
  }

  if (Object.keys(workflow.nodes).length === 0) {
    return (
      <div
        data-testid="workflow-canvas-empty"
        className="flex h-full w-full items-center justify-center text-gray-500"
      >
        {t("workflow.empty")}
      </div>
    );
  }

  return (
    <div data-testid="workflow-canvas" className="relative h-full w-full" onClick={() => { setCtxMenu(null); setStickyCtxMenu(null); }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeDoubleClick}
        onNodeContextMenu={handleNodeContextMenu}
        onNodesChange={onNodesChange}
        onNodeDragStart={handleNodeDragStart}
        onNodeDragStop={handleNodeDragStop}
        onPaneContextMenu={handlePaneContextMenu}
        onPaneClick={() => { setCtxMenu(null); setStickyCtxMenu(null); setSelectedStickyId(null); setEditingStickyId(null); }}
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
        <WorkBoardPanel
          workflow={workflow}
          boardItems={boardItems}
          onJump={selectWorkflowNode}
        />
      </ReactFlow>
      {ctxMenu && (
        <CanvasContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          onInsertNote={handleInsertNote}
          onClose={() => setCtxMenu(null)}
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
    </div>
  );
}

/** Pure helper — unit-testable without rendering React Flow. */
export function buildWorkflowGraph(
  workflow: WorkFlow | null,
  selectedNodeId: string | null = null,
  contextWindowByModel: Record<string, number> = {},
): {
  nodes: Node<WorkFlowNodeData>[];
  edges: Edge[];
} {
  if (!workflow) return { nodes: [], edges: [] };
  const wf = workflow;
  // Flow-brief was retired backend-side on 2026-04-21, but any
  // persisted WorkFlow from before that date may still carry a
  // scope="flow" BRIEF WorkNode. Drop them from layout so legacy
  // data doesn't paint a stray disconnected node on the canvas.
  const graphNodes: Record<string, WorkFlowNode> = {};
  for (const [id, n] of Object.entries(wf.nodes)) {
    if (n.step_kind === "brief" && n.scope === "flow") continue;
    graphNodes[id] = n;
  }
  const briefIds = new Set<string>();
  for (const [id, n] of Object.entries(graphNodes)) {
    if (n.step_kind === "brief") {
      briefIds.add(id);
    }
  }
  const laidOut = layoutDag<WorkFlowNode>(graphNodes, wf.root_ids, {
    columnWidth: 240,
    rowHeight: 160,
    stackAboveIds: briefIds,
  });
  const rootSet = new Set(wf.root_ids);
  // Briefs are rendered as floating bubbles with no edge to their
  // source (see edge-creation skip below). Excluding them here keeps
  // a WorkNode whose only child is a brief from showing a dangling
  // right-side source handle — nothing connects to it visibly.
  const hasChild = new Set<string>();
  for (const n of Object.values(graphNodes)) {
    if (briefIds.has(n.id)) continue;
    for (const pid of n.parent_ids) hasChild.add(pid);
  }
  const rfNodes: Node<WorkFlowNodeData>[] = laidOut.map(({ node, position }) => {
    const isBrief = briefIds.has(node.id);
    // Brief positions are always derived from their source; skip any
    // stored position_x/y (legacy rows) so briefs stay attached.
    const pos =
      !isBrief && node.position_x != null && node.position_y != null
        ? { x: node.position_x, y: node.position_y }
        : position;
    const ref = node.model_override;
    const ctxKey = ref ? `${ref.provider_id}:${ref.model_id}` : "";
    return {
      id: node.id,
      type: "workflow",
      position: pos,
      data: {
        node,
        isSelected: node.id === selectedNodeId,
        isRoot: rootSet.has(node.id),
        isLeaf: !hasChild.has(node.id),
        maxContextTokens: contextWindowByModel[ctxKey] ?? null,
      },
      selectable: false,
      draggable: !isBrief,
    };
  });
  const rfEdges: Edge[] = [];
  for (const { node } of laidOut) {
    // Briefs are visually attached to their source as bubbles; skip
    // edge creation so the source's hover overlay (inherited model
    // chip, etc.) doesn't leak onto a meaningless connector line.
    if (briefIds.has(node.id)) continue;
    for (const parentId of node.parent_ids) {
      if (!(parentId in graphNodes)) continue;
      const edgeColor = node.status === "planned" ? "#9ca3af" : "#374151";
      rfEdges.push({
        id: `${parentId}->${node.id}`,
        source: parentId,
        target: node.id,
        sourceHandle: "main-source",
        targetHandle: "main-target",
        animated: node.status === "running",
        markerEnd: {
          // Direction indicator — a WorkFlow DAG has both fan-out
          // (planner → delegates, draft → tool_calls) and fan-in
          // (judge_post aggregating siblings); the arrow disambiguates
          // which end is the parent when two edges pass near each other.
          type: MarkerType.ArrowClosed,
          color: edgeColor,
          width: 14,
          height: 14,
        },
        style: {
          stroke: edgeColor,
          strokeDasharray: node.status === "planned" ? "6 4" : undefined,
        },
      });
    }
  }
  return { nodes: rfNodes, edges: rfEdges };
}

function sortBoardItemsAsc(a: BoardItem, b: BoardItem): number {
  const ta = a.created_at ? Date.parse(a.created_at) : 0;
  const tb = b.created_at ? Date.parse(b.created_at) : 0;
  return ta - tb;
}

/** WorkFlow-layer MemoryBoard panel — lists scope='node' briefs for the
 * currently viewed WorkFlow (outer or sub) and jumps to the source
 * WorkNode on click. Sub-workflow filtering is by ``workflow_id`` so
 * each drill level shows only its own briefs. */
function WorkBoardPanel({
  workflow,
  boardItems,
  onJump,
}: {
  workflow: WorkFlow | null;
  boardItems: Record<NodeId, BoardItem>;
  onJump: (nodeId: NodeId) => void;
}) {
  const { t } = useTranslation();
  const items = useMemo(() => {
    if (!workflow) return [];
    return Object.values(boardItems)
      .filter((item) => item.scope === "node" && item.workflow_id === workflow.id)
      .sort(sortBoardItemsAsc);
  }, [workflow, boardItems]);
  return (
    <MemoryBoardPanel
      testId="workflow-memoryboard-panel"
      title={t("workflow.memoryboard_panel_title")}
      emptyText={t("workflow.memoryboard_panel_empty")}
      fallbackLabel={t("workflow.memoryboard_panel_fallback_badge")}
      items={items}
      onItemClick={(item) => onJump(item.source_node_id)}
    />
  );
}
