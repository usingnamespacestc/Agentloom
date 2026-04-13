/**
 * Three-segment slider for the ChatFlow's default_execution_mode.
 *
 * Lives in the top header (not in the settings modal) because the
 * mode meaningfully changes how every new turn behaves and the user
 * wants to see / change it at a glance.
 */

import { useTranslation } from "react-i18next";

import { useChatFlowStore } from "@/store/chatflowStore";
import { EXECUTION_MODES, type ExecutionMode } from "@/types/schema";

/** Per-mode color for the active segment. Gray → amber → violet
 * suggests escalating autonomy / risk. */
const ACTIVE_STYLE: Record<ExecutionMode, string> = {
  direct: "bg-gray-200 text-gray-900 ring-1 ring-gray-300",
  semi_auto: "bg-amber-200 text-amber-900 ring-1 ring-amber-400",
  auto: "bg-violet-200 text-violet-900 ring-1 ring-violet-400",
};

const DOT_STYLE: Record<ExecutionMode, string> = {
  direct: "bg-gray-400",
  semi_auto: "bg-amber-500",
  auto: "bg-violet-500",
};

export function ExecutionModeSlider() {
  const { t } = useTranslation();
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const patchChatFlow = useChatFlowStore((s) => s.patchChatFlow);

  if (!chatflow) return null;

  const value = chatflow.default_execution_mode;

  return (
    <div className="inline-flex h-7 flex-shrink-0 items-center rounded border border-gray-300 bg-gray-50 p-0.5">
      {EXECUTION_MODES.map((mode) => {
        const active = mode === value;
        return (
          <button
            key={mode}
            type="button"
            onClick={() => {
              if (mode !== value) void patchChatFlow({ default_execution_mode: mode });
            }}
            title={t(`chatflow_settings.execution_mode_${mode}_hint`)}
            data-testid={`execution-mode-${mode}`}
            className={[
              "inline-flex items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium transition-colors",
              active
                ? ACTIVE_STYLE[mode]
                : "text-gray-500 hover:text-gray-800",
            ].join(" ")}
          >
            <span
              className={[
                "inline-block h-1.5 w-1.5 rounded-full",
                active ? DOT_STYLE[mode] : "bg-gray-300",
              ].join(" ")}
            />
            {t(`chatflow_settings.execution_mode_${mode}_short`)}
          </button>
        );
      })}
    </div>
  );
}
