/**
 * Right-side Conversation view (M8.5 round 2).
 *
 * Two modes:
 *
 * - **ChatFlow mode**: top shows the user/agent message chain from the
 *   root to (and strictly ending at) the currently selected ChatNode.
 *   Nothing past the selected node is rendered — if the user wants to
 *   see later messages, they need to click a later node. Inline branch
 *   selectors appear below any node on the path that has >1 children,
 *   so the user can switch branch mid-conversation. Bottom shows a
 *   disabled input box placeholder — M9 will wire it up.
 *
 * - **WorkFlow mode** (drill-down): top shows the I/O of the workflow
 *   path from root to the selected WorkFlow node (same strict prefix
 *   semantics). Bottom shows a read-only node detail panel.
 *
 * Chat styling is deliberately chat-app minimal: no "user"/"agent"
 * labels, no boxy borders. User message is a right-aligned bubble;
 * agent response is left-aligned flowing text. The currently selected
 * node gets a thin colored strip on the left — enough to locate it
 * without turning the panel into a form.
 *
 * The left edge is a draggable resize handle. Width is clamped in the
 * store (RIGHT_PANEL_MIN..RIGHT_PANEL_MAX).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Markdown from "react-markdown";
import { useTranslation } from "react-i18next";

import { resolvePath } from "./pathUtils";
import {
  RIGHT_PANEL_MAX,
  RIGHT_PANEL_MIN,
  useChatFlowStore,
} from "@/store/chatflowStore";
import type {
  ChatFlow,
  ChatFlowNode,
  NodeId,
  PendingTurn,
  WorkFlowNode,
} from "@/types/schema";

export function ConversationView() {
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const viewMode = useChatFlowStore((s) => s.viewMode);
  const drillDownChatNodeId = useChatFlowStore((s) => s.drillDownChatNodeId);
  const width = useChatFlowStore((s) => s.rightPanelWidth);
  const setWidth = useChatFlowStore((s) => s.setRightPanelWidth);

  const drilledChatNode =
    chatflow && drillDownChatNodeId ? chatflow.nodes[drillDownChatNodeId] ?? null : null;

  return (
    <aside
      data-testid="conversation-view"
      className="relative flex h-full flex-col border-l border-gray-200 bg-white"
      style={{ width }}
    >
      <ResizeHandle width={width} setWidth={setWidth} />
      {viewMode === "chatflow" ? (
        <ChatFlowConversation chatflow={chatflow} />
      ) : (
        <WorkFlowConversation chatNode={drilledChatNode} />
      )}
    </aside>
  );
}

// ---------------------------------------------------------------- Resize handle

interface ResizeHandleProps {
  width: number;
  setWidth: (w: number) => void;
}

function ResizeHandle({ width, setWidth }: ResizeHandleProps) {
  const startX = useRef(0);
  const startWidth = useRef(width);

  const onMouseMove = useCallback(
    (e: MouseEvent) => {
      const dx = startX.current - e.clientX; // panel grows as mouse moves left
      setWidth(startWidth.current + dx);
    },
    [setWidth],
  );

  const onMouseUp = useCallback(() => {
    window.removeEventListener("mousemove", onMouseMove);
    window.removeEventListener("mouseup", onMouseUp);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, [onMouseMove]);

  const onMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    startX.current = e.clientX;
    startWidth.current = width;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
  };

  useEffect(() => {
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, [onMouseMove, onMouseUp]);

  return (
    <div
      data-testid="conversation-resize-handle"
      onMouseDown={onMouseDown}
      className="absolute -left-1 top-0 z-10 h-full w-2 cursor-col-resize hover:bg-blue-200/40"
      title={`${RIGHT_PANEL_MIN}–${RIGHT_PANEL_MAX} px`}
    />
  );
}

// ---------------------------------------------------------------- ChatFlow mode

function ChatFlowConversation({ chatflow }: { chatflow: ChatFlow | null }) {
  const { t } = useTranslation();
  const selectedNodeId = useChatFlowStore((s) => s.selectedNodeId);
  const selectNode = useChatFlowStore((s) => s.selectNode);
  const pickBranch = useChatFlowStore((s) => s.pickBranch);
  const sendTurn = useChatFlowStore((s) => s.sendTurn);
  const deleteNode = useChatFlowStore((s) => s.deleteNode);
  const retryNode = useChatFlowStore((s) => s.retryNode);
  const deleteQueueItem = useChatFlowStore((s) => s.deleteQueueItem);
  const cancelNode = useChatFlowStore((s) => s.cancelNode);

  const [inputText, setInputText] = useState("");
  const [sending, setSending] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  const { path, forks } = useMemo(() => {
    if (!chatflow) return { path: [], forks: [] };
    return resolvePath(
      { nodes: chatflow.nodes, rootIds: chatflow.root_ids },
      selectedNodeId,
    );
  }, [chatflow, selectedNodeId]);

  const forkAt = useMemo(() => {
    const m = new Map<NodeId, (typeof forks)[number]>();
    for (const f of forks) m.set(f.nodeId, f);
    return m;
  }, [forks]);

  // The leaf of the current path — the node we send turns relative to.
  const leafNode = chatflow && path.length > 0
    ? chatflow.nodes[path[path.length - 1]] ?? null
    : null;

  // Auto-scroll to bottom when path changes.
  useEffect(() => {
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [path]);

  const handleSend = useCallback(async () => {
    const text = inputText.trim();
    if (!text || sending) return;
    setSending(true);
    setInputText("");
    try {
      // Always send relative to the selected node (the path endpoint).
      // If the selected node is a non-leaf, this creates a fork — a
      // new branch off that node, not an append to the latest leaf.
      await sendTurn(text, selectedNodeId ?? undefined);
    } finally {
      setSending(false);
    }
  }, [inputText, sending, sendTurn, selectedNodeId]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        void handleSend();
      }
    },
    [handleSend],
  );

  if (!chatflow) {
    return (
      <EmptyBody testid="conversation-empty">{t("chatflow.select_chatflow")}</EmptyBody>
    );
  }

  if (path.length === 0) {
    return <EmptyBody testid="conversation-empty">{t("chatflow.no_selection")}</EmptyBody>;
  }

  return (
    <>
      <header className="flex items-center justify-between border-b border-gray-100 px-4 py-2">
        <div className="text-sm font-semibold text-gray-800">{t("conversation.panel_title")}</div>
        <div className="text-[10px] text-gray-400">{path.length}</div>
      </header>

      <div
        ref={bodyRef}
        data-testid="conversation-body"
        className="flex-1 space-y-5 overflow-auto px-4 py-4"
      >
        {path.map((nid) => {
          const node = chatflow.nodes[nid];
          if (!node) return null;
          const fork = forkAt.get(nid);
          return (
            <div key={nid}>
              <ChatMessageBubble
                node={node}
                isSelected={nid === selectedNodeId}
                onSelect={() => selectNode(nid)}
              />
              {fork && (
                <BranchSelector
                  chatflowNodes={chatflow.nodes}
                  fork={fork}
                  onPick={(childId) => pickBranch(fork.nodeId, childId)}
                />
              )}
            </div>
          );
        })}

        {/* Pending queue items on the leaf node. */}
        {leafNode && leafNode.pending_queue?.length > 0 && (
          <div data-testid="pending-queue" className="space-y-2">
            {leafNode.pending_queue.map((p) => (
              <PendingBubble
                key={p.id}
                pending={p}
                onDelete={() => {
                  if (leafNode) void deleteQueueItem(leafNode.id, p.id);
                }}
              />
            ))}
          </div>
        )}
      </div>

      <footer className="border-t border-gray-100 bg-gray-50 px-4 py-2">
        {/* Cancel control for a running leaf node. */}
        {leafNode?.status === "running" && (
          <div data-testid="running-controls" className="mb-2 flex items-center gap-2">
            <span className="text-[11px] text-yellow-600">
              {t("conversation.running")}
            </span>
            <button
              type="button"
              data-testid="cancel-button"
              onClick={() => void cancelNode(leafNode.id)}
              className="rounded border border-red-300 bg-red-50 px-2 py-0.5 text-[10px] text-red-700 hover:bg-red-100"
            >
              {t("conversation.cancel")}
            </button>
          </div>
        )}

        {/* Retry / Delete controls for a failed leaf node. */}
        {leafNode?.status === "failed" && (
          <div data-testid="failed-controls" className="mb-2 flex items-center gap-2">
            <span className="text-[11px] text-red-600">
              {leafNode.error || t("node.status.failed")}
            </span>
            <button
              type="button"
              data-testid="retry-button"
              onClick={() => void retryNode(leafNode.id)}
              className="rounded border border-orange-300 bg-orange-50 px-2 py-0.5 text-[10px] text-orange-700 hover:bg-orange-100"
            >
              {t("conversation.retry")}
            </button>
            <button
              type="button"
              data-testid="delete-button"
              onClick={() => void deleteNode(leafNode.id)}
              className="rounded border border-red-300 bg-red-50 px-2 py-0.5 text-[10px] text-red-700 hover:bg-red-100"
            >
              {t("conversation.delete_failed")}
            </button>
          </div>
        )}

        <div className="flex gap-2">
          <textarea
            rows={2}
            data-testid="conversation-input"
            className="flex-1 resize-none rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 placeholder:text-gray-400 focus:border-blue-300 focus:outline-none"
            placeholder={t("conversation.input_placeholder_active")}
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <button
            type="button"
            data-testid="send-button"
            disabled={!inputText.trim() || sending}
            onClick={() => void handleSend()}
            className="self-end rounded bg-blue-500 px-3 py-1 text-xs text-white hover:bg-blue-600 disabled:bg-gray-300 disabled:text-gray-500"
          >
            {sending ? "…" : t("conversation.send")}
          </button>
        </div>
      </footer>
    </>
  );
}

