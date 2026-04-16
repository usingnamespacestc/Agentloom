import { useCallback, useRef, useEffect } from "react";
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

  useEffect(() => {
    textRef.current?.focus();
  }, []);

  const handleTitleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      onTitleChange(id, e.target.value);
    },
    [id, onTitleChange],
  );

  const handleTextChange = useCallback(
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
    <div className="group flex h-full w-full flex-col rounded border border-yellow-300 bg-yellow-50 shadow-sm">
      <NodeResizer
        isVisible={selected}
        minWidth={140}
        minHeight={80}
        lineClassName="!border-yellow-400"
        handleClassName="!h-2 !w-2 !rounded-sm !border-yellow-400 !bg-yellow-200"
      />
      <div className="flex items-center justify-between border-b border-yellow-200 px-2 py-0.5">
        <input
          type="text"
          value={title}
          onChange={handleTitleChange}
          className="min-w-0 flex-1 bg-transparent text-[10px] font-medium text-yellow-700 placeholder:text-yellow-400 focus:outline-none"
          placeholder="Note"
        />
        <button
          type="button"
          onClick={handleDelete}
          className="ml-1 flex-shrink-0 text-[10px] text-yellow-500 opacity-0 hover:text-red-500 group-hover:opacity-100"
        >
          ✕
        </button>
      </div>
      <textarea
        ref={textRef}
        value={text}
        onChange={handleTextChange}
        className="min-h-0 flex-1 resize-none bg-transparent px-2 py-1 text-[12px] text-gray-700 placeholder:text-yellow-400 focus:outline-none"
        placeholder="Type a note…"
      />
    </div>
  );
}
