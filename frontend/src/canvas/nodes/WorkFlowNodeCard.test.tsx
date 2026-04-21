/**
 * WorkFlowNodeCard rendering tests.
 *
 * Scope is narrow on purpose: we verify that each ``role`` value
 * paints a distinguishable card (visible role badge + a role-derived
 * class on the container) and that legacy direct-mode nodes
 * (``role === null``) fall back to the original step_kind accent
 * without a role badge.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { ReactFlowProvider } from "@xyflow/react";

import { WorkFlowNodeCard, type WorkFlowNodeData } from "./WorkFlowNodeCard";
import type { NodeProps } from "@xyflow/react";
import type { WorkFlowNode, WorkNodeRole } from "@/types/schema";
import { WORK_NODE_ROLES } from "@/types/schema";

function buildWorkNode(overrides: Partial<WorkFlowNode> = {}): WorkFlowNode {
  const iso = "2026-04-13T00:00:00Z";
  return {
    id: overrides.id ?? "wn-1",
    parent_ids: [],
    description: { text: "", provenance: "unset", updated_at: iso },
    inputs: null,
    expected_outcome: null,
    status: "succeeded",
    resolved_model: null,
    locked: false,
    error: null,
    position_x: null,
    position_y: null,
    created_at: iso,
    updated_at: iso,
    started_at: null,
    finished_at: null,
    step_kind: "draft",
    role: null,
    tool_constraints: null,
    model_override: null,
    output_message: null,
    usage: null,
    ...overrides,
  } as WorkFlowNode;
}

function renderCard(node: WorkFlowNode) {
  const data: WorkFlowNodeData = {
    node,
    isSelected: false,
    isRoot: true,
    isLeaf: true,
    maxContextTokens: null,
  };
  // WorkFlowNodeCard uses @xyflow/react's <Handle>, which needs a
  // ReactFlowProvider somewhere in the tree to avoid a store error.
  return render(
    <ReactFlowProvider>
      <WorkFlowNodeCard {...({ data } as unknown as NodeProps)} />
    </ReactFlowProvider>,
  );
}

describe("WorkFlowNodeCard role styling", () => {
  it("renders a role badge for every non-null role", () => {
    for (const role of WORK_NODE_ROLES) {
      const { unmount } = renderCard(
        buildWorkNode({ id: `n-${role}`, role: role as WorkNodeRole }),
      );
      const badge = screen.getByTestId(`role-badge-${role}`);
      expect(badge).toBeInTheDocument();
      // Each role's container element carries the data-role attribute
      // so downstream assertions / visual inspection can confirm the
      // paint path without depending on Tailwind class strings.
      const card = screen.getByTestId(`workflow-node-n-${role}`);
      expect(card.getAttribute("data-role")).toBe(role);
      unmount();
    }
  });

  it("paints distinct container classes per role", () => {
    const seen = new Set<string>();
    for (const role of WORK_NODE_ROLES) {
      const { unmount } = renderCard(
        buildWorkNode({ id: `n-${role}`, role: role as WorkNodeRole }),
      );
      const card = screen.getByTestId(`workflow-node-n-${role}`);
      seen.add(card.className);
      unmount();
    }
    // All six role flavors must be visually distinguishable.
    expect(seen.size).toBe(WORK_NODE_ROLES.length);
  });

  it("leaves legacy (role === null) nodes with the step_kind accent and no role badge", () => {
    renderCard(buildWorkNode({ id: "legacy", role: null, step_kind: "draft" }));
    const card = screen.getByTestId("workflow-node-legacy");
    expect(card.getAttribute("data-role")).toBe("none");
    // Legacy llm_call still gets the sky accent it had before M12.1.
    expect(card.className).toContain("border-sky-300");
    expect(card.className).toContain("bg-sky-50");
    // No role badge is rendered for null-role nodes.
    expect(screen.queryByTestId(/^role-badge-/)).toBeNull();
  });
});

