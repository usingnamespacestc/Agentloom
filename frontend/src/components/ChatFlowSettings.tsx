/**
 * Per-ChatFlow settings modal.
 *
 * Scope: settings that belong to a single chatflow (not the whole
 * workspace). First version only exposes the two default models
 * (chat / work); system prompt, tool allowlist, etc. land later.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api";
import type { MCPServerState, ProviderSummary, ToolDTO } from "@/lib/api";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ProviderModelRef } from "@/types/schema";

type TabId = "models" | "execution" | "compact" | "tools";

const TABS: Array<{ id: TabId; labelKey: string }> = [
  { id: "models", labelKey: "chatflow_settings.tab_models" },
  { id: "execution", labelKey: "chatflow_settings.tab_execution" },
  { id: "compact", labelKey: "chatflow_settings.tab_compact" },
  { id: "tools", labelKey: "chatflow_settings.tab_tools" },
];

interface ChatFlowSettingsProps {
  open: boolean;
  onClose: () => void;
}

function refKey(ref: ProviderModelRef | null): string {
  return ref ? `${ref.provider_id}::${ref.model_id}` : "";
}

function parseRefKey(key: string): ProviderModelRef | null {
  if (!key) return null;
  const [provider_id, ...rest] = key.split("::");
  return { provider_id, model_id: rest.join("::") };
}

export function ChatFlowSettings({ open, onClose }: ChatFlowSettingsProps) {
  const { t } = useTranslation();
  const chatflow = useChatFlowStore((s) => s.chatflow);
  const patchChatFlow = useChatFlowStore((s) => s.patchChatFlow);

  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [mcpServers, setMcpServers] = useState<MCPServerState[]>([]);
  const [allTools, setAllTools] = useState<ToolDTO[]>([]);
  const [modelKey, setModelKey] = useState("");
  const [judgeModelKey, setJudgeModelKey] = useState("");
  const [toolCallModelKey, setToolCallModelKey] = useState("");
  // MemoryBoard brief pin. Empty string means ``null`` on the wire,
  // which disables MemoryBoard writing entirely — the engine skips
  // brief auto-spawn. The context-window invariant (brief_cw >=
  // draft_cw) is enforced on the backend at save time, so users see
  // any violation as a 400 from the PATCH call.
  const [briefModelKey, setBriefModelKey] = useState("");
  // Runtime environment note prepended to every tool-bearing LLM call.
  // ``null`` field on the chatflow → use backend default; ``""`` →
  // user explicitly cleared the static framing (system info still
  // injected). The textarea can't represent ``null`` directly, so we
  // track an extra flag to distinguish "not yet edited (use default)"
  // from "edited to empty string".
  const [runtimeNoteText, setRuntimeNoteText] = useState("");
  const [runtimeNoteCustomized, setRuntimeNoteCustomized] = useState(false);
  const [retryBudgetStr, setRetryBudgetStr] = useState("");
  const [minGroundRatioPctStr, setMinGroundRatioPctStr] = useState("");
  const [groundGraceStr, setGroundGraceStr] = useState("");
  // Compact (conversation-compaction) settings. Trigger-pct is an
  // integer percentage 0-100; an empty string sends ``null`` (Tier 1
  // disabled). Target-pct / keep-recent use the same percentage /
  // integer patterns.
  const [compactTriggerPctStr, setCompactTriggerPctStr] = useState("");
  const [compactTargetPctStr, setCompactTargetPctStr] = useState("");
  const [compactKeepRecentStr, setCompactKeepRecentStr] = useState("");
  const [recalledStickyTurnsStr, setRecalledStickyTurnsStr] = useState("");
  const [compactModelKey, setCompactModelKey] = useState("");
  const [compactRequireConfirmation, setCompactRequireConfirmation] = useState(true);
  // ChatFlow-layer auto-compact (dual-track). Same string-as-percentage
  // pattern as the WorkFlow tier above.
  const [chatnodeCompactTriggerPctStr, setChatnodeCompactTriggerPctStr] = useState("");
  const [chatnodeCompactTargetPctStr, setChatnodeCompactTargetPctStr] = useState("");
  // Shared preserve-strategy toggle: "by_count" = honor the N knob and
  // ignore target-pct on the preserve side; "by_budget" = greedy-pack
  // tail under target-pct × ctx after summary tokens are subtracted.
  const [compactPreserveMode, setCompactPreserveMode] = useState<
    "by_count" | "by_budget"
  >("by_count");
  // Per-tool visibility: set of built-in tool names the user has
  // enabled for this chatflow. A tool is "checked" iff its name is
  // NOT in the stored ``disabled_tool_names`` list.
  const [enabledBuiltinNames, setEnabledBuiltinNames] = useState<string[]>([]);
  // Which globally-enabled MCP servers are "checked" (= their tools
  // are visible to this chatflow). Derived from the stored
  // ``disabled_tool_names`` list on open: a server is checked iff
  // none of its registered tool names are in that list.
  const [enabledMcpIds, setEnabledMcpIds] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  const [activeTab, setActiveTab] = useState<TabId>("models");

  const loadProviders = useCallback(async () => {
    try {
      const list = await api.listProviders();
      setProviders(list);
    } catch {
      // ignore
    }
  }, []);

  const loadMcpServers = useCallback(async () => {
    try {
      const list = await api.listMCPServers();
      setMcpServers(list);
    } catch {
      // ignore
    }
  }, []);

  const loadTools = useCallback(async () => {
    try {
      const list = await api.listTools();
      setAllTools(list);
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (open) {
      void loadProviders();
      void loadMcpServers();
      void loadTools();
      setModelKey(refKey(chatflow?.draft_model ?? null));
      setJudgeModelKey(refKey(chatflow?.default_judge_model ?? null));
      setToolCallModelKey(refKey(chatflow?.default_tool_call_model ?? null));
      setBriefModelKey(refKey(chatflow?.brief_model ?? null));
      setRetryBudgetStr(String(chatflow?.judge_retry_budget ?? 3));
      // Runtime environment note hydrate. ``null`` → unedited / use
      // backend default (textarea blank, "use default" hint shown);
      // any string (including "") → user-edited.
      const note = chatflow?.runtime_environment_note;
      setRuntimeNoteText(note ?? "");
      setRuntimeNoteCustomized(note != null);
      // min_ground_ratio is stored as a 0-1 fraction but surfaced to
      // the user as a 0-100 percentage. ``null`` means "fuse disabled"
      // and shows as an empty input.
      const mgr = chatflow?.min_ground_ratio;
      setMinGroundRatioPctStr(
        mgr == null ? "" : String(Math.round(mgr * 1000) / 10),
      );
      setGroundGraceStr(String(chatflow?.ground_ratio_grace_nodes ?? 20));
      // Compact config — trigger_pct and target_pct are stored as 0-1
      // fractions and surfaced as 0-100 integer percentages. ``null``
      // on trigger_pct means Tier 1 is disabled and shows as empty.
      const trig = chatflow?.compact_trigger_pct;
      setCompactTriggerPctStr(
        trig == null ? "" : String(Math.round(trig * 100)),
      );
      setCompactTargetPctStr(
        String(Math.round((chatflow?.compact_target_pct ?? 0.5) * 100)),
      );
      setCompactKeepRecentStr(
        String(chatflow?.compact_keep_recent_count ?? 3),
      );
      setRecalledStickyTurnsStr(
        String(chatflow?.recalled_context_sticky_turns ?? 3),
      );
      setCompactModelKey(refKey(chatflow?.compact_model ?? null));
      setCompactRequireConfirmation(chatflow?.compact_require_confirmation ?? true);
      const cnTrig = chatflow?.chatnode_compact_trigger_pct;
      setChatnodeCompactTriggerPctStr(
        cnTrig == null ? "" : String(Math.round(cnTrig * 100)),
      );
      setChatnodeCompactTargetPctStr(
        String(Math.round((chatflow?.chatnode_compact_target_pct ?? 0.4) * 100)),
      );
      setCompactPreserveMode(chatflow?.compact_preserve_mode ?? "by_count");
    }
  }, [open, chatflow, loadProviders, loadMcpServers, loadTools]);

  // Derive per-tool checkbox state from the stored ``disabled_tool_names``.
  // A built-in is "checked" iff its name is NOT on the denylist; an MCP
  // server is checked iff none of its registered tools are.
  useEffect(() => {
    if (!open || !chatflow) return;
    const disabled = new Set(chatflow.disabled_tool_names ?? []);
    const builtinNames = allTools
      .filter((tt) => !tt.name.startsWith("mcp__"))
      .map((tt) => tt.name);
    setEnabledBuiltinNames(builtinNames.filter((n) => !disabled.has(n)));
    const enabled = mcpServers
      .filter((s) => s.enabled)
      .filter((s) => !s.tool_names.some((name) => disabled.has(name)))
      .map((s) => s.id);
    setEnabledMcpIds(enabled);
  }, [open, chatflow, mcpServers, allTools]);

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
    // Pinned first, then alphabetical within each group.
    out.sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
    return out;
  }, [providers]);

  // Resolve the context_window backing the compact % ↔ token two-way
  // binding. Compact model pin wins; otherwise the chatflow default
  // model; otherwise the engine's ``DEFAULT_CONTEXT_WINDOW_TOKENS``
  // fallback (keep in sync with agentloom/engine/workflow_engine.py).
  const DEFAULT_CTX_WINDOW = 32000;
  const ctxWindowByKey = useMemo(() => {
    const map: Record<string, number> = {};
    for (const p of providers) {
      for (const m of p.available_models) {
        if (m.context_window != null) {
          map[`${p.id}::${m.id}`] = m.context_window;
        }
      }
    }
    return map;
  }, [providers]);
  const compactCtxWindow = useMemo(() => {
    return (
      ctxWindowByKey[compactModelKey] ??
      ctxWindowByKey[modelKey] ??
      DEFAULT_CTX_WINDOW
    );
  }, [ctxWindowByKey, compactModelKey, modelKey]);

  if (!open || !chatflow) return null;

  const handleSave = async () => {
    setSaving(true);
    try {
      const trimmed = retryBudgetStr.trim();
      const parsed = trimmed === "" ? NaN : Number(trimmed);
      const budget =
        Number.isFinite(parsed) && Number.isInteger(parsed) && parsed >= -1
          ? parsed
          : (chatflow?.judge_retry_budget ?? 3);
      // Grounding fuse inputs: empty % disables the fuse (sends null);
      // numeric % in [0, 100] is clamped and converted back to a 0-1
      // fraction. Invalid input falls back to whatever's already stored.
      const mgrTrim = minGroundRatioPctStr.trim();
      let minGroundRatio: number | null;
      if (mgrTrim === "") {
        minGroundRatio = null;
      } else {
        const pct = Number(mgrTrim);
        if (Number.isFinite(pct) && pct >= 0 && pct <= 100) {
          minGroundRatio = pct / 100;
        } else {
          minGroundRatio = chatflow?.min_ground_ratio ?? null;
        }
      }
      const graceTrim = groundGraceStr.trim();
      const graceParsed = graceTrim === "" ? NaN : Number(graceTrim);
      const groundGrace =
        Number.isFinite(graceParsed) && Number.isInteger(graceParsed) && graceParsed >= 0
          ? graceParsed
          : (chatflow?.ground_ratio_grace_nodes ?? 20);
      // Rebuild disabled_tool_names: preserve any stored entries the
      // UI doesn't know about (e.g. tools from a disconnected MCP
      // server), then append every built-in the user unchecked plus
      // every tool name from MCP servers left unchecked.
      const knownNames = new Set<string>([
        ...allTools.map((tt) => tt.name),
        ...mcpServers.flatMap((s) => s.tool_names),
      ]);
      const preserved = (chatflow?.disabled_tool_names ?? []).filter(
        (name) => !knownNames.has(name),
      );
      const builtinHidden = allTools
        .filter((tt) => !tt.name.startsWith("mcp__"))
        .filter((tt) => !enabledBuiltinNames.includes(tt.name))
        .map((tt) => tt.name);
      const mcpHidden = mcpServers
        .filter((s) => s.enabled && !enabledMcpIds.includes(s.id))
        .flatMap((s) => s.tool_names);
      const persistedDisabled = [...preserved, ...builtinHidden, ...mcpHidden];
      // Compact settings: trigger-pct empty → Tier 1 off; otherwise a
      // 0-100 % converted to 0-1 fraction. Target-pct defaults back
      // to the stored value on invalid input.
      const compactTrigTrim = compactTriggerPctStr.trim();
      let compactTrigger: number | null;
      if (compactTrigTrim === "") {
        compactTrigger = null;
      } else {
        const pct = Number(compactTrigTrim);
        compactTrigger =
          Number.isFinite(pct) && pct >= 0 && pct <= 100
            ? pct / 100
            : (chatflow?.compact_trigger_pct ?? 0.7);
      }
      const compactTgtTrim = compactTargetPctStr.trim();
      const compactTgtParsed = compactTgtTrim === "" ? NaN : Number(compactTgtTrim);
      const compactTarget =
        Number.isFinite(compactTgtParsed) && compactTgtParsed >= 1 && compactTgtParsed <= 95
          ? compactTgtParsed / 100
          : (chatflow?.compact_target_pct ?? 0.5);
      const compactKeepTrim = compactKeepRecentStr.trim();
      const compactKeepParsed =
        compactKeepTrim === "" ? NaN : Number(compactKeepTrim);
      const compactKeepRecent =
        Number.isFinite(compactKeepParsed) &&
        Number.isInteger(compactKeepParsed) &&
        compactKeepParsed >= 0
          ? compactKeepParsed
          : (chatflow?.compact_keep_recent_count ?? 3);
      const recalledStickyTrim = recalledStickyTurnsStr.trim();
      const recalledStickyParsed =
        recalledStickyTrim === "" ? NaN : Number(recalledStickyTrim);
      const recalledSticky =
        Number.isFinite(recalledStickyParsed) &&
        Number.isInteger(recalledStickyParsed) &&
        recalledStickyParsed >= 0
          ? recalledStickyParsed
          : (chatflow?.recalled_context_sticky_turns ?? 3);
      const chatnodeTrigTrim = chatnodeCompactTriggerPctStr.trim();
      let chatnodeTrigger: number | null;
      if (chatnodeTrigTrim === "") {
        chatnodeTrigger = null;
      } else {
        const pct = Number(chatnodeTrigTrim);
        chatnodeTrigger =
          Number.isFinite(pct) && pct >= 0 && pct <= 100
            ? pct / 100
            : (chatflow?.chatnode_compact_trigger_pct ?? 0.6);
      }
      const chatnodeTgtTrim = chatnodeCompactTargetPctStr.trim();
      const chatnodeTgtParsed = chatnodeTgtTrim === "" ? NaN : Number(chatnodeTgtTrim);
      const chatnodeTarget =
        Number.isFinite(chatnodeTgtParsed) && chatnodeTgtParsed >= 1 && chatnodeTgtParsed <= 95
          ? chatnodeTgtParsed / 100
          : (chatflow?.chatnode_compact_target_pct ?? 0.4);
      // runtime_environment_note: send null when the user hasn't edited
      // (keep backend default lookup); send the typed string otherwise
      // (including empty string for explicit opt-out of the static framing).
      const noteForPatch: string | null = runtimeNoteCustomized
        ? runtimeNoteText
        : null;
      await patchChatFlow({
        draft_model: parseRefKey(modelKey),
        default_judge_model: parseRefKey(judgeModelKey),
        default_tool_call_model: parseRefKey(toolCallModelKey),
        brief_model: parseRefKey(briefModelKey),
        runtime_environment_note: noteForPatch,
        judge_retry_budget: budget,
        min_ground_ratio: minGroundRatio,
        ground_ratio_grace_nodes: groundGrace,
        disabled_tool_names: persistedDisabled,
        compact_trigger_pct: compactTrigger,
        compact_target_pct: compactTarget,
        compact_keep_recent_count: compactKeepRecent,
        compact_model: parseRefKey(compactModelKey),
        compact_require_confirmation: compactRequireConfirmation,
        chatnode_compact_trigger_pct: chatnodeTrigger,
        chatnode_compact_target_pct: chatnodeTarget,
        compact_preserve_mode: compactPreserveMode,
        recalled_context_sticky_turns: recalledSticky,
      });
      onClose();
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onClose}
    >
      <div
        className="flex h-[80vh] w-[620px] flex-col rounded-xl border border-gray-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-gray-800">
            {t("chatflow_settings.title")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          >
            {"\u2715"}
          </button>
        </div>

        <div className="flex flex-1 overflow-hidden">
          <nav className="flex w-36 flex-col border-r border-gray-100 bg-gray-50/60 py-2">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                data-testid={`chatflow-settings-tab-${tab.id}`}
                className={[
                  "px-4 py-2 text-left text-xs",
                  activeTab === tab.id
                    ? "bg-white font-medium text-blue-600 border-r-2 border-r-blue-500"
                    : "text-gray-600 hover:bg-gray-100",
                ].join(" ")}
              >
                {t(tab.labelKey)}
              </button>
            ))}
          </nav>

          <div className="flex-1 overflow-auto px-5 py-4">
            {activeTab === "models" && (
              <div className="space-y-4">
                <p className="text-[10px] text-gray-400">
                  {t("chatflow_settings.advanced_models_hint")}
                </p>
                <ModelPicker
                  label={t("chatflow_settings.default_model")}
                  hint={t("chatflow_settings.default_model_hint")}
                  value={modelKey}
                  options={modelOptions}
                  onChange={setModelKey}
                />
                {modelOptions.length === 0 && (
                  <p className="rounded border border-dashed border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-700">
                    {t("chatflow_settings.no_models_hint")}
                  </p>
                )}
                <ModelPicker
                  label={t("chatflow_settings.default_judge_model")}
                  hint={t("chatflow_settings.default_judge_model_hint")}
                  value={judgeModelKey}
                  options={modelOptions}
                  onChange={setJudgeModelKey}
                  inheritOption={t("chatflow_settings.inherit_main_model")}
                />
                <ModelPicker
                  label={t("chatflow_settings.default_tool_call_model")}
                  hint={t("chatflow_settings.default_tool_call_model_hint")}
                  value={toolCallModelKey}
                  options={modelOptions}
                  onChange={setToolCallModelKey}
                  inheritOption={t("chatflow_settings.inherit_main_model")}
                />
                <ModelPicker
                  label={t("chatflow_settings.brief_model")}
                  hint={t("chatflow_settings.brief_model_hint")}
                  value={briefModelKey}
                  options={modelOptions}
                  onChange={setBriefModelKey}
                  inheritOption={t("chatflow_settings.inherit_main_model")}
                />
              </div>
            )}

            {activeTab === "execution" && (
              <div className="space-y-4">
                <label className="block">
                  <span className="text-[11px] font-medium text-gray-500">
                    {t("chatflow_settings.judge_retry_budget")}
                  </span>
                  <input
                    type="number"
                    min={-1}
                    step={1}
                    value={retryBudgetStr}
                    onChange={(e) => setRetryBudgetStr(e.target.value)}
                    data-testid="judge-retry-budget-input"
                    className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
                  />
                  <p className="mt-1 text-[10px] text-gray-400">
                    {t("chatflow_settings.judge_retry_budget_hint")}
                  </p>
                </label>

                <label className="block">
                  <span className="text-[11px] font-medium text-gray-500">
                    {t("chatflow_settings.min_ground_ratio")}
                  </span>
                  <input
                    type="number"
                    min={0}
                    max={100}
                    step={1}
                    value={minGroundRatioPctStr}
                    onChange={(e) => setMinGroundRatioPctStr(e.target.value)}
                    data-testid="min-ground-ratio-input"
                    className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
                  />
                  <p className="mt-1 text-[10px] text-gray-400">
                    {t("chatflow_settings.min_ground_ratio_hint")}
                  </p>
                </label>

                <label className="block">
                  <span className="text-[11px] font-medium text-gray-500">
                    {t("chatflow_settings.ground_ratio_grace_nodes")}
                  </span>
                  <input
                    type="number"
                    min={0}
                    step={1}
                    value={groundGraceStr}
                    onChange={(e) => setGroundGraceStr(e.target.value)}
                    data-testid="ground-ratio-grace-input"
                    className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
                  />
                  <p className="mt-1 text-[10px] text-gray-400">
                    {t("chatflow_settings.ground_ratio_grace_nodes_hint")}
                  </p>
                </label>

                <label className="block">
                  <span className="flex items-center justify-between text-[11px] font-medium text-gray-500">
                    {t("chatflow_settings.runtime_environment_note")}
                    {!runtimeNoteCustomized && (
                      <span className="ml-2 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] font-normal text-gray-500">
                        {t("chatflow_settings.runtime_environment_note_default")}
                      </span>
                    )}
                  </span>
                  <textarea
                    rows={6}
                    value={runtimeNoteText}
                    onChange={(e) => {
                      setRuntimeNoteText(e.target.value);
                      setRuntimeNoteCustomized(true);
                    }}
                    placeholder={t(
                      "chatflow_settings.runtime_environment_note_placeholder",
                    )}
                    data-testid="runtime-environment-note-input"
                    className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-[11px] leading-relaxed text-gray-700 focus:border-blue-400 focus:outline-none"
                  />
                  <div className="mt-1 flex items-center justify-between">
                    <p className="text-[10px] text-gray-400">
                      {t("chatflow_settings.runtime_environment_note_hint")}
                    </p>
                    {runtimeNoteCustomized && (
                      <button
                        type="button"
                        onClick={() => {
                          setRuntimeNoteText("");
                          setRuntimeNoteCustomized(false);
                        }}
                        className="text-[10px] text-blue-500 hover:underline"
                      >
                        {t("chatflow_settings.runtime_environment_note_reset")}
                      </button>
                    )}
                  </div>
                </label>
              </div>
            )}

            {activeTab === "compact" && (
              <CompactSettingsSection
                triggerPctStr={compactTriggerPctStr}
                onTriggerPctChange={setCompactTriggerPctStr}
                targetPctStr={compactTargetPctStr}
                onTargetPctChange={setCompactTargetPctStr}
                keepRecentStr={compactKeepRecentStr}
                onKeepRecentChange={setCompactKeepRecentStr}
                recalledStickyTurnsStr={recalledStickyTurnsStr}
                onRecalledStickyTurnsChange={setRecalledStickyTurnsStr}
                modelKey={compactModelKey}
                onModelKeyChange={setCompactModelKey}
                modelOptions={modelOptions}
                requireConfirmation={compactRequireConfirmation}
                onRequireConfirmationChange={setCompactRequireConfirmation}
                chatnodeTriggerPctStr={chatnodeCompactTriggerPctStr}
                onChatnodeTriggerPctChange={setChatnodeCompactTriggerPctStr}
                chatnodeTargetPctStr={chatnodeCompactTargetPctStr}
                onChatnodeTargetPctChange={setChatnodeCompactTargetPctStr}
                ctxWindow={compactCtxWindow}
                preserveMode={compactPreserveMode}
                onPreserveModeChange={setCompactPreserveMode}
              />
            )}

            {activeTab === "tools" && (
              <div className="space-y-4">
                <EffectiveToolCatalogSection
                  allTools={allTools}
                  mcpServers={mcpServers}
                  enabledBuiltinNames={enabledBuiltinNames}
                  enabledMcpIds={enabledMcpIds}
                />
                <BuiltinToolsSection
                  tools={allTools.filter((tt) => !tt.name.startsWith("mcp__"))}
                  selectedNames={enabledBuiltinNames}
                  onChange={setEnabledBuiltinNames}
                />
                <MCPEnablementSection
                  servers={mcpServers}
                  selectedIds={enabledMcpIds}
                  onChange={setEnabledMcpIds}
                />
              </div>
            )}
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
          >
            {t("providers.cancel")}
          </button>
          <button
            type="button"
            onClick={() => void handleSave()}
            disabled={saving}
            className="rounded bg-blue-500 px-4 py-1.5 text-xs text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {saving ? "..." : t("providers.save")}
          </button>
        </div>
      </div>
    </div>
  );
}

function ModelPicker({
  label,
  hint,
  value,
  options,
  onChange,
  inheritOption,
}: {
  label: string;
  hint: string;
  value: string;
  options: Array<{ key: string; label: string; pinned: boolean }>;
  onChange: (v: string) => void;
  /** When provided, render an extra ``""``-keyed option labeled with this
   * string at the top of the list so the user can clear the pin (the
   * empty key serializes back to ``null`` in patchChatFlow, meaning
   * "fall back to default_model"). Used by the per-call-type pickers. */
  inheritOption?: string;
}) {
  const valueMissing = value !== "" && !options.some((o) => o.key === value);
  return (
    <label className="block">
      <span className="text-[11px] font-medium text-gray-500">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
      >
        {inheritOption !== undefined && (
          <option value="">{inheritOption}</option>
        )}
        {valueMissing && <option value={value}>{value}</option>}
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.pinned ? "\u2605 " : ""}
            {o.label}
          </option>
        ))}
      </select>
      <p className="mt-1 text-[10px] text-gray-400">{hint}</p>
    </label>
  );
}

