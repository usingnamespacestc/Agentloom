/**
 * Tiny DAG layout — enough for M8 read-only rendering.
 *
 * Each node is assigned to a "level" equal to ``1 + max(parent levels)``
 * (roots are level 0). Within a level, nodes are ordered by their
 * creation timestamp, then by id as a stable tiebreaker.
 *
 * In horizontal mode (the default as of M8.5) levels run left→right
 * and siblings within a level stack top→bottom. In vertical mode
 * levels run top→bottom and siblings spread left→right. M8 used the
 * vertical mode; we flipped it because ChatFlow naturally reads as a
 * left-to-right conversation stream (n8n / LangGraph style).
 *
 * Why not Dagre? Dagre is great but adds ~30 KB and a runtime for a
 * problem we don't really have until M9's interactive edits.
 *
 * Coordinates are returned in React Flow's pixel space. The canvas
 * component adds viewport padding via ``fitView``.
 */

import type { NodeBaseFields, NodeId } from "@/types/schema";

export type LayoutDirection = "horizontal" | "vertical";

export interface LaidOutNode<T extends NodeBaseFields> {
  node: T;
  level: number;
  position: { x: number; y: number };
}

export interface LayoutOptions {
  /** Spacing between levels (horizontal px in horizontal mode, vertical px in vertical mode). */
  columnWidth?: number;
  /** Spacing between siblings within a level. */
  rowHeight?: number;
  offsetX?: number;
  offsetY?: number;
  direction?: LayoutDirection;
}

/** Approximate card dimensions (w-52 = 208px, typical height ~160px). */
export const NODE_WIDTH = 208;
export const NODE_HEIGHT = 160;

const DEFAULTS: Required<LayoutOptions> = {
  columnWidth: NODE_WIDTH + 100,
  rowHeight: NODE_HEIGHT + 50,
  offsetX: 40,
  offsetY: 40,
  direction: "horizontal",
};

/**
 * Compute positions for a DAG of nodes.
 *
 * Caller supplies a ``nodes`` map and a list of ``root_ids``. The
 * function is generic so it works for both ChatFlow and WorkFlow.
 * Unknown parent ids are treated as missing (node lifts to level 0).
 */
