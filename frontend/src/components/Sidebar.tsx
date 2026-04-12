/**
 * Left sidebar — tree view with nested folders and chatflows.
 *
 * All folders are visible as collapsible tree nodes (like VS Code's
 * file explorer). Chatflows sit alongside sub-folders at each level.
 * Drag-and-drop works between any visible levels:
 * - Drag a chatflow onto a folder row → move into that folder
 * - Drag a chatflow onto the "root drop zone" → move to root
 * - Folders themselves are not draggable yet (future enhancement)
 *
 * A "root drop zone" strip appears at the top of the list when a drag
 * is in progress, so users can always move items back to the top level.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { chatflowDisplayTitle } from "@/lib/chatflowLabel";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlowSummary, Folder } from "@/types/schema";

const SIDEBAR_WIDTH = 260;
const SIDEBAR_COLLAPSED_WIDTH = 48;

export function Sidebar() {
  const { t } = useTranslation();
  const chatflowList = useChatFlowStore((s) => s.chatflowList);
  const folderList = useChatFlowStore((s) => s.folderList);
  const listLoading = useChatFlowStore((s) => s.listLoading);
  const collapsed = useChatFlowStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useChatFlowStore((s) => s.toggleSidebar);
  const currentId = useChatFlowStore((s) => s.chatflow?.id ?? null);
  const loadChatFlow = useChatFlowStore((s) => s.loadChatFlow);
  const createChatFlow = useChatFlowStore((s) => s.createChatFlow);
  const deleteChatFlow = useChatFlowStore((s) => s.deleteChatFlow);
  const createFolder = useChatFlowStore((s) => s.createFolder);
  const renameFolder = useChatFlowStore((s) => s.renameFolder);
  const deleteFolder = useChatFlowStore((s) => s.deleteFolder);
  const moveFolder = useChatFlowStore((s) => s.moveFolder);
  const moveChatFlowToFolder = useChatFlowStore((s) => s.moveChatFlowToFolder);
  const fetchList = useChatFlowStore((s) => s.fetchChatFlowList);

  const [pendingDelete, setPendingDelete] = useState<{
    type: "chatflow" | "folder";
    id: string;
  } | null>(null);

  const [inputDialog, setInputDialog] = useState<{
    title: string;
    defaultValue: string;
    onConfirm: (value: string) => void;
  } | null>(null);

  // Collapsed folders in the tree — persisted to localStorage so
  // refresh preserves the user's expand/collapse choices.
  const [collapsedIds, setCollapsedIds] = useState<Set<string>>(() => {
    try {
      const raw = localStorage.getItem("sidebar_collapsed");
      if (raw) return new Set(JSON.parse(raw) as string[]);
    } catch { /* ignore */ }
    return new Set();
  });

  // Whether a drag is in progress (to show the root drop zone).
  const [isDragging, setIsDragging] = useState(false);
  const [dragOverId, setDragOverId] = useState<string | null>(null);

  useEffect(() => {
    void fetchList();
  }, [fetchList]);

  // Build lookup maps.
  const { rootFolders, rootChatflows, childFolders, childChatflows } = useMemo(() => {
    const childFolders: Record<string, Folder[]> = {};
    const rootFolders: Folder[] = [];
    for (const f of folderList) {
      if (f.parent_id) {
        (childFolders[f.parent_id] ??= []).push(f);
      } else {
        rootFolders.push(f);
      }
    }
    const childChatflows: Record<string, ChatFlowSummary[]> = {};
    const rootChatflows: ChatFlowSummary[] = [];
    for (const cf of chatflowList) {
      if (cf.folder_id) {
        (childChatflows[cf.folder_id] ??= []).push(cf);
      } else {
        rootChatflows.push(cf);
      }
    }
    return { rootFolders, rootChatflows, childFolders, childChatflows };
  }, [folderList, chatflowList]);

  const toggleCollapse = useCallback((folderId: string) => {
    setCollapsedIds((prev) => {
      const next = new Set(prev);
      if (next.has(folderId)) next.delete(folderId);
      else next.add(folderId);
      localStorage.setItem("sidebar_collapsed", JSON.stringify([...next]));
      return next;
    });
  }, []);

  const handleSelect = (id: string) => {
    if (id !== currentId) void loadChatFlow(id);
  };

  const handleNewChat = () => void createChatFlow();

  const handleNewFolder = (parentId?: string | null) => {
    setInputDialog({
      title: t("sidebar.folder_name_prompt"),
      defaultValue: "",
      onConfirm: (name) => {
        if (name.trim()) void createFolder(name.trim(), parentId ?? null);
      },
    });
  };

  const handleRenameFolder = (e: React.MouseEvent, folderId: string, currentName: string) => {
    e.stopPropagation();
    setInputDialog({
      title: t("sidebar.folder_rename_prompt"),
      defaultValue: currentName,
      onConfirm: (name) => {
        if (name.trim() && name.trim() !== currentName) {
          void renameFolder(folderId, name.trim());
        }
      },
    });
  };

  const confirmDelete = () => {
    if (!pendingDelete) return;
    if (pendingDelete.type === "chatflow") {
      void deleteChatFlow(pendingDelete.id);
    } else {
      void deleteFolder(pendingDelete.id);
    }
    setPendingDelete(null);
  };

  // ---- Drag handlers ----
  const [draggedId, setDraggedId] = useState<string | null>(null);

  const onDragStart = useCallback(
    (e: React.DragEvent, type: "chatflow" | "folder", id: string) => {
      e.dataTransfer.setData("application/json", JSON.stringify({ type, id }));
      e.dataTransfer.effectAllowed = "move";
      // Need a minimal timeout so the browser captures the ghost image before
      // we modify the element's opacity via state.
      requestAnimationFrame(() => {
        setIsDragging(true);
        setDraggedId(id);
      });
    },
    [],
  );

  const onDragEnd = useCallback(() => {
    setIsDragging(false);
    setDragOverId(null);
    setDraggedId(null);
  }, []);

  const onFolderDragOver = useCallback((e: React.DragEvent, folderId: string) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDragOverId(folderId);
  }, []);

  const onFolderDragLeave = useCallback(() => setDragOverId(null), []);

  const onDrop = useCallback(
    (e: React.DragEvent, targetFolderId: string | null) => {
      e.preventDefault();
      e.stopPropagation();
      setDragOverId(null);
      setIsDragging(false);
      setDraggedId(null);
      try {
        const data = JSON.parse(e.dataTransfer.getData("application/json")) as {
          type: string;
          id: string;
        };
        if (data.type === "chatflow") {
          void moveChatFlowToFolder(data.id, targetFolderId);
        } else if (data.type === "folder" && data.id !== targetFolderId) {
          void moveFolder(data.id, targetFolderId);
        }
      } catch {
        // ignore
      }
    },
    [moveChatFlowToFolder],
  );

  // Count total items (recursive) inside a folder.
  const countItems = useCallback(
    (folderId: string): number => {
      const cfs = (childChatflows[folderId] ?? []).length;
      const subs = childFolders[folderId] ?? [];
      return cfs + subs.reduce((sum, f) => sum + 1 + countItems(f.id), 0);
    },
    [childFolders, childChatflows],
  );

  // ---- Recursive tree renderer ----
  const renderFolder = (folder: Folder, depth: number) => {
    const isCollapsed = collapsedIds.has(folder.id);
    const subs = childFolders[folder.id] ?? [];
    const cfs = childChatflows[folder.id] ?? [];
    const total = countItems(folder.id);

    return (
      <div key={folder.id} data-testid={`folder-${folder.id}`}>
        {/* Folder row */}
        <div
          draggable={true}
          onDragStart={(e) => onDragStart(e, "folder", folder.id)}
          onDragEnd={onDragEnd}
          onClick={() => toggleCollapse(folder.id)}
          onDragOver={(e) => onFolderDragOver(e, folder.id)}
          onDragLeave={onFolderDragLeave}
          onDrop={(e) => onDrop(e, folder.id)}
          className={[
            "group/folder flex cursor-pointer items-center gap-1 border-b border-gray-100 py-2 pr-2 text-xs text-gray-600 hover:bg-gray-100",
            dragOverId === folder.id ? "bg-blue-50 ring-1 ring-inset ring-blue-300" : "",
            draggedId === folder.id ? "opacity-40" : "",
          ].join(" ")}
          style={{ paddingLeft: 8 + depth * 16 }}
        >
          <span
            className="inline-block w-3 text-center text-[9px] text-gray-400 transition-transform"
            style={{ transform: isCollapsed ? "rotate(0deg)" : "rotate(90deg)" }}
          >
            {"\u25B6"}
          </span>
          <span className="text-[12px]">{"\uD83D\uDCC1"}</span>
          <span className="min-w-0 flex-1 truncate font-medium">{folder.name}</span>
          {total > 0 && (
            <span className="text-[10px] text-gray-400">{total}</span>
          )}
          {/* Add sub-folder */}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              handleNewFolder(folder.id);
            }}
            className="hidden h-4 w-4 items-center justify-center rounded text-[10px] text-gray-400 hover:text-blue-500 group-hover/folder:flex"
            title={t("sidebar.new_folder")}
          >
            +
          </button>
          <button
            type="button"
            onClick={(e) => handleRenameFolder(e, folder.id, folder.name)}
            className="hidden h-4 w-4 items-center justify-center rounded text-[10px] text-gray-400 hover:text-blue-500 group-hover/folder:flex"
            title={t("sidebar.folder_rename_prompt")}
          >
            {"\u270E"}
          </button>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setPendingDelete({ type: "folder", id: folder.id });
            }}
            className="hidden h-4 w-4 items-center justify-center rounded text-[10px] text-gray-400 hover:text-red-500 group-hover/folder:flex"
            title={t("chatflow.delete")}
          >
            {"\u2715"}
          </button>
        </div>

        {/* Children */}
        {!isCollapsed && (
          <>
            {subs.map((sub) => renderFolder(sub, depth + 1))}
            {cfs.map((cf) => renderChatflow(cf, depth + 1))}
            {subs.length === 0 && cfs.length === 0 && (
              <div
                className="border-b border-gray-100 py-1.5 text-[10px] italic text-gray-400"
                style={{ paddingLeft: 24 + depth * 16 }}
              >
                {t("sidebar.folder_empty")}
              </div>
            )}
          </>
        )}
      </div>
    );
  };

  const renderChatflow = (cf: ChatFlowSummary, depth: number) => (
    <div
      key={cf.id}
      data-testid={`sidebar-item-${cf.id}`}
      draggable={true}
      onDragStart={(e) => onDragStart(e, "chatflow", cf.id)}
      onDragEnd={onDragEnd}
      onClick={() => handleSelect(cf.id)}
      className={[
        "group/item flex cursor-pointer items-center gap-1.5 border-b border-gray-100 py-2 pr-2 text-xs transition-colors",
        cf.id === currentId
          ? "bg-blue-50 text-blue-700"
          : "text-gray-700 hover:bg-gray-100",
        draggedId === cf.id ? "opacity-40" : "",
      ].join(" ")}
      style={{ paddingLeft: 8 + depth * 16 }}
    >
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium">{chatflowDisplayTitle(cf)}</div>
        {cf.updated_at && (
          <div className="mt-0.5 text-[10px] text-gray-400">
            {formatRelativeDate(cf.updated_at)}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setPendingDelete({ type: "chatflow", id: cf.id });
        }}
        className="hidden h-5 w-5 flex-shrink-0 items-center justify-center rounded text-[10px] text-gray-400 hover:bg-red-50 hover:text-red-500 group-hover/item:flex"
        title={t("chatflow.delete")}
      >
        {"\u2715"}
      </button>
    </div>
  );

  return (
    <aside
      data-testid="sidebar"
      className="relative flex h-full flex-col border-r border-gray-200 bg-gray-50 transition-[width] duration-200"
      style={{ width: collapsed ? SIDEBAR_COLLAPSED_WIDTH : SIDEBAR_WIDTH }}
    >
      {/* Top bar */}
      <div className="flex items-center justify-between border-b border-gray-200 px-2 py-2">
        <button
          type="button"
          data-testid="sidebar-toggle"
          onClick={toggleSidebar}
          className="flex h-7 w-7 items-center justify-center rounded text-gray-500 hover:bg-gray-200 hover:text-gray-700"
          title={collapsed ? t("sidebar.expand") : t("sidebar.collapse")}
        >
          {collapsed ? "\u2261" : "\u2190"}
        </button>
        {!collapsed && (
          <div className="flex gap-1">
            <button
              type="button"
              data-testid="sidebar-new-folder"
              onClick={() => handleNewFolder(null)}
              className="rounded border border-gray-300 bg-white px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-100"
              title={t("sidebar.new_folder")}
            >
              {"\uD83D\uDCC1"}
            </button>
            <button
              type="button"
              data-testid="sidebar-new-chat"
              onClick={handleNewChat}
              className="rounded bg-blue-500 px-2.5 py-1 text-[11px] text-white hover:bg-blue-600"
            >
              + {t("chatflow.new")}
            </button>
          </div>
        )}
      </div>

      {collapsed ? (
        <div className="flex flex-col items-center gap-2 pt-3">
          <button
            type="button"
            onClick={handleNewChat}
            className="flex h-8 w-8 items-center justify-center rounded bg-blue-500 text-white hover:bg-blue-600"
            title={t("chatflow.new")}
          >
            +
          </button>
        </div>
      ) : (
        <div className="flex-1 overflow-auto">
          {listLoading && chatflowList.length === 0 && (
            <div className="px-3 py-4 text-[11px] text-gray-400">
              {t("chatflow.loading")}
            </div>
          )}

          {/* Tree: folders first, then unfiled chatflows */}
          {rootFolders.map((f) => renderFolder(f, 0))}

          {/* Separator */}
          {rootFolders.length > 0 && rootChatflows.length > 0 && (
            <div className="mx-3 my-0.5 border-t border-gray-300" />
          )}

          {rootChatflows.map((cf) => renderChatflow(cf, 0))}

          {/* Root drop zone — visible during drag, at the bottom so it doesn't shift the list */}
          {isDragging && (
            <div
              onDragOver={(e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = "move";
                setDragOverId("__root__");
              }}
              onDragLeave={() => setDragOverId(null)}
              onDrop={(e) => onDrop(e, null)}
              className={[
                "mx-2 mt-1 mb-1 rounded border border-dashed px-2 py-1.5 text-center text-[10px] transition-colors",
                dragOverId === "__root__"
                  ? "border-blue-400 bg-blue-50 text-blue-600"
                  : "border-gray-300 text-gray-400",
              ].join(" ")}
            >
              {t("sidebar.drop_to_root")}
            </div>
          )}

          {!listLoading && chatflowList.length === 0 && folderList.length === 0 && (
            <div className="px-3 py-8 text-center text-[11px] text-gray-400">
              {t("sidebar.no_chats")}
            </div>
          )}
        </div>
      )}

      {/* Delete confirmation */}
      {pendingDelete && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
          onClick={() => setPendingDelete(null)}
        >
          <div
            className="w-72 rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="mb-3 text-sm text-gray-700">
              {pendingDelete.type === "folder"
                ? t("sidebar.delete_folder_confirm")
                : t("sidebar.delete_confirm")}
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setPendingDelete(null)}
                className="rounded border border-gray-300 bg-white px-3 py-1 text-xs text-gray-600 hover:bg-gray-50"
              >
                {t("chatflow.cancel_action")}
              </button>
              <button
                type="button"
                data-testid="sidebar-delete-confirm"
                onClick={confirmDelete}
                className="rounded bg-red-500 px-3 py-1 text-xs text-white hover:bg-red-600"
              >
                {t("chatflow.delete")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Input dialog */}
      {inputDialog && (
        <InputDialog
          title={inputDialog.title}
          defaultValue={inputDialog.defaultValue}
          confirmLabel={t("sidebar.confirm")}
          cancelLabel={t("chatflow.cancel_action")}
          onConfirm={(value) => {
            inputDialog.onConfirm(value);
            setInputDialog(null);
          }}
          onCancel={() => setInputDialog(null)}
        />
      )}
    </aside>
  );
}

// ---------------------------------------------------------------- Input dialog

function InputDialog({
  title,
  defaultValue,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
}: {
  title: string;
  defaultValue: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(defaultValue);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      onConfirm(value);
    } else if (e.key === "Escape") {
      onCancel();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onCancel}
    >
      <div
        className="w-72 rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="mb-2 text-sm text-gray-700">{title}</p>
        <input
          type="text"
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          className="mb-3 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
        />
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="rounded border border-gray-300 bg-white px-3 py-1 text-xs text-gray-600 hover:bg-gray-50"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={() => onConfirm(value)}
            className="rounded bg-blue-500 px-3 py-1 text-xs text-white hover:bg-blue-600"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Helpers

function formatRelativeDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 7) return `${diffDay}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
