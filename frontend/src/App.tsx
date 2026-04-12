/**
 * M8.5 app shell — left sidebar + horizontal ChatFlow canvas +
 * ConversationView, with drill-down to a full-canvas WorkFlow view.
 *
 * The sidebar lists all chatflows and supports create / switch / delete.
 * A ``?chatflow=<id>`` query param is still honoured on first load for
 * backward compatibility and share links.
 */

import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { ChatFlowCanvas } from "@/canvas/ChatFlowCanvas";
import { ConversationView } from "@/canvas/ConversationView";
import { WorkFlowCanvas } from "@/canvas/WorkFlowCanvas";
import { ChatFlowHeader } from "@/components/ChatFlowHeader";
import { Sidebar } from "@/components/Sidebar";
import { useChatFlowStore } from "@/store/chatflowStore";

export default function App() {
  const { t } = useTranslation();
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const loadState = useChatFlowStore((s) => s.loadState);
  const errorMessage = useChatFlowStore((s) => s.errorMessage);
  const viewMode = useChatFlowStore((s) => s.viewMode);
  const drillDownChatNodeId = useChatFlowStore((s) => s.drillDownChatNodeId);
  const exitWorkflow = useChatFlowStore((s) => s.exitWorkflow);
  const loadChatFlow = useChatFlowStore((s) => s.loadChatFlow);
  const setSSEFactory = useChatFlowStore((s) => s.setSSEFactory);

  // Grab ``?chatflow=`` once, before the first load attempt.
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

  const drilledChatNode =
    chatflow && drillDownChatNodeId ? chatflow.nodes[drillDownChatNodeId] ?? null : null;

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
            <WorkFlowCanvas chatNode={drilledChatNode} />
          )}

          {viewMode === "workflow" && (
            <button
              type="button"
              onClick={exitWorkflow}
              data-testid="exit-workflow"
              className="absolute left-3 top-3 z-20 rounded border border-gray-300 bg-white/90 px-2 py-1 text-xs text-gray-700 shadow-sm hover:bg-white"
            >
              ← {t("workflow.back_to_chatflow")}
            </button>
          )}
        </section>

        <ConversationView />
      </main>
    </div>
  );
}
