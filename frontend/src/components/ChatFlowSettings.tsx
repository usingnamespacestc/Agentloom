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
  const [retryBudgetStr, setRetryBudgetStr] = useState("");
  const [minGroundRatioPctStr, setMinGroundRatioPctStr] = useState("");
  const [groundGraceStr, setGroundGraceStr] = useState("");
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
  // The three per-call-type model fallbacks are rarely changed —
  // day-to-day model picking lives in the composer picker. Collapse
  // them behind an Advanced disclosure so this modal leads with the
  // knobs users actually turn. Open on demand to change a ChatFlow's
  // fallback without disturbing other ChatFlows' composer preferences.
  const [advancedOpen, setAdvancedOpen] = useState(false);

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
      setModelKey(refKey(chatflow?.default_model ?? null));
      setJudgeModelKey(refKey(chatflow?.default_judge_model ?? null));
      setToolCallModelKey(refKey(chatflow?.default_tool_call_model ?? null));
      setRetryBudgetStr(String(chatflow?.judge_retry_budget ?? 3));
      // min_ground_ratio is stored as a 0-1 fraction but surfaced to
      // the user as a 0-100 percentage. ``null`` means "fuse disabled"
      // and shows as an empty input.
      const mgr = chatflow?.min_ground_ratio;
      setMinGroundRatioPctStr(
        mgr == null ? "" : String(Math.round(mgr * 1000) / 10),
      );
      setGroundGraceStr(String(chatflow?.ground_ratio_grace_nodes ?? 20));
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
      await patchChatFlow({
        default_model: parseRefKey(modelKey),
        default_judge_model: parseRefKey(judgeModelKey),
        default_tool_call_model: parseRefKey(toolCallModelKey),
        judge_retry_budget: budget,
        min_ground_ratio: minGroundRatio,
        ground_ratio_grace_nodes: groundGrace,
        disabled_tool_names: persistedDisabled,
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
        className="flex w-[520px] flex-col rounded-xl border border-gray-200 bg-white shadow-2xl"
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

        <div className="space-y-4 px-5 py-4">
          <div className="rounded border border-gray-200">
            <button
              type="button"
              onClick={() => setAdvancedOpen((v) => !v)}
              className="flex w-full items-center justify-between px-3 py-2 text-[11px] font-medium text-gray-600 hover:bg-gray-50"
              data-testid="chatflow-settings-advanced-toggle"
            >
              <span>{t("chatflow_settings.advanced_models")}</span>
              <span className="text-gray-400">{advancedOpen ? "\u25BE" : "\u25B8"}</span>
            </button>
            {advancedOpen && (
              <div className="space-y-4 border-t border-gray-200 px-3 py-3">
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
              </div>
            )}
          </div>

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
