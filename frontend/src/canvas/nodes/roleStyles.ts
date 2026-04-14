/**
 * Role-based color palette for WorkNode cards.
 *
 * Orthogonal to ``step_kind``: ``role`` is the node's structural
 * position in the recursive planner model (§3.4.4 / ADR-024), and
 * this module turns that into a visual identity the user can read
 * at a glance.
 *
 * Palette intent:
 *   pre_judge / post_judge   — cool slate (framing / exit gates)
 *   planner / planner_judge  — warm amber (planning side)
 *   worker  / worker_judge   — emerald (execution side)
 *
 * Within each pair the ``_judge`` flavor shares the hue of its non-judge
 * counterpart but uses a stronger border (``border-2`` + darker shade)
 * and a slightly deeper fill so it reads as "related, but evaluative".
 *
 * ``role === null`` nodes (direct-mode / legacy rows) get ``null`` back
 * and the caller falls through to the original step_kind accent so the
 * legacy look is preserved exactly.
 */

import type { WorkNodeRole } from "@/types/schema";

export interface RoleStyle {
  /** Tailwind classes for the card container (border + background). */
  container: string;
  /** Tailwind classes for the role badge (background + text). */
  badge: string;
}

const ROLE_STYLES: Record<WorkNodeRole, RoleStyle> = {
  pre_judge: {
    container: "border-2 border-slate-400 bg-slate-100",
    badge: "bg-slate-200 text-slate-800",
  },
  planner: {
    container: "border border-amber-300 bg-amber-50",
    badge: "bg-amber-200 text-amber-900",
  },
  planner_judge: {
    container: "border-2 border-amber-500 bg-amber-100",
    badge: "bg-amber-300 text-amber-950",
  },
  worker: {
    container: "border border-emerald-300 bg-emerald-50",
    badge: "bg-emerald-200 text-emerald-900",
  },
  worker_judge: {
    container: "border-2 border-emerald-600 bg-emerald-100",
    badge: "bg-emerald-300 text-emerald-950",
  },
  post_judge: {
    container: "border-2 border-slate-500 bg-slate-200",
    badge: "bg-slate-300 text-slate-900",
  },
};

/**
 * Return the visual style for a WorkNode role, or ``null`` for legacy
 * direct-mode nodes (so the caller can fall back to the step_kind accent).
 */
export function getRoleStyle(role: WorkNodeRole | null | undefined): RoleStyle | null {
  if (role == null) return null;
  return ROLE_STYLES[role] ?? null;
}
