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
import type { CreateProviderBody, ModelInfoDTO, ProviderSummary } from "@/lib/api";
import { formatTokensKM } from "@/lib/tokenFormat";
import { usePreferencesStore } from "@/store/preferencesStore";

type TabId = "providers" | "canvas";

const TABS: Array<{ id: TabId; labelKey: string }> = [
  { id: "providers", labelKey: "providers.title" },
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

  const rows: Array<{ key: string; value: boolean; onChange: (v: boolean) => void }> = [
    { key: "show_node_id", value: showNodeId, onChange: setShowNodeId },
    { key: "show_chatflow_id", value: showChatflowId, onChange: setShowChatflowId },
    { key: "show_tokens", value: showTokens, onChange: setShowTokens },
    { key: "show_gen_time", value: showGenTime, onChange: setShowGenTime },
    { key: "show_gen_speed", value: showGenSpeed, onChange: setShowGenSpeed },
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
  const [baseUrl, setBaseUrl] = useState(DEFAULT_URLS.openai_compat);
  const [keySource, setKeySource] = useState<KeySource>("env_var");
  const [envVar, setEnvVar] = useState("");
  const [inlineKey, setInlineKey] = useState("");
  const [models, setModels] = useState<ModelInfoDTO[]>([]);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [presetId, setPresetId] = useState<string>("custom");

  useEffect(() => {
    if (providerId) {
      void api.getProvider(providerId).then((p) => {
        setName(p.friendly_name);
        setKind(p.provider_kind);
        setBaseUrl(p.base_url);
        setKeySource(p.api_key_source as KeySource);
        setEnvVar(p.api_key_env_var ?? "");
        setModels(p.available_models);
      });
    }
  }, [providerId]);

  const handleKindChange = (newKind: string) => {
    setKind(newKind);
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
    base_url: baseUrl.trim(),
    api_key_source: keySource,
    api_key_env_var: keySource === "env_var" ? envVar.trim() || null : null,
    api_key_inline: keySource === "inline" ? inlineKey : null,
    available_models: models,
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
              <div
                key={m.id}
                className="flex items-center gap-2 rounded px-1.5 py-1 text-[11px] hover:bg-gray-50"
              >
                <span className="min-w-0 flex-1 truncate font-mono text-gray-700" title={m.id}>
                  {m.id}
                </span>
                <ContextWindowInput
                  value={m.context_window}
                  onCommit={(v) => setContextWindow(m.id, v)}
                  placeholder={t("providers.context_window")}
                  title={t("providers.context_window_hint")}
                />
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

// Parses "32k", "128K", "1m", "1.5M", "4096" (case-insensitive, optional
// space). k = 1024, m = 1024*1024. Returns null for empty / invalid input.
function parseContextWindow(raw: string): number | null {
  const s = raw.trim().toLowerCase();
  if (!s) return null;
  const match = /^(\d+(?:\.\d+)?)\s*([km]?)$/.exec(s);
  if (!match) return null;
  const n = Number.parseFloat(match[1]);
  if (!Number.isFinite(n) || n <= 0) return null;
  const mult = match[2] === "k" ? 1024 : match[2] === "m" ? 1024 * 1024 : 1;
  return Math.round(n * mult);
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
    const parsed = parseContextWindow(draft);
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
