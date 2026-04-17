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

import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from "react";
import Markdown from "react-markdown";
import { useTranslation } from "react-i18next";

import { resolvePath } from "./pathUtils";
import {
  RIGHT_PANEL_MAX,
  RIGHT_PANEL_MIN,
  resolveDrilledWorkflow,
  useChatFlowStore,
} from "@/store/chatflowStore";
import { usePreferencesStore, type ComposerModelMap } from "@/store/preferencesStore";
import { api } from "@/lib/api";
import type { ProviderSummary } from "@/lib/api";
import type {
  ChatFlow,
  ChatFlowNode,
  NodeId,
  PendingTurn,
  ProviderModelRef,
  TokenUsage,
  WireMessage,
  WorkFlow,
  WorkFlowNode,
} from "@/types/schema";

export function ConversationView() {
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const viewMode = useChatFlowStore((s) => s.viewMode);
  const drillStack = useChatFlowStore((s) => s.drillStack);
  const width = useChatFlowStore((s) => s.rightPanelWidth);
  const setWidth = useChatFlowStore((s) => s.setRightPanelWidth);

  const drilledWorkflow = useMemo(
    () => resolveDrilledWorkflow(chatflow, drillStack),
    [chatflow, drillStack],
  );

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
        <WorkFlowConversation workflow={drilledWorkflow} />
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
  const enqueueTurn = useChatFlowStore((s) => s.enqueueTurn);
  const deleteNode = useChatFlowStore((s) => s.deleteNode);
  const retryNode = useChatFlowStore((s) => s.retryNode);
  const deleteQueueItem = useChatFlowStore((s) => s.deleteQueueItem);
  const cancelNode = useChatFlowStore((s) => s.cancelNode);

  const composerModels = usePreferencesStore((s) => s.composerModels);
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
      // Routing:
      // - Leaf still running/planned: enqueue on the leaf so it walks
      //   down when the current turn finishes. The pending queue is
      //   editable (delete / reorder) from the UI bubble row above the
      //   composer.
      // - Leaf terminal (succeeded/failed) OR user selected a non-leaf
      //   node: fork / append via sendTurn. sendTurn + a non-null parent
      //   triggers the fork-semantics memory (new branch off selected).
      const busy =
        leafNode &&
        leafNode.id === selectedNodeId &&
        (leafNode.status === "running" || leafNode.status === "planned");
      if (busy && leafNode) {
        await enqueueTurn(leafNode.id, text, composerModels);
      } else {
        await sendTurn(text, selectedNodeId ?? undefined, composerModels);
      }
    } finally {
      setSending(false);
    }
  }, [
    inputText,
    sending,
    sendTurn,
    enqueueTurn,
    selectedNodeId,
    leafNode,
    composerModels,
  ]);

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

      <ComposerFooter
        leafNode={leafNode}
        inputText={inputText}
        setInputText={setInputText}
        sending={sending}
        handleSend={handleSend}
        handleKeyDown={handleKeyDown}
        cancelNode={cancelNode}
        retryNode={retryNode}
        deleteNode={deleteNode}
        composerModels={composerModels}
        t={t}
      />
    </>
  );
}

// ---------------------------------------------------------------- ComposerFooter

const FOOTER_MIN = 80;
const FOOTER_MAX = 400;
const FOOTER_DEFAULT = 120;

