/**
 * Small colored pill showing a node's status in the user's language.
 *
 * Colors are semantic, not theme tokens, because they have to mean
 * the same thing to colorblind users no matter what mode the app is
 * in — green for succeeded, red for failed, yellow for running/waiting,
 * gray for dashed. Tailwind classes are used so we get hover/dark
 * variants for free when M9 adds a theme switcher.
 */

import { useTranslation } from "react-i18next";

import type { NodeStatus } from "@/types/schema";

const STATUS_CLASSES: Record<NodeStatus, string> = {
  planned: "bg-gray-200 text-gray-700 border-gray-300",
  running: "bg-yellow-100 text-yellow-800 border-yellow-300",
  waiting_for_rate_limit: "bg-amber-100 text-amber-800 border-amber-300",
  waiting_for_user: "bg-amber-200 text-amber-900 border-amber-400",
  succeeded: "bg-green-100 text-green-800 border-green-300",
  failed: "bg-red-100 text-red-800 border-red-300",
  retrying: "bg-orange-100 text-orange-800 border-orange-300",
  cancelled: "bg-zinc-200 text-zinc-600 border-zinc-300",
};

export function StatusBadge({ status }: { status: NodeStatus }) {
  const { t } = useTranslation();
  const cls = STATUS_CLASSES[status] ?? STATUS_CLASSES.planned;
  return (
    <span
      data-testid={`status-badge-${status}`}
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${cls}`}
    >
      {t(`node.status.${status}`)}
    </span>
  );
}
