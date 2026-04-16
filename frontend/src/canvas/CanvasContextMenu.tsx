import { useTranslation } from "react-i18next";

export interface CanvasContextMenuProps {
  x: number;
  y: number;
  onInsertNote: () => void;
  onClose: () => void;
}

export function CanvasContextMenu({ x, y, onInsertNote, onClose }: CanvasContextMenuProps) {
  const { t } = useTranslation();
  return (
    <div
      className="fixed z-50 min-w-[140px] rounded border border-gray-200 bg-white py-1 shadow-lg"
      style={{ left: x, top: y }}
      onContextMenu={(e) => { e.preventDefault(); onClose(); }}
    >
      <button
        type="button"
        className="w-full px-3 py-1.5 text-left text-[12px] text-gray-700 hover:bg-gray-100"
        onClick={() => { onInsertNote(); onClose(); }}
      >
        {t("canvas.insert_note")}
      </button>
    </div>
  );
}

export interface StickyNoteMenuProps {
  x: number;
  y: number;
  onDelete: () => void;
  onClose: () => void;
}

export function StickyNoteContextMenu({ x, y, onDelete, onClose }: StickyNoteMenuProps) {
  const { t } = useTranslation();
  return (
    <div
      className="fixed z-50 min-w-[120px] rounded border border-gray-200 bg-white py-1 shadow-lg"
      style={{ left: x, top: y }}
      onContextMenu={(e) => { e.preventDefault(); onClose(); }}
    >
      <button
        type="button"
        className="w-full px-3 py-1.5 text-left text-[12px] text-red-600 hover:bg-gray-100"
        onClick={() => { onDelete(); onClose(); }}
      >
        {t("canvas.delete_note")}
      </button>
    </div>
  );
}
