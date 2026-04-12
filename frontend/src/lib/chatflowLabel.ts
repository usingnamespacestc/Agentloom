/**
 * Single source of truth for the display label of a chatflow —
 * used by both the sidebar list and the top-bar title. When the
 * user hasn't typed a title, we fall back to the creation
 * timestamp (month/day + hour:minute) so multiple unnamed
 * chatflows are still distinguishable in the sidebar.
 */

export interface LabelableChatFlow {
  title: string | null;
  created_at?: string | null;
  id: string;
}

export function chatflowDisplayTitle(cf: LabelableChatFlow): string {
  const t = cf.title?.trim();
  if (t) return t;
  if (cf.created_at) {
    const d = new Date(cf.created_at);
    if (!Number.isNaN(d.getTime())) {
      return d.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    }
  }
  return cf.id.slice(0, 8);
}
