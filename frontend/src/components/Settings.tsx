/**
 * Settings modal — tabbed panel for workspace/canvas preferences.
 *
 * Current tabs:
 * - ``providers`` — LLM provider CRUD, test connection, pinned models
 * - ``canvas`` — local display preferences (node id overlay, etc.)
 *
 * Provider state is server-backed; canvas prefs are client-only
 * (``usePreferencesStore`` → ``localStorage``). New tabs plug in by
 * extending ``TABS`` and adding a branch to the body switch.
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api";
import type {
  CreateMCPServerBody,
  CreateProviderBody,
  JsonMode,
  MCPServerKind,
  MCPServerState,
  ModelInfoDTO,
  ProviderSubKind,
  ProviderSummary,
  ToolDTO,
  ToolState,
} from "@/lib/api";
import { SUB_KIND_PARAM_WHITELIST } from "@/lib/api";
import { formatTokensKM, parseTokensKM } from "@/lib/tokenFormat";
import { usePreferencesStore } from "@/store/preferencesStore";

type TabId = "providers" | "mcp" | "tools" | "canvas";

const TABS: Array<{ id: TabId; labelKey: string }> = [
  { id: "providers", labelKey: "providers.title" },
  { id: "mcp", labelKey: "settings.tab_mcp" },
  { id: "tools", labelKey: "settings.tab_tools" },
  { id: "canvas", labelKey: "settings.tab_canvas" },
];

interface SettingsProps {
  open: boolean;
  onClose: () => void;
}

export function Settings({ open, onClose }: SettingsProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<TabId>("providers");

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onClose}
    >
      <div
        className="flex h-[80vh] w-[620px] flex-col rounded-xl border border-gray-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-gray-800">{t("settings.title")}</h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          >
            {"\u2715"}
          </button>
        </div>

        {/* Tabs + body */}
        <div className="flex flex-1 overflow-hidden">
          {/* Tab bar — vertical on the left */}
          <nav className="flex w-36 flex-col border-r border-gray-100 bg-gray-50/60 py-2">
            {TABS.map((tab) => (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
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

          {/* Body */}
          <div className="flex-1 overflow-auto px-5 py-4">
            {activeTab === "providers" && <ProvidersPanel />}
            {activeTab === "mcp" && <MCPServersPanel />}
            {activeTab === "tools" && <ToolsPanel />}
            {activeTab === "canvas" && <CanvasPanel />}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Providers panel

function ProvidersPanel() {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null); // null = list, "new" = create
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

  const fetchProviders = useCallback(async () => {
    setLoading(true);
    try {
      const list = await api.listProviders();
      setProviders(list);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchProviders();
  }, [fetchProviders]);

  const handleDelete = async (id: string) => {
    await api.deleteProvider(id);
    setDeleteConfirm(null);
    void fetchProviders();
  };

  if (editingId !== null) {
    return (
      <ProviderForm
        providerId={editingId === "new" ? null : editingId}
        onSaved={() => {
          setEditingId(null);
          void fetchProviders();
        }}
        onCancel={() => setEditingId(null)}
      />
    );
  }

  return (
    <div>
      {loading && providers.length === 0 && (
        <div className="py-8 text-center text-xs text-gray-400">Loading...</div>
      )}

      {!loading && providers.length === 0 && (
        <div className="py-8 text-center text-xs text-gray-400">
          {t("providers.no_providers")}
        </div>
      )}

      {providers.map((p) => (
        <div
          key={p.id}
          className="group mb-2 rounded-lg border border-gray-200 px-4 py-3 hover:border-gray-300"
        >
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-medium text-gray-800">{p.friendly_name}</div>
              <div className="mt-0.5 text-[11px] text-gray-400">
                {p.provider_kind === "openai_compat"
                  ? t("providers.kind_openai_compat")
                  : t("providers.kind_anthropic_native")}
                {" \u00B7 "}
                {p.base_url}
              </div>
              {p.available_models.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-1">
                  {p.available_models.slice(0, 5).map((m) => (
                    <span
                      key={m.id}
                      className={[
                        "rounded-full px-1.5 py-0.5 text-[10px]",
                        m.pinned
                          ? "bg-blue-100 text-blue-700"
                          : "bg-gray-100 text-gray-500",
                      ].join(" ")}
                    >
                      {m.id}
                    </span>
                  ))}
                  {p.available_models.length > 5 && (
                    <span className="text-[10px] text-gray-400">
                      +{p.available_models.length - 5}
                    </span>
                  )}
                </div>
              )}
            </div>
            <div className="flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
              <button
                type="button"
                onClick={() => setEditingId(p.id)}
                className="rounded border border-gray-300 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
              >
                {t("providers.edit")}
              </button>
              <button
                type="button"
                onClick={() => setDeleteConfirm(p.id)}
                className="rounded border border-red-200 px-2 py-1 text-[11px] text-red-500 hover:bg-red-50"
              >
                {t("providers.delete")}
              </button>
            </div>
          </div>
        </div>
      ))}

      <button
        type="button"
        onClick={() => setEditingId("new")}
        className="mt-2 w-full rounded-lg border border-dashed border-gray-300 py-2 text-xs text-gray-500 hover:border-blue-400 hover:text-blue-500"
      >
        + {t("providers.add")}
      </button>

      {deleteConfirm && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/20"
          onClick={() => setDeleteConfirm(null)}
        >
          <div
            className="w-72 rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="mb-3 text-sm text-gray-700">{t("providers.delete_confirm")}</p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteConfirm(null)}
                className="rounded border border-gray-300 bg-white px-3 py-1 text-xs text-gray-600 hover:bg-gray-50"
              >
                {t("providers.cancel")}
              </button>
              <button
                type="button"
                onClick={() => void handleDelete(deleteConfirm)}
                className="rounded bg-red-500 px-3 py-1 text-xs text-white hover:bg-red-600"
              >
                {t("providers.delete")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- Canvas panel

function CanvasPanel() {
  const { t } = useTranslation();
  const showNodeId = usePreferencesStore((s) => s.showNodeId);
  const setShowNodeId = usePreferencesStore((s) => s.setShowNodeId);
  const showChatflowId = usePreferencesStore((s) => s.showChatflowId);
  const setShowChatflowId = usePreferencesStore((s) => s.setShowChatflowId);
  const showTokens = usePreferencesStore((s) => s.showTokens);
  const setShowTokens = usePreferencesStore((s) => s.setShowTokens);
  const showGenTime = usePreferencesStore((s) => s.showGenTime);
  const setShowGenTime = usePreferencesStore((s) => s.setShowGenTime);
  const showGenSpeed = usePreferencesStore((s) => s.showGenSpeed);
  const setShowGenSpeed = usePreferencesStore((s) => s.setShowGenSpeed);
  const showWorkNodeModel = usePreferencesStore((s) => s.showWorkNodeModel);
  const setShowWorkNodeModel = usePreferencesStore((s) => s.setShowWorkNodeModel);

  const rows: Array<{ key: string; value: boolean; onChange: (v: boolean) => void }> = [
    { key: "show_node_id", value: showNodeId, onChange: setShowNodeId },
    { key: "show_chatflow_id", value: showChatflowId, onChange: setShowChatflowId },
    { key: "show_tokens", value: showTokens, onChange: setShowTokens },
    { key: "show_gen_time", value: showGenTime, onChange: setShowGenTime },
    { key: "show_gen_speed", value: showGenSpeed, onChange: setShowGenSpeed },
    { key: "show_worknode_model", value: showWorkNodeModel, onChange: setShowWorkNodeModel },
  ];

  return (
    <div className="space-y-4">
      {rows.map((row) => (
        <label key={row.key} className="flex items-start gap-3">
          <input
            type="checkbox"
            checked={row.value}
            onChange={(e) => row.onChange(e.target.checked)}
            className="mt-0.5"
          />
          <div>
            <div className="text-xs font-medium text-gray-700">
              {t(`canvas_prefs.${row.key}`)}
            </div>
            <div className="mt-0.5 text-[11px] text-gray-500">
              {t(`canvas_prefs.${row.key}_hint`)}
            </div>
          </div>
        </label>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- Provider form

const PROVIDER_KINDS = [
  { value: "openai_compat", labelKey: "providers.kind_openai_compat" },
  { value: "anthropic_native", labelKey: "providers.kind_anthropic_native" },
];

const DEFAULT_URLS: Record<string, string> = {
  openai_compat: "https://api.openai.com/v1",
  anthropic_native: "https://api.anthropic.com",
};

type KeySource = "env_var" | "inline" | "none";

interface Preset {
  id: string;
  labelKey: string;
  kind: string;
  baseUrl: string;
  keySource: KeySource;
  envVar?: string;
}

const PRESETS: Preset[] = [
  { id: "custom", labelKey: "providers.preset_custom", kind: "openai_compat", baseUrl: "", keySource: "env_var" },
  { id: "openai", labelKey: "providers.preset_openai", kind: "openai_compat", baseUrl: "https://api.openai.com/v1", keySource: "env_var", envVar: "OPENAI_API_KEY" },
  { id: "anthropic", labelKey: "providers.preset_anthropic", kind: "anthropic_native", baseUrl: "https://api.anthropic.com", keySource: "env_var", envVar: "ANTHROPIC_API_KEY" },
];

function ProviderForm({
  providerId,
  onSaved,
  onCancel,
}: {
  providerId: string | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();

  // Treat "new provider but Test already created a row" the same as an edit:
  // once ``persistedId`` is set, Save patches that row instead of creating
  // another one (which caused the double-add bug). ``createdInSession``
  // tracks rows we inserted via Test so Cancel can clean them up.
  const [persistedId, setPersistedId] = useState<string | null>(providerId);
  const [createdInSession, setCreatedInSession] = useState(false);
  const isNew = persistedId === null;

  const [name, setName] = useState("");
  const [kind, setKind] = useState("openai_compat");
  const [subKind, setSubKind] = useState<ProviderSubKind | null>(null);
  const [baseUrl, setBaseUrl] = useState(DEFAULT_URLS.openai_compat);
  const [keySource, setKeySource] = useState<KeySource>("env_var");
  const [envVar, setEnvVar] = useState("");
  const [inlineKey, setInlineKey] = useState("");
  const [models, setModels] = useState<ModelInfoDTO[]>([]);
  const [jsonMode, setJsonMode] = useState<JsonMode>("none");
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [presetId, setPresetId] = useState<string>("custom");

  useEffect(() => {
    if (providerId) {
      void api.getProvider(providerId).then((p) => {
        setName(p.friendly_name);
        setKind(p.provider_kind);
        setSubKind(p.provider_sub_kind ?? null);
        setBaseUrl(p.base_url);
        setKeySource(p.api_key_source as KeySource);
        setEnvVar(p.api_key_env_var ?? "");
        setModels(p.available_models);
        setJsonMode(p.json_mode ?? "none");
      });
    }
  }, [providerId]);

  const handleKindChange = (newKind: string) => {
    setKind(newKind);
    // anthropic_native has a single valid sub_kind; auto-assign.
    // openai_compat needs admin to pick between openai_chat/ollama/volcengine.
    setSubKind(newKind === "anthropic_native" ? "anthropic" : null);
    if (isNew) {
      setBaseUrl(DEFAULT_URLS[newKind] ?? "");
    }
  };

  const handlePresetChange = (id: string) => {
    setPresetId(id);
    const preset = PRESETS.find((p) => p.id === id);
    if (!preset || preset.id === "custom") return;
    setKind(preset.kind);
    setBaseUrl(preset.baseUrl);
    setKeySource(preset.keySource);
    setEnvVar(preset.envVar ?? "");
    if (preset.keySource !== "inline") setInlineKey("");
    if (!name.trim()) setName(t(preset.labelKey));
  };

  const buildBody = (friendly?: string): CreateProviderBody => ({
    friendly_name: (friendly ?? name).trim(),
    provider_kind: kind,
    provider_sub_kind: subKind,
    base_url: baseUrl.trim(),
    api_key_source: keySource,
    api_key_env_var: keySource === "env_var" ? envVar.trim() || null : null,
    api_key_inline: keySource === "inline" ? inlineKey : null,
    available_models: models,
    json_mode: jsonMode,
  });

  const handleSave = async () => {
    setSaving(true);
    try {
      const body = buildBody();
      if (persistedId) {
        await api.patchProvider(persistedId, body);
      } else {
        const res = await api.createProvider(body);
        setPersistedId(res.id);
      }
      setCreatedInSession(false);
      onSaved();
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = async () => {
    if (createdInSession && persistedId) {
      try {
        await api.deleteProvider(persistedId);
      } catch {
        // ignore — user can clean up from the list
      }
    }
    onCancel();
  };

  const handleTest = async () => {
    setTestResult(null);
    setSaving(true);
    try {
      let id = persistedId;
      if (!id) {
        const body = buildBody(name.trim() || "Untitled");
        const res = await api.createProvider(body);
        id = res.id;
        setPersistedId(id);
        setCreatedInSession(true);
      } else {
        // Persist current form edits before testing.
        await api.patchProvider(id, buildBody());
      }
      const result = await api.testProvider(id);
      setTestResult(result);
    } catch (e) {
      setTestResult({ ok: false, error: String(e) });
    } finally {
      setSaving(false);
    }
  };

  const handleDiscoverModels = async () => {
    if (!persistedId) return;
    setDiscovering(true);
    try {
      const res = await api.discoverModels(persistedId);
      setModels(res.models);
    } catch {
      // ignore
    } finally {
      setDiscovering(false);
    }
  };

  const togglePinned = (modelId: string) => {
    setModels((prev) =>
      prev.map((m) => (m.id === modelId ? { ...m, pinned: !m.pinned } : m)),
    );
  };

  const setContextWindow = (modelId: string, next: number | null) => {
    setModels((prev) =>
      prev.map((m) => (m.id === modelId ? { ...m, context_window: next } : m)),
    );
  };

  const setModelJsonMode = (modelId: string, next: JsonMode | null) => {
    setModels((prev) =>
      prev.map((m) => (m.id === modelId ? { ...m, json_mode: next } : m)),
    );
  };

  const [expandedSamplingId, setExpandedSamplingId] = useState<string | null>(null);

  type SamplingKey =
    | "temperature"
    | "top_p"
    | "top_k"
    | "presence_penalty"
    | "frequency_penalty"
    | "repetition_penalty"
    | "num_ctx"
    | "thinking_budget_tokens";

  const setModelSampling = (modelId: string, key: SamplingKey, next: number | null) => {
    setModels((prev) =>
      prev.map((m) => (m.id === modelId ? { ...m, [key]: next } : m)),
    );
  };

  const setModelThinkingEnabled = (modelId: string, next: boolean | null) => {
    setModels((prev) =>
      prev.map((m) => (m.id === modelId ? { ...m, thinking_enabled: next } : m)),
    );
  };

  const SAMPLING_FIELDS: ReadonlyArray<{ key: SamplingKey; label: string; step: string }> = [
    { key: "temperature", label: "temperature", step: "0.01" },
    { key: "top_p", label: "top_p", step: "0.01" },
    { key: "top_k", label: "top_k", step: "1" },
    { key: "presence_penalty", label: "presence_pen", step: "0.1" },
    { key: "frequency_penalty", label: "frequency_pen", step: "0.1" },
    { key: "repetition_penalty", label: "repetition_pen", step: "0.01" },
    { key: "num_ctx", label: "num_ctx", step: "1" },
    { key: "thinking_budget_tokens", label: "thinking_budget", step: "1" },
  ];

  // Effective whitelist for the currently-picked sub_kind. Null means no
  // sub_kind yet → editor disabled (admin must classify first).
  const samplingWhitelist = subKind ? SUB_KIND_PARAM_WHITELIST[subKind] : null;

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">
        {isNew ? t("providers.add") : t("providers.edit")}
      </h3>

      {isNew && (
        <div>
          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">{t("providers.preset")}</span>
            <select
              value={presetId}
              onChange={(e) => handlePresetChange(e.target.value)}
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            >
              {PRESETS.map((p) => (
                <option key={p.id} value={p.id}>
                  {t(p.labelKey)}
                </option>
              ))}
            </select>
          </label>
          <p className="mt-1.5 rounded border border-gray-100 bg-gray-50 px-2 py-1.5 text-[10px] leading-relaxed text-gray-500">
            {t("providers.local_provider_hint")}
          </p>
        </div>
      )}

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("providers.name")}</span>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. DeepSeek, Volcengine, Anthropic"
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
        />
      </label>

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("providers.kind")}</span>
        <select
          value={kind}
          onChange={(e) => handleKindChange(e.target.value)}
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
        >
          {PROVIDER_KINDS.map((k) => (
            <option key={k.value} value={k.value}>
              {t(k.labelKey)}
            </option>
          ))}
        </select>
      </label>

      {kind === "openai_compat" && (
        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">
            {t("providers.sub_kind")}
          </span>
          <select
            value={subKind ?? ""}
            onChange={(e) =>
              setSubKind(e.target.value === "" ? null : (e.target.value as ProviderSubKind))
            }
            className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
          >
            <option value="">{t("providers.sub_kind_unset")}</option>
            <option value="openai_chat">{t("providers.sub_kind_openai_chat")}</option>
            <option value="ollama">{t("providers.sub_kind_ollama")}</option>
            <option value="volcengine">{t("providers.sub_kind_volcengine")}</option>
            <option value="llamacpp">{t("providers.sub_kind_llamacpp")}</option>
          </select>
          <p className="mt-1 text-[10px] text-gray-400">{t("providers.sub_kind_hint")}</p>
        </label>
      )}

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("providers.base_url")}</span>
        <input
          type="text"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs font-mono text-gray-700 focus:border-blue-400 focus:outline-none"
        />
      </label>

      <div>
        <span className="text-[11px] font-medium text-gray-500">{t("providers.api_key_source")}</span>
        <div className="mt-1 flex gap-3">
          <label className="flex items-center gap-1 text-xs text-gray-600">
            <input
              type="radio"
              name="keySource"
              checked={keySource === "env_var"}
              onChange={() => setKeySource("env_var")}
            />
            {t("providers.api_key_env_var")}
          </label>
          <label className="flex items-center gap-1 text-xs text-gray-600">
            <input
              type="radio"
              name="keySource"
              checked={keySource === "inline"}
              onChange={() => setKeySource("inline")}
            />
            {t("providers.api_key_inline")}
          </label>
          <label className="flex items-center gap-1 text-xs text-gray-600">
            <input
              type="radio"
              name="keySource"
              checked={keySource === "none"}
              onChange={() => setKeySource("none")}
            />
            {t("providers.api_key_none")}
          </label>
        </div>
        {keySource === "env_var" && (
          <input
            type="text"
            value={envVar}
            onChange={(e) => setEnvVar(e.target.value)}
            placeholder="OPENAI_API_KEY"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-xs font-mono text-gray-700 focus:border-blue-400 focus:outline-none"
          />
        )}
        {keySource === "inline" && (
          <input
            type="password"
            value={inlineKey}
            onChange={(e) => setInlineKey(e.target.value)}
            placeholder="sk-..."
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-xs font-mono text-gray-700 focus:border-blue-400 focus:outline-none"
          />
        )}
        {keySource === "none" && (
          <p className="mt-1 text-[10px] text-gray-400">{t("providers.api_key_none_hint")}</p>
        )}
      </div>

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">
          {t("providers.json_mode")}
        </span>
        <select
          value={jsonMode}
          onChange={(e) => setJsonMode(e.target.value as JsonMode)}
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
        >
          <option value="none">{t("providers.json_mode_none")}</option>
          <option value="object">{t("providers.json_mode_object")}</option>
          <option value="schema">{t("providers.json_mode_schema")}</option>
        </select>
        <p className="mt-1 text-[10px] text-gray-400">
          {t("providers.json_mode_hint")}
        </p>
      </label>

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => void handleTest()}
          disabled={saving}
          className="rounded border border-gray-300 px-3 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
        >
          {t("providers.test_connection")}
        </button>
        {testResult && (
          <span
            className={[
              "text-[11px]",
              testResult.ok ? "text-green-600" : "text-red-500",
            ].join(" ")}
          >
            {testResult.ok
              ? t("providers.test_success")
              : `${t("providers.test_failed")}: ${testResult.error ?? ""}`}
          </span>
        )}
      </div>

      <div>
        <div className="flex items-center justify-between">
          <span className="text-[11px] font-medium text-gray-500">{t("providers.models")}</span>
          {!isNew && (
            <button
              type="button"
              onClick={() => void handleDiscoverModels()}
              disabled={discovering}
              className="text-[10px] text-blue-500 hover:text-blue-600 disabled:opacity-50"
            >
              {discovering ? "..." : t("providers.discover_models")}
            </button>
          )}
        </div>
        {models.length > 0 ? (
          <div className="mt-1 max-h-32 space-y-0.5 overflow-auto rounded border border-gray-200 p-1.5">
            {models.map((m) => (
              <div key={m.id} className="rounded hover:bg-gray-50">
                <div className="flex items-center gap-2 px-1.5 py-1 text-[11px]">
                  <span className="min-w-0 flex-1 truncate font-mono text-gray-700" title={m.id}>
                    {m.id}
                  </span>
                  <ContextWindowInput
                    value={m.context_window}
                    onCommit={(v) => setContextWindow(m.id, v)}
                    placeholder={t("providers.context_window")}
                    title={t("providers.context_window_hint")}
                  />
                  <select
                    value={m.json_mode ?? ""}
                    onChange={(e) =>
                      setModelJsonMode(
                        m.id,
                        e.target.value === "" ? null : (e.target.value as JsonMode),
                      )
                    }
                    title={t("providers.json_mode_model_hint")}
                    className="rounded border border-gray-200 bg-white px-1 py-0.5 text-[10px] text-gray-600 focus:border-blue-400 focus:outline-none"
                  >
                    <option value="">{t("providers.json_mode_inherit")}</option>
                    <option value="none">{t("providers.json_mode_none")}</option>
                    <option value="object">{t("providers.json_mode_object")}</option>
                    <option value="schema">{t("providers.json_mode_schema")}</option>
                  </select>
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedSamplingId((cur) => (cur === m.id ? null : m.id))
                    }
                    title={t("providers.sampling_toggle_hint")}
                    className={[
                      "rounded px-1 py-0.5 text-[10px]",
                      expandedSamplingId === m.id
                        ? "bg-blue-100 text-blue-700"
                        : "bg-gray-100 text-gray-500 hover:text-gray-700",
                    ].join(" ")}
                  >
                    {expandedSamplingId === m.id ? "\u25BE" : "\u25B8"} T
                  </button>
                  <button
                    type="button"
                    onClick={() => togglePinned(m.id)}
                    className={[
                      "rounded-full px-1.5 py-0.5 text-[9px]",
                      m.pinned
                        ? "bg-blue-100 text-blue-700"
                        : "bg-gray-100 text-gray-400 hover:text-gray-600",
                    ].join(" ")}
                  >
                    {m.pinned ? "\u2605" : "\u2606"} {t("providers.pinned")}
                  </button>
                </div>
                {expandedSamplingId === m.id && (
                  samplingWhitelist === null ? (
                    <div className="border-t border-gray-100 bg-amber-50 px-2 py-1.5 text-[10px] text-amber-700">
                      {t("providers.sub_kind_required_for_sampling")}
                    </div>
                  ) : (
                    <div className="border-t border-gray-100 bg-gray-50 px-1.5 py-1.5 text-[10px]">
                      <div className="grid grid-cols-4 gap-1">
                        {SAMPLING_FIELDS.filter((f) => samplingWhitelist.has(f.key)).map((f) => (
                          <SamplingInput
                            key={f.key}
                            label={f.label}
                            value={(m[f.key] as number | null | undefined) ?? null}
                            step={f.step}
                            onCommit={(v) => setModelSampling(m.id, f.key, v)}
                          />
                        ))}
                      </div>
                      {samplingWhitelist.has("thinking_enabled") && (
                        <ThinkingToggle
                          value={m.thinking_enabled ?? null}
                          onChange={(v) => setModelThinkingEnabled(m.id, v)}
                          t={t}
                        />
                      )}
                    </div>
                  )
                )}
              </div>
            ))}
          </div>
        ) : (
          <div className="mt-1 rounded border border-dashed border-gray-200 py-2 text-center text-[10px] text-gray-400">
            {isNew
              ? t("providers.discover_models_hint")
              : t("providers.discover_models")}
          </div>
        )}
      </div>

      <div className="flex justify-end gap-2 border-t border-gray-100 pt-3">
        <button
          type="button"
          onClick={() => void handleCancel()}
          className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
        >
          {t("providers.cancel")}
        </button>
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={saving || !name.trim() || !baseUrl.trim()}
          className="rounded bg-blue-500 px-4 py-1.5 text-xs text-white hover:bg-blue-600 disabled:opacity-50"
        >
          {saving ? "..." : t("providers.save")}
        </button>
      </div>
    </div>
  );
}

function formatContextWindow(n: number | null | undefined): string {
  return formatTokensKM(n);
}

function ContextWindowInput({
  value,
  onCommit,
  placeholder,
  title,
}: {
  value: number | null;
  onCommit: (next: number | null) => void;
  placeholder: string;
  title: string;
}) {
  const [draft, setDraft] = useState(formatContextWindow(value));

  useEffect(() => {
    setDraft(formatContextWindow(value));
  }, [value]);

  const commit = () => {
    const parsed = parseTokensKM(draft);
    if (parsed !== value) onCommit(parsed);
    setDraft(formatContextWindow(parsed));
  };

  return (
    <input
      type="text"
      inputMode="text"
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          (e.target as HTMLInputElement).blur();
        }
        if (e.key === "Escape") {
          setDraft(formatContextWindow(value));
          (e.target as HTMLInputElement).blur();
        }
      }}
      placeholder={placeholder}
      title={title}
      className="w-20 rounded border border-gray-200 bg-white px-1.5 py-0.5 text-right text-[10px] text-gray-600 focus:border-blue-400 focus:outline-none"
    />
  );
}

function SamplingInput({
  label,
  value,
  step,
  onCommit,
}: {
  label: string;
  value: number | null;
  step: string;
  onCommit: (next: number | null) => void;
}) {
  const [draft, setDraft] = useState(value === null ? "" : String(value));

  useEffect(() => {
    setDraft(value === null ? "" : String(value));
  }, [value]);

  const commit = () => {
    const trimmed = draft.trim();
    if (trimmed === "") {
      if (value !== null) onCommit(null);
      return;
    }
    const parsed = Number(trimmed);
    if (Number.isFinite(parsed)) {
      if (parsed !== value) onCommit(parsed);
      setDraft(String(parsed));
    } else {
      setDraft(value === null ? "" : String(value));
    }
  };

  return (
    <label className="flex flex-col gap-0.5">
      <span className="truncate text-[9px] font-mono text-gray-500" title={label}>
        {label}
      </span>
      <input
        type="number"
        inputMode="decimal"
        step={step}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLInputElement).blur();
          }
          if (e.key === "Escape") {
            setDraft(value === null ? "" : String(value));
            (e.target as HTMLInputElement).blur();
          }
        }}
        placeholder="—"
        className="w-full rounded border border-gray-200 bg-white px-1 py-0.5 text-right text-[10px] text-gray-700 focus:border-blue-400 focus:outline-none"
      />
    </label>
  );
}

function ThinkingToggle({
  value,
  onChange,
  t,
}: {
  value: boolean | null;
  onChange: (next: boolean | null) => void;
  t: (key: string) => string;
}) {
  // Tri-state: null = "inherit provider default", true = forced on, false = forced off.
  return (
    <div className="mt-1.5 flex items-center gap-2 border-t border-gray-200/60 pt-1.5">
      <span className="text-[10px] font-mono text-gray-500">thinking</span>
      <select
        value={value === null ? "default" : value ? "on" : "off"}
        onChange={(e) => {
          const v = e.target.value;
          onChange(v === "default" ? null : v === "on");
        }}
        className="rounded border border-gray-200 bg-white px-1 py-0.5 text-[10px] text-gray-700 focus:border-blue-400 focus:outline-none"
      >
        <option value="default">{t("providers.thinking_default")}</option>
        <option value="on">{t("providers.thinking_on")}</option>
        <option value="off">{t("providers.thinking_off")}</option>
      </select>
    </div>
  );
}

// ---------------------------------------------------------------- MCP Servers panel

interface MCPPreset {
  id: string;
  labelKey: string;
  serverId: string;
  kind: MCPServerKind;
  url: string;
}

const MCP_PRESETS: MCPPreset[] = [
  { id: "custom", labelKey: "mcp.preset_custom", serverId: "", kind: "http", url: "" },
  { id: "deepwiki", labelKey: "mcp.preset_deepwiki", serverId: "deepwiki", kind: "http", url: "https://mcp.deepwiki.com/mcp" },
  { id: "cloudflare_docs", labelKey: "mcp.preset_cloudflare_docs", serverId: "cloudflare_docs", kind: "http", url: "https://docs.mcp.cloudflare.com/mcp" },
  { id: "tavily", labelKey: "mcp.preset_tavily", serverId: "tavily", kind: "http", url: "https://mcp.tavily.com/mcp/?tavilyApiKey=YOUR_KEY" },
  { id: "context7", labelKey: "mcp.preset_context7", serverId: "context7", kind: "http", url: "https://mcp.context7.com/mcp" },
  { id: "github", labelKey: "mcp.preset_github", serverId: "github", kind: "http", url: "https://api.githubcopilot.com/mcp/" },
  { id: "exa", labelKey: "mcp.preset_exa", serverId: "exa", kind: "http", url: "https://mcp.exa.ai/mcp" },
];

function MCPServersPanel() {
  const { t } = useTranslation();
  const [servers, setServers] = useState<MCPServerState[]>([]);
  const [loading, setLoading] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const fetchServers = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await api.listMCPServers());
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchServers();
  }, [fetchServers]);

  const handleDelete = async (id: string) => {
    setBusy(id);
    try {
      await api.deleteMCPServer(id);
      setDeleteConfirm(null);
      await fetchServers();
    } finally {
      setBusy(null);
    }
  };

  const handleToggleEnabled = async (s: MCPServerState) => {
    setBusy(s.id);
    try {
      await api.patchMCPServer(s.id, { enabled: !s.enabled });
      await fetchServers();
    } finally {
      setBusy(null);
    }
  };

  const handleReconnect = async (id: string) => {
    setBusy(id);
    try {
      await api.reconnectMCPServer(id);
      await fetchServers();
    } finally {
      setBusy(null);
    }
  };

  if (editingId !== null) {
    return (
      <MCPServerForm
        serverId={editingId === "new" ? null : editingId}
        existing={servers.find((s) => s.id === editingId) ?? null}
        onSaved={() => {
          setEditingId(null);
          void fetchServers();
        }}
        onCancel={() => setEditingId(null)}
      />
    );
  }

  return (
    <div>
      {loading && servers.length === 0 && (
        <div className="py-8 text-center text-xs text-gray-400">Loading...</div>
      )}

      {!loading && servers.length === 0 && (
        <div className="py-8 text-center text-xs text-gray-400">{t("mcp.no_servers")}</div>
      )}

      {servers.map((s) => {
        const statusColor = !s.enabled
          ? "bg-gray-300"
          : s.is_connected
            ? "bg-green-500"
            : "bg-red-500";
        const statusLabel = !s.enabled
          ? t("mcp.disconnected")
          : s.is_connected
            ? t("mcp.connected")
            : t("mcp.disconnected");
        return (
          <div
            key={s.id}
            className="group mb-2 rounded-lg border border-gray-200 px-4 py-3 hover:border-gray-300"
          >
            <div className="flex items-center justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className={`h-2 w-2 rounded-full ${statusColor}`} />
                  <span className="text-sm font-medium text-gray-800">{s.friendly_name}</span>
                  <span className="font-mono text-[10px] text-gray-400">{s.server_id}</span>
                </div>
                <div className="mt-0.5 truncate text-[11px] text-gray-400" title={s.url ?? s.command ?? ""}>
                  {s.kind === "http" ? s.url : s.command}
                </div>
                <div className="mt-1 flex items-center gap-2 text-[10px] text-gray-500">
                  <span>{statusLabel}</span>
                  {s.enabled && (
                    <span>
                      {"\u00B7"} {s.tool_count} {t("mcp.tools")}
                    </span>
                  )}
                </div>
                {s.tool_names.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {s.tool_names.slice(0, 6).map((n) => (
                      <span
                        key={n}
                        className="rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[9px] text-gray-600"
                        title={n}
                      >
                        {n.replace(/^mcp__[^_]+__/, "")}
                      </span>
                    ))}
                    {s.tool_names.length > 6 && (
                      <span className="text-[10px] text-gray-400">+{s.tool_names.length - 6}</span>
                    )}
                  </div>
                )}
                {s.last_error && (
                  <div
                    className="mt-1 truncate rounded bg-red-50 px-2 py-1 font-mono text-[10px] text-red-600"
                    title={s.last_error}
                  >
                    {s.last_error}
                  </div>
                )}
              </div>
              <div className="ml-3 flex flex-col items-end gap-1">
                <label className="flex items-center gap-1 text-[10px] text-gray-500">
                  <input
                    type="checkbox"
                    checked={s.enabled}
                    disabled={busy === s.id}
                    onChange={() => void handleToggleEnabled(s)}
                  />
                  {t("mcp.enabled")}
                </label>
                <div className="flex gap-1 opacity-0 transition-opacity group-hover:opacity-100">
                  {s.enabled && (
                    <button
                      type="button"
                      onClick={() => void handleReconnect(s.id)}
                      disabled={busy === s.id}
                      className="rounded border border-gray-300 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50 disabled:opacity-50"
                    >
                      {t("mcp.reconnect")}
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setEditingId(s.id)}
                    className="rounded border border-gray-300 px-2 py-1 text-[11px] text-gray-600 hover:bg-gray-50"
                  >
                    {t("mcp.edit")}
                  </button>
                  <button
                    type="button"
                    onClick={() => setDeleteConfirm(s.id)}
                    className="rounded border border-red-200 px-2 py-1 text-[11px] text-red-500 hover:bg-red-50"
                  >
                    {t("mcp.delete")}
                  </button>
                </div>
              </div>
            </div>
          </div>
        );
      })}

      <button
        type="button"
        onClick={() => setEditingId("new")}
        className="mt-2 w-full rounded-lg border border-dashed border-gray-300 py-2 text-xs text-gray-500 hover:border-blue-400 hover:text-blue-500"
      >
        + {t("mcp.add")}
      </button>

      {deleteConfirm && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/20"
          onClick={() => setDeleteConfirm(null)}
        >
          <div
            className="w-72 rounded-lg border border-gray-200 bg-white p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="mb-3 text-sm text-gray-700">{t("mcp.delete_confirm")}</p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteConfirm(null)}
                className="rounded border border-gray-300 bg-white px-3 py-1 text-xs text-gray-600 hover:bg-gray-50"
              >
                {t("mcp.cancel")}
              </button>
              <button
                type="button"
                onClick={() => void handleDelete(deleteConfirm)}
                className="rounded bg-red-500 px-3 py-1 text-xs text-white hover:bg-red-600"
              >
                {t("mcp.delete")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function MCPServerForm({
  serverId,
  existing,
  onSaved,
  onCancel,
}: {
  serverId: string | null;
  existing: MCPServerState | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const isNew = serverId === null;

  const [presetId, setPresetId] = useState("custom");
  const [serverIdField, setServerIdField] = useState(existing?.server_id ?? "");
  const [friendlyName, setFriendlyName] = useState(existing?.friendly_name ?? "");
  const [kind, setKind] = useState<MCPServerKind>(existing?.kind ?? "http");
  const [url, setUrl] = useState(existing?.url ?? "");
  const [headersText, setHeadersText] = useState("{}");
  const [command, setCommand] = useState(existing?.command ?? "");
  const [argsText, setArgsText] = useState("");
  const [envText, setEnvText] = useState("");
  const [enabled, setEnabled] = useState(existing?.enabled ?? true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handlePreset = (id: string) => {
    setPresetId(id);
    const p = MCP_PRESETS.find((x) => x.id === id);
    if (!p || p.id === "custom") return;
    setServerIdField(p.serverId);
    setKind(p.kind);
    setUrl(p.url);
    if (!friendlyName.trim()) setFriendlyName(t(p.labelKey));
  };

  const parseHeaders = (): Record<string, string> => {
    const s = headersText.trim();
    if (!s) return {};
    try {
      const parsed = JSON.parse(s);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("headers must be a JSON object");
      }
      return parsed as Record<string, string>;
    } catch (e) {
      throw new Error(`headers JSON: ${(e as Error).message}`);
    }
  };

  const parseLines = (raw: string): string[] =>
    raw.split("\n").map((l) => l.trim()).filter((l) => l.length > 0);

  const parseEnv = (): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const line of parseLines(envText)) {
      const eq = line.indexOf("=");
      if (eq <= 0) throw new Error(`env line missing '=': ${line}`);
      out[line.slice(0, eq).trim()] = line.slice(eq + 1);
    }
    return out;
  };

  const handleSave = async () => {
    setError(null);
    setSaving(true);
    try {
      if (isNew) {
        const body: CreateMCPServerBody = {
          server_id: serverIdField.trim(),
          friendly_name: friendlyName.trim(),
          kind,
          enabled,
        };
        if (kind === "http") {
          body.url = url.trim();
          body.headers = parseHeaders();
        } else {
          body.command = command.trim();
          body.args = parseLines(argsText);
          body.env = parseEnv();
        }
        await api.createMCPServer(body);
      } else {
        const patch: Parameters<typeof api.patchMCPServer>[1] = {
          friendly_name: friendlyName.trim(),
          enabled,
        };
        if (kind === "http") {
          patch.url = url.trim();
          patch.headers = parseHeaders();
        } else {
          patch.command = command.trim();
          patch.args = parseLines(argsText);
          patch.env = parseEnv();
        }
        await api.patchMCPServer(serverId!, patch);
      }
      onSaved();
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">
        {isNew ? t("mcp.add") : t("mcp.edit")}
      </h3>

      {isNew && (
        <label className="block">
          <span className="text-[11px] font-medium text-gray-500">{t("mcp.preset")}</span>
          <select
            value={presetId}
            onChange={(e) => handlePreset(e.target.value)}
            className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
          >
            {MCP_PRESETS.map((p) => (
              <option key={p.id} value={p.id}>
                {t(p.labelKey)}
              </option>
            ))}
          </select>
        </label>
      )}

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("mcp.server_id")}</span>
        <input
          type="text"
          value={serverIdField}
          onChange={(e) => setServerIdField(e.target.value)}
          disabled={!isNew}
          placeholder="tavily, github, deepwiki..."
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none disabled:bg-gray-50 disabled:text-gray-500"
        />
        <p className="mt-1 text-[10px] text-gray-400">{t("mcp.server_id_hint")}</p>
      </label>

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("mcp.friendly_name")}</span>
        <input
          type="text"
          value={friendlyName}
          onChange={(e) => setFriendlyName(e.target.value)}
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
        />
      </label>

      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("mcp.kind")}</span>
        <select
          value={kind}
          onChange={(e) => setKind(e.target.value as MCPServerKind)}
          disabled={!isNew}
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none disabled:bg-gray-50 disabled:text-gray-500"
        >
          <option value="http">{t("mcp.kind_http")}</option>
          <option value="stdio">{t("mcp.kind_stdio")}</option>
        </select>
      </label>

      {kind === "http" ? (
        <>
          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">{t("mcp.url")}</span>
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://mcp.example.com/mcp"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">{t("mcp.headers")}</span>
            <textarea
              value={headersText}
              onChange={(e) => setHeadersText(e.target.value)}
              rows={3}
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>
        </>
      ) : (
        <>
          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">{t("mcp.command")}</span>
            <input
              type="text"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              placeholder="npx, uvx, /usr/bin/python..."
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">{t("mcp.args")}</span>
            <textarea
              value={argsText}
              onChange={(e) => setArgsText(e.target.value)}
              rows={3}
              placeholder="-y\n@modelcontextprotocol/server-foo"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">{t("mcp.env")}</span>
            <textarea
              value={envText}
              onChange={(e) => setEnvText(e.target.value)}
              rows={2}
              placeholder="API_KEY=xxx"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 font-mono text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>
        </>
      )}

      <label className="flex items-start gap-2">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
          className="mt-0.5"
        />
        <div>
          <div className="text-xs font-medium text-gray-700">{t("mcp.enabled")}</div>
          <div className="text-[10px] text-gray-500">{t("mcp.enabled_hint")}</div>
        </div>
      </label>

      {error && (
        <div className="rounded border border-red-200 bg-red-50 px-2 py-1.5 font-mono text-[11px] text-red-600">
          {error}
        </div>
      )}

      <div className="flex justify-end gap-2 border-t border-gray-100 pt-3">
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50"
        >
          {t("mcp.cancel")}
        </button>
        <button
          type="button"
          onClick={() => void handleSave()}
          disabled={
            saving ||
            !friendlyName.trim() ||
            !serverIdField.trim() ||
            (kind === "http" ? !url.trim() : !command.trim())
          }
          className="rounded bg-blue-500 px-4 py-1.5 text-xs text-white hover:bg-blue-600 disabled:opacity-50"
        >
          {saving ? "..." : t("mcp.save")}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Tools panel

function ToolsPanel() {
  const { t } = useTranslation();
  const [tools, setTools] = useState<ToolDTO[]>([]);
  const [states, setStates] = useState<Record<string, ToolState>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [toolList, settings] = await Promise.all([
        api.listTools(),
        api.getWorkspaceSettings(),
      ]);
      setTools(toolList);
      setStates(settings.tool_states ?? {});
      setDirty(false);
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const effectiveState = (name: string): ToolState => {
    if (name in states) return states[name];
    // Fallback matches backend defaults: Bash = available, others = default_allow.
    if (name === "Bash") return "available";
    return "default_allow";
  };

  const setState = (name: string, next: ToolState) => {
    setStates((prev) => ({ ...prev, [name]: next }));
    setDirty(true);
  };

  const save = async () => {
    setSaving(true);
    try {
      const updated = await api.patchWorkspaceSettings({ tool_states: states });
      setStates(updated.tool_states ?? {});
      setDirty(false);
    } finally {
      setSaving(false);
    }
  };

  // Split built-ins vs MCP tools; MCP tools carry the `mcp__` prefix.
  const builtins = tools.filter((tt) => !tt.name.startsWith("mcp__"));
  const mcpTools = tools.filter((tt) => tt.name.startsWith("mcp__"));

  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-sm font-medium text-gray-800">
          {t("tools_panel.title")}
        </h3>
        <p className="mt-1 text-[11px] text-gray-500">
          {t("tools_panel.description")}
        </p>
      </div>

      {loading ? (
        <p className="text-xs text-gray-500">{t("mcp.loading")}</p>
      ) : (
        <>
          <ToolStateGroup
            heading={t("tools_panel.builtin_heading")}
            tools={builtins}
            stateFor={effectiveState}
            onChange={setState}
          />
          {mcpTools.length > 0 && (
            <ToolStateGroup
              heading={t("tools_panel.mcp_heading")}
              tools={mcpTools}
              stateFor={effectiveState}
              onChange={setState}
            />
          )}
        </>
      )}

      <div className="flex items-center justify-between border-t border-gray-100 pt-3">
        <p className="text-[10px] text-gray-400">
          {t("tools_panel.legend")}
        </p>
        <button
          type="button"
          onClick={() => void save()}
          disabled={!dirty || saving}
          className="rounded bg-blue-500 px-4 py-1.5 text-xs text-white hover:bg-blue-600 disabled:opacity-50"
        >
          {saving ? "..." : t("providers.save")}
        </button>
      </div>
    </div>
  );
}

function ToolStateGroup({
  heading,
  tools,
  stateFor,
  onChange,
}: {
  heading: string;
  tools: ToolDTO[];
  stateFor: (name: string) => ToolState;
  onChange: (name: string, next: ToolState) => void;
}) {
  const { t } = useTranslation();
  const options: Array<{ value: ToolState; labelKey: string }> = [
    { value: "default_allow", labelKey: "tools_panel.state_default_allow" },
    { value: "available", labelKey: "tools_panel.state_available" },
    { value: "disabled", labelKey: "tools_panel.state_disabled" },
  ];

  return (
    <div>
      <h4 className="mb-1 text-[11px] font-medium uppercase tracking-wide text-gray-500">
        {heading}
      </h4>
      <div className="space-y-1 rounded border border-gray-200">
        {tools.map((tt) => (
          <div
            key={tt.name}
            className="flex items-center gap-3 border-b border-gray-100 px-3 py-2 last:border-b-0"
          >
            <div className="min-w-0 flex-1">
              <div className="truncate font-mono text-[11px] text-gray-700">
                {tt.name}
              </div>
              {tt.description && (
                <div className="truncate text-[10px] text-gray-400">
                  {tt.description}
                </div>
              )}
            </div>
            <div className="flex gap-2 text-[10px]">
              {options.map((opt) => {
                const active = stateFor(tt.name) === opt.value;
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => onChange(tt.name, opt.value)}
                    className={[
                      "rounded border px-2 py-0.5",
                      active
                        ? "border-blue-500 bg-blue-50 text-blue-700"
                        : "border-gray-200 text-gray-500 hover:bg-gray-50",
                    ].join(" ")}
                  >
                    {t(opt.labelKey)}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
