/**
 * Custom React Flow node for one ChatFlowNode.
 *
 * Horizontal layout (M8.5): handles on the left (target) and right
 * (source) edges. Taller / narrower than the old vertical card so
 * more columns fit on screen; truncation is more aggressive.
 *
 * The "⤢ Enter workflow" button is the only way to drill into the
 * inner WorkFlow view — the old "double-click to open" affordance
 * was removed because it overlapped with single-click selection.
 *
 * The button click is forwarded via CustomEvent so the parent canvas
 * (which owns the React Flow instance) can translate it into a store
 * action without threading a callback through node data.
 */

import { Handle, Position, type NodeProps } from "@xyflow/react";
import Markdown from "react-markdown";
import { useTranslation } from "react-i18next";

import { StatusBadge } from "./StatusBadge";
import type { ChatFlowNode } from "@/types/schema";

export interface ChatFlowNodeData extends Record<string, unknown> {
  node: ChatFlowNode;
  isSelected: boolean;
  /** Whether this node can be deleted (false when running or ancestor of running). */
  canDelete: boolean;
  /** Whether deleting this node would cascade (has descendants). */
  isLeaf: boolean;
}

const TRUNCATE = 90;

function truncate(text: string, n = TRUNCATE): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n - 1)}…`;
}

export function ChatFlowNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const { node, isSelected, canDelete, isLeaf } = data as ChatFlowNodeData;
  const isMerge = node.parent_ids.length >= 2;
  const isGreetingRoot = node.user_message === null;
  const hasWorkflow = Object.keys(node.workflow.nodes).length > 0;
  const isDashed = node.status === "planned" || node.status === "running";

  const onEnter = (e: React.MouseEvent) => {
    e.stopPropagation();
    window.dispatchEvent(
      new CustomEvent("agentloom:enter-workflow", { detail: { chatNodeId: node.id } }),
    );
  };

  const onDelete = (e: React.MouseEvent) => {
    e.stopPropagation();
    window.dispatchEvent(
      new CustomEvent("agentloom:delete-node", {
        detail: { nodeId: node.id, isLeaf },
      }),
    );
  };

  return (
    <div
      data-testid={`chatflow-node-${node.id}`}
      className={[
        "group/card relative rounded-lg border bg-white shadow-sm w-48 p-2.5 text-xs",
        isSelected ? "border-blue-500 ring-2 ring-blue-200" : "border-gray-300",
        isMerge ? "border-purple-400" : "",
        isDashed ? "border-dashed" : "",
      ].join(" ")}
    >
      <Handle type="target" position={Position.Left} />

      {/* Delete button — top-right, visible on hover */}
      {canDelete && (
        <button
          type="button"
          onClick={onDelete}
          data-testid={`chatflow-node-${node.id}-delete`}
          className="absolute -top-2 -right-2 z-10 hidden h-5 w-5 items-center justify-center rounded-full border border-red-300 bg-red-50 text-[10px] text-red-500 shadow-sm hover:bg-red-100 hover:text-red-700 group-hover/card:flex"
          title={isLeaf ? t("chatflow.delete") : t("chatflow.delete_cascade")}
        >
          ✕
        </button>
      )}

      <div className="flex items-center justify-between mb-1.5">
        <StatusBadge status={node.status} />
        {isMerge && (
          <span className="text-[10px] text-purple-600 font-medium">⨯{node.parent_ids.length}</span>
        )}
        {node.pending_queue?.length > 0 && (
          <span className="text-[10px] text-blue-500 font-medium">
            +{node.pending_queue.length}
          </span>
        )}
      </div>

      {isGreetingRoot ? (
        <div className="mb-1.5">
          <div className="text-[10px] text-gray-500 mb-0.5">{t("chatflow.agent")}</div>
          <div className="prose prose-sm max-w-none text-xs text-gray-900 break-words leading-snug">
            {node.agent_response.text ? (
              <Markdown>{truncate(node.agent_response.text)}</Markdown>
            ) : (
              <span className="italic text-gray-400">—</span>
            )}
          </div>
        </div>
      ) : (
        <>
          <div className="mb-1.5">
            <div className="text-[10px] text-gray-500 mb-0.5">{t("chatflow.user")}</div>
            <div className="prose prose-sm max-w-none text-xs text-gray-900 break-words leading-snug">
              {node.user_message?.text ? (
                <Markdown>{truncate(node.user_message.text)}</Markdown>
              ) : (
                <span className="italic text-gray-400">—</span>
              )}
            </div>
          </div>

          <div className="mb-1.5">
            <div className="text-[10px] text-gray-500 mb-0.5">{t("chatflow.agent")}</div>
            <div className="prose prose-sm max-w-none text-xs text-gray-900 break-words leading-snug">
              {node.status === "running" ? (
                <span className="inline-flex items-center gap-1 text-yellow-600">
                  <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-yellow-400" />
                  thinking…
                </span>
              ) : node.agent_response.text ? (
                <Markdown>{truncate(node.agent_response.text)}</Markdown>
              ) : (
                <span className="italic text-gray-400">—</span>
              )}
            </div>
          </div>
        </>
      )}

      {hasWorkflow && (
        <button
          type="button"
          onClick={onEnter}
          data-testid={`chatflow-node-${node.id}-enter`}
          className="mt-1 flex w-full items-center justify-center gap-1 rounded border border-gray-200 bg-gray-50 px-2 py-1 text-[10px] text-gray-600 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700"
        >
          <span>⤢</span>
          <span>{t("chatflow.open_workflow")}</span>
        </button>
      )}

      <Handle type="source" position={Position.Right} />
    </div>
  );
}
