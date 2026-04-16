/**
 * App shell — left sidebar + horizontal ChatFlow canvas +
 * ConversationView, with drill-down to a full-canvas WorkFlow view
 * that supports nested sub-workflows (§3.4.3).
 *
 * The breadcrumb in the top-left walks the drill stack: ChatFlow →
 * outer WorkFlow → sub-workflow (depth N). Earlier segments are
 * clickable and truncate the stack back to that depth.
 */

import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { ChatFlowCanvas } from "@/canvas/ChatFlowCanvas";
import { ConversationView } from "@/canvas/ConversationView";
import { WorkFlowCanvas } from "@/canvas/WorkFlowCanvas";
import { ChatFlowHeader } from "@/components/ChatFlowHeader";
import { Sidebar } from "@/components/Sidebar";
import { resolveDrilledWorkflow, useChatFlowStore } from "@/store/chatflowStore";

export default function App() {
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const loadState = useChatFlowStore((s) => s.loadState);
  const errorMessage = useChatFlowStore((s) => s.errorMessage);
  const viewMode = useChatFlowStore((s) => s.viewMode);
  const drillStack = useChatFlowStore((s) => s.drillStack);
  const loadChatFlow = useChatFlowStore((s) => s.loadChatFlow);
  const setSSEFactory = useChatFlowStore((s) => s.setSSEFactory);
  const { t } = useTranslation();

  const initialChatflowId = useMemo(() => {
    if (typeof window === "undefined") return null;
    const params = new URLSearchParams(window.location.search);
    return params.get("chatflow");
  }, []);

  useEffect(() => {
    if (typeof window !== "undefined" && typeof EventSource !== "undefined") {
      setSSEFactory((url) => new EventSource(url));
    }
    if (initialChatflowId) {
      void loadChatFlow(initialChatflowId);
    }
  }, [initialChatflowId, loadChatFlow, setSSEFactory]);

  const drilledWorkflow = useMemo(
    () => resolveDrilledWorkflow(chatflow, drillStack),
    [chatflow, drillStack],
  );
  const outerChatNodeId =
    drillStack.length > 0 && drillStack[0].kind === "chatnode" ? drillStack[0].chatNodeId : null;
  const subPath = useMemo(
    () => drillStack.slice(1).map((f) => (f.kind === "subworkflow" ? f.parentWorkNodeId : "")).filter(Boolean),
    [drillStack],
  );

  return (
    <div className="flex h-full flex-col">
      <ChatFlowHeader />

      <main className="flex min-h-0 flex-1">
        <Sidebar />

        <section className="relative min-w-0 flex-1">
          {loadState === "loading" && (
            <div
              data-testid="chatflow-loading"
              className="absolute inset-0 z-10 flex items-center justify-center bg-white/70 text-sm text-gray-500"
            >
              {t("chatflow.loading")}
            </div>
          )}
          {loadState === "error" && (
            <div
              data-testid="chatflow-error"
              className="absolute inset-0 z-10 flex items-center justify-center bg-white/70 text-sm text-red-600"
            >
              {t("chatflow.load_failed")}
              {errorMessage ? `: ${errorMessage}` : ""}
            </div>
          )}

          {viewMode === "chatflow" ? (
            <ChatFlowCanvas chatflow={chatflow} />
          ) : (
            <WorkFlowCanvas
              workflow={drilledWorkflow}
              outerChatNodeId={outerChatNodeId}
              subPath={subPath}
            />
          )}

          {viewMode === "workflow" && <DrillBreadcrumb />}
        </section>

        <ConversationView />
      </main>
    </div>
  );
}

function DrillBreadcrumb() {
  const { t } = useTranslation();
  const drillStack = useChatFlowStore((s) => s.drillStack);
  const exitWorkflow = useChatFlowStore((s) => s.exitWorkflow);
  const truncateDrillStack = useChatFlowStore((s) => s.truncateDrillStack);

  return (
    <nav
      data-testid="drill-breadcrumb"
      className="absolute left-3 top-3 z-20 flex items-center gap-1 rounded border border-gray-300 bg-white/90 px-2 py-1 text-xs text-gray-700 shadow-sm"
    >
      <button
        type="button"
        onClick={exitWorkflow}
        data-testid="exit-workflow"
        className="hover:underline"
      >
        ← {t("workflow.breadcrumb_chatflow")}
      </button>
      {drillStack.map((frame, idx) => {
        const isLast = idx === drillStack.length - 1;
        const truncateTo = idx + 1;
        const label =
          frame.kind === "chatnode"
            ? t("workflow.breadcrumb_outer")
            : t("workflow.breadcrumb_subworkflow_depth", { depth: idx });
        return (
          <span key={idx} className="flex items-center gap-1">
            <span className="text-gray-400">/</span>
            {isLast ? (
              <span className="font-medium text-gray-900">{label}</span>
            ) : (
              <button
                type="button"
                data-testid={`drill-breadcrumb-${idx}`}
                onClick={() => truncateDrillStack(truncateTo)}
                className="hover:underline"
              >
                {label}
              </button>
            )}
          </span>
        );
      })}
    </nav>
  );
}