/** Collect all thinking text from LLM call WorkNodes in a ChatNode's workflow. */
function collectThinking(node: ChatFlowNode): string {
  const parts: string[] = [];
  for (const wn of Object.values(node.workflow.nodes)) {
    const thinking = wn.output_message?.extras?.thinking;
    if (typeof thinking === "string" && thinking) {
      parts.push(thinking);
    }
  }
  return parts.join("\n\n");
}

/** Collapsible block for LLM thinking/reasoning content. */
function ThinkingBlock({ text, label }: { text: string; label: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 text-[11px] text-gray-400 hover:text-gray-600"
      >
        <span className="inline-block transition-transform" style={{ transform: open ? "rotate(90deg)" : "rotate(0deg)" }}>
          ▸
        </span>
        {label}
      </button>
      {open && (
        <div className="prose prose-sm mt-1 max-w-none rounded border border-gray-100 bg-gray-50 px-3 py-2 text-[12px] text-gray-500 break-words">
          <Markdown>{text}</Markdown>
        </div>
      )}
    </div>
  );
}

function ChatMessageBubble({
  node,
  isSelected,
  onSelect,
}: {
  node: ChatFlowNode;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const { t } = useTranslation();
  const userText = node.user_message?.text ?? "";
  const agentText = node.agent_response.text;
  const isRunning = node.status === "running";
  const isFailed = node.status === "failed";
  const thinking = collectThinking(node);

  return (
    <div
      data-testid={`conversation-node-${node.id}`}
      onClick={onSelect}
      className={[
        "group relative cursor-pointer pl-3 transition-colors",
        isSelected
          ? "border-l-2 border-blue-400"
          : "border-l-2 border-transparent hover:border-gray-200",
      ].join(" ")}
    >
      {userText && (
        <div className="mb-2 flex justify-end">
          <div className="prose prose-sm prose-invert max-w-[85%] rounded-2xl bg-blue-500 px-3 py-2 text-[13px] text-white break-words">
            <Markdown>{userText}</Markdown>
          </div>
        </div>
      )}
      {thinking && (
        <ThinkingBlock text={thinking} label={t("conversation.thinking")} />
      )}
      {agentText && (
        <div className={[
          "prose prose-sm max-w-none text-[13px] leading-relaxed break-words",
          isFailed ? "text-red-600" : "text-gray-800",
        ].join(" ")}>
          <Markdown>{agentText}</Markdown>
        </div>
      )}
      {isRunning && !agentText && (
        <div data-testid={`conversation-node-${node.id}-running`} className="flex items-center gap-1.5 text-[12px] text-yellow-600">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-yellow-400" />
          thinking…
        </div>
      )}
      {isFailed && !agentText && (
        <div className="text-[12px] text-red-500">{node.error || "failed"}</div>
      )}
      {!userText && !agentText && !isRunning && !isFailed && (
        <div className="text-[12px] italic text-gray-400">—</div>
      )}
    </div>
  );
}

interface BranchSelectorProps {
  chatflowNodes: Record<NodeId, ChatFlowNode>;
  fork: { nodeId: NodeId; childIds: NodeId[]; chosenChildId: NodeId | null };
  onPick: (childId: NodeId) => void;
}

function BranchSelector({ chatflowNodes, fork, onPick }: BranchSelectorProps) {
  const { t } = useTranslation();
  return (
    <div
      data-testid={`branch-selector-${fork.nodeId}`}
      className="mt-3 flex flex-wrap items-center gap-1.5 pl-3"
    >
      <span className="text-[10px] uppercase tracking-wide text-gray-400">
        {t("conversation.branch_label")}
      </span>
      {fork.childIds.map((cid, idx) => {
        const child = chatflowNodes[cid];
        const preview = previewFor(child);
        const isActive = cid === fork.chosenChildId;
        return (
          <button
            key={cid}
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onPick(cid);
            }}
            data-testid={`branch-option-${cid}`}
            className={[
              "rounded-full border px-2 py-0.5 text-[10px] transition-colors",
              isActive
                ? "border-blue-400 bg-blue-50 text-blue-700"
                : "border-gray-200 bg-white text-gray-500 hover:border-blue-300 hover:text-blue-600",
            ].join(" ")}
          >
            #{idx + 1} {preview}
          </button>
        );
      })}
    </div>
  );
}

