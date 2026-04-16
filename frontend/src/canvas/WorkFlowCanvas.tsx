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
  applyNodeChanges,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useTranslation } from "react-i18next";

import { layoutDag } from "./layout";
import { CanvasContextMenu } from "./CanvasContextMenu";
import { contextWindowMap } from "./ChatFlowCanvas";
import { WorkFlowBlackboard } from "./WorkFlowBlackboard";
import { StickyNoteNode, type StickyNoteData } from "./nodes/StickyNoteNode";
import { WorkFlowNodeCard, type WorkFlowNodeData } from "./nodes/WorkFlowNodeCard";
import { api } from "@/lib/api";
import type { ProviderSummary } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { NodeId, StickyNote, WorkFlow, WorkFlowNode } from "@/types/schema";

const NODE_TYPES = { workflow: WorkFlowNodeCard, stickyNote: StickyNoteNode };

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
  const reactFlow = useReactFlow();

  // Sticky notes — persisted via workflow.sticky_notes
  const [stickyNotes, setStickyNotes] = useState<Record<string, StickyNote>>({});
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

  const handlePaneContextMenu = useCallback((event: React.MouseEvent) => {
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
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastWorkflowId = useRef<string | null>(null);

  const flushPositions = useCallback(() => {
    if (subPath.length > 0 || !chatflowId || !outerChatNodeId || dirtyPositions.current.size === 0) return;
    const positions = [...dirtyPositions.current]
      .map((id) => {
        const pos = dragPositions.current[id];
        return pos ? { id, x: pos.x, y: pos.y } : null;
      })
      .filter(Boolean) as { id: string; x: number; y: number }[];
    dirtyPositions.current.clear();
    if (positions.length > 0) {
      void api.patchWorkflowPositions(chatflowId, outerChatNodeId, positions);
    }
  }, [chatflowId, outerChatNodeId, subPath]);

  useEffect(() => {
    if (workflow?.id !== lastWorkflowId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      lastWorkflowId.current = workflow?.id ?? null;
    }
    const laid = buildWorkflowGraph(workflow, workflowSelectedNodeId, ctxWindowByModel);
    const merged = laid.nodes.map((n) => ({
      ...n,
      position: dragPositions.current[n.id] ?? n.position,
    }));
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const stickyNodes: Node<any>[] = Object.values(stickyNotes).map((note) => ({
      id: note.id,
      type: "stickyNote",
      position: dragPositions.current[note.id] ?? { x: note.x, y: note.y },
      data: { title: note.title, text: note.text, onTitleChange: onNoteTitleChange, onTextChange: onNoteTextChange, onDelete: onNoteDelete } satisfies StickyNoteData,
      style: { width: note.width, height: note.height },
    }));
    setNodes([...merged, ...stickyNodes]);
    setEdges(laid.edges);
  }, [workflow, workflowSelectedNodeId, ctxWindowByModel, stickyNotes, onNoteTitleChange, onNoteTextChange, onNoteDelete]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const filtered = changes.filter((c) => c.type !== "select" || ("id" in c && String(c.id).startsWith("_sticky_")));
    if (filtered.length === 0) return;
    for (const c of filtered) {
      if (c.type === "position" && c.position) {
        dragPositions.current[c.id] = c.position;
        if (String(c.id).startsWith("_sticky_")) {
          updateStickyNote(c.id, { x: c.position.x, y: c.position.y });
        } else {
          dirtyPositions.current.add(c.id);
        }
      }
      if (c.type === "dimensions" && c.dimensions && String(c.id).startsWith("_sticky_")) {
        updateStickyNote(c.id, { width: c.dimensions.width, height: c.dimensions.height });
      }
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    setNodes((ns) => applyNodeChanges(filtered, ns) as Node<any>[]);

    if (dirtyPositions.current.size > 0) {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPositions, 500);
    }
  }, [flushPositions, updateStickyNote]);

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    selectWorkflowNode(node.id);
  };

  const handleSelectNote = useCallback(
    (nodeId: string) => {
      selectWorkflowNode(nodeId);
      const rfNode = reactFlow.getNode(nodeId);
      if (!rfNode) return;
      const width = rfNode.measured?.width ?? 200;
      const height = rfNode.measured?.height ?? 100;
      const cx = rfNode.position.x + width / 2;
      const cy = rfNode.position.y + height / 2;
      reactFlow.setCenter(cx, cy, { zoom: reactFlow.getZoom(), duration: 300 });
    },
    [reactFlow, selectWorkflowNode],
  );

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
    <div data-testid="workflow-canvas" className="relative h-full w-full" onClick={() => setCtxMenu(null)}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodeClick={handleNodeClick}
        onNodesChange={onNodesChange}
        onPaneContextMenu={handlePaneContextMenu}
        onPaneClick={() => setCtxMenu(null)}
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
      {ctxMenu && (
        <CanvasContextMenu
          x={ctxMenu.x}
          y={ctxMenu.y}
          onInsertNote={handleInsertNote}
          onClose={() => setCtxMenu(null)}
        />
      )}
      <WorkFlowBlackboard
        notes={workflow.shared_notes}
        selectedNodeId={workflowSelectedNodeId}
        onSelectNote={handleSelectNote}
      />
    </div>
  );
}

/** Pure helper — unit-testable without rendering React Flow. */
export function buildWorkflowGraph(
  workflow: WorkFlow | null,
  selectedNodeId: string | null = null,
  contextWindowByModel: Record<string, number> = {},
): { nodes: Node<WorkFlowNodeData>[]; edges: Edge[] } {
  if (!workflow) return { nodes: [], edges: [] };
  const wf = workflow;
  const laidOut = layoutDag<WorkFlowNode>(wf.nodes, wf.root_ids, {
    columnWidth: 240,
    rowHeight: 160,
  });
  const rootSet = new Set(wf.root_ids);
  const hasChild = new Set<string>();
  for (const n of Object.values(wf.nodes)) {
    for (const pid of n.parent_ids) hasChild.add(pid);
  }
  const rfNodes: Node<WorkFlowNodeData>[] = laidOut.map(({ node, position }) => {
    const pos =
      node.position_x != null && node.position_y != null
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
    };
  });
  const rfEdges: Edge[] = [];
  for (const { node } of laidOut) {
    for (const parentId of node.parent_ids) {
      if (!(parentId in wf.nodes)) continue;
      rfEdges.push({
        id: `${parentId}->${node.id}`,
        source: parentId,
        target: node.id,
        animated: node.status === "running",
        style: {
          stroke: node.status === "planned" ? "#9ca3af" : "#374151",
          strokeDasharray: node.status === "planned" ? "6 4" : undefined,
        },
      });
    }
  }
  return { nodes: rfNodes, edges: rfEdges };
}
