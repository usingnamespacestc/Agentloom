import { useCallback, useRef, useEffect, useState } from "react";
import { NodeResizer, type NodeProps } from "@xyflow/react";
import { useTranslation } from "react-i18next";

export interface StickyNoteData extends Record<string, unknown> {
  title: string;
  text: string;
  onTitleChange: (id: string, title: string) => void;
  onTextChange: (id: string, text: string) => void;
  onDelete: (id: string) => void;
}

const LONG_PRESS_MS = 300;

export function StickyNoteNode({ id, data, selected }: NodeProps) {
  const { title, text, onTitleChange, onTextChange, onDelete } = data as StickyNoteData;
  const { t } = useTranslation();
  const textRef = useRef<HTMLTextAreaElement>(null);
  const titleRef = useRef<HTMLInputElement>(null);

  // --- editing vs display mode ---
  const [editing, setEditing] = useState(false);

  // Exit editing when deselected
  useEffect(() => {
    if (!selected) setEditing(false);
  }, [selected]);

  useEffect(() => {
    if (editing) textRef.current?.focus();
  }, [editing]);

  const handleDoubleClick = useCallback((e: React.MouseEvent) => {
    e.stopPropagation();
    setEditing(true);
  }, []);

  // --- long-press drag ---
  const [dragReady, setDragReady] = useState(false);
  const longPressTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (editing) return;
    longPressTimer.current = setTimeout(() => {
      setDragReady(true);
    }, LONG_PRESS_MS);
  }, [editing]);

  const onPointerUp = useCallback(() => {
    if (longPressTimer.current) {
      clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
    if (dragReady) setDragReady(false);
  }, [dragReady]);

  useEffect(() => {
    if (!dragReady) return;
    const up = () => setDragReady(false);
    window.addEventListener("pointerup", up);
    return () => window.removeEventListener("pointerup", up);
  }, [dragReady]);

  // --- context menu ---
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (!ctxMenu) return;
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [ctxMenu]);

  const handleTitleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => onTitleChange(id, e.target.value),
    [id, onTitleChange],
  );

  const handleTextChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => onTextChange(id, e.target.value),
    [id, onTextChange],
  );

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onDelete(id);
    },
    [id, onDelete],
  );

  const handleContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setCtxMenu({ x: e.clientX, y: e.clientY });
  }, []);

  return (
    <div
      className={`group flex h-full w-full flex-col rounded border shadow-sm ${
        dragReady
          ? "border-yellow-500 bg-yellow-100 cursor-grabbing"
          : selected
            ? "border-yellow-400 bg-yellow-50"
            : "border-yellow-300 bg-yellow-50"
      } ${dragReady ? "sticky-drag-active" : "nodrag"}`}
      onContextMenu={handleContextMenu}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onDoubleClick={handleDoubleClick}
    >
      <NodeResizer
        isVisible={selected}
        minWidth={140}
        minHeight={80}
        lineClassName="!border-yellow-400"
        handleClassName="!h-2 !w-2 !rounded-sm !border-yellow-400 !bg-yellow-200"
      />
      <div className="flex items-center justify-between border-b border-yellow-200 px-2 py-0.5">
        {editing ? (
          <input
            ref={titleRef}
            type="text"
            value={title}
            onChange={handleTitleChange}
            className="nodrag min-w-0 flex-1 bg-transparent text-[10px] font-medium text-yellow-700 placeholder:text-yellow-400 focus:outline-none"
            placeholder="Note"
          />
        ) : (
          <span className="min-w-0 flex-1 truncate text-[10px] font-medium text-yellow-700">
            {title || "Note"}
          </span>
        )}
        <button
          type="button"
          onClick={handleDelete}
          className="nodrag ml-1 flex-shrink-0 text-[10px] text-yellow-500 opacity-0 hover:text-red-500 group-hover:opacity-100"
        >
          ✕
        </button>
      </div>
      {editing ? (
        <textarea
          ref={textRef}
          value={text}
          onChange={handleTextChange}
          className="nodrag min-h-0 flex-1 resize-none bg-transparent px-2 py-1 text-[12px] text-gray-700 placeholder:text-yellow-400 focus:outline-none"
          placeholder="Type a note…"
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap px-2 py-1 text-[12px] text-gray-700">
          {text || <span className="text-yellow-400">Type a note…</span>}
        </div>
      )}
      {ctxMenu && (
        <div
          className="fixed z-50 min-w-[120px] rounded border border-gray-200 bg-white py-1 shadow-lg"
          style={{ left: ctxMenu.x, top: ctxMenu.y }}
        >
          <button
            type="button"
            className="w-full px-3 py-1.5 text-left text-[12px] text-red-600 hover:bg-gray-100"
            onClick={handleDelete}
          >
            {t("canvas.delete_note")}
          </button>
        </div>
      )}
    </div>
  );
}