function ComposerFooter({
  leafNode,
  inputText,
  setInputText,
  sending,
  handleSend,
  handleKeyDown,
  cancelNode,
  retryNode,
  deleteNode,
  composerModels,
  t,
}: {
  leafNode: ChatFlowNode | null;
  inputText: string;
  setInputText: (v: string) => void;
  sending: boolean;
  handleSend: () => void;
  handleKeyDown: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  cancelNode: (id: string) => Promise<void>;
  retryNode: (id: string, models?: ComposerModelMap | null) => Promise<void>;
  deleteNode: (id: string) => Promise<void>;
  composerModels: ComposerModelMap;
  t: (k: string) => string;
}) {
  const [footerHeight, setFooterHeight] = useState(FOOTER_DEFAULT);
  const dragging = useRef(false);
  const startY = useRef(0);
  const startH = useRef(0);

  const onPointerDown = useCallback((e: RPointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    dragging.current = true;
    startY.current = e.clientY;
    startH.current = footerHeight;
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  }, [footerHeight]);

  const onPointerMove = useCallback((e: RPointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    const delta = startY.current - e.clientY;
    setFooterHeight(Math.max(FOOTER_MIN, Math.min(FOOTER_MAX, startH.current + delta)));
  }, []);

  const onPointerUp = useCallback(() => {
    dragging.current = false;
  }, []);

  return (
    <div className="flex flex-col" style={{ height: footerHeight, minHeight: FOOTER_MIN }}>
      {/* Drag handle */}
      <div
        data-testid="composer-resize-handle"
        className="group flex h-1.5 cursor-row-resize items-center justify-center border-t border-gray-100 hover:bg-blue-50"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
      >
        <div className="h-0.5 w-8 rounded-full bg-gray-300 group-hover:bg-blue-400" />
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-visible bg-gray-50 px-4 py-2">
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
              onClick={() => void retryNode(leafNode.id, composerModels)}
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

        <div className="mb-1.5">
          <ComposerModelPicker />
        </div>

        <div className="flex min-h-0 flex-1 gap-2">
          <textarea
            data-testid="conversation-input"
            className="min-h-0 flex-1 resize-none rounded border border-gray-200 bg-white px-2 py-1 text-xs text-gray-700 placeholder:text-gray-400 focus:border-blue-300 focus:outline-none"
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
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- ComposerModelPicker
//
// Per-turn model selector with one row per ModelKind (llm / judge /
// tool_call). Each kind is independently sticky — picks live in
// `usePreferencesStore.composerModels` and persist across turns and
// sessions until the user changes them. Each kind flows down to the
// engine as the PendingTurn's matching ``*_spawn_model`` field:
//
//   - llm        → ChatNode.resolved_model + main llm_call.model_override
//   - judge      → WorkFlow.judge_model_override (this turn only)
//   - tool_call  → WorkFlow.tool_call_model_override (this turn only)
//
// ``null`` for any kind = inherit (engine falls back through the
// chatflow's default for that kind, then to the main turn model).
// The button label shows the llm pick (or "judge+1" style ellipsis
// when other kinds are also pinned) so the most-changed knob is
// always visible.

type ComposerKind = "llm" | "judge" | "tool_call";
const COMPOSER_KINDS: ComposerKind[] = ["llm", "judge", "tool_call"];

function refKey(ref: ProviderModelRef | null): string {
  return ref ? `${ref.provider_id}::${ref.model_id}` : "";
}

function parseRefKey(key: string): ProviderModelRef | null {
  if (!key) return null;
  const [provider_id, ...rest] = key.split("::");
  return { provider_id, model_id: rest.join("::") };
}

function ComposerModelPicker() {
  const { t } = useTranslation();
  const composerModels = usePreferencesStore((s) => s.composerModels);
  const setComposerModel = usePreferencesStore((s) => s.setComposerModel);
  const [open, setOpen] = useState(false);
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const popupRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    void api
      .listProviders()
      .then((list) => {
        if (!cancelled) setProviders(list);
      })
      .catch(() => {
        // ignore — picker just shows empty list
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      const target = e.target as Node;
      if (popupRef.current?.contains(target)) return;
      if (buttonRef.current?.contains(target)) return;
      setOpen(false);
    };
    window.addEventListener("mousedown", onClick);
    return () => window.removeEventListener("mousedown", onClick);
  }, [open]);

  const modelOptions = useMemo(() => {
    const out: Array<{ key: string; label: string; pinned: boolean }> = [];
    for (const p of providers) {
      for (const m of p.available_models) {
        out.push({
          key: `${p.id}::${m.id}`,
          label: `${p.friendly_name} / ${m.id}`,
          pinned: m.pinned,
        });
      }
    }
    out.sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
    return out;
  }, [providers]);

  const llmRef = composerModels.llm;
  const otherPins = COMPOSER_KINDS.filter(
    (k) => k !== "llm" && composerModels[k] !== null,
  ).length;
  const buttonText = llmRef
    ? otherPins > 0
      ? `${llmRef.model_id} +${otherPins}`
      : llmRef.model_id
    : otherPins > 0
      ? `${t("composer_model.button_inherit")} +${otherPins}`
      : t("composer_model.button_inherit");
  const anyPinned = llmRef !== null || otherPins > 0;

  return (
    <div className="relative inline-block">
      <button
        ref={buttonRef}
        type="button"
        data-testid="composer-model-button"
        onClick={() => setOpen((v) => !v)}
        className={[
          "flex items-center gap-1 rounded border px-2 py-0.5 text-[10px]",
          anyPinned
            ? "border-blue-300 bg-blue-50 text-blue-700 hover:bg-blue-100"
            : "border-gray-200 bg-white text-gray-500 hover:bg-gray-50",
        ].join(" ")}
        title={t("composer_model.hint")}
      >
        <span className="text-gray-400">{t("composer_model.button_label")}:</span>
        <span className="font-medium">{buttonText}</span>
        <span className="text-gray-400">▾</span>
      </button>

      {open && (
        <div
          ref={popupRef}
          data-testid="composer-model-popup"
          className="absolute bottom-full left-0 z-20 mb-1 w-72 rounded-lg border border-gray-200 bg-white p-3 shadow-lg"
        >
          <div className="mb-2 text-[11px] font-semibold text-gray-700">
            {t("composer_model.title")}
          </div>
          {COMPOSER_KINDS.map((kind) => (
            <div key={kind} className="mb-2 last:mb-0">
              <div className="mb-1 text-[10px] font-medium text-gray-500">
                {t(`composer_model.kind_${kind}`)}
              </div>
              <select
                data-testid={`composer-model-select-${kind}`}
                value={refKey(composerModels[kind])}
                onChange={(e) => setComposerModel(kind, parseRefKey(e.target.value))}
                className="w-full rounded border border-gray-300 px-2 py-1 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
              >
                <option value="">{t("composer_model.inherit_option")}</option>
                {modelOptions.map((o) => (
                  <option key={o.key} value={o.key}>
                    {o.pinned ? "\u2605 " : ""}
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
          ))}
          <p className="mt-1 text-[10px] text-gray-400">{t("composer_model.hint")}</p>
        </div>
      )}
    </div>
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

function CopyTextButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onClick = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch { /* ignore */ }
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className="shrink-0 rounded px-1 py-0.5 text-[10px] text-gray-400 hover:bg-gray-100 hover:text-gray-600"
      title={t("common.copy")}
    >
      {copied ? t("common.copied") : t("common.copy")}
    </button>
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
  const showNodeId = usePreferencesStore((s) => s.showNodeId);
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
        <div className="mb-2 flex items-end justify-end gap-1">
          <CopyTextButton text={userText} />
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
      <MetaFooter
        nodeId={showNodeId ? node.id : null}
        usage={aggregateWorkflowUsage(node)}
        startedAt={node.started_at}
        finishedAt={node.finished_at}
        copyText={agentText || null}
      />
    </div>
  );
}

function aggregateWorkflowUsage(chatNode: ChatFlowNode): TokenUsage | null {
  let any = false;
  const acc: TokenUsage = {
    prompt_tokens: 0,
    completion_tokens: 0,
    total_tokens: 0,
    cached_tokens: 0,
    reasoning_tokens: 0,
  };
  for (const wn of Object.values(chatNode.workflow.nodes)) {
    const u = wn.usage;
    if (!u) continue;
    any = true;
    acc.prompt_tokens += u.prompt_tokens;
    acc.completion_tokens += u.completion_tokens;
    acc.total_tokens += u.total_tokens;
    acc.cached_tokens += u.cached_tokens;
    acc.reasoning_tokens += u.reasoning_tokens;
  }
  return any ? acc : null;
}

function durationSeconds(startedAt: string | null, finishedAt: string | null): number | null {
  if (!startedAt || !finishedAt) return null;
  const s = Date.parse(startedAt);
  const f = Date.parse(finishedAt);
  if (Number.isNaN(s) || Number.isNaN(f) || f <= s) return null;
  return (f - s) / 1000;
}

function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m${s.toString().padStart(2, "0")}s`;
}

function MetaFooter({
  nodeId,
  usage,
  startedAt,
  finishedAt,
  copyText,
}: {
  nodeId: string | null;
  usage: TokenUsage | null;
  startedAt: string | null;
  finishedAt: string | null;
  copyText?: string | null;
}) {
  const { t } = useTranslation();
  const showTokens = usePreferencesStore((s) => s.showTokens);
  const showGenTime = usePreferencesStore((s) => s.showGenTime);
  const showGenSpeed = usePreferencesStore((s) => s.showGenSpeed);
  const [copied, setCopied] = useState(false);

  const duration = durationSeconds(startedAt, finishedAt);
  const showUsage = usage !== null;
  const tokensPart = showTokens && showUsage
    ? `↑${usage!.prompt_tokens} ↓${usage!.completion_tokens}` +
      (usage!.cached_tokens > 0 ? ` (${usage!.cached_tokens} cached)` : "")
    : null;
  const timePart = showGenTime && duration !== null ? formatDuration(duration) : null;
  const speedPart = showGenSpeed && duration !== null && showUsage && usage!.completion_tokens > 0
    ? `${(usage!.completion_tokens / duration).toFixed(1)} tok/s`
    : null;

  const statsParts = [tokensPart, timePart, speedPart].filter(Boolean) as string[];
  if (nodeId === null && statsParts.length === 0) return null;

  const onCopyId = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!nodeId) return;
    try {
      await navigator.clipboard.writeText(nodeId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 900);
    } catch {
      // ignore
    }
  };

  return (
    <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px] text-gray-400">
      {statsParts.length > 0 && <span>{statsParts.join(" · ")}</span>}
      {nodeId && (
        <span
          onClick={onCopyId}
          className="cursor-pointer select-all truncate font-mono hover:text-blue-500"
          title={copied ? t("common.copied") : nodeId}
        >
          {copied ? t("common.copied") : nodeId}
        </span>
      )}
      {copyText && <CopyTextButton text={copyText} />}
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
  const { t } = useTranslation();
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
        title={t("conversation.queue_remove")}
      >
        ✕
      </button>
    </div>
  );
}

// ---------------------------------------------------------------- WorkFlow mode

function WorkFlowConversation({ workflow }: { workflow: WorkFlow | null }) {
  const { t } = useTranslation();
  const workflowSelectedNodeId = useChatFlowStore((s) => s.workflowSelectedNodeId);
  const selectWorkflowNode = useChatFlowStore((s) => s.selectWorkflowNode);
  const pickWorkflowBranch = useChatFlowStore((s) => s.pickWorkflowBranch);

  const { path, forks } = useMemo(() => {
    if (!workflow) return { path: [], forks: [] };
    return resolvePath<WorkFlowNode>(
      { nodes: workflow.nodes, rootIds: workflow.root_ids },
      workflowSelectedNodeId,
    );
  }, [workflow, workflowSelectedNodeId]);

  const forkAt = useMemo(() => {
    const m = new Map<NodeId, (typeof forks)[number]>();
    for (const f of forks) m.set(f.nodeId, f);
    return m;
  }, [forks]);

  if (!workflow) {
    return <EmptyBody testid="conversation-empty">{t("workflow.no_selection")}</EmptyBody>;
  }

  if (path.length === 0) {
    return <EmptyBody testid="conversation-empty">{t("workflow.empty")}</EmptyBody>;
  }

  const selectedNode = workflowSelectedNodeId
    ? workflow.nodes[workflowSelectedNodeId] ?? null
    : workflow.nodes[path[path.length - 1]] ?? null;

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
          const node = workflow.nodes[nid];
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
                  wfNodes={workflow.nodes}
                  fork={fork}
                  onPick={(childId) => pickWorkflowBranch(fork.nodeId, childId)}
                />
              )}
            </div>
          );
        })}
      </div>

      <footer className="max-h-[40%] overflow-auto border-t border-gray-100 bg-gray-50 px-4 py-2">
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
  const { t } = useTranslation();
  const showNodeId = usePreferencesStore((s) => s.showNodeId);
  const isRunning = node.status === "running";
  const isFailed = node.status === "failed";
  const streamingDelta = useChatFlowStore(
    (s) => (isRunning ? s.streamingDeltas[node.id] ?? "" : ""),
  );
  const kindColor =
    node.step_kind === "llm_call"
      ? "text-sky-600"
      : node.step_kind === "tool_call"
        ? "text-emerald-700"
        : node.step_kind === "judge_call"
          ? "text-amber-700"
          : "text-violet-700";

  const thinking =
    node.step_kind === "llm_call" &&
    typeof node.output_message?.extras?.thinking === "string"
      ? (node.output_message.extras.thinking as string)
      : "";

  return (
    <div
      data-testid={`wf-io-${node.id}`}
      onClick={onSelect}
      className={[
        "group cursor-pointer pl-3 transition-colors",
        isSelected
          ? "border-l-2 border-blue-400"
          : "border-l-2 border-transparent hover:border-gray-200",
      ].join(" ")}
    >
      <div className={`mb-1 text-[10px] uppercase tracking-wide ${kindColor}`}>
        {t(`node.kind.${node.step_kind}`)}
      </div>

      {node.step_kind === "llm_call" && (
        <>
          <NodeInputBlock node={node} t={t} />
          {thinking && (
            <ThinkingBlock text={thinking} label={t("conversation.thinking")} />
          )}
          {node.output_message?.content ? (
            <div
              className={[
                "prose prose-sm max-w-none text-[13px] leading-relaxed break-words",
                isFailed ? "text-red-600" : "text-gray-800",
              ].join(" ")}
            >
              <Markdown>{node.output_message.content}</Markdown>
            </div>
          ) : isRunning ? (
            streamingDelta ? (
              <div className="max-w-none text-[13px] leading-relaxed break-words text-gray-800 whitespace-pre-wrap">
                {streamingDelta}
                <span className="inline-block w-1.5 h-3 align-middle bg-sky-400 animate-pulse ml-0.5" />
              </div>
            ) : (
              <div className="flex items-center gap-1.5 text-[12px] text-yellow-600">
                <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-yellow-400" />
                thinking…
              </div>
            )
          ) : isFailed ? (
            <div className="text-[12px] text-red-500">{node.error || "failed"}</div>
          ) : (
            <div className="text-[12px] italic text-gray-400">—</div>
          )}
        </>
      )}

      {node.step_kind === "tool_call" && (
        <ToolCallBubbleBody node={node} t={t} isRunning={isRunning} />
      )}

      {node.step_kind === "judge_call" && (
        <>
          <NodeInputBlock node={node} t={t} />
          <JudgeBubbleBody node={node} t={t} isRunning={isRunning} isFailed={isFailed} />
        </>
      )}

      {node.step_kind === "sub_agent_delegation" && (
        <div className="text-[12px] italic text-gray-500">
          {t("node.kind.sub_agent_delegation")}
        </div>
      )}

      <MetaFooter
        nodeId={showNodeId ? node.id : null}
        usage={node.usage ?? null}
        startedAt={node.started_at}
        finishedAt={node.finished_at}
      />
    </div>
  );
}

function ToolCallBubbleBody({
  node,
  t,
  isRunning,
}: {
  node: WorkFlowNode;
  t: (k: string) => string;
  isRunning: boolean;
}) {
  const result = node.tool_result;
  const args = node.tool_args;
  return (
    <>
      <div className="mb-1 font-mono text-[12px] text-gray-700">
        {node.tool_name ?? "tool"}
      </div>
      {args && Object.keys(args).length > 0 && (
        <CollapsibleJSON
          label={t("workflow.detail_tool_args")}
          value={args}
          defaultOpen={false}
        />
      )}
      {result ? (
        <pre
          className={[
            "whitespace-pre-wrap break-words rounded bg-white/60 px-2 py-1.5 font-mono text-[12px] leading-relaxed",
            result.is_error ? "text-red-700" : "text-gray-800",
          ].join(" ")}
        >
          {result.content}
        </pre>
      ) : isRunning ? (
        <div className="flex items-center gap-1.5 text-[12px] text-yellow-600">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-yellow-400" />
          {t("node.status.running")}
        </div>
      ) : (
        <div className="text-[12px] italic text-gray-400">—</div>
      )}
    </>
  );
}

function JudgeBubbleBody({
  node,
  t,
  isRunning,
  isFailed,
}: {
  node: WorkFlowNode;
  t: (k: string) => string;
  isRunning: boolean;
  isFailed: boolean;
}) {
  const streamingDelta = useChatFlowStore(
    (s) => (isRunning ? s.streamingDeltas[node.id] ?? "" : ""),
  );
  const variant = node.judge_variant;
  const verdict = node.judge_verdict;
  const headline = verdict
    ? variant === "pre"
      ? verdict.feasibility
      : variant === "during"
        ? verdict.during_verdict
        : verdict.post_verdict
    : null;
  const headlineColor =
    headline === "ok" || headline === "accept" || headline === "continue"
      ? "text-green-700"
      : headline === "risky" || headline === "retry" || headline === "revise"
        ? "text-amber-700"
        : headline
          ? "text-red-700"
          : "text-gray-400";

  return (
    <>
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-[11px]">
        {variant && (
          <span className="rounded bg-amber-200/60 px-1.5 py-0.5 font-medium text-amber-900">
            {t(`workflow.judge_variant_${variant}`)}
          </span>
        )}
        {headline && (
          <span className={`font-semibold ${headlineColor}`}>{headline}</span>
        )}
      </div>

      {verdict?.user_message && (
        <div className="prose prose-sm mb-1 max-w-none text-[13px] leading-relaxed text-gray-800 break-words">
          <Markdown>{verdict.user_message}</Markdown>
        </div>
      )}

      {verdict?.blockers && verdict.blockers.length > 0 && (
        <JudgeList label={t("workflow.blockers")} items={verdict.blockers} />
      )}
      {verdict?.missing_inputs && verdict.missing_inputs.length > 0 && (
        <JudgeList label={t("workflow.missing_inputs")} items={verdict.missing_inputs} />
      )}
      {verdict?.critiques && verdict.critiques.length > 0 && (
        <JudgeList
          label={t("workflow.critiques")}
          items={verdict.critiques.map((c) => `[${c.severity}] ${c.issue}`)}
        />
      )}
      {verdict?.issues && verdict.issues.length > 0 && (
        <JudgeList
          label={t("workflow.issues")}
          items={verdict.issues.map(
            (i) => `${i.location}: ${i.expected} → ${i.actual}`,
          )}
        />
      )}

      {verdict?.redo_targets && verdict.redo_targets.length > 0 && (
        <JudgeList
          label={t("workflow.redo_targets")}
          items={verdict.redo_targets.map(
            (r) => `${r.node_id}: ${r.critique}`,
          )}
        />
      )}

      {verdict?.merged_response && (
        <div className="mt-1 rounded border border-gray-200 bg-gray-50/60 p-1.5">
          <div className="mb-0.5 text-[10px] font-medium uppercase tracking-wide text-gray-500">
            merged response
          </div>
          <div className="prose prose-sm max-w-none text-[13px] leading-relaxed break-words text-gray-700">
            <Markdown>{verdict.merged_response}</Markdown>
          </div>
        </div>
      )}

      {node.output_message?.content && (
        <div className="prose prose-sm max-w-none text-[13px] leading-relaxed break-words text-gray-700">
          <Markdown>{node.output_message.content}</Markdown>
        </div>
      )}

      {!verdict && isRunning && (
        streamingDelta ? (
          <div className="max-w-none text-[13px] leading-relaxed break-words text-gray-500 italic whitespace-pre-wrap">
            {streamingDelta}
            <span className="inline-block w-1.5 h-3 align-middle bg-amber-500 animate-pulse ml-0.5" />
          </div>
        ) : (
          <div className="flex items-center gap-1.5 text-[12px] text-yellow-600">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-yellow-400" />
            {t("node.status.running")}
          </div>
        )
      )}
      {!verdict && isFailed && (
        <div className="text-[12px] text-red-500">{node.error || "failed"}</div>
      )}
      {!verdict && !isRunning && !isFailed && (
        <div className="text-[12px] italic text-gray-400">—</div>
      )}
    </>
  );
}

function JudgeList({ label, items }: { label: string; items: string[] }) {
  return (
    <div className="mb-1">
      <div className="text-[10px] font-medium uppercase tracking-wide text-gray-500">
        {label}
      </div>
      <ul className="ml-4 list-disc text-[12px] leading-snug text-gray-700">
        {items.map((it, i) => (
          <li key={i} className="break-words">
            {it}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Collapsible "Input (N msgs)" block rendered above the output in
 * the upper bubble for llm_call / judge_call. Defaults to collapsed
 * because prompts are usually large and noisy. */
function NodeInputBlock({
  node,
  t,
}: {
  node: WorkFlowNode;
  t: (k: string) => string;
}) {
  const [open, setOpen] = useState(false);
  const msgs = node.input_messages;
  if (!msgs || msgs.length === 0) return null;
  return (
    <div className="mb-1.5 rounded border border-gray-200 bg-gray-50/60">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-0.5 text-left text-[10px] uppercase tracking-wide text-gray-500 hover:bg-gray-100"
      >
        <span>{t("workflow.detail_input_messages")} · {msgs.length}</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="border-t border-gray-200 px-1.5 py-1">
          <MessageList messages={msgs} />
        </div>
      )}
    </div>
  );
}

function CollapsibleJSON({
  label,
  value,
  defaultOpen,
}: {
  label: string;
  value: unknown;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mb-1.5 rounded border border-gray-200 bg-gray-50/60">
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-0.5 text-left text-[10px] uppercase tracking-wide text-gray-500 hover:bg-gray-100"
      >
        <span>{label}</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words border-t border-gray-200 px-1.5 py-1 font-mono text-[10px] text-gray-800">
          {JSON.stringify(value, null, 2)}
        </pre>
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

  const hasSubWorkflow =
    node.step_kind === "sub_agent_delegation" &&
    node.sub_workflow != null &&
    Object.keys(node.sub_workflow.nodes).length > 0;

  return (
    <div className="space-y-2 text-[11px]">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-gray-800">{t("workflow.detail_title")}</span>
          <span className="rounded bg-gray-200 px-1.5 py-0.5 font-mono text-[9px] uppercase text-gray-700">
            {node.step_kind}
          </span>
        </div>
        {hasSubWorkflow && (
          <button
            type="button"
            data-testid={`worknode-detail-${node.id}-enter`}
            onClick={(e) => {
              e.stopPropagation();
              // Reserved for sub_agent_delegation: route into the inner
              // WorkFlow once the engine actually populates ``sub_workflow``.
              window.dispatchEvent(
                new CustomEvent("agentloom:enter-sub-workflow", {
                  detail: { workNodeId: node.id },
                }),
              );
            }}
            className="flex items-center gap-1 rounded border border-gray-200 bg-white px-1.5 py-0.5 text-[10px] text-gray-600 hover:border-blue-300 hover:bg-blue-50 hover:text-blue-700"
          >
            <span>⤢</span>
            <span>{t("chatflow.open_workflow")}</span>
          </button>
        )}
      </div>

      <DetailRow label={t("workflow.detail_status")}>
        <span className="capitalize">{node.status}</span>
      </DetailRow>
      <DetailRow label={t("workflow.detail_id")}>
        <CopyableId nodeId={node.id} />
      </DetailRow>
      <DetailRow label={t("workflow.detail_description")}>
        {node.description.text || <span className="italic text-gray-400">—</span>}
      </DetailRow>
      {node.expected_outcome?.text && (
        <DetailRow label={t("workflow.detail_expected")}>
          {node.expected_outcome.text}
        </DetailRow>
      )}
      {node.model_override && (
        <DetailRow label={t("workflow.detail_model")}>
          <span className="font-mono">
            {`${node.model_override.provider_id}/${node.model_override.model_id}`}
          </span>
        </DetailRow>
      )}

      {node.step_kind === "tool_call" && node.tool_name && (
        <DetailRow label={t("workflow.detail_tool_name")}>
          <span className="font-mono">{node.tool_name}</span>
        </DetailRow>
      )}

      {node.error && (
        <DetailRow label={t("workflow.detail_error")}>
          <pre className="whitespace-pre-wrap break-words rounded bg-red-50 px-1.5 py-1 font-mono text-[10px] text-red-700">
            {node.error}
          </pre>
        </DetailRow>
      )}
      {node.usage && (
        <DetailRow label={t("workflow.tokens")}>
          <UsageBreakdown usage={node.usage} />
        </DetailRow>
      )}
    </div>
  );
}

function CopyableId({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const onCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(nodeId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 900);
    } catch {
      // ignore
    }
  };
  return (
    <span
      onClick={onCopy}
      className="cursor-pointer select-all break-all font-mono text-[10px] text-gray-600 hover:text-blue-500"
      title={copied ? t("common.copied") : nodeId}
    >
      {copied ? t("common.copied") : nodeId}
    </span>
  );
}

function UsageBreakdown({ usage }: { usage: TokenUsage }) {
  const parts: string[] = [
    `↑${usage.prompt_tokens}`,
    `↓${usage.completion_tokens}`,
  ];
  if (usage.cached_tokens > 0) parts.push(`(${usage.cached_tokens} cached)`);
  if (usage.reasoning_tokens > 0) parts.push(`(${usage.reasoning_tokens} reasoning)`);
  return <div className="font-mono text-[10px] text-gray-700">{parts.join(" ")}</div>;
}

function MessageList({ messages }: { messages: WireMessage[] }) {
  return (
    <div className="space-y-1">
      {messages.map((msg, i) => (
        <MessageRow key={i} message={msg} />
      ))}
    </div>
  );
}

function MessageRow({ message }: { message: WireMessage }) {
  const [open, setOpen] = useState(true);
  const roleColor =
    message.role === "user"
      ? "bg-blue-100 text-blue-800"
      : message.role === "assistant"
        ? "bg-emerald-100 text-emerald-800"
        : message.role === "system"
          ? "bg-gray-200 text-gray-700"
          : "bg-amber-100 text-amber-800";
  const hasToolUses = message.tool_uses && message.tool_uses.length > 0;
  const hasContent = !!message.content;
  return (
    <div className="rounded border border-gray-200 bg-white">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 rounded px-1.5 py-0.5 text-left hover:bg-gray-50"
      >
        <span className={`rounded px-1.5 py-0.5 text-[9px] font-semibold uppercase ${roleColor}`}>
          {message.role}
        </span>
        <span className="text-[9px] text-gray-400">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && (hasContent || hasToolUses) && (
        <div className="border-t border-gray-100 px-1.5 py-1">
          {hasContent && (
            <div className="prose prose-sm max-w-none whitespace-pre-wrap break-words text-[11px] text-gray-800">
              <Markdown>{message.content}</Markdown>
            </div>
          )}
          {hasToolUses && (
            <div className="mt-1 space-y-0.5">
              {message.tool_uses.map((tu) => (
                <div
                  key={tu.id}
                  className="rounded bg-gray-50 px-1.5 py-0.5 font-mono text-[10px] text-gray-700"
                >
                  <span className="font-semibold">{tu.name}</span>
                  {Object.keys(tu.arguments ?? {}).length > 0 && (
                    <pre className="mt-0.5 whitespace-pre-wrap break-words text-[10px]">
                      {JSON.stringify(tu.arguments, null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-gray-500">{label}</div>
      <div className="mt-0.5 break-words text-gray-800">{children}</div>
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
