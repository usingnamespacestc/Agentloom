/**
 * SVG overlay that draws the model-family "ribbons" on top of the
 * ChatFlow canvas when the user hovers a parent→child edge.
 *
 * Rules:
 *   - Hover-only. Nothing renders unless an edge is hovered.
 *     ``hoveredEdge`` carries the full {parent, child} pair because
 *     merge nodes have multiple incoming edges.
 *   - One colored ribbon per ModelKind (today: just `llm`; adding
 *     `tool_call` / judge_* later is a data-shape change in
 *     ``effectiveModel.ts`` — this layer doesn't care).
 *   - Each segment goes from the parent's *right* side to the child's
 *     *left* side, mirroring the actual DAG edge endpoints (the React
 *     Flow handles). When a node has both incoming and outgoing edges
 *     in the same family, an extra horizontal segment runs through
 *     the card from left to right so the ribbon visually "贯穿" /
 *     penetrates the card instead of breaking at every node.
 *   - Ribbons float *above* node cards (z-index 10). Momentary
 *     occlusion of card content is acceptable since they only appear
 *     during hover.
 */

import { useMemo } from "react";
import { useStore, type ReactFlowState } from "@xyflow/react";

import { ribbonFamilies, type RibbonFamily } from "./effectiveModel";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlow, NodeId } from "@/types/schema";

/** Fallback card size when React Flow hasn't measured a node yet. */
const CARD_FALLBACK_W = 208; // matches `w-52` on ChatFlowNodeCard
const CARD_FALLBACK_H = 140;

interface NodeBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

const rfNodesSelector = (s: ReactFlowState) => s.nodes;
const rfTransformSelector = (s: ReactFlowState) => s.transform;

export function ModelRibbonLayer({ chatflow }: { chatflow: ChatFlow }) {
  const hoveredEdge = useChatFlowStore((s) => s.hoveredEdge);
  const rfNodes = useStore(rfNodesSelector);
  const transform = useStore(rfTransformSelector);

  const boxes = useMemo(() => {
    const m = new Map<NodeId, NodeBox>();
    for (const n of rfNodes) {
      m.set(n.id, {
        x: n.position.x,
        y: n.position.y,
        w: n.width ?? n.measured?.width ?? CARD_FALLBACK_W,
        h: n.height ?? n.measured?.height ?? CARD_FALLBACK_H,
      });
    }
    return m;
  }, [rfNodes]);

  const families = useMemo<RibbonFamily[]>(() => {
    if (!hoveredEdge) return [];
    const { parent, child } = hoveredEdge;
    if (!chatflow.nodes[parent] || !chatflow.nodes[child]) return [];
    return ribbonFamilies(chatflow, parent, child);
  }, [chatflow, hoveredEdge]);

  if (!hoveredEdge || families.length === 0) return null;

  const [tx, ty, tz] = transform;

  return (
    <svg
      data-testid="model-ribbon-layer"
      className="pointer-events-none absolute inset-0 h-full w-full"
      style={{ zIndex: 10, overflow: "visible" }}
    >
      <g transform={`translate(${tx}, ${ty}) scale(${tz})`}>
        {families.map((family, idx) => (
          <FamilyRibbon
            key={family.kind}
            family={family}
            boxes={boxes}
            stackIndex={idx}
            stackTotal={families.length}
          />
        ))}
      </g>
    </svg>
  );
}

function FamilyRibbon({
  family,
  boxes,
  stackIndex,
  stackTotal,
}: {
  family: RibbonFamily;
  boxes: Map<NodeId, NodeBox>;
  stackIndex: number;
  stackTotal: number;
}) {
  // When multiple kinds coexist, nudge each channel's path up/down by
  // a few px so lines don't perfectly overlap. Centered stack.
  const yNudge = 8 * (stackIndex - (stackTotal - 1) / 2);

  const paths: string[] = [];

  // Inter-node segments: parent's right edge → child's left edge.
  // Side-to-side endpoints (not centers) match the React Flow handles
  // so the ribbon visually replaces / overlays the actual DAG edge.
  for (const [parentId, childId] of family.edges) {
    const a = boxes.get(parentId);
    const b = boxes.get(childId);
    if (!a || !b) continue;
    const p1 = { x: a.x + a.w, y: a.y + a.h / 2 + yNudge };
    const p2 = { x: b.x, y: b.y + b.h / 2 + yNudge };
    paths.push(sidewaysArc(p1, p2));
  }

  // Pass-through segments: when a node has BOTH incoming and outgoing
  // edges in this family, draw a straight line through it from left to
  // right so the ribbon visually "贯穿" (penetrates) the card. Without
  // this the ribbon would terminate at each card's edge and visually
  // break at every node — losing the "this whole chain ran on the same
  // model" affordance.
  const incoming = new Set<NodeId>();
  const outgoing = new Set<NodeId>();
  for (const [p, c] of family.edges) {
    outgoing.add(p);
    incoming.add(c);
  }
  for (const nid of family.nodeIds) {
    if (!incoming.has(nid) || !outgoing.has(nid)) continue;
    const box = boxes.get(nid);
    if (!box) continue;
    const y = box.y + box.h / 2 + yNudge;
    paths.push(`M ${box.x} ${y} L ${box.x + box.w} ${y}`);
  }

  return (
    <g
      stroke={family.color}
      strokeLinecap="round"
      strokeLinejoin="round"
      fill="none"
    >
      {paths.map((d, i) => (
        <path key={i} d={d} strokeWidth={4} strokeOpacity={0.9} />
      ))}
    </g>
  );
}

/**
 * Cubic Bezier from a node's right side to the next node's left side
 * with horizontal control-point tangents — same shape as React Flow's
 * default bezier edge. When source and target share a y-coordinate the
 * curve degenerates to a straight horizontal line, which is exactly
 * what we want now that endpoints sit on the card sides (no need for
 * the old "always slightly bowed" lift, which only existed when the
 * ribbon went center-to-center and would otherwise overlap cards).
 */
function sidewaysArc(
  from: { x: number; y: number },
  to: { x: number; y: number },
): string {
  const dx = to.x - from.x;
  const cp1 = { x: from.x + dx * 0.5, y: from.y };
  const cp2 = { x: to.x - dx * 0.5, y: to.y };
  return `M ${from.x} ${from.y} C ${cp1.x} ${cp1.y}, ${cp2.x} ${cp2.y}, ${to.x} ${to.y}`;
}
