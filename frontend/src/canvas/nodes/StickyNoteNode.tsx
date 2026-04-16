import { useCallback, useRef, useEffect, useState } from "react";
import { NodeResizer, type NodeProps } from "@xyflow/react";

export interface StickyNoteData extends Record<string, unknown> {
  title: string;
  text: string;
  onTitleChange: (id: string, title: string) => void;
  onTextChange: (id: string, text: string) => void;
  onDelete: (id: string) => void;
}

export function StickyNoteNode({ id, data, selected }: NodeProps) {
  const { title, text, onTitleChange, onTextChange, onDelete } = data as StickyNoteData;
  const textRef = useRef<HTMLTextAreaElement>(null);

  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!selected) setEditing(false);
  }, [selected]);

  useEffect(() => {
    if (editing) textRef.current?.focus();
  }, [editing]);

  const handleDoubleClick = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setEditing(true);
  }, []);

  const handleTitleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => onTitleChange(id, e.target.value),
    [id, onTitleChange],
  );

  const handleTextChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => onTextChange(id, e.target.value),
    [id, onTextChange],
  );

  return (
    <div
      className={`group flex h-full w-full flex-col rounded border shadow-sm ${
        selected ? "border-yellow-400 bg-yellow-50" : "border-yellow-300 bg-yellow-50"
      }`}
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
            type="text"
            value={title}
            onChange={handleTitleChange}
            className="nodrag nowheel min-w-0 flex-1 bg-transparent text-[10px] font-medium text-yellow-700 placeholder:text-yellow-400 focus:outline-none"
            placeholder="Note"
            onDoubleClick={(e) => e.stopPropagation()}
          />
        ) : (
          <span className="min-w-0 flex-1 select-none truncate text-[10px] font-medium text-yellow-700">
            {title || "Note"}
          </span>
        )}
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onDelete(id); }}
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
          className="nodrag nowheel min-h-0 flex-1 resize-none bg-transparent px-2 py-1 text-[12px] text-gray-700 placeholder:text-yellow-400 focus:outline-none"
          placeholder="Type a note…"
          onDoubleClick={(e) => e.stopPropagation()}
        />
      ) : (
        <div className="min-h-0 flex-1 select-none overflow-auto whitespace-pre-wrap px-2 py-1 text-[12px] text-gray-700">
          {text || <span className="text-yellow-400">Type a note…</span>}
        </div>
      )}
    </div>
  );
}
