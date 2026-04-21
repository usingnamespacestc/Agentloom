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
import { NodeIdLine } from "./NodeIdLine";
import { formatTokensKM } from "@/lib/tokenFormat";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlowNode, WorkFlowNode } from "@/types/schema";

/** Fallback denominator when the node's resolved model has no
 * configured ``context_window`` (e.g. a provider seeded before the
 * field was added). Keeps the bar showing a plausible ratio instead
 * of hiding it. */
export const DEFAULT_MAX_CONTEXT_TOKENS = 32_000;

export interface ChatFlowNodeData extends Record<string, unknown> {
  node: ChatFlowNode;
  isSelected: boolean;
  /** Whether this node can be deleted (false when running or ancestor of running). */
  canDelete: boolean;
  /** Whether deleting this node would cascade (has descendants). */
  isLeaf: boolean;
  /** Whether this node is a root (no parents). */
  isRoot: boolean;
  /** Accumulated context tokens from root to this node (inclusive). */
  contextTokens: number;
  /** Context window of the node's resolved model, or ``null`` when the
   * provider didn't declare one — in that case the bar falls back to
   * :data:`DEFAULT_MAX_CONTEXT_TOKENS`. */
  maxContextTokens: number | null;
}

const TRUNCATE = 90;

function truncate(text: string, n = TRUNCATE): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n - 1)}…`;
}

/** Walk the inner WorkFlow (recursing into sub_workflows) and collect
 * IDs of every WorkNode currently in ``running`` state. The ChatFlow
 * card uses the longest streaming delta among these to show live
 * activity without forcing the user to drill into the WorkFlow view. */
function collectRunningWorkNodeIds(nodes: Record<string, WorkFlowNode>): string[] {
  const out: string[] = [];
  for (const wn of Object.values(nodes)) {
    if (wn.status === "running") out.push(wn.id);
    if (wn.sub_workflow) {
      out.push(...collectRunningWorkNodeIds(wn.sub_workflow.nodes));
    }
  }
  return out;
}

export function ChatFlowNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const {
    node,
    isSelected,
    canDelete,
    isLeaf,
    isRoot,
    contextTokens,
    maxContextTokens,
  } = data as ChatFlowNodeData;
  const isMerge = node.parent_ids.length >= 2;
  const isMergeSettled = isMerge && node.status === "succeeded";
  const isGreetingRoot = node.user_message === null;
  const hasWorkflow = Object.keys(node.workflow.nodes).length > 0;
  const isDashed = node.status === "planned" || node.status === "running";
  const isAwaitingUser = !!node.workflow.pending_user_prompt;
  const isCompact = node.compact_snapshot != null;
  const executionMode = node.workflow.execution_mode;
  const isPendingMergeFirst = useChatFlowStore(
    (s) => s.pendingMergeFirstId === node.id,
  );
  // ChatBoardItem lookup (PR 3 cascading inheritance, 2026-04-20) —
  // keyed by the ChatNode id. ``scope='chat'`` rows are auto-written by
  // ``_spawn_chat_board_item`` when a ChatNode reaches SUCCEEDED. We
  // render a small badge on the card tooltip-revealing the description;
  // a full MemoryBoard browser lands in a later PR.
  const chatBoardItem = useChatFlowStore((s) => s.boardItems[node.id]);
  const hasChatBoardItem =
    chatBoardItem !== undefined && chatBoardItem.scope === "chat";

  // Live preview: pick the longest streaming delta among any running
  // WorkNode under this ChatNode (handles parallel siblings + nested
  // sub-workflows). When no delta has arrived yet we fall back to the
  // ``thinking…`` placeholder below.
  const livePreview = useChatFlowStore((s) => {
    if (node.status !== "running") return "";
    const ids = collectRunningWorkNodeIds(node.workflow.nodes);
    let best = "";
    for (const id of ids) {
      const d = s.streamingDeltas[id];
      if (d && d.length > best.length) best = d;
    }
    return best;
  });

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
      data-compact={isCompact ? "1" : undefined}
      className={[
        "group/card relative rounded-lg border shadow-sm w-52 p-2.5 text-xs",
        isCompact
          ? "bg-teal-50 border-l-[3px] border-l-teal-500"
          : isAwaitingUser
            ? "bg-amber-50 border-l-[3px] border-l-amber-500"
            : isRoot
              ? "bg-blue-50 border-l-[3px] border-l-blue-400"
              : isLeaf
                ? "bg-green-50"
                : "bg-white",
        isSelected
          ? "border-blue-500 ring-2 ring-blue-200"
          : isCompact
            ? "border-teal-300"
            : isAwaitingUser
              ? "border-amber-300"
              : isRoot
                ? "border-blue-200"
                : isLeaf
                  ? "border-green-200"
                  : "border-gray-300",
        isMerge ? "border-purple-400" : "",
        isDashed ? "border-dashed" : "",
        executionMode === "auto_plan"
          ? "outline outline-2 outline-violet-400 outline-offset-2"
          : executionMode === "native_react"
            ? "outline outline-1 outline-sky-300 outline-offset-2"
            : "",
        isPendingMergeFirst
          ? "ring-4 ring-violet-400 animate-pulse"
          : "",
      ].join(" ")}
      data-pending-merge-first={isPendingMergeFirst ? "1" : undefined}
      data-merge={isMergeSettled ? "1" : undefined}
      title={t(`chatflow_settings.execution_mode_${executionMode}_hint`)}
    >
      {!isRoot && (
        <Handle id="main-target" type="target" position={Position.Left} />
      )}
      {hasChatBoardItem && (
        <Handle id="brief-source" type="source" position={Position.Top} />
      )}

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
        {isCompact && (
          <span
            title={t("chatflow.compact_badge_hint")}
            className="inline-flex items-center gap-0.5 rounded bg-teal-200/80 px-1 py-0.5 text-[10px] font-semibold text-teal-900"
          >
            <span aria-hidden>⟲</span>
            {t("chatflow.compact_badge")}
          </span>
        )}
        {isAwaitingUser && (
          <span
            title={t("chatflow.awaiting_user_hint")}
            className="rounded bg-amber-200/80 px-1 py-0.5 text-[10px] font-semibold text-amber-900"
          >
            {t("chatflow.awaiting_user")}
          </span>
        )}
        {isMerge && (
          <span
            className="text-[10px] text-purple-600 font-medium"
            title={
              isMergeSettled
                ? t("chatflow.merge_badge_hint")
                : undefined
            }
          >
            {isMergeSettled ? "⨝" : "⨯"}
            {node.parent_ids.length}
          </span>
        )}
        {node.pending_queue?.length > 0 && (
          <span className="text-[10px] text-blue-500 font-medium">
            +{node.pending_queue.length}
          </span>
        )}
        {hasChatBoardItem && (
          <span
            data-testid={`chatflow-node-${node.id}-chatboard-badge`}
            data-source-kind={chatBoardItem.source_kind}
            title={chatBoardItem.description}
            className="inline-flex items-center rounded bg-indigo-100 px-1 py-0.5 text-[9px] font-medium text-indigo-700"
          >
            {t("chatflow.chatboard_badge")}
          </span>
        )}
      </div>

      {isCompact ? (
        <div className="mb-1.5">
          <div className="text-[10px] text-teal-700 mb-0.5">{t("chatflow.compact_summary")}</div>
          <div className="prose prose-sm max-w-none text-xs text-gray-900 break-words leading-snug">
            {node.agent_response.text ? (
              <Markdown>{truncate(node.agent_response.text)}</Markdown>
            ) : node.status === "running" ? (
              <span className="inline-flex items-center gap-1 text-teal-600">
                <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-teal-400" />
                compacting…
              </span>
            ) : (
              <span className="italic text-gray-400">—</span>
            )}
          </div>
        </div>
      ) : isGreetingRoot ? (
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
                livePreview ? (
                  <div data-testid="chatflow-streaming-preview" className="text-gray-700">
                    <Markdown>{truncate(livePreview)}</Markdown>
                    <span className="inline-block w-1 h-3 align-middle bg-yellow-400 animate-pulse ml-0.5" />
                  </div>
                ) : (
                  <span className="inline-flex items-center gap-1 text-yellow-600">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-yellow-400" />
                    thinking…
                  </span>
                )
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

      {contextTokens > 0 && (
        <TokenBar tokens={contextTokens} maxTokens={maxContextTokens} />
      )}

      <NodeIdLine nodeId={node.id} />

      {!isLeaf && (
        <Handle id="main-source" type="source" position={Position.Right} />
      )}
    </div>
  );
}

export function TokenBar({
  tokens,
  maxTokens,
}: {
  tokens: number;
  maxTokens?: number | null;
}) {
  const denom = maxTokens && maxTokens > 0 ? maxTokens : DEFAULT_MAX_CONTEXT_TOKENS;
  const pct = Math.min(100, (tokens / denom) * 100);
  const color =
    pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-yellow-400" : "bg-blue-400";
  return (
    <div className="mt-1.5" title={`${tokens} / ${formatTokensKM(denom)} tokens`}>
      <div className="flex items-center justify-between text-[9px] text-gray-500 mb-0.5">
        <span>{formatTokensKM(tokens)}</span>
        <span>{pct.toFixed(0)}%</span>
      </div>
      <div className="h-1 w-full rounded-full bg-gray-200">
        <div
          className={`h-1 rounded-full ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
