/**
 * Read-only overlay showing the current WorkFlow's blackboard
 * (``shared_notes``). Engine appends one line per WorkNode success +
 * one per judge verdict; rendering them lets the developer see what
 * the aggregator has accumulated without diving into the API.
 *
 * Positioned bottom-left of the canvas, offset rightward to clear
 * React Flow's <Controls> stack which sits in the same corner.
 * Collapsed by default so it doesn't fight with the canvas; click the
 * header to expand.
 *
 * Selection is bidirectional with the canvas: clicking a post selects
 * the corresponding WorkNode and centers the viewport on it; selecting
 * a WorkNode in the canvas highlights matching posts here and scrolls
 * the first one into view (only when the panel is open).
 */

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { SharedNote } from "@/types/schema";

export interface WorkFlowBlackboardProps {
  notes: SharedNote[] | undefined;
  selectedNodeId: string | null;
  onSelectNote: (nodeId: string) => void;
}

export function WorkFlowBlackboard({
  notes,
  selectedNodeId,
  onSelectNote,
}: WorkFlowBlackboardProps) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const count = notes?.length ?? 0;
  const firstSelectedRef = useRef<HTMLLIElement | null>(null);

  useEffect(() => {
    if (!open || !selectedNodeId) return;
    firstSelectedRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [open, selectedNodeId]);

  let firstMatchAssigned = false;
  return (
    <div
      data-testid="workflow-blackboard"
      className="absolute bottom-2 left-14 z-10 max-w-md rounded-md border border-gray-300 bg-white/95 shadow-sm"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1 text-[11px] text-gray-700 hover:bg-gray-50"
      >
        <span className="font-medium">
          {t("workflow.blackboard")} ({count})
        </span>
        <span className="text-gray-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && count > 0 && (
        <ul className="max-h-64 overflow-auto border-t border-gray-200 px-2 py-1 text-[11px] text-gray-700">
          {notes!.map((n, i) => {
            const isSelected = n.author_node_id === selectedNodeId;
            const setRef = isSelected && !firstMatchAssigned
              ? (el: HTMLLIElement | null) => {
                  firstSelectedRef.current = el;
                }
              : undefined;
            if (isSelected) firstMatchAssigned = true;
            return (
              <li
                key={i}
                ref={setRef}
                onClick={() => onSelectNote(n.author_node_id)}
                className={
                  "cursor-pointer rounded px-1 py-1 border-b border-gray-100 last:border-b-0 " +
                  (isSelected
                    ? "bg-blue-50 ring-1 ring-blue-300"
                    : "hover:bg-gray-50")
                }
              >
                <div className="flex items-center gap-1 text-[10px] text-gray-500">
                  <span className="font-mono">{n.author_node_id.slice(0, 8)}</span>
                  {n.role && <span>· {n.role}</span>}
                  <span>· {n.kind}</span>
                </div>
                <div className="break-words leading-snug">{n.summary}</div>
              </li>
            );
          })}
        </ul>
      )}
      {open && count === 0 && (
        <div className="border-t border-gray-200 px-2 py-1 text-[11px] italic text-gray-400">
          {t("workflow.blackboard_empty")}
        </div>
      )}
    </div>
  );
}
