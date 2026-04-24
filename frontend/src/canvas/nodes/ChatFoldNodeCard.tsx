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

export interface ChatFoldNodeData extends Record<string, unknown> {
  /** Host ChatNode id (compact / pack) whose fold state this card
   * represents — used to invoke ``unfoldChatNode`` from the card's
   * right-click menu. */
  hostId: string;
  /** Number of ChatNodes currently hidden by this fold. */
  foldedCount: number;
  /** Host kind for badge tinting. */
  hostKind: "compact" | "pack";
}

export function ChatFoldNodeCard({ data }: NodeProps) {
  const { t } = useTranslation();
  const { hostId, foldedCount, hostKind } = data as ChatFoldNodeData;
  const tint =
    hostKind === "pack"
      ? "border-rose-300 bg-rose-50 text-rose-900"
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
    </div>
  );
}
