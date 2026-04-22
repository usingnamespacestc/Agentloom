/**
 * Bottom-left floating panel listing MemoryBoard briefs for the
 * currently viewed flow. Shared between ``ChatFlowCanvas`` (scope='chat')
 * and ``WorkFlowCanvas`` (scope='node'); the parent passes the pre-filtered
 * items and a click handler that jumps the canvas to the source node.
 *
 * Rows show the source_kind badge, short id, first line of description,
 * and a fallback badge when the brief came from the deterministic code
 * template. The panel collapses via a header toggle and swallows canvas
 * pane clicks so interacting with it never deselects the active node.
 */

import { useState } from "react";

import type { BoardItem } from "@/types/schema";

const SOURCE_KIND_COLOR: Record<string, string> = {
  chat_turn: "bg-blue-100 text-blue-800",
  chat_compact: "bg-teal-100 text-teal-800",
  chat_merge: "bg-rose-100 text-rose-800",
  draft: "bg-blue-100 text-blue-800",
  tool_call: "bg-amber-100 text-amber-800",
  judge_call: "bg-purple-100 text-purple-800",
  delegate: "bg-gray-100 text-gray-700",
  compress: "bg-teal-100 text-teal-800",
  merge: "bg-rose-100 text-rose-800",
  brief: "bg-sky-100 text-sky-800",
};

function firstLine(text: string): string {
  const trimmed = text.trim();
  if (!trimmed) return "";
  return trimmed.split(/\r?\n/, 1)[0] ?? "";
}

export interface MemoryBoardPanelProps {
  title: string;
  items: BoardItem[];
  emptyText: string;
  fallbackLabel: string;
  onItemClick: (item: BoardItem) => void;
  testId: string;
}

export function MemoryBoardPanel({
  title,
  items,
  emptyText,
  fallbackLabel,
  onItemClick,
  testId,
}: MemoryBoardPanelProps) {
  const [open, setOpen] = useState(true);
  const count = items.length;

  return (
    <div
      data-testid={testId}
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      className="absolute bottom-2 right-2 z-10 w-72 rounded-md border border-gray-300 bg-white/95 shadow-sm"
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded-md px-2 py-1 text-[11px] text-gray-700 hover:bg-gray-50"
      >
        <span className="font-medium">
          {title} ({count})
        </span>
        <span className="text-gray-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && count > 0 && (
        <ul className="max-h-64 overflow-auto border-t border-gray-200 text-[11px]">
          {items.map((item) => {
            const color = SOURCE_KIND_COLOR[item.source_kind] ?? "bg-gray-100 text-gray-700";
            const preview = firstLine(item.description);
            return (
              <li
                key={item.id}
                data-testid={`${testId}-item-${item.source_node_id}`}
                onClick={() => onItemClick(item)}
                className="cursor-pointer border-b border-gray-100 px-2 py-1 last:border-b-0 hover:bg-gray-50"
                title={item.description}
              >
                <div className="flex items-center gap-1 text-[10px]">
                  <span className={`rounded px-1 py-[1px] font-mono ${color}`}>
                    {item.source_kind}
                  </span>
                  <span className="font-mono text-gray-500">
                    {item.source_node_id.slice(-6)}
                  </span>
                  {item.fallback && (
                    <span className="rounded bg-amber-100 px-1 py-[1px] font-medium text-amber-700">
                      {fallbackLabel}
                    </span>
                  )}
                </div>
                {preview && (
                  <div className="mt-0.5 break-words leading-snug text-gray-700">
                    {preview}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {open && count === 0 && (
        <div className="border-t border-gray-200 px-2 py-1 text-[11px] italic text-gray-400">
          {emptyText}
        </div>
      )}
    </div>
  );
}
