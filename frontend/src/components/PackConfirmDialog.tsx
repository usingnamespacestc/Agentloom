/**
 * Pack confirmation dialog.
 *
 * The user reaches this by (a) right-clicking a ChatNode → "select as
 * pack start" to stash ``pendingPackStartId`` in the store, then (b)
 * right-clicking another ChatNode → "pack to here", which opens this
 * dialog with both ids in hand. Submitting calls
 * ``commitPackTo(endId, knobs)`` which derives the primary-parent-chain
 * range and POSTs to ``/api/chatflows/{id}/pack``.
 *
 * Ancestor-descendant validity is checked by the store action before
 * hitting the server; any invalid pairing surfaces as an error in
 * this dialog.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "@/lib/api";
import type { ProviderSummary } from "@/lib/api";
import { parseTokensKM } from "@/lib/tokenFormat";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlow, NodeId, ProviderModelRef } from "@/types/schema";

export interface PackConfirmDialogProps {
  open: boolean;
  onClose: () => void;
  chatflow: ChatFlow;
  /** The pack start ChatNode id (set earlier via "select as pack
   * start"). This is the earlier-in-chain endpoint. */
  startId: NodeId;
  /** The pack end ChatNode id — the user's second right-click target.
   * Pack node's parent will be ``packed_range[-1]`` which equals this
   * id when the two ids are already in start→end order. */
  endId: NodeId;
}

function refKey(ref: ProviderModelRef | null): string {
  return ref ? `${ref.provider_id}::${ref.model_id}` : "";
}

function parseRefKey(key: string): ProviderModelRef | null {
  if (!key) return null;
  const [provider_id, ...rest] = key.split("::");
  return { provider_id, model_id: rest.join("::") };
}

/** Walk primary-parent chain from ``from`` upward until we hit
 * ``target`` or run out. Returns the reversed range (root→tip order)
 * or ``null`` when they're not ancestor-descendant. Duplicates the
 * store's logic so we can preview the range before the user submits. */
function derivePackedRange(
  chat: ChatFlow,
  startId: NodeId,
  endId: NodeId,
): NodeId[] | null {
  const walkUpTo = (from: NodeId, target: NodeId): NodeId[] | null => {
    const range: NodeId[] = [];
    const guard = new Set<NodeId>();
    let cur: NodeId | null = from;
    while (cur !== null && !guard.has(cur)) {
      guard.add(cur);
      range.unshift(cur);
      if (cur === target) return range;
      const parents: NodeId[] = chat.nodes[cur]?.parent_ids ?? [];
      cur = parents.length > 0 ? parents[0] : null;
    }
    return null;
  };
  return walkUpTo(endId, startId) ?? walkUpTo(startId, endId);
}

