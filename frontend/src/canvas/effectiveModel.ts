/**
 * Helpers for the ChatFlow canvas's model-family ribbons.
 *
 * Ribbon semantics (clarified after the §4.10 rework — the model lives
 * on the parent→child edge, not on nodes):
 *
 *   When the user hovers a specific edge, we draw one colored ribbon
 *   per ModelKind. For each kind, the ribbon covers the connected
 *   component of edges (in the line graph: two edges adjacent iff
 *   they share an endpoint) that all carry the *same* model in that
 *   kind as the hovered edge.
 *
 *   So with edges 1→2, 2→3, 2→4, where 1→2 and 2→3 used llm=a but 2→4
 *   used llm=c, hovering 1→2 highlights {1→2, 2→3} in color(a). The
 *   hovered edge is always in its own family by construction (start
 *   of the BFS). Node membership is "any node touched by a family
 *   edge" — node 1 still gets a ribbon endpoint even if its own
 *   spawn model differs, because what matters is the *edge* color.
 *
 *   Multiple kinds (e.g. llm + tool_call) can produce overlapping
 *   ribbons in different colors. Today only `llm` is wired end-to-end
 *   on the ChatNode — `tool_call` will slot in here once edges carry
 *   per-kind resolved models. Adding it is one new entry in
 *   ``MODEL_KINDS`` plus an ``edgeModel`` branch.
 */

import type { ChatFlow, ChatFlowNode, NodeId, ProviderModelRef } from "@/types/schema";

export type ChatFlowNodeMap = Record<NodeId, ChatFlowNode>;

/** Reference equality for ProviderModelRef — null-safe. */
export function modelRefEquals(
  a: ProviderModelRef | null | undefined,
  b: ProviderModelRef | null | undefined,
): boolean {
  if (!a && !b) return true;
  if (!a || !b) return false;
  return a.provider_id === b.provider_id && a.model_id === b.model_id;
}

/**
 * Categories of LLM call an edge can carry. Today only `llm` exists
 * end-to-end on the ChatNode snapshot (`resolved_model`). Future kinds
 * (`tool_call`, judge variants) will hang off the same per-edge
 * snapshot once the backend stores them.
 */
export type ModelKind = "llm";

export const MODEL_KINDS: ModelKind[] = ["llm"];

/**
 * Palette for the hover ribbons. Color is keyed by the *actual model*
 * a node called — not by ModelKind — so switching from Opus to Sonnet
 * mid-chain shows up as two differently-colored ribbon segments. We
 * hash the `provider_id::model_id` string into a fixed palette so the
 * mapping is stable across renders and across nodes.
 *
 * Curated 16-color set (Tableau-20 derived). Picked over Tailwind 500
 * for: lower saturation (less neon, sits next to muted UI nicely),
 * better perceived-brightness consistency (every color reads as a
 * "line" not a "highlight"), and wider hue separation so even with
 * many models the chance of two adjacent-looking colors is low.
 */
const MODEL_PALETTE = [
  "#4e79a7", // steel blue
  "#f28e2b", // pumpkin
  "#59a14f", // leaf green
  "#e15759", // coral red
  "#b07aa1", // dusty mauve
  "#76b7b2", // sage teal
  "#edc948", // mustard
  "#ff9da7", // salmon
  "#9c755f", // umber
  "#bab0ac", // warm gray
  "#5b8ff9", // azure
  "#d4a017", // dark gold
  "#5d6ab3", // periwinkle
  "#c14b89", // raspberry
  "#43a290", // jade
  "#a05195", // plum
];

const DEFAULT_MODEL_COLOR = "#9ca3af"; // gray — "no model resolved yet"

/**
 * Map a model ref to a color. Stable across sessions (pure hash, no
 * registry / no allocation order).
 *
 * Strategy: hash → palette slot for the *base* color, then use
 * independent slices of the same hash to apply a small hue + lightness
 * jitter. Two models that hash into the same palette slot then look
 * like distinct shades of the slot color rather than the exact same
 * color — graceful degradation when distinct-model count exceeds
 * ``MODEL_PALETTE.length``. With 16 palette slots × 25 hue offsets ×
 * 13 lightness offsets the effective collision-free space is ~5k
 * distinct colors, well past any realistic per-chatflow model count.
 */
export function colorForModel(ref: ProviderModelRef | null): string {
  if (!ref) return DEFAULT_MODEL_COLOR;
  const key = `${ref.provider_id}::${ref.model_id}`;
  let h = 0;
  for (let i = 0; i < key.length; i++) {
    h = (h * 31 + key.charCodeAt(i)) | 0;
  }
  const abs = Math.abs(h);
  const base = MODEL_PALETTE[abs % MODEL_PALETTE.length];
  // Independent bit slices → jitters that vary independently of the
  // palette pick. Centered on zero so palette colors remain the
  // canonical look.
  const dHue = ((abs >>> 12) % 25) - 12; // -12 .. +12 degrees
  const dL = ((abs >>> 20) % 13) - 6;    // -6 .. +6 percentage points
  if (dHue === 0 && dL === 0) return base;
  return shiftHsl(base, dHue, dL);
}

