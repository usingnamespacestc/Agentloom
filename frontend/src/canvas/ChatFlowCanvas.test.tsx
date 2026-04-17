/**
 * ChatFlowCanvas tests.
 *
 * We focus on the pure ``buildGraph`` helper for most assertions
 * (React Flow's DOM rendering is well-tested upstream and requires
 * a full resize observer shim that happy-dom doesn't provide).
 * A single smoke-render confirms the empty state still renders a
 * localized placeholder.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";

import { ChatFlowCanvas, buildGraph } from "./ChatFlowCanvas";
import type { ChatFlow } from "@/types/schema";

function seed(): ChatFlow {
  return {
    id: "c1",
    title: null,
    description: null,
    tags: [],
    default_model: null,
    default_judge_model: null,
    default_tool_call_model: null,
    default_execution_mode: 'direct',
    judge_retry_budget: 3,
    min_ground_ratio: null,
    ground_ratio_grace_nodes: 20,
      disabled_tool_names: [],
    root_ids: ["a"],
    created_at: "2026-04-10T00:00:00Z",
    nodes: {
      a: {
        id: "a",
        parent_ids: [],
        description: { text: "", provenance: "unset", updated_at: "2026-04-10T00:00:00Z" },
        inputs: null,
        expected_outcome: null,
        status: "succeeded",
        resolved_model: null,
        locked: false,
        error: null,
        position_x: null,
        position_y: null,
        created_at: "2026-04-10T00:00:00Z",
        updated_at: "2026-04-10T00:00:00Z",
        started_at: null,
        finished_at: null,
        user_message: { text: "hi", provenance: "pure_user", updated_at: "2026-04-10T00:00:00Z" },
        agent_response: { text: "hey", provenance: "pure_agent", updated_at: "2026-04-10T00:00:00Z" },
        workflow: { id: "wf", root_ids: [], nodes: {} },
        pending_queue: [],
      },
      b: {
        id: "b",
        parent_ids: ["a"],
        description: { text: "", provenance: "unset", updated_at: "2026-04-10T00:00:01Z" },
        inputs: null,
        expected_outcome: null,
        status: "planned",
        resolved_model: null,
        locked: false,
        error: null,
        position_x: null,
        position_y: null,
        created_at: "2026-04-10T00:00:01Z",
        updated_at: "2026-04-10T00:00:01Z",
        started_at: null,
        finished_at: null,
        user_message: { text: "more", provenance: "pure_user", updated_at: "2026-04-10T00:00:01Z" },
        agent_response: { text: "", provenance: "unset", updated_at: "2026-04-10T00:00:01Z" },
        workflow: { id: "wf2", root_ids: [], nodes: {} },
        pending_queue: [],
      },
    },
  };
}

describe("buildGraph", () => {
  it("maps chatflow nodes to React Flow nodes", () => {
    const { nodes, edges } = buildGraph(seed(), null);
    expect(nodes.map((n) => n.id).sort()).toEqual(["a", "b"]);
    expect(nodes.every((n) => n.type === "chatflow")).toBe(true);
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe("a");
    expect(edges[0].target).toBe("b");
  });

  it("marks the selected node via data.isSelected", () => {
    const { nodes } = buildGraph(seed(), "b");
    expect(nodes.find((n) => n.id === "b")?.data.isSelected).toBe(true);
    expect(nodes.find((n) => n.id === "a")?.data.isSelected).toBe(false);
  });

  it("returns empty graph when chatflow is null", () => {
    expect(buildGraph(null, null)).toEqual({ nodes: [], edges: [] });
  });

  it("dashes edges touching a planned node", () => {
    const { edges } = buildGraph(seed(), null);
    // b is planned → edge a->b should be dashed.
    expect(edges[0].style?.strokeDasharray).toBe("6 4");
  });

  it("marks root and leaf nodes correctly", () => {
    const { nodes } = buildGraph(seed(), null);
    const nodeA = nodes.find((n) => n.id === "a")!;
    const nodeB = nodes.find((n) => n.id === "b")!;
    expect(nodeA.data.isRoot).toBe(true);
    expect(nodeA.data.isLeaf).toBe(false);
    expect(nodeB.data.isRoot).toBe(false);
    expect(nodeB.data.isLeaf).toBe(true);
  });

  it("marks running nodes as undeletable", () => {
    const cf = seed();
    cf.nodes["b"].status = "running";
    const { nodes } = buildGraph(cf, null);
    // b is running → cannot delete
    expect(nodes.find((n) => n.id === "b")!.data.canDelete).toBe(false);
    // a is ancestor of running → also cannot delete
    expect(nodes.find((n) => n.id === "a")!.data.canDelete).toBe(false);
  });
});

describe("ChatFlowCanvas rendering", () => {
  it("renders the empty-state placeholder when chatflow is null", () => {
    render(<ChatFlowCanvas chatflow={null} />);
    expect(screen.getByTestId("chatflow-canvas-empty")).toBeInTheDocument();
  });

  it("renders the empty-chatflow placeholder when no nodes", () => {
    render(
      <ChatFlowCanvas
        chatflow={{
          id: "empty",
          title: null,
          description: null,
          tags: [],
          default_model: null,
    default_judge_model: null,
    default_tool_call_model: null,
          default_execution_mode: 'direct',
          judge_retry_budget: 3,
    min_ground_ratio: null,
    ground_ratio_grace_nodes: 20,
      disabled_tool_names: [],
          root_ids: [],
          nodes: {},
          created_at: "2026-04-10T00:00:00Z",
        }}
      />,
    );
    expect(screen.getByTestId("chatflow-canvas-empty")).toBeInTheDocument();
  });
});
