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
import { describe, it, expect, afterEach } from "vitest";
import { ReactFlowProvider } from "@xyflow/react";

import { WorkFlowNodeCard, type WorkFlowNodeData } from "./WorkFlowNodeCard";
import type { NodeProps } from "@xyflow/react";
import type { BoardItem, WorkFlowNode, WorkNodeRole } from "@/types/schema";
import { WORK_NODE_ROLES } from "@/types/schema";
import { useChatFlowStore } from "@/store/chatflowStore";

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
    step_kind: "llm_call",
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
    renderCard(buildWorkNode({ id: "legacy", role: null, step_kind: "llm_call" }));
    const card = screen.getByTestId("workflow-node-legacy");
    expect(card.getAttribute("data-role")).toBe("none");
    // Legacy llm_call still gets the sky accent it had before M12.1.
    expect(card.className).toContain("border-sky-300");
    expect(card.className).toContain("bg-sky-50");
    // No role badge is rendered for null-role nodes.
    expect(screen.queryByTestId(/^role-badge-/)).toBeNull();
  });
});

describe("WorkFlowNodeCard — MemoryBoard bubble", () => {
  afterEach(() => {
    // Wipe the store between tests so a seeded BoardItem doesn't leak
    // into the next render.
    useChatFlowStore.setState({ boardItems: {} });
  });

  function seedBoardItem(item: BoardItem) {
    useChatFlowStore.setState({ boardItems: { [item.source_node_id]: item } });
  }

  it("renders a node-brief bubble above a WorkNode when a matching BoardItem exists", () => {
    const node = buildWorkNode({
      id: "bubble-host",
      step_kind: "llm_call",
    });
    seedBoardItem({
      id: "bi-1",
      chatflow_id: "cf-1",
      workflow_id: "wf-1",
      source_node_id: "bubble-host",
      source_kind: "llm_call",
      scope: "node",
      description: "Computed the answer in three steps.",
      fallback: false,
      created_at: "2026-04-20T00:00:00Z",
    });
    renderCard(node);
    const bubble = screen.getByTestId("node-brief-bubble");
    expect(bubble).toBeInTheDocument();
    // The truncated text shows the first 80 chars of the description.
    expect(bubble.textContent).toContain("Computed the answer");
    expect(bubble.getAttribute("data-fallback")).toBe("false");
  });

  it("hides the bubble when no BoardItem is seeded for this node", () => {
    renderCard(buildWorkNode({ id: "no-bubble" }));
    expect(screen.queryByTestId("node-brief-bubble")).toBeNull();
  });

  it("does not render a bubble on a brief WorkNode itself (recursion guard)", () => {
    const node = buildWorkNode({
      id: "brief-node",
      step_kind: "brief" as WorkFlowNode["step_kind"],
    });
    seedBoardItem({
      id: "bi-2",
      chatflow_id: "cf-1",
      workflow_id: "wf-1",
      source_node_id: "brief-node",
      source_kind: "brief",
      scope: "node",
      description: "would be a brief-of-brief",
      fallback: false,
      created_at: "2026-04-20T00:00:00Z",
    });
    renderCard(node);
    expect(screen.queryByTestId("node-brief-bubble")).toBeNull();
  });

  it("passes through the fallback flag as a data attribute", () => {
    seedBoardItem({
      id: "bi-3",
      chatflow_id: "cf-1",
      workflow_id: "wf-1",
      source_node_id: "fallback-host",
      source_kind: "tool_call",
      scope: "node",
      description: "tool_call shell: ls /tmp",
      fallback: true,
      created_at: "2026-04-20T00:00:00Z",
    });
    renderCard(buildWorkNode({ id: "fallback-host", step_kind: "tool_call" }));
    expect(
      screen.getByTestId("node-brief-bubble").getAttribute("data-fallback"),
    ).toBe("true");
  });
});
