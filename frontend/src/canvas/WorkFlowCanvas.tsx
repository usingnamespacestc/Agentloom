/**
 * Main-area canvas for the drill-down view of a ChatNode's inner
 * WorkFlow DAG. Replaces the old right-side WorkFlowPanel.
 *
 * Renders the workflow with the same horizontal layout and drag /
 * single-selection semantics as ``ChatFlowCanvas``. Selection feeds
 * into ``workflowSelectedNodeId`` in the store so the right-side
 * ConversationView can show a matching I/O + detail panel.
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
import type { ChatFlowNode, WorkFlowNode } from "@/types/schema";

const NODE_TYPES = { workflow: WorkFlowNodeCard };

export interface WorkFlowCanvasProps {
  chatNode: ChatFlowNode | null;
}

export function WorkFlowCanvas({ chatNode }: WorkFlowCanvasProps) {
  const { t } = useTranslation();
  const chatflowId = useChatFlowStore((s) => s.chatflow?.id ?? null);
  const workflowSelectedNodeId = useChatFlowStore((s) => s.workflowSelectedNodeId);
  const selectWorkflowNode = useChatFlowStore((s) => s.selectWorkflowNode);

  const [nodes, setNodes] = useState<Node<WorkFlowNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const dragPositions = useRef<Record<string, { x: number; y: number }>>({});
  const dirtyPositions = useRef<Set<string>>(new Set());
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastChatNodeId = useRef<string | null>(null);

  // Mirrors ChatFlowCanvas's persistence: drag positions live in a ref
  // (so SSE reconciliation doesn't snap them back) and a 500ms-debounced
  // PATCH ships dirty ids to the backend.
  const flushPositions = useCallback(() => {
    if (!chatflowId || !chatNode || dirtyPositions.current.size === 0) return;
    const positions = [...dirtyPositions.current]
      .map((id) => {
        const pos = dragPositions.current[id];
        return pos ? { id, x: pos.x, y: pos.y } : null;
      })
      .filter(Boolean) as { id: string; x: number; y: number }[];
    dirtyPositions.current.clear();
    if (positions.length > 0) {
      void api.patchWorkflowPositions(chatflowId, chatNode.id, positions);
    }
  }, [chatflowId, chatNode]);

  useEffect(() => {
    if (chatNode?.id !== lastChatNodeId.current) {
      dragPositions.current = {};
      dirtyPositions.current.clear();
      lastChatNodeId.current = chatNode?.id ?? null;
    }
    const laid = buildWorkflowGraph(chatNode, workflowSelectedNodeId);
    const merged = laid.nodes.map((n) => ({
      ...n,
      position: dragPositions.current[n.id] ?? n.position,
    }));
    setNodes(merged);
    setEdges(laid.edges);
  }, [chatNode, workflowSelectedNodeId]);

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

  if (!chatNode) {
    return (
      <div
        data-testid="workflow-canvas-empty"
        className="flex h-full w-full items-center justify-center text-gray-500"
      >
        {t("workflow.no_selection")}
      </div>
    );
  }

  if (Object.keys(chatNode.workflow.nodes).length === 0) {
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
  chatNode: ChatFlowNode | null,
  selectedNodeId: string | null = null,
): { nodes: Node<WorkFlowNodeData>[]; edges: Edge[] } {
  if (!chatNode) return { nodes: [], edges: [] };
  const wf = chatNode.workflow;
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
