/**
 * Visible ChatBoard-brief card stacked above its source ChatNode.
 *
 * Mirrors WorkFlow's ``BriefBody`` pattern: the chat-brief text lives
 * on the ChatBoardItem (``scope='chat'``), not on a ChatNode, so this
 * card is a *synthetic* React Flow node injected by ``buildGraph`` —
 * it doesn't map to any row in ``chatflow.nodes``. The card subscribes
 * to the ``boardItems`` slice directly so description updates (SSE
 * refresh, compact/merge follow-ups) re-render live without a rebuild.
 */

import { useState } from "react";
import { Handle, Position, type NodeProps } from "@xyflow/react";
import { useTranslation } from "react-i18next";

import { useChatFlowStore } from "@/store/chatflowStore";

export interface ChatBriefNodeData extends Record<string, unknown> {
  /** ChatNode id this brief summarizes — used to look up the live
   * BoardItem from the store. */
  sourceNodeId: string;
}

const TRUNCATE = 140;

function truncate(text: string, n = TRUNCATE): string {
  if (text.length <= n) return text;
  return `${text.slice(0, n - 1)}…`;
}

export function ChatBriefNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const { sourceNodeId } = data as ChatBriefNodeData;
  const boardItem = useChatFlowStore((s) => s.boardItems[sourceNodeId]);
  const [expanded, setExpanded] = useState(false);

  if (!boardItem || boardItem.scope !== "chat") {
    return null;
  }

  const text = boardItem.description || "";
  const { fallback, source_kind } = boardItem;

  return (
    <div
      data-testid={`chat-brief-node-${sourceNodeId}`}
      data-source-kind={source_kind}
      data-fallback={fallback ? "true" : "false"}
      className="relative w-52 rounded-md border border-indigo-200 bg-indigo-50 p-2 text-[11px] leading-snug text-gray-800 shadow-sm"
    >
      <Handle id="brief-target" type="target" position={Position.Bottom} />
      <div className="mb-1 flex items-center justify-between gap-1">
        <span className="font-semibold text-indigo-700">
          {t("chatflow.chat_brief_label")}
        </span>
        {fallback && (
          <span
            title={t("chatflow.chat_brief_fallback_hint")}
            className="rounded bg-amber-100 px-1 py-0.5 text-[9px] font-medium text-amber-700"
          >
            {t("chatflow.chat_brief_fallback_badge")}
          </span>
        )}
      </div>
      {text ? (
        <div
          className="cursor-pointer select-none break-words"
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
          title={text}
        >
          {expanded ? text : truncate(text)}
        </div>
      ) : (
        <span className="italic text-gray-400">—</span>
      )}
    </div>
  );
}
