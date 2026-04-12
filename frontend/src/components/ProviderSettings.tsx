/**
 * Provider settings panel — slide-out overlay for managing LLM providers.
 *
 * Supports:
 * - List all configured providers
 * - Create / edit / delete providers
 * - Test connection (validates API key + base URL)
 * - Discover models from the remote API
 * - Pin a model as default
 */

import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api";
import type { CreateProviderBody, ModelInfoDTO, ProviderSummary } from "@/lib/api";

interface ProviderSettingsProps {
  open: boolean;
  onClose: () => void;
}

export function ProviderSettings({ open, onClose }: ProviderSettingsProps) {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null); // null = list view, "new" = create form
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
    if (open) void fetchProviders();
  }, [open, fetchProviders]);

  const handleDelete = async (id: string) => {
    await api.deleteProvider(id);
    setDeleteConfirm(null);
    void fetchProviders();
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      onClick={onClose}
    >
      <div
        className="flex h-[80vh] w-[560px] flex-col rounded-xl border border-gray-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-gray-800">{t("providers.title")}</h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          >
            {"\u2715"}
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-auto px-5 py-3">
          {editingId !== null ? (
            <ProviderForm
              providerId={editingId === "new" ? null : editingId}
              onSaved={() => {
                setEditingId(null);
                void fetchProviders();
              }}
              onCancel={() => setEditingId(null)}
            />
          ) : (
            <>
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
            </>
          )}
        </div>
      </div>

      {/* Delete confirmation */}
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

// ---------------------------------------------------------------- Form

const PROVIDER_KINDS = [
  { value: "openai_compat", labelKey: "providers.kind_openai_compat" },
  { value: "anthropic_native", labelKey: "providers.kind_anthropic_native" },
];

const DEFAULT_URLS: Record<string, string> = {
  openai_compat: "https://api.openai.com/v1",
  anthropic_native: "https://api.anthropic.com",
};

function ProviderForm({
  providerId,
  onSaved,
  onCancel,
}: {
  providerId: string | null; // null = creating new
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const isNew = providerId === null;

  const [name, setName] = useState("");
  const [kind, setKind] = useState("openai_compat");
  const [baseUrl, setBaseUrl] = useState(DEFAULT_URLS.openai_compat);
  const [keySource, setKeySource] = useState<"env_var" | "inline">("env_var");
  const [envVar, setEnvVar] = useState("");
  const [inlineKey, setInlineKey] = useState("");
  const [models, setModels] = useState<ModelInfoDTO[]>([]);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; error?: string } | null>(null);
  const [discovering, setDiscovering] = useState(false);

  // Load existing provider for editing
  useEffect(() => {
    if (!isNew && providerId) {
      void api.getProvider(providerId).then((p) => {
        setName(p.friendly_name);
        setKind(p.provider_kind);
        setBaseUrl(p.base_url);
        setKeySource(p.api_key_source as "env_var" | "inline");
        setEnvVar(p.api_key_env_var ?? "");
        setModels(p.available_models);
      });
    }
  }, [isNew, providerId]);

  const handleKindChange = (newKind: string) => {
    setKind(newKind);
    if (isNew) {
      setBaseUrl(DEFAULT_URLS[newKind] ?? "");
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const body: CreateProviderBody = {
        friendly_name: name.trim(),
        provider_kind: kind,
        base_url: baseUrl.trim(),
        api_key_source: keySource,
        api_key_env_var: keySource === "env_var" ? envVar.trim() || null : null,
        api_key_inline: keySource === "inline" ? inlineKey : null,
        available_models: models,
      };
      if (isNew) {
        await api.createProvider(body);
      } else {
        await api.patchProvider(providerId!, body);
      }
      onSaved();
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    setTestResult(null);
    // Need to save first if new, so we have an ID to test.
    if (isNew) {
      setSaving(true);
      try {
        const body: CreateProviderBody = {
          friendly_name: name.trim() || "Untitled",
          provider_kind: kind,
          base_url: baseUrl.trim(),
          api_key_source: keySource,
          api_key_env_var: keySource === "env_var" ? envVar.trim() || null : null,
          api_key_inline: keySource === "inline" ? inlineKey : null,
        };
        const res = await api.createProvider(body);
        // Now test it
        const result = await api.testProvider(res.id);
        setTestResult(result);
        // Switch to edit mode
        // We'll just report the result - user can save explicitly
      } catch (e) {
        setTestResult({ ok: false, error: String(e) });
      } finally {
        setSaving(false);
      }
      return;
    }
    try {
      const result = await api.testProvider(providerId!);
      setTestResult(result);
    } catch (e) {
      setTestResult({ ok: false, error: String(e) });
    }
  };

  const handleDiscoverModels = async () => {
    if (!providerId) return;
    setDiscovering(true);
    try {
      const res = await api.discoverModels(providerId);
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

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-medium text-gray-700">
        {isNew ? t("providers.add") : t("providers.edit")}
      </h3>

      {/* Name */}
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

      {/* Kind */}
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

      {/* Base URL */}
      <label className="block">
        <span className="text-[11px] font-medium text-gray-500">{t("providers.base_url")}</span>
        <input
          type="text"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs font-mono text-gray-700 focus:border-blue-400 focus:outline-none"
        />
      </label>

      {/* API Key Source */}
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
        </div>
        {keySource === "env_var" ? (
          <input
            type="text"
            value={envVar}
            onChange={(e) => setEnvVar(e.target.value)}
            placeholder="OPENAI_API_KEY"
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-xs font-mono text-gray-700 focus:border-blue-400 focus:outline-none"
          />
        ) : (
          <input
            type="password"
            value={inlineKey}
            onChange={(e) => setInlineKey(e.target.value)}
            placeholder="sk-..."
            className="mt-1 w-full rounded border border-gray-300 px-2 py-1.5 text-xs font-mono text-gray-700 focus:border-blue-400 focus:outline-none"
          />
        )}
      </div>

      {/* Test Connection */}
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

      {/* Models */}
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
                className="flex items-center justify-between rounded px-1.5 py-1 text-[11px] hover:bg-gray-50"
              >
                <span className="font-mono text-gray-700">{m.id}</span>
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
              ? "Save first, then discover models"
              : t("providers.discover_models")}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex justify-end gap-2 border-t border-gray-100 pt-3">
        <button
          type="button"
          onClick={onCancel}
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
