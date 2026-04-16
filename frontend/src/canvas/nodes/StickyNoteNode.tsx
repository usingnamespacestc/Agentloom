import { useCallback, useRef, useEffect } from "react";
import type { NodeProps } from "@xyflow/react";

export interface StickyNoteData extends Record<string, unknown> {
  text: string;
  onTextChange: (id: string, text: string) => void;
  onDelete: (id: string) => void;
}

export function StickyNoteNode({ id, data }: NodeProps) {
  const { text, onTextChange, onDelete } = data as StickyNoteData;
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      onTextChange(id, e.target.value);
    },
    [id, onTextChange],
  );

  const handleDelete = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation();
      onDelete(id);
    },
    [id, onDelete],
  );

  return (
    <div className="group min-w-[140px] max-w-[280px] rounded border border-yellow-300 bg-yellow-50 shadow-sm">
      <div className="flex items-center justify-between border-b border-yellow-200 px-2 py-0.5">
        <span className="text-[10px] font-medium text-yellow-700">Note</span>
        <button
          type="button"
          onClick={handleDelete}
          className="text-[10px] text-yellow-500 opacity-0 hover:text-red-500 group-hover:opacity-100"
        >
          ✕
        </button>
      </div>
      <textarea
        ref={ref}
        value={text}
        onChange={handleChange}
        className="w-full resize-none bg-transparent px-2 py-1 text-[12px] text-gray-700 placeholder:text-yellow-400 focus:outline-none"
        placeholder="Type a note…"
        rows={3}
      />
    </div>
  );
}
