/**
 * Synthetic fold node — a view-only placeholder injected by
 * ``buildGraph`` when a pack / compact ChatNode has been folded via
 * ``foldedChatNodeIds`` in the store.
 *
 * Unlike :file:`ChatBriefNodeCard`, this card doesn't correspond to any
 * row in ``chatflow.nodes`` or in ``boardItems``. It's purely a visual
 * proxy for "the range of nodes collapsed by this fold operation." The
 * upstream chain's terminal edge flows *into* the fold (left handle);
 * the host (the compact/pack ChatNode this fold belongs to) is the
 * fold's primary successor on the right. Fork / pack edges that would
 * otherwise land on range-internal nodes get re-routed to distinct
 * handles so the user can read:
 *
 * - ``top``   — a branch that emerged from a member EARLIER in the fold
 *   (visually: "this fork came from deep inside").
 * - ``right`` — a branch that emerged from the LAST range member
 *   (visually: "this fork came out right next to the host").
 * - ``bottom`` — any pack ChatNode that had a range member as its
 *   parent (mirrors the existing pack-below convention).
 *
 * Right-clicking a fold card opens the same unfold action the host's
 * context menu offers — users can pick either anchor to restore the
 * range.
 */

import { type NodeProps, Handle, Position } from "@xyflow/react";
import { useTranslation } from "react-i18next";

import { formatTokensKM } from "@/lib/tokenFormat";
import { useChatFlowStore } from "@/store/chatflowStore";

export interface FoldPeekMember {
  /** ChatNode id — clicking expands the fold AND selects this node. */
  id: string;
  /** First line of the ChatNode's user_message (falls back to
   * agent_response for greeting / assistant-only turns). Truncated;
   * the card styles it with ``truncate`` to fit the w-40 width. */
  firstLine: string;
  /** Source of the first line so the card can annotate role if it
   * wants to. Today we just show the text, but this is useful
   * metadata for future styling. */
  role: "user" | "assistant";
}

export interface ChatFoldNodeData extends Record<string, unknown> {
  /** Host ChatNode id (compact / pack) whose fold state this card
   * represents — used to invoke ``unfoldChatNode`` from the card's
   * right-click menu. */
  hostId: string;
  /** Number of ChatNodes currently hidden by this fold. */
  foldedCount: number;
  /** Host kind for badge tinting. */
  hostKind: "compact" | "pack" | "merge";
  /** Sum of ``nodeTokens`` (entry_prompt_tokens + output_response_tokens)
   * across every ChatNode claimed by this fold. Gives users a quick
   * read on "how much conversation is tucked away behind this card."
   * 0 when the nodes predate the token-metering fields. */
  foldedTokens: number;
  /** First N ChatNodes in the fold ordered NEAREST-HOST-FIRST
   * (temporally newest — the ones the user just folded, which they're
   * most likely to want to peek at). Sliced to ``_PEEK_LIMIT`` so the
   * card doesn't grow unbounded; any surplus shows as an "+ N more"
   * footer driven by ``extraCount``. Click a row → unfoldChatNode +
   * selectNode so users can drill in immediately. */
  peekMembers: FoldPeekMember[];
  /** Count of claimed members beyond ``peekMembers``. When > 0 the
   * card renders a "+N more" teaser below the peek list. */
  extraCount: number;
}

export function ChatFoldNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const {
    hostId,
    foldedCount,
    hostKind,
    foldedTokens,
    peekMembers,
    extraCount,
  } = data as ChatFoldNodeData;
  const unfoldChatNode = useChatFlowStore((s) => s.unfoldChatNode);
  const selectNode = useChatFlowStore((s) => s.selectNode);
  const tint =
    hostKind === "pack"
      ? "border-rose-300 bg-rose-50 text-rose-900"
      : hostKind === "merge"
        ? "border-purple-300 bg-purple-50 text-purple-900"
        : "border-teal-300 bg-teal-50 text-teal-900";

  return (
    <div
      data-testid={`chat-fold-node-${hostId}`}
      data-host-kind={hostKind}
      className={[
        "relative w-40 rounded-md border border-dashed px-2.5 py-2 text-[11px] leading-snug shadow-sm",
        tint,
      ].join(" ")}
      title={t("chatflow.fold_node_hint", { count: foldedCount })}
    >
      {/* Input from upstream chain — whatever was the parent of the
        * first range member now points here. */}
      <Handle id="fold-input" type="target" position={Position.Left} />
      {/* Output to the host (compact/pack) plus any fork child of the
        * LAST range member (sibling of host). Multiple children share
        * this one source handle — React Flow allows that. */}
      <Handle id="fold-output-right" type="source" position={Position.Right} />
      {/* Output to fork children that originated from EARLIER range
        * members (deeper inside the fold). Rendered UP from the card
        * to visually cue "emerged from inside". */}
      <Handle id="fold-output-top" type="source" position={Position.Top} />
      {/* Output to pack ChatNodes hanging off any range member —
        * preserves the existing pack-below convention. */}
      <Handle id="fold-output-bottom" type="source" position={Position.Bottom} />
      <div className="flex items-center gap-1">
        <span aria-hidden>⊞</span>
        <span className="font-semibold">
          {t("chatflow.fold_node_label", { count: foldedCount })}
        </span>
      </div>
      {foldedTokens > 0 && (
        <div
          data-testid={`chat-fold-node-${hostId}-tokens`}
          className="mt-1 text-[10px] opacity-70"
          title={t("chatflow.fold_node_tokens_hint", { count: foldedTokens })}
        >
          {t("chatflow.fold_node_tokens_label", {
            tokens: formatTokensKM(foldedTokens),
          })}
        </div>
      )}
      {peekMembers.length > 0 && (
        <div
          data-testid={`chat-fold-node-${hostId}-peek`}
          className="mt-1.5 space-y-0.5 border-t border-current/10 pt-1.5"
        >
          {peekMembers.map((m) => (
            <button
              key={m.id}
              type="button"
              data-testid={`chat-fold-node-${hostId}-peek-${m.id}`}
              onClick={(e) => {
                e.stopPropagation();
                // Expand the fold AND jump to the member in one click —
                // matches Claude Code's "Read with offset/limit" drill-in.
                unfoldChatNode(hostId);
                selectNode(m.id);
              }}
              title={m.firstLine}
              className="flex w-full items-baseline gap-1 truncate rounded text-left text-[10px] leading-snug opacity-80 hover:bg-current/5 hover:opacity-100"
            >
              <span className="shrink-0 font-mono text-[9px] opacity-70">
                {m.id.slice(0, 8)}
              </span>
              <span className="flex-1 truncate">
                {m.firstLine || (
                  <span className="italic opacity-50">
                    {t("chatflow.fold_node_empty_turn")}
                  </span>
                )}
              </span>
            </button>
          ))}
          {extraCount > 0 && (
            <div
              data-testid={`chat-fold-node-${hostId}-more`}
              className="text-[10px] italic opacity-60"
            >
              {t("chatflow.fold_node_more_count", { count: extraCount })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
