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
import type { ProviderSummary } from "@/lib/api";
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
  const [modelKey, setModelKey] = useState("");
  const [saving, setSaving] = useState(false);

  const loadProviders = useCallback(async () => {
    try {
      const list = await api.listProviders();
      setProviders(list);
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    if (open) {
      void loadProviders();
      setModelKey(refKey(chatflow?.default_model ?? null));
    }
  }, [open, chatflow, loadProviders]);

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
      await patchChatFlow({
        default_model: parseRefKey(modelKey),
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
}: {
  label: string;
  hint: string;
  value: string;
  options: Array<{ key: string; label: string; pinned: boolean }>;
  onChange: (v: string) => void;
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