function previewFor(node: ChatFlowNode | undefined): string {
  if (!node) return "";
  const msg = node.user_message?.text || node.agent_response.text || "";
  return msg.length > 24 ? `${msg.slice(0, 23)}…` : msg || "—";
}

// ---------------------------------------------------------------- Pending bubble

function PendingBubble({
  pending,
  onDelete,
}: {
  pending: PendingTurn;
  onDelete: () => void;
}) {
  return (
    <div
      data-testid={`pending-${pending.id}`}
      className="flex items-start justify-end gap-1.5"
    >
      <div className="max-w-[85%] rounded-2xl border border-dashed border-blue-300 bg-blue-50 px-3 py-2 text-[12px] text-blue-700 whitespace-pre-wrap break-words">
        <span className="mr-1 text-[10px] text-blue-400">queued</span>
        {pending.text}
      </div>
      <button
        type="button"
        onClick={onDelete}
        data-testid={`pending-delete-${pending.id}`}
        className="mt-1 text-[10px] text-red-400 hover:text-red-600"
        title="Remove from queue"
      >
        ✕
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- WorkFlow mode

function WorkFlowConversation({ chatNode }: { chatNode: ChatFlowNode | null }) {
  const { t } = useTranslation();
  const workflowSelectedNodeId = useChatFlowStore((s) => s.workflowSelectedNodeId);
  const selectWorkflowNode = useChatFlowStore((s) => s.selectWorkflowNode);
  const pickWorkflowBranch = useChatFlowStore((s) => s.pickWorkflowBranch);

  const { path, forks } = useMemo(() => {
    if (!chatNode) return { path: [], forks: [] };
    return resolvePath<WorkFlowNode>(
      { nodes: chatNode.workflow.nodes, rootIds: chatNode.workflow.root_ids },
      workflowSelectedNodeId,
    );
  }, [chatNode, workflowSelectedNodeId]);

  const forkAt = useMemo(() => {
    const m = new Map<NodeId, (typeof forks)[number]>();
    for (const f of forks) m.set(f.nodeId, f);
    return m;
  }, [forks]);

  if (!chatNode) {
    return <EmptyBody testid="conversation-empty">{t("workflow.no_selection")}</EmptyBody>;
  }

  if (path.length === 0) {
    return <EmptyBody testid="conversation-empty">{t("workflow.empty")}</EmptyBody>;
  }

  const selectedNode = workflowSelectedNodeId
    ? chatNode.workflow.nodes[workflowSelectedNodeId] ?? null
    : chatNode.workflow.nodes[path[path.length - 1]] ?? null;

  return (
    <>
      <header className="flex items-center justify-between border-b border-gray-100 px-4 py-2">
        <div className="text-sm font-semibold text-gray-800">{t("workflow.io_title")}</div>
        <div className="text-[10px] text-gray-400">{path.length}</div>
      </header>

      <div
        data-testid="conversation-body"
        className="flex-1 space-y-4 overflow-auto px-4 py-4"
      >
        {path.map((nid) => {
          const node = chatNode.workflow.nodes[nid];
          if (!node) return null;
          const fork = forkAt.get(nid);
          return (
            <div key={nid}>
              <WorkFlowIOBubble
                node={node}
                isSelected={nid === workflowSelectedNodeId}
                onSelect={() => selectWorkflowNode(nid)}
              />
              {fork && (
                <WorkFlowBranchSelector
                  wfNodes={chatNode.workflow.nodes}
                  fork={fork}
                  onPick={(childId) => pickWorkflowBranch(fork.nodeId, childId)}
                />
              )}
            </div>
          );
        })}
      </div>

      <footer className="max-h-[45%] overflow-auto border-t border-gray-100 bg-gray-50 px-4 py-2">
        <NodeDetailPanel node={selectedNode} />
      </footer>
    </>
  );
}

function WorkFlowIOBubble({
  node,
  isSelected,
  onSelect,
}: {
  node: WorkFlowNode;
  isSelected: boolean;
  onSelect: () => void;
}) {
  const kindColor =
    node.step_kind === "llm_call"
      ? "text-sky-600"
      : node.step_kind === "tool_call"
        ? "text-emerald-700"
        : "text-violet-700";

  return (
    <div
      data-testid={`wf-io-${node.id}`}
      onClick={onSelect}
      className={[
        "cursor-pointer pl-3 transition-colors",
        isSelected ? "border-l-2 border-blue-400" : "border-l-2 border-transparent hover:border-gray-200",
      ].join(" ")}
    >
      <div className={`mb-0.5 text-[10px] uppercase tracking-wide ${kindColor}`}>
        {node.step_kind.replace(/_/g, " ")}
      </div>
      {node.step_kind === "llm_call" && (
        <div className="prose prose-sm max-w-none text-[12px] leading-relaxed text-gray-800 break-words">
          {node.output_message?.content
            ? <Markdown>{node.output_message.content}</Markdown>
            : <span className="italic text-gray-400">—</span>}
        </div>
      )}
      {node.step_kind === "tool_call" && (
        <div>
          <div className="font-mono text-[11px] text-gray-700">{node.tool_name ?? "tool"}</div>
          {node.tool_result && (
            <div
              className={[
                "mt-0.5 text-[12px] whitespace-pre-wrap break-words",
                node.tool_result.is_error ? "text-red-700" : "text-gray-800",
              ].join(" ")}
            >
              {node.tool_result.content}
            </div>
          )}
        </div>
      )}
      {node.step_kind === "sub_agent_delegation" && (
        <div className="italic text-gray-500">delegation</div>
      )}
    </div>
  );
}

function WorkFlowBranchSelector({
  wfNodes,
  fork,
  onPick,
}: {
  wfNodes: Record<NodeId, WorkFlowNode>;
  fork: { nodeId: NodeId; childIds: NodeId[]; chosenChildId: NodeId | null };
  onPick: (childId: NodeId) => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      data-testid={`wf-branch-selector-${fork.nodeId}`}
      className="mt-2 flex flex-wrap items-center gap-1.5 pl-3"
    >
      <span className="text-[10px] uppercase tracking-wide text-gray-400">
        {t("conversation.branch_label")}
      </span>
      {fork.childIds.map((cid, idx) => {
        const child = wfNodes[cid];
        const preview = child ? t(`node.kind.${child.step_kind}`) : "";
        const isActive = cid === fork.chosenChildId;
        return (
          <button
            key={cid}
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onPick(cid);
            }}
            data-testid={`wf-branch-option-${cid}`}
            className={[
              "rounded-full border px-2 py-0.5 text-[10px] transition-colors",
              isActive
                ? "border-blue-400 bg-blue-50 text-blue-700"
                : "border-gray-200 bg-white text-gray-500 hover:border-blue-300 hover:text-blue-600",
            ].join(" ")}
          >
            #{idx + 1} {preview}
          </button>
        );
      })}
    </div>
  );
}

