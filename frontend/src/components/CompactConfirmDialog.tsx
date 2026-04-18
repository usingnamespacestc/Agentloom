/**
 * Tier 2 manual-compact confirmation dialog.
 *
 * The user reaches this by clicking the "Compact" button in the
 * conversation header. The dialog collects parameters for the compact
 * worker: an optional natural-language instruction (what matters to
 * the user), must-keep / must-drop hints that the worker is asked to
 * respect, a model override, and overrides for the two Tier 1 knobs
 * (``preserve_recent_turns``, target footprint). Submitting POSTs to
 * ``/api/chatflows/{id}/nodes/{parentNodeId}/compact`` — the backend
 * walks the chain up to and including ``parentNodeId`` and spawns a
 * compact ChatNode as its child.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api";
import type { ProviderSummary } from "@/lib/api";
import type { ChatFlow, ChatFlowNode, ProviderModelRef } from "@/types/schema";

export interface CompactConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  chatflow: ChatFlow;
  parentNode: ChatFlowNode;
  /** Called with the newly-created compact ChatNode id so the caller
   * can switch the conversation selection onto it — otherwise the
   * user's next turn would fork beside the compact, not under it. */
  onCreated?: (nodeId: string) => void;
}

function refKey(ref: ProviderModelRef | null): string {
  return ref ? `${ref.provider_id}::${ref.model_id}` : "";
}

function parseRefKey(key: string): ProviderModelRef | null {
  if (!key) return null;
  const [provider_id, ...rest] = key.split("::");
  return { provider_id, model_id: rest.join("::") };
}

export function CompactConfirmDialog({
  open,
  onClose,
  chatflow,
  parentNode,
  onCreated,
}: CompactConfirmDialogProps) {
  const { t } = useTranslation();
  const [instruction, setInstruction] = useState("");
  const [mustKeep, setMustKeep] = useState("");
  const [mustDrop, setMustDrop] = useState("");
  const [preserveStr, setPreserveStr] = useState("");
  const [targetTokensStr, setTargetTokensStr] = useState("");
  const [modelKey, setModelKey] = useState(
    refKey(chatflow.compact_model ?? null),
  );
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    // Reset every open — users don't expect sticky instructions across
    // invocations. Defaults inherit from ChatFlow settings.
    setInstruction("");
    setMustKeep("");
    setMustDrop("");
    setPreserveStr(String(chatflow.compact_preserve_recent_turns ?? 3));
    setTargetTokensStr("");
    setModelKey(refKey(chatflow.compact_model ?? null));
    setErrorMessage(null);
    void (async () => {
      try {
        const list = await api.listProviders();
        setProviders(list);
      } catch {
        // ignore — model picker just falls back to inherit.
      }
    })();
  }, [open, chatflow]);

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
    out.sort((a, b) => {
      if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
      return a.label.localeCompare(b.label);
    });
    return out;
  }, [providers]);

  if (!open) return null;

  const handleSubmit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setErrorMessage(null);
    try {
      const preserveTrim = preserveStr.trim();
      const preserveNum = preserveTrim === "" ? NaN : Number(preserveTrim);
      const preserve =
        Number.isFinite(preserveNum) &&
        Number.isInteger(preserveNum) &&
        preserveNum >= 0
          ? preserveNum
          : null;
      const targetTrim = targetTokensStr.trim();
      const targetNum = targetTrim === "" ? NaN : Number(targetTrim);
      const targetTokens =
        Number.isFinite(targetNum) && Number.isInteger(targetNum) && targetNum > 0
          ? targetNum
          : null;
      const res = await api.compactChain(chatflow.id, parentNode.id, {
        compact_instruction: instruction.trim() || null,
        must_keep: mustKeep.trim(),
        must_drop: mustDrop.trim(),
        preserve_recent_turns: preserve,
        target_tokens: targetTokens,
        model: parseRefKey(modelKey),
      });
      onCreated?.(res.node_id);
      onClose();
    } catch (e) {
      setErrorMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      data-testid="compact-dialog"
      onClick={onClose}
    >
      <div
        className="flex w-[560px] max-h-[90vh] flex-col rounded-xl border border-gray-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-gray-800">
            {t("compact_dialog.title")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="flex h-6 w-6 items-center justify-center rounded text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          >
            {"\u2715"}
          </button>
        </div>

        <div className="space-y-4 overflow-auto px-5 py-4">
          <p className="text-[11px] text-gray-500">
            {t("compact_dialog.hint")}
          </p>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("compact_dialog.instruction")}
            </span>
            <textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              rows={3}
              data-testid="compact-dialog-instruction"
              placeholder={t("compact_dialog.instruction_placeholder")}
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
            <p className="mt-1 text-[10px] text-gray-400">
              {t("compact_dialog.instruction_hint")}
            </p>
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("compact_dialog.must_keep")}
            </span>
            <textarea
              value={mustKeep}
              onChange={(e) => setMustKeep(e.target.value)}
              rows={2}
              data-testid="compact-dialog-must-keep"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("compact_dialog.must_drop")}
            </span>
            <textarea
              value={mustDrop}
              onChange={(e) => setMustDrop(e.target.value)}
              rows={2}
              data-testid="compact-dialog-must-drop"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-[11px] font-medium text-gray-500">
                {t("compact_dialog.preserve_turns")}
              </span>
              <input
                type="number"
                min={0}
                step={1}
                value={preserveStr}
                onChange={(e) => setPreserveStr(e.target.value)}
                data-testid="compact-dialog-preserve-turns"
                className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
              />
              <p className="mt-1 text-[10px] text-gray-400">
                {t("compact_dialog.preserve_turns_hint")}
              </p>
            </label>

            <label className="block">
              <span className="text-[11px] font-medium text-gray-500">
                {t("compact_dialog.target_tokens")}
              </span>
              <input
                type="number"
                min={0}
                step={64}
                value={targetTokensStr}
                onChange={(e) => setTargetTokensStr(e.target.value)}
                data-testid="compact-dialog-target-tokens"
                placeholder={t("compact_dialog.target_tokens_placeholder")}
                className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
              />
              <p className="mt-1 text-[10px] text-gray-400">
                {t("compact_dialog.target_tokens_hint")}
              </p>
            </label>
          </div>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("compact_dialog.model")}
            </span>
            <select
              value={modelKey}
              onChange={(e) => setModelKey(e.target.value)}
              data-testid="compact-dialog-model"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-blue-400 focus:outline-none"
            >
              <option value="">{t("compact_dialog.model_inherit")}</option>
              {modelOptions.map((o) => (
                <option key={o.key} value={o.key}>
                  {o.pinned ? "\u2605 " : ""}
                  {o.label}
                </option>
              ))}
            </select>
          </label>

          {errorMessage && (
            <p
              data-testid="compact-dialog-error"
              className="rounded border border-red-300 bg-red-50 px-3 py-2 text-[11px] text-red-700"
            >
              {errorMessage}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-3">
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            {t("providers.cancel")}
          </button>
          <button
            type="button"
            onClick={() => void handleSubmit()}
            disabled={submitting}
            data-testid="compact-dialog-submit"
            className="rounded bg-blue-500 px-4 py-1.5 text-xs text-white hover:bg-blue-600 disabled:opacity-50"
          >
            {submitting ? "..." : t("compact_dialog.submit")}
          </button>
        </div>
      </div>
    </div>
  );
}