function MCPEnablementSection({
  servers,
  selectedIds,
  onChange,
}: {
  servers: MCPServerState[];
  selectedIds: string[];
  onChange: (next: string[]) => void;
}) {
  const { t } = useTranslation();
  // Only globally-enabled servers are eligible — a globally-disabled
  // server's tools aren't registered, so toggling it per-chatflow has
  // no effect.
  const eligible = servers.filter((s) => s.enabled);

  if (eligible.length === 0) {
    return (
      <div>
        <span className="text-[11px] font-medium text-gray-500">
          {t("chatflow_settings.mcp_servers")}
        </span>
        <p className="mt-1 rounded border border-dashed border-gray-200 bg-gray-50 px-3 py-2 text-[11px] text-gray-500">
          {t("chatflow_settings.mcp_no_global")}
        </p>
      </div>
    );
  }

  const toggle = (id: string) => {
    if (selectedIds.includes(id)) {
      onChange(selectedIds.filter((x) => x !== id));
    } else {
      onChange([...selectedIds, id]);
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between">
        <span className="text-[11px] font-medium text-gray-500">
          {t("chatflow_settings.mcp_servers")}
        </span>
      </div>
      <div className="mt-1 space-y-1 rounded border border-gray-200 p-2">
        {eligible.map((s) => {
          const checked = selectedIds.includes(s.id);
          return (
            <label
              key={s.id}
              className="flex items-center gap-2 rounded px-1.5 py-1 text-[11px] hover:bg-gray-50"
            >
              <input
                type="checkbox"
                checked={checked}
                onChange={() => toggle(s.id)}
              />
              <span className="font-medium text-gray-700">{s.friendly_name}</span>
              <span className="font-mono text-[9px] text-gray-400">{s.server_id}</span>
              {s.is_connected ? (
                <span className="ml-auto text-[9px] text-green-600">
                  {s.tool_count} {t("mcp.tools")}
                </span>
              ) : (
                <span className="ml-auto text-[9px] text-red-500">
                  {t("mcp.disconnected")}
                </span>
              )}
            </label>
          );
        })}
      </div>
      <p className="mt-1 text-[10px] text-gray-400">
        {t("chatflow_settings.mcp_servers_hint")}
      </p>
    </div>
  );
}

function CompactSettingsSection({
  triggerPctStr,
  onTriggerPctChange,
  targetPctStr,
  onTargetPctChange,
  keepRecentStr,
  onKeepRecentChange,
  recalledStickyTurnsStr,
  onRecalledStickyTurnsChange,
  modelKey,
  onModelKeyChange,
  modelOptions,
  requireConfirmation,
  onRequireConfirmationChange,
  chatnodeTriggerPctStr,
  onChatnodeTriggerPctChange,
  chatnodeTargetPctStr,
  onChatnodeTargetPctChange,
  ctxWindow,
  preserveMode,
  onPreserveModeChange,
}: {
  triggerPctStr: string;
  onTriggerPctChange: (v: string) => void;
  targetPctStr: string;
  onTargetPctChange: (v: string) => void;
  keepRecentStr: string;
  onKeepRecentChange: (v: string) => void;
  recalledStickyTurnsStr: string;
  onRecalledStickyTurnsChange: (v: string) => void;
  modelKey: string;
  onModelKeyChange: (v: string) => void;
  modelOptions: Array<{ key: string; label: string; pinned: boolean }>;
  requireConfirmation: boolean;
  onRequireConfirmationChange: (v: boolean) => void;
  chatnodeTriggerPctStr: string;
  onChatnodeTriggerPctChange: (v: string) => void;
  chatnodeTargetPctStr: string;
  onChatnodeTargetPctChange: (v: string) => void;
  /** Effective context_window (in tokens) used for the percent ↔ token
   * two-way binding on the target inputs. Resolved by the parent from
   * the compact model pin → default model → engine fallback, so the
   * binding stays correct when the user switches compact_model. */
  ctxWindow: number;
  /** Shared preserve-strategy for both tiers. ``by_count`` honors the N
   * knob and disables the two target-volume inputs; ``by_budget``
   * disables the N knob and drives tail selection from target_pct. */
  preserveMode: "by_count" | "by_budget";
  onPreserveModeChange: (v: "by_count" | "by_budget") => void;
}) {
  const { t } = useTranslation();
  // Derive a display string for the tokens sibling of a percent input.
  // Empty percent → empty tokens (and vice-versa on the handler).
  const pctStrToTokensStr = (pctStr: string): string => {
    const trimmed = pctStr.trim();
    if (trimmed === "") return "";
    const pct = Number(trimmed);
    if (!Number.isFinite(pct)) return "";
    return String(Math.round((pct / 100) * ctxWindow));
  };
  // Handler for token-input → percent-input direction. Clamped to the
  // same 1-95 range the percent input enforces so the two stay in a
  // valid state even mid-edit.
  const onTokensEdit =
    (onPctChange: (v: string) => void) =>
    (v: string) => {
      const trimmed = v.trim();
      if (trimmed === "") {
        onPctChange("");
        return;
      }
      const tokens = Number(trimmed);
      if (!Number.isFinite(tokens) || ctxWindow <= 0) return;
      const pct = Math.round((tokens / ctxWindow) * 100);
      onPctChange(String(pct));
    };
  const targetDisabled = preserveMode === "by_count";
  const keepRecentDisabled = preserveMode === "by_budget";
  const targetInputClass = `w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none${
    targetDisabled ? " bg-gray-100 text-gray-400 cursor-not-allowed" : ""
  }`;
  const keepRecentInputClass = `mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none${
    keepRecentDisabled ? " bg-gray-100 text-gray-400 cursor-not-allowed" : ""
  }`;
  return (
    <div>
      <p className="text-[10px] text-gray-400">
        {t("chatflow_settings.compact_hint")}
      </p>
      <div className="mt-3 space-y-3">
        {/* Preserve-strategy toggle: left = by_count (记忆力数值), right =
            by_budget (目标体积). Shared between both tiers — the engine
            reads a single ``compact_preserve_mode`` field. */}
        <div>
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.compact_preserve_mode_label")}
          </span>
          <div
            role="radiogroup"
            aria-label={t("chatflow_settings.compact_preserve_mode_label")}
            className="mt-0.5 flex overflow-hidden rounded border border-gray-300"
          >
            <button
              type="button"
              role="radio"
              aria-checked={preserveMode === "by_count"}
              data-testid="compact-preserve-mode-by-count"
              onClick={() => onPreserveModeChange("by_count")}
              className={`flex-1 px-2 py-1.5 text-[11px] ${
                preserveMode === "by_count"
                  ? "bg-blue-500 text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              {t("chatflow_settings.compact_preserve_mode_by_count")}
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={preserveMode === "by_budget"}
              data-testid="compact-preserve-mode-by-budget"
              onClick={() => onPreserveModeChange("by_budget")}
              className={`flex-1 border-l border-gray-300 px-2 py-1.5 text-[11px] ${
                preserveMode === "by_budget"
                  ? "bg-blue-500 text-white"
                  : "bg-white text-gray-600 hover:bg-gray-50"
              }`}
            >
              {t("chatflow_settings.compact_preserve_mode_by_budget")}
            </button>
          </div>
          <p className="mt-1 text-[10px] text-gray-400">
            {preserveMode === "by_count"
              ? t("chatflow_settings.compact_preserve_mode_by_count_hint")
              : t("chatflow_settings.compact_preserve_mode_by_budget_hint")}
          </p>
        </div>

        <h4 className="text-[11px] font-semibold uppercase tracking-wide text-gray-500">
          {t("chatflow_settings.compact_chatflow_layer")}
        </h4>
        <p className="-mt-2 text-[10px] text-gray-400">
          {t("chatflow_settings.compact_chatflow_layer_hint")}
        </p>

        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.chatnode_compact_trigger_pct")}
          </span>
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            value={chatnodeTriggerPctStr}
            onChange={(e) => onChatnodeTriggerPctChange(e.target.value)}
            data-testid="chatnode-compact-trigger-pct-input"
            className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
          />
          <p className="mt-1 text-[10px] text-gray-400">
            {t("chatflow_settings.chatnode_compact_trigger_pct_hint")}
          </p>
        </label>

        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.chatnode_compact_target_pct")}
          </span>
          <div className="mt-0.5 flex gap-2">
            <input
              type="number"
              min={1}
              max={95}
              step={1}
              value={chatnodeTargetPctStr}
              onChange={(e) => onChatnodeTargetPctChange(e.target.value)}
              data-testid="chatnode-compact-target-pct-input"
              disabled={targetDisabled}
              className={targetInputClass}
            />
            <input
              type="number"
              min={0}
              step={1}
              value={pctStrToTokensStr(chatnodeTargetPctStr)}
              onChange={(e) => onTokensEdit(onChatnodeTargetPctChange)(e.target.value)}
              data-testid="chatnode-compact-target-tokens-input"
              disabled={targetDisabled}
              className={targetInputClass}
            />
          </div>
          <p className="mt-1 text-[10px] text-gray-400">
            {t("chatflow_settings.chatnode_compact_target_pct_hint")}
          </p>
          <p className="mt-0.5 text-[10px] text-gray-400">
            {t("chatflow_settings.compact_target_tokens_hint", { ctx: ctxWindow })}
          </p>
        </label>

        <h4 className="mt-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
          {t("chatflow_settings.compact_workflow_layer")}
        </h4>
        <p className="-mt-2 text-[10px] text-gray-400">
          {t("chatflow_settings.compact_workflow_layer_hint")}
        </p>

        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.compact_trigger_pct")}
          </span>
          <input
            type="number"
            min={0}
            max={100}
            step={1}
            value={triggerPctStr}
            onChange={(e) => onTriggerPctChange(e.target.value)}
            data-testid="compact-trigger-pct-input"
            className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
          />
          <p className="mt-1 text-[10px] text-gray-400">
            {t("chatflow_settings.compact_trigger_pct_hint")}
          </p>
        </label>

        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.compact_target_pct")}
          </span>
          <div className="mt-0.5 flex gap-2">
            <input
              type="number"
              min={1}
              max={95}
              step={1}
              value={targetPctStr}
              onChange={(e) => onTargetPctChange(e.target.value)}
              data-testid="compact-target-pct-input"
              disabled={targetDisabled}
              className={targetInputClass}
            />
            <input
              type="number"
              min={0}
              step={1}
              value={pctStrToTokensStr(targetPctStr)}
              onChange={(e) => onTokensEdit(onTargetPctChange)(e.target.value)}
              data-testid="compact-target-tokens-input"
              disabled={targetDisabled}
              className={targetInputClass}
            />
          </div>
          <p className="mt-1 text-[10px] text-gray-400">
            {t("chatflow_settings.compact_target_pct_hint")}
          </p>
          <p className="mt-0.5 text-[10px] text-gray-400">
            {t("chatflow_settings.compact_target_tokens_hint", { ctx: ctxWindow })}
          </p>
        </label>

        <h4 className="mt-2 text-[11px] font-semibold uppercase tracking-wide text-gray-500">
          {t("chatflow_settings.compact_shared")}
        </h4>

        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.compact_keep_recent_count")}
          </span>
          <input
            type="number"
            min={0}
            step={1}
            value={keepRecentStr}
            onChange={(e) => onKeepRecentChange(e.target.value)}
            data-testid="compact-keep-recent-count-input"
            disabled={keepRecentDisabled}
            className={keepRecentInputClass}
          />
          <p className="mt-1 text-[10px] text-gray-400">
            {t("chatflow_settings.compact_keep_recent_count_hint")}
          </p>
        </label>

        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("chatflow_settings.recalled_context_sticky_turns")}
          </span>
          <input
            type="number"
            min={0}
            step={1}
            value={recalledStickyTurnsStr}
            onChange={(e) => onRecalledStickyTurnsChange(e.target.value)}
            data-testid="recalled-context-sticky-turns-input"
            className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
          />
          <p className="mt-1 text-[10px] text-gray-400">
            {t("chatflow_settings.recalled_context_sticky_turns_hint")}
          </p>
        </label>

        <ModelPicker
          label={t("chatflow_settings.compact_model")}
          hint={t("chatflow_settings.compact_model_hint")}
          value={modelKey}
          options={modelOptions}
          onChange={onModelKeyChange}
          inheritOption={t("chatflow_settings.inherit_main_model")}
        />

        <label className="flex items-center gap-2 text-[11px] text-gray-700">
          <input
            type="checkbox"
            checked={requireConfirmation}
            onChange={(e) => onRequireConfirmationChange(e.target.checked)}
            data-testid="compact-require-confirmation-input"
          />
          <span>{t("chatflow_settings.compact_require_confirmation")}</span>
        </label>
        <p className="-mt-2 pl-6 text-[10px] text-gray-400">
          {t("chatflow_settings.compact_require_confirmation_hint")}
        </p>
      </div>
    </div>
  );
}

function EffectiveToolCatalogSection({
  allTools,
  mcpServers,
  enabledBuiltinNames,
  enabledMcpIds,
}: {
  allTools: ToolDTO[];
  mcpServers: MCPServerState[];
  enabledBuiltinNames: string[];
  enabledMcpIds: string[];
}) {
  const { t } = useTranslation();
  const [showDisabled, setShowDisabled] = useState(false);

  // Compute the live effective catalog from the dialog's working
  // edit state (NOT from chatflow.disabled_tool_names) so the
  // listing updates as the user toggles checkboxes below — same
  // resolution rule as the save path so what you see here is what
  // the worker will actually see.
  const enabledMcpIdSet = useMemo(() => new Set(enabledMcpIds), [enabledMcpIds]);
  const enabledBuiltinSet = useMemo(
    () => new Set(enabledBuiltinNames),
    [enabledBuiltinNames],
  );
  const mcpEnabledNames = useMemo(() => {
    const out = new Set<string>();
    for (const s of mcpServers) {
      if (!s.enabled) continue;
      if (!enabledMcpIdSet.has(s.id)) continue;
      for (const n of s.tool_names) out.add(n);
    }
    return out;
  }, [mcpServers, enabledMcpIdSet]);

  const { enabled, disabled } = useMemo(() => {
    const en: ToolDTO[] = [];
    const dis: ToolDTO[] = [];
    for (const tt of allTools) {
      const isMcp = tt.name.startsWith("mcp__");
      const isOn = isMcp
        ? mcpEnabledNames.has(tt.name)
        : enabledBuiltinSet.has(tt.name);
      (isOn ? en : dis).push(tt);
    }
    return { enabled: en, disabled: dis };
  }, [allTools, mcpEnabledNames, enabledBuiltinSet]);

  if (allTools.length === 0) return null;

  return (
    <div>
      <span className="text-[11px] font-medium text-gray-500">
        {t("chatflow_settings.effective_tool_catalog")} ({enabled.length})
      </span>
      <div className="mt-1 max-h-64 overflow-y-auto rounded border border-gray-200 p-2">
        {enabled.length === 0 ? (
          <p className="px-1 py-1 text-[10px] italic text-gray-400">
            {t("chatflow_settings.effective_tool_catalog_empty")}
          </p>
        ) : (
          <ul className="space-y-1">
            {enabled.map((tt) => (
              <li
                key={tt.name}
                className="flex items-baseline gap-2 px-1 py-0.5 text-[11px]"
              >
                <span className="font-mono text-gray-700">{tt.name}</span>
                {tt.description && (
                  <span className="truncate text-[10px] text-gray-400">
                    {tt.description}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
        {disabled.length > 0 && (
          <div className="mt-2 border-t border-gray-100 pt-2">
            <button
              type="button"
              onClick={() => setShowDisabled((v) => !v)}
              className="flex items-center gap-1 text-[10px] text-gray-400 hover:text-gray-600"
            >
              <span>{showDisabled ? "▾" : "▸"}</span>
              <span>
                {t("chatflow_settings.effective_tool_catalog_disabled_label")} (
                {disabled.length})
              </span>
            </button>
            {showDisabled && (
              <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto pl-3">
                {disabled.map((tt) => (
                  <li
                    key={tt.name}
                    className="flex items-baseline gap-2 px-1 py-0.5 text-[11px] opacity-60"
                  >
                    <span className="font-mono text-gray-500 line-through decoration-gray-300">
                      {tt.name}
                    </span>
                    {tt.description && (
                      <span className="truncate text-[10px] text-gray-400">
                        {tt.description}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
      <p className="mt-1 text-[10px] text-gray-400">
        {t("chatflow_settings.effective_tool_catalog_hint")}
      </p>
    </div>
  );
}


function BuiltinToolsSection({
  tools,
  selectedNames,
  onChange,
}: {
  tools: ToolDTO[];
  selectedNames: string[];
  onChange: (next: string[]) => void;
}) {
  const { t } = useTranslation();
  if (tools.length === 0) return null;

  const toggle = (name: string) => {
    if (selectedNames.includes(name)) {
      onChange(selectedNames.filter((x) => x !== name));
    } else {
      onChange([...selectedNames, name]);
    }
  };

  return (
    <div>
      <span className="text-[11px] font-medium text-gray-500">
        {t("chatflow_settings.builtin_tools")}
      </span>
      <div className="mt-1 space-y-1 rounded border border-gray-200 p-2">
        {tools.map((tt) => {
          const checked = selectedNames.includes(tt.name);
          return (
            <label
              key={tt.name}
              className="flex items-center gap-2 rounded px-1.5 py-1 text-[11px] hover:bg-gray-50"
            >
              <input
                type="checkbox"
                checked={checked}
                onChange={() => toggle(tt.name)}
              />
              <span className="font-mono text-gray-700">{tt.name}</span>
              {tt.description && (
                <span className="ml-2 truncate text-[10px] text-gray-400">
                  {tt.description}
                </span>
              )}
            </label>
          );
        })}
      </div>
      <p className="mt-1 text-[10px] text-gray-400">
        {t("chatflow_settings.builtin_tools_hint")}
      </p>
    </div>
  );
}
