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

import { useCallback, useEffect, useRef, useState } from "react";

import type { BoardItem } from "@/types/schema";

const SOURCE_KIND_COLOR: Record<string, string> = {
  chat_turn: "bg-blue-100 text-blue-800",
  chat_compact: "bg-teal-100 text-teal-800",
  chat_merge: "bg-rose-100 text-rose-800",
  chat_pack: "bg-rose-200 text-rose-900",
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
  //: List max-height in pixels. Initial ~256px (matches the old
  //: ``max-h-64``). A top drag handle lets the user extend it
  //: upward — direction flip matches the panel's bottom anchor.
  //: Capped at ``0.7 * innerHeight`` so it can't escape viewport.
  //:
  //: Performance note: mousemove fires 60+ Hz. Writing to React
  //: state on every frame re-renders every BoardItem row (an O(N)
  //: reconciliation) which feels laggy on long lists. Instead we
  //: mutate the <ul>'s ``style.maxHeight`` directly during drag via
  //: the ref and only commit the final value to state on mouseup —
  //: React re-renders once per drag, not per frame.
  const [listHeight, setListHeight] = useState(256);
  const count = items.length;
  const listRef = useRef<HTMLUListElement | null>(null);
  const dragStateRef = useRef<{
    startY: number;
    startHeight: number;
    currentHeight: number;
  } | null>(null);

  const onHandleMouseDown = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      e.preventDefault();
      e.stopPropagation();
      dragStateRef.current = {
        startY: e.clientY,
        startHeight: listHeight,
        currentHeight: listHeight,
      };
    },
    [listHeight],
  );

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const st = dragStateRef.current;
      if (st === null) return;
      const dy = st.startY - e.clientY; // upward drag = positive delta
      const maxPx = Math.floor(window.innerHeight * 0.7);
      const next = Math.min(Math.max(128, st.startHeight + dy), maxPx);
      st.currentHeight = next;
      // Bypass React — direct DOM write is ~0ms, no reconciliation.
      if (listRef.current !== null) {
        listRef.current.style.maxHeight = `${next}px`;
      }
    };
    const onUp = () => {
      const st = dragStateRef.current;
      if (st !== null) {
        // Commit once so future re-renders preserve the user's size.
        setListHeight(st.currentHeight);
      }
      dragStateRef.current = null;
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  //: Default compact height (matches the initial state above) so the
  //: toggle can return to "normal" after a user maxed it out.
  const DEFAULT_HEIGHT = 256;
  const maxPx = () => Math.floor(window.innerHeight * 0.7);
  const toggleMax = useCallback(() => {
    setListHeight((h) => (h >= maxPx() - 4 ? DEFAULT_HEIGHT : maxPx()));
  }, []);

  return (
    <div
      data-testid={testId}
      onClick={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.stopPropagation()}
      className="absolute bottom-2 left-14 z-10 w-72 rounded-md border border-gray-300 bg-white/95 shadow-sm"
    >
      <div className="flex w-full items-stretch gap-0 rounded-md text-[11px] text-gray-700">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex flex-1 items-center justify-between gap-2 rounded-l-md px-2 py-1 hover:bg-gray-50"
      >
        <span className="font-medium">
          {title} ({count})
        </span>
        <span className="text-gray-400">{open ? "▾" : "▸"}</span>
      </button>
      {open && count > 0 && (
        <button
          type="button"
          data-testid={`${testId}-maximize`}
          onClick={toggleMax}
          className="rounded-r-md border-l border-gray-200 px-2 text-gray-500 hover:bg-gray-100"
          title={listHeight >= maxPx() - 4 ? "还原高度" : "展开全部"}
        >
          {listHeight >= maxPx() - 4 ? "▼" : "▲"}
        </button>
      )}
      </div>
      {open && count > 0 && (
        <>
          {/* Top drag handle — the panel is bottom-anchored so users
              expect "drag up to grow". CSS resize only offers a
              bottom-right corner grip, which felt backwards; this is
              a JS-driven top edge handle that tracks pointer Y delta. */}
          <div
            data-testid={`${testId}-resize-handle`}
            onMouseDown={onHandleMouseDown}
            className="h-1.5 cursor-ns-resize border-t border-gray-200 bg-gray-50 hover:bg-gray-200"
            title="拖动调整高度"
          />
          <ul
            ref={listRef}
            style={{ maxHeight: `${listHeight}px` }}
            className="overflow-auto text-[11px]"
          >
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
                {((item.produced_tags && item.produced_tags.length > 0) ||
                  (item.consumed_tags && item.consumed_tags.length > 0)) && (
                  <div className="mt-1 flex flex-wrap gap-0.5 text-[9px]">
                    {item.produced_tags?.map((tag) => (
                      <span
                        key={`p-${tag}`}
                        className="rounded bg-emerald-50 px-1 py-[1px] font-mono text-emerald-700"
                        title={`produced: ${tag}`}
                      >
                        {`+${tag}`}
                      </span>
                    ))}
                    {item.consumed_tags?.map((tag) => (
                      <span
                        key={`c-${tag}`}
                        className="rounded bg-indigo-50 px-1 py-[1px] font-mono text-indigo-700"
                        title={`consumed: ${tag}`}
                      >
                        {`→${tag}`}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
          </ul>
        </>
      )}
      {open && count === 0 && (
        <div className="border-t border-gray-200 px-2 py-1 text-[11px] italic text-gray-400">
          {emptyText}
        </div>
      )}
    </div>
  );
}