/**
 * Apply a hue rotation and lightness shift to a `#rrggbb` color and
 * emit an `hsl(...)` string (SVG accepts both formats). Stays in the
 * 15–75% lightness band so the result is always readable as a stroke
 * on white.
 */
function shiftHsl(hex: string, dHueDeg: number, dLPct: number): string {
  const r = parseInt(hex.slice(1, 3), 16) / 255;
  const g = parseInt(hex.slice(3, 5), 16) / 255;
  const b = parseInt(hex.slice(5, 7), 16) / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const l = (max + min) / 2;
  let hue = 0;
  let sat = 0;
  if (max !== min) {
    const d = max - min;
    sat = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === r) hue = ((g - b) / d + (g < b ? 6 : 0)) / 6;
    else if (max === g) hue = ((b - r) / d + 2) / 6;
    else hue = ((r - g) / d + 4) / 6;
  }
  const newHue = ((hue * 360 + dHueDeg) % 360 + 360) % 360;
  const newL = Math.max(0.15, Math.min(0.75, l + dLPct / 100));
  return `hsl(${newHue.toFixed(1)}, ${(sat * 100).toFixed(1)}%, ${(newL * 100).toFixed(1)}%)`;
}

/**
 * Per-kind model carried on the edge `parent → child`. Keyed off the
 * child since Agentloom has no first-class edge objects; the spawn-time
 * model snapshot lives on the spawned node.
 *
 * `parentId` is part of the signature for symmetry with future per-edge
 * kinds (e.g. when a merge node's incoming edges carry different models)
 * even though today only the child matters.
 */
export function edgeModel(
  chatflow: ChatFlow,
  _parentId: NodeId,
  childId: NodeId,
  kind: ModelKind,
): ProviderModelRef | null {
  const child = chatflow.nodes[childId];
  if (!child) return null;
  switch (kind) {
    case "llm":
      return child.resolved_model ?? chatflow.default_model;
  }
}

function modelKey(ref: ProviderModelRef | null): string {
  return ref ? `${ref.provider_id}::${ref.model_id}` : "__default__";
}

function edgeKey(p: NodeId, c: NodeId): string {
  return `${p}\u0000${c}`;
}

export interface RibbonFamily {
  kind: ModelKind;
  /** The model this family is grouped by — drives the ribbon color. */
  modelRef: ProviderModelRef | null;
  /** `colorForModel(modelRef)` — hashed from provider_id::model_id so
   * different models get visually distinct ribbons. */
  color: string;
  /** Every node touched by a family edge (just for diagnostics — the
   * SVG renders edges, not nodes). */
  nodeIds: Set<NodeId>;
  /** parent→child pairs in the family; one ribbon arc per pair. */
  edges: Array<[NodeId, NodeId]>;
}

/**
 * Compute one RibbonFamily per ModelKind, starting from the hovered
 * edge `(hoveredParentId → hoveredChildId)`.
 *
 * Family construction is a BFS over the line graph of edges: two edges
 * are adjacent iff they share a node. We only follow adjacencies whose
 * per-kind model matches the hovered edge's. The hovered edge is the
 * BFS seed, so it is *always* in its own family (was a real bug in the
 * previous node-based BFS — the hovered edge could go un-highlighted
 * if its endpoints' resolved_models disagreed).
 */
export function ribbonFamilies(
  chatflow: ChatFlow,
  hoveredParentId: NodeId,
  hoveredChildId: NodeId,
): RibbonFamily[] {
  const allEdges: Array<[NodeId, NodeId]> = [];
  const edgesByNode = new Map<NodeId, Array<[NodeId, NodeId]>>();
  for (const n of Object.values(chatflow.nodes)) {
    for (const pid of n.parent_ids) {
      const e: [NodeId, NodeId] = [pid, n.id];
      allEdges.push(e);
      for (const endpoint of e) {
        const arr = edgesByNode.get(endpoint);
        if (arr) arr.push(e);
        else edgesByNode.set(endpoint, [e]);
      }
    }
  }

  const families: RibbonFamily[] = [];
  for (const kind of MODEL_KINDS) {
    const startModel = edgeModel(chatflow, hoveredParentId, hoveredChildId, kind);
    const startKey = modelKey(startModel);
    const visited = new Set<string>();
    const familyEdges: Array<[NodeId, NodeId]> = [];
    const queue: Array<[NodeId, NodeId]> = [[hoveredParentId, hoveredChildId]];
    while (queue.length > 0) {
      const [p, c] = queue.shift()!;
      const k = edgeKey(p, c);
      if (visited.has(k)) continue;
      if (modelKey(edgeModel(chatflow, p, c, kind)) !== startKey) continue;
      visited.add(k);
      familyEdges.push([p, c]);
      for (const adj of edgesByNode.get(p) ?? []) queue.push(adj);
      for (const adj of edgesByNode.get(c) ?? []) queue.push(adj);
    }
    const nodeIds = new Set<NodeId>();
    for (const [p, c] of familyEdges) {
      nodeIds.add(p);
      nodeIds.add(c);
    }
    families.push({
      kind,
      modelRef: startModel,
      color: colorForModel(startModel),
      nodeIds,
      edges: familyEdges,
    });
  }
  return families;
}
