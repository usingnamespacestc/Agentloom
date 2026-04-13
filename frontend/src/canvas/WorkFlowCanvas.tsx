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
import { WorkFlowNodeCard, type WorkFlowNodeData } from "./nodes/WorkFlowNodeCard";
import { api } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { NodeId, WorkFlow, WorkFlowNode } from "@/types/schema";

const NODE_TYPES = { workflow: WorkFlowNodeCard };

export interface WorkFlowCanvasProps {
  workflow: WorkFlow | null;
  /** ChatNode that owns the drill-stack root. Used as the persistence
   * key for outer-workflow drag positions. ``null`` while the stack is
   * empty. */
  outerChatNodeId: NodeId | null;
  /** True when ``workflow`` is a nested sub_workflow rather than the
   * outer one — disables position-saving (no backend endpoint yet). */
  nested: boolean;
}

export function WorkFlowCanvas({ workflow, outerChatNodeId, nested }: WorkFlowCanvasProps) {
  const { t } = useTranslation();
  const chatflowId = useChatFlowStore((s) => s.chatflow?.id ?? null);
  const workflowSelectedNodeId = useChatFlowStore((s) => s.workflowSelectedNodeId);
  const selectWorkflowNode = useChatFlowStore((s) => s.selectWorkflowNode);

  const [nodes, setNodes] = useState<Node<WorkFlowNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const dragPositions = useRef<Record<string, { x: number; y: number }>>({});
  const dirtyPositions = useRef<Set<string>>(new Set());
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastWorkflowId = useRef<string | null>(null);

  const flushPositions = useCallback(() => {
    if (nested || !chatflowId || !outerChatNodeId || dirtyPositions.current.size === 0) return;
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
  }, [chatflowId, outerChatNodeId, nested]);

  useEffect(() => {
    if (workflow?.id !== lastWorkflowId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      lastWorkflowId.current = workflow?.id ?? null;
    }
    const laid = buildWorkflowGraph(workflow, workflowSelectedNodeId);
    const merged = laid.nodes.map((n) => ({
      ...n,
      position: dragPositions.current[n.id] ?? n.position,
    }));
    setNodes(merged);
    setEdges(laid.edges);
  }, [workflow, workflowSelectedNodeId]);

  const onNodesChange = useCallback((changes: NodeChange[]) => {
    const filtered = changes.filter((c) => c.type !== "select");
    if (filtered.length === 0) return;
    for (const c of filtered) {
      if (c.type === "position" && c.position) {
        dragPositions.current[c.id] = c.position;
        dirtyPositions.current.add(c.id);
      }
    }
    setNodes((ns) => applyNodeChanges(filtered, ns) as Node<WorkFlowNodeData>[]);

    if (dirtyPositions.current.size > 0) {
      if (saveTimer.current) clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(flushPositions, 500);
    }
  }, [flushPositions]);

  const handleNodeClick: NodeMouseHandler = (_event, node) => {
    selectWorkflowNode(node.id);
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
    <div data-testid="workflow-canvas" className="relative h-full w-full">
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
    </div>
  );
}

/** Pure helper — unit-testable without rendering React Flow. */
export function buildWorkflowGraph(
  workflow: WorkFlow | null,
  selectedNodeId: string | null = null,
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
    return {
      id: node.id,
      type: "workflow",
      position: pos,
      data: {
        node,
        isSelected: node.id === selectedNodeId,
        isRoot: rootSet.has(node.id),
        isLeaf: !hasChild.has(node.id),
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