export function PackConfirmDialog({
  open,
  onClose,
  chatflow,
  startId,
  endId,
}: PackConfirmDialogProps) {
  const { t } = useTranslation();
  const commitPackTo = useChatFlowStore((s) => s.commitPackTo);
  const cancelPendingPack = useChatFlowStore((s) => s.cancelPendingPack);
  const [instruction, setInstruction] = useState("");
  const [mustKeep, setMustKeep] = useState("");
  const [mustDrop, setMustDrop] = useState("");
  const [useDetailedIndex, setUseDetailedIndex] = useState(true);
  const [preserveLastNStr, setPreserveLastNStr] = useState("0");
  const [targetTokensStr, setTargetTokensStr] = useState("");
  const [modelKey, setModelKey] = useState(
    refKey(chatflow.compact_model ?? null),
  );
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setInstruction("");
    setMustKeep("");
    setMustDrop("");
    setUseDetailedIndex(true);
    setPreserveLastNStr("0");
    setTargetTokensStr("");
    setModelKey(refKey(chatflow.compact_model ?? null));
    setErrorMessage(null);
    void (async () => {
      try {
        const list = await api.listProviders();
        setProviders(list);
      } catch {
        // ignore — model picker falls back to "inherit".
      }
    })();
  }, [open, chatflow]);

  const range = useMemo(
    () => (open ? derivePackedRange(chatflow, startId, endId) : null),
    [open, chatflow, startId, endId],
  );

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

  const rangeValid = range !== null && range.length >= 2;

  const handleSubmit = async () => {
    if (submitting) return;
    if (!rangeValid) {
      setErrorMessage(t("pack_dialog.error_range_invalid"));
      return;
    }
    setSubmitting(true);
    setErrorMessage(null);
    try {
      const nTrim = preserveLastNStr.trim();
      const nNum = nTrim === "" ? 0 : Number(nTrim);
      const preserveLastN =
        Number.isFinite(nNum) && Number.isInteger(nNum) && nNum >= 0
          ? nNum
          : 0;
      const targetTokens = parseTokensKM(targetTokensStr);
      await commitPackTo(endId, {
        use_detailed_index: useDetailedIndex,
        preserve_last_n: preserveLastN,
        pack_instruction: instruction.trim(),
        must_keep: mustKeep.trim(),
        must_drop: mustDrop.trim(),
        target_tokens: targetTokens,
        model: parseRefKey(modelKey),
      });
      onClose();
    } catch (e) {
      setErrorMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const handleCancel = () => {
    cancelPendingPack();
    onClose();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30"
      data-testid="pack-dialog"
      onClick={handleCancel}
    >
      <div
        className="flex w-[560px] max-h-[90vh] flex-col rounded-xl border border-gray-200 bg-white shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-gray-800">
            {t("pack_dialog.title")}
          </h2>
          <button
            type="button"
            onClick={handleCancel}
            className="flex h-6 w-6 items-center justify-center rounded text-gray-400 hover:bg-gray-100 hover:text-gray-600"
          >
            {"\u2715"}
          </button>
        </div>

        <div className="space-y-4 overflow-auto px-5 py-4">
          <p className="text-[11px] text-gray-500">{t("pack_dialog.hint")}</p>

          <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-900">
            <div className="font-semibold">
              {t("pack_dialog.range_label")}{" "}
              {rangeValid ? `(${range!.length})` : ""}
            </div>
            {rangeValid ? (
              <div className="mt-1 font-mono text-[10px] leading-relaxed break-all">
                {range!.map((id, i) => (
                  <span key={id}>
                    {i > 0 && <span className="text-rose-400"> → </span>}
                    {id.slice(-8)}
                  </span>
                ))}
              </div>
            ) : (
              <div
                className="mt-1 text-[10px] text-red-700"
                data-testid="pack-dialog-range-invalid"
              >
                {t("pack_dialog.error_range_invalid")}
              </div>
            )}
          </div>

          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              checked={useDetailedIndex}
              onChange={(e) => setUseDetailedIndex(e.target.checked)}
              data-testid="pack-dialog-use-detailed-index"
              className="mt-0.5"
            />
            <span className="flex-1">
              <span className="block text-[11px] font-medium text-gray-700">
                {t("pack_dialog.use_detailed_index")}
              </span>
              <span className="block text-[10px] text-gray-500">
                {t("pack_dialog.use_detailed_index_hint")}
              </span>
            </span>
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("pack_dialog.instruction")}
            </span>
            <textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
              rows={3}
              data-testid="pack-dialog-instruction"
              placeholder={t("pack_dialog.instruction_placeholder")}
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-rose-400 focus:outline-none"
            />
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("pack_dialog.must_keep")}
            </span>
            <textarea
              value={mustKeep}
              onChange={(e) => setMustKeep(e.target.value)}
              rows={2}
              data-testid="pack-dialog-must-keep"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-rose-400 focus:outline-none"
            />
          </label>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("pack_dialog.must_drop")}
            </span>
            <textarea
              value={mustDrop}
              onChange={(e) => setMustDrop(e.target.value)}
              rows={2}
              data-testid="pack-dialog-must-drop"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-rose-400 focus:outline-none"
            />
          </label>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="text-[11px] font-medium text-gray-500">
                {t("pack_dialog.preserve_last_n")}
              </span>
              <input
                type="number"
                min={0}
                step={1}
                value={preserveLastNStr}
                onChange={(e) => setPreserveLastNStr(e.target.value)}
                data-testid="pack-dialog-preserve-last-n"
                className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-rose-400 focus:outline-none"
              />
              <p className="mt-1 text-[10px] text-gray-400">
                {t("pack_dialog.preserve_last_n_hint")}
              </p>
            </label>

            <label className="block">
              <span className="text-[11px] font-medium text-gray-500">
                {t("pack_dialog.target_tokens")}
              </span>
              <input
                type="text"
                inputMode="text"
                value={targetTokensStr}
                onChange={(e) => setTargetTokensStr(e.target.value)}
                data-testid="pack-dialog-target-tokens"
                placeholder={t("pack_dialog.target_tokens_placeholder")}
                className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-rose-400 focus:outline-none"
              />
              <p className="mt-1 text-[10px] text-gray-400">
                {t("pack_dialog.target_tokens_hint")}
              </p>
            </label>
          </div>

          <label className="block">
            <span className="text-[11px] font-medium text-gray-500">
              {t("pack_dialog.model")}
            </span>
            <select
              value={modelKey}
              onChange={(e) => setModelKey(e.target.value)}
              data-testid="pack-dialog-model"
              className="mt-0.5 w-full rounded border border-gray-300 px-2 py-1.5 text-xs text-gray-700 focus:border-rose-400 focus:outline-none"
            >
              <option value="">{t("pack_dialog.model_inherit")}</option>
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
              data-testid="pack-dialog-error"
              className="rounded border border-red-300 bg-red-50 px-3 py-2 text-[11px] text-red-700"
            >
              {errorMessage}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 border-t border-gray-100 px-5 py-3">
          <button
            type="button"
            onClick={handleCancel}
            disabled={submitting}
            className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50"
          >
            {t("providers.cancel")}
          </button>
          <button
            type="button"
            onClick={() => void handleSubmit()}
            disabled={submitting || !rangeValid}
            data-testid="pack-dialog-submit"
            className="rounded bg-rose-500 px-4 py-1.5 text-xs text-white hover:bg-rose-600 disabled:opacity-50"
          >
            {submitting ? "..." : t("pack_dialog.submit")}
          </button>
        </div>
      </div>
    </div>
  );
}
