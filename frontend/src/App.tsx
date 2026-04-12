/**
 * M8.5 app shell — horizontal ChatFlow canvas + ConversationView,
 * with drill-down to a full-canvas WorkFlow view.
 *
 * Loads a chatflow by reading the ``?chatflow=<id>`` query param on
 * mount. M9 will add a real routing story.
 *
 * The right panel is a single <ConversationView> that adapts to
 * whichever view mode is active (chatflow vs. workflow drill-down).
 */

import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";

import { ChatFlowCanvas } from "@/canvas/ChatFlowCanvas";
import { ConversationView } from "@/canvas/ConversationView";
import { WorkFlowCanvas } from "@/canvas/WorkFlowCanvas";
import { useChatFlowStore } from "@/store/chatflowStore";

export default function App() {
  const { t, i18n } = useTranslation();
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
    // Only enable SSE in the real browser. vitest runs in happy-dom
    // where EventSource exists but there's nothing to connect to.
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
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-2">
        <div>
          <h1 className="text-lg font-semibold text-gray-900">{t("app.title")}</h1>
          <p className="text-xs text-gray-500">{t("app.tagline")}</p>
        </div>
        <button
          type="button"
          className="rounded border border-gray-300 bg-white px-3 py-1 text-xs text-gray-700 hover:bg-gray-50"
          onClick={() =>
            i18n.changeLanguage(i18n.language === "zh-CN" ? "en-US" : "zh-CN")
          }
        >
          {t("app.switch_language")}
        </button>
      </header>

      <main className="flex min-h-0 flex-1">
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