function NodeDetailPanel({ node }: { node: WorkFlowNode | null }) {
  const { t } = useTranslation();
  if (!node) {
    return <div className="italic text-[11px] text-gray-400">{t("workflow.no_selection")}</div>;
  }

  return (
    <div className="space-y-1.5 text-[11px]">
      <div className="font-semibold text-gray-800">{t("workflow.detail_title")}</div>
      <DetailRow label={t("workflow.detail_status")}>{node.status}</DetailRow>
      <DetailRow label={t("workflow.detail_description")}>
        {node.description.text || <span className="italic text-gray-400">—</span>}
      </DetailRow>
      <DetailRow label={t("workflow.detail_expected")}>
        {node.expected_outcome?.text || <span className="italic text-gray-400">—</span>}
      </DetailRow>
      {node.model_override && (
        <DetailRow label={t("workflow.detail_model")}>
          {`${node.model_override.provider_id}/${node.model_override.model_id}`}
        </DetailRow>
      )}
      {node.step_kind === "tool_call" && (
        <>
          <DetailRow label={t("workflow.detail_tool_name")}>
            <span className="font-mono">{node.tool_name ?? "—"}</span>
          </DetailRow>
          {node.tool_args && (
            <DetailRow label={t("workflow.detail_tool_args")}>
              <pre className="whitespace-pre-wrap break-words rounded bg-white px-1.5 py-0.5 font-mono text-[10px]">
                {JSON.stringify(node.tool_args, null, 2)}
              </pre>
            </DetailRow>
          )}
        </>
      )}
      {node.error && (
        <DetailRow label={t("workflow.detail_error")}>
          <span className="text-red-700">{node.error}</span>
        </DetailRow>
      )}
      {node.usage && (
        <DetailRow label={t("workflow.tokens")}>
          {`${node.usage.prompt_tokens}/${node.usage.completion_tokens}` +
            (node.usage.cached_tokens > 0 ? ` (${node.usage.cached_tokens} cached)` : "")}
        </DetailRow>
      )}
    </div>
  );
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase text-gray-500">{label}</div>
      <div className="break-words text-gray-800">{children}</div>
    </div>
  );
}

function EmptyBody({ testid, children }: { testid: string; children: React.ReactNode }) {
  return (
    <div
      data-testid={testid}
      className="flex h-full items-center justify-center text-xs text-gray-500"
    >
      {children}
    </div>
  );
}