export function layoutDag<T extends NodeBaseFields>(
  nodes: Record<NodeId, T>,
  rootIds: NodeId[],
  options: LayoutOptions = {},
): LaidOutNode<T>[] {
  const opts = { ...DEFAULTS, ...options };

  // Topological order via Kahn's algorithm so parents are always
  // visited before children and we can assign levels in one pass.
  const incoming = new Map<NodeId, Set<NodeId>>();
  for (const [id, node] of Object.entries(nodes)) {
    const parents = new Set<NodeId>();
    for (const p of node.parent_ids) {
      if (p in nodes) parents.add(p);
    }
    incoming.set(id, parents);
  }

  const ready: NodeId[] = [];
  for (const [id, deps] of incoming.entries()) {
    if (deps.size === 0) ready.push(id);
  }
  // Stable ordering at roots: prefer the order given by ``rootIds``
  // and only fall back to insertion order for stray roots.
  ready.sort((a, b) => {
    const ai = rootIds.indexOf(a);
    const bi = rootIds.indexOf(b);
    if (ai !== -1 && bi !== -1) return ai - bi;
    if (ai !== -1) return -1;
    if (bi !== -1) return 1;
    return compareForStability(nodes[a], nodes[b]);
  });

  const levels = new Map<NodeId, number>();
  const visited = new Set<NodeId>();
  const queue = [...ready];
  while (queue.length > 0) {
    const id = queue.shift()!;
    if (visited.has(id)) continue;
    visited.add(id);
    const node = nodes[id];
    let lvl = 0;
    for (const p of node.parent_ids) {
      if (levels.has(p)) {
        lvl = Math.max(lvl, (levels.get(p) ?? 0) + 1);
      }
    }
    levels.set(id, lvl);
    // Any child that now has all its parents visited joins the queue.
    for (const [otherId, deps] of incoming.entries()) {
      if (deps.has(id)) {
        deps.delete(id);
        if (deps.size === 0 && !visited.has(otherId)) {
          queue.push(otherId);
        }
      }
    }
  }

  // Any node that wasn't reached (because of cycles or stale parent
  // refs) is dropped to level 0 — we don't want the UI to crash.
  for (const id of Object.keys(nodes)) {
    if (!levels.has(id)) levels.set(id, 0);
  }

  // Build a children map (parent → sorted children list).
  const children = new Map<NodeId, NodeId[]>();
  for (const [id, node] of Object.entries(nodes)) {
    for (const p of node.parent_ids) {
      if (p in nodes) {
        const list = children.get(p) ?? [];
        list.push(id);
        children.set(p, list);
      }
    }
  }
  for (const list of children.values()) {
    list.sort((a, b) => compareForStability(nodes[a], nodes[b]));
  }

  // Compute the "span" of each node's subtree (in row-height units).
  // A leaf has span 1. A node with children has span = sum of children
  // spans. This ensures sibling subtrees never overlap vertically.
  // For merge nodes (multiple parents), only the *first* parent "owns"
  // the child for spacing purposes — other parents just draw an edge.
  const primaryParent = new Map<NodeId, NodeId | null>();
  for (const [id, node] of Object.entries(nodes)) {
    const parents = node.parent_ids.filter((p) => p in nodes);
    primaryParent.set(id, parents.length > 0 ? parents[0] : null);
  }

  const spanCache = new Map<NodeId, number>();
  function subtreeSpan(id: NodeId): number {
    if (spanCache.has(id)) return spanCache.get(id)!;
    const kids = (children.get(id) ?? []).filter((c) => primaryParent.get(c) === id);
    if (kids.length === 0) {
      spanCache.set(id, 1);
      return 1;
    }
    let total = 0;
    for (const kid of kids) {
      total += subtreeSpan(kid);
    }
    spanCache.set(id, total);
    return total;
  }

  // Identify roots and sort them by creation time (stable).
  const roots: NodeId[] = [];
  for (const id of Object.keys(nodes)) {
    if (primaryParent.get(id) === null) roots.push(id);
  }
  roots.sort((a, b) => compareForStability(nodes[a], nodes[b]));

  // Assign vertical positions top-down: each node is centered within
  // its allocated vertical band.
  const positions = new Map<NodeId, { x: number; y: number }>();

  function placeSubtree(id: NodeId, lvl: number, topSlot: number): void {
    const span = subtreeSpan(id);
    const centerY = topSlot + span / 2 - 0.5;
    const pos =
      opts.direction === "horizontal"
        ? { x: opts.offsetX + lvl * opts.columnWidth, y: opts.offsetY + centerY * opts.rowHeight }
        : { x: opts.offsetX + centerY * opts.columnWidth, y: opts.offsetY + lvl * opts.rowHeight };
    positions.set(id, pos);

    const kids = (children.get(id) ?? []).filter((c) => primaryParent.get(c) === id);
    let cursor = topSlot;
    for (const kid of kids) {
      const kidLvl = levels.get(kid) ?? lvl + 1;
      placeSubtree(kid, kidLvl, cursor);
      cursor += subtreeSpan(kid);
    }
  }

  let rootCursor = 0;
  for (const rid of roots) {
    const lvl = levels.get(rid) ?? 0;
    placeSubtree(rid, lvl, rootCursor);
    rootCursor += subtreeSpan(rid);
  }

  // Place merge nodes that weren't placed by their primary parent
  // (shouldn't happen, but guard against it).
  for (const id of Object.keys(nodes)) {
    if (!positions.has(id)) {
      const lvl = levels.get(id) ?? 0;
      const pos =
        opts.direction === "horizontal"
          ? { x: opts.offsetX + lvl * opts.columnWidth, y: opts.offsetY + rootCursor * opts.rowHeight }
          : { x: opts.offsetX + rootCursor * opts.columnWidth, y: opts.offsetY + lvl * opts.rowHeight };
      positions.set(id, pos);
      rootCursor += 1;
    }
  }

  const laidOut: LaidOutNode<T>[] = [];
  for (const id of Object.keys(nodes)) {
    laidOut.push({
      node: nodes[id],
      level: levels.get(id) ?? 0,
      position: positions.get(id)!,
    });
  }
  // Sort by level, then by cross-axis position (y in horizontal, x in
  // vertical) so callers iterating the result see nodes in visual order.
  laidOut.sort((a, b) => {
    if (a.level !== b.level) return a.level - b.level;
    if (opts.direction === "horizontal") return a.position.y - b.position.y;
    return a.position.x - b.position.x;
  });
  return laidOut;
}

function compareForStability(a: NodeBaseFields, b: NodeBaseFields): number {
  // created_at is an ISO8601 string; lexicographic compare is correct.
  if (a.created_at !== b.created_at) {
    return a.created_at < b.created_at ? -1 : 1;
  }
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}
