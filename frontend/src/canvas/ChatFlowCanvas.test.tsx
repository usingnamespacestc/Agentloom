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

import { ChatFlowCanvas, buildGraph, CHAT_BRIEF_NODE_PREFIX } from "./ChatFlowCanvas";
import type { BoardItem, ChatFlow } from "@/types/schema";

function seed(): ChatFlow {
  return {
    id: "c1",
    title: null,
    description: null,
    tags: [],
    draft_model: null,
    default_judge_model: null,
    default_tool_call_model: null,
    brief_model: null,
    default_execution_mode: 'native_react',
    judge_retry_budget: 3,
    min_ground_ratio: null,
    ground_ratio_grace_nodes: 20,
      disabled_tool_names: [],
      compact_trigger_pct: 0.7,
      compact_target_pct: 0.5,
      compact_keep_recent_count: 3,
      compact_preserve_mode: "by_count",
      recalled_context_sticky_turns: 3,
      compact_model: null,
      compact_require_confirmation: true,
      chatnode_compact_trigger_pct: 0.6,
      chatnode_compact_target_pct: 0.4,
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
        compact_snapshot: null,
        entry_prompt_tokens: null,
        output_response_tokens: null,
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
        compact_snapshot: null,
        entry_prompt_tokens: null,
        output_response_tokens: null,
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

  it("preserves compact_snapshot on node data for canvas rendering", () => {
    const cf = seed();
    cf.nodes["b"].compact_snapshot = {
      summary: "s",
      preserved_messages: [],
      preserved_before_summary: false,
    };
    const { nodes } = buildGraph(cf, null);
    // The card component reads node.compact_snapshot off data.node.
    expect(nodes.find((n) => n.id === "b")!.data.node.compact_snapshot).not.toBeNull();
  });

  it("emits a synthetic chat-brief node stacked above its source ChatNode", () => {
    const cf = seed();
    const briefItem: BoardItem = {
      id: "bi-1",
      chatflow_id: cf.id,
      workflow_id: null,
      source_node_id: "a",
      source_kind: "chat_turn",
      scope: "chat",
      description: "user said hi; agent replied hey",
      fallback: false,
      created_at: "2026-04-21T00:00:00Z",
    };
    const { nodes, edges } = buildGraph(cf, null, {}, { a: briefItem });
    const briefNode = nodes.find(
      (n) => n.id === `${CHAT_BRIEF_NODE_PREFIX}a`,
    );
    expect(briefNode).toBeDefined();
    expect(briefNode!.type).toBe("chatBrief");
    expect(briefNode!.selectable).toBe(false);
    expect(briefNode!.draggable).toBe(false);
    // Brief is stacked above source — y strictly less.
    const sourceNode = nodes.find((n) => n.id === "a")!;
    expect(briefNode!.position.y).toBeLessThan(sourceNode.position.y);
    expect(briefNode!.position.x).toBe(sourceNode.position.x);
    // Brief is bubble-attached; no connector edge (the inherited-model
    // hover overlay on regular edges was meaningless for briefs).
    expect(edges.some((e) => e.id.startsWith("brief->"))).toBe(false);
  });

  it("skips non-chat scope BoardItems when emitting brief nodes", () => {
    const cf = seed();
    // scope='node' is a WorkBoard row — must not paint a chat-brief
    // even if it's keyed at a ChatNode id.
    const wrongScope: BoardItem = {
      id: "bi-2",
      chatflow_id: cf.id,
      workflow_id: "wf",
      source_node_id: "a",
      source_kind: "draft",
      scope: "node",
      description: "ignored",
      fallback: false,
      created_at: "2026-04-21T00:00:00Z",
    };
    const { nodes, edges } = buildGraph(cf, null, {}, { a: wrongScope });
    expect(
      nodes.find((n) => n.id === `${CHAT_BRIEF_NODE_PREFIX}a`),
    ).toBeUndefined();
    expect(edges.find((e) => e.id === "brief->a")).toBeUndefined();
  });

  it("does not emit brief nodes when boardItems is empty", () => {
    const { nodes, edges } = buildGraph(seed(), null, {}, {});
    expect(
      nodes.some((n) => n.id.startsWith(CHAT_BRIEF_NODE_PREFIX)),
    ).toBe(false);
    expect(edges.some((e) => e.id.startsWith("brief->"))).toBe(false);
  });
});

// ---- Fold projection tests ----
//
// These exercise ``buildGraph``'s fold-aware projection: hidden-set
// computation, edge re-route through the fold host, and the
// ``isFoldHost`` / ``foldedCount`` signals on node data.

function makeTurn(
  id: string,
  parentIds: string[],
  overrides: Partial<ChatFlow["nodes"][string]> = {},
): ChatFlow["nodes"][string] {
  return {
    id,
    parent_ids: parentIds,
    description: { text: "", provenance: "unset", updated_at: "2026-04-24T00:00:00Z" },
    inputs: null,
    expected_outcome: null,
    status: "succeeded",
    resolved_model: null,
    locked: false,
    error: null,
    position_x: null,
    position_y: null,
    created_at: "2026-04-24T00:00:00Z",
    updated_at: "2026-04-24T00:00:00Z",
    started_at: null,
    finished_at: null,
    user_message: { text: id, provenance: "pure_user", updated_at: "2026-04-24T00:00:00Z" },
    agent_response: { text: id, provenance: "pure_agent", updated_at: "2026-04-24T00:00:00Z" },
    workflow: { id: `wf-${id}`, root_ids: [], nodes: {} },
    pending_queue: [],
    compact_snapshot: null,
    pack_snapshot: null,
    entry_prompt_tokens: null,
    output_response_tokens: null,
    ...overrides,
  };
}

function baseChatFlow(
  nodes: Record<string, ChatFlow["nodes"][string]>,
  rootIds: string[] = ["root"],
): ChatFlow {
  return {
    id: "cf-fold",
    title: null,
    description: null,
    tags: [],
    draft_model: null,
    default_judge_model: null,
    default_tool_call_model: null,
    brief_model: null,
    default_execution_mode: "native_react",
    judge_retry_budget: 3,
    min_ground_ratio: null,
    ground_ratio_grace_nodes: 20,
    disabled_tool_names: [],
    compact_trigger_pct: 0.7,
    compact_target_pct: 0.3,
    compact_keep_recent_count: 3,
    compact_preserve_mode: "by_count",
    recalled_context_sticky_turns: 3,
    compact_model: null,
    compact_require_confirmation: true,
    chatnode_compact_trigger_pct: 0.6,
    chatnode_compact_target_pct: 0.4,
    root_ids: rootIds,
    nodes,
    created_at: "2026-04-24T00:00:00Z",
  };
}

describe("buildGraph fold projection", () => {
  it("single pack fold: range hidden, external parent edge re-routes to pack", () => {
    // root → a → b → c → pack (packed_range=[a,b,c])
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      b: makeTurn("b", ["a"]),
      c: makeTurn("c", ["b"]),
      pack: makeTurn("pack", ["c"], {
        pack_snapshot: {
          summary: "summary",
          packed_range: ["a", "b", "c"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["pack"]);
    const { nodes: rn, edges } = buildGraph(cf, null, {}, {}, folded);
    const visibleIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    expect(visibleIds).toEqual(["pack", "root"]);
    // Only edge left: root → pack (re-routed from root → a, since a is hidden)
    expect(edges.map((e) => `${e.source}->${e.target}`).sort()).toEqual([
      "root->pack",
    ]);
    // Pack card carries the isFoldHost + foldedCount signals.
    const packNode = rn.find((n) => n.id === "pack");
    expect(packNode?.data.isFoldHost).toBe(true);
    expect(packNode?.data.foldedCount).toBe(3);
  });

  it("single compact fold: pre-compact ancestors hidden, root edge re-routes", () => {
    // root → x → y → compact   (compact_snapshot marks it a compact host)
    const nodes = {
      root: makeTurn("root", []),
      x: makeTurn("x", ["root"]),
      y: makeTurn("y", ["x"]),
      compact: makeTurn("compact", ["y"], {
        compact_snapshot: {
          summary: "summary",
          preserved_messages: [],
          preserved_before_summary: false,
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["compact"]);
    const { nodes: rn, edges } = buildGraph(cf, null, {}, {}, folded);
    const visibleIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    // root walk stops at root (no merge, reaches parent_ids=[]), so all of
    // {root, x, y} end up in compact's fold range.
    expect(visibleIds).toEqual(["compact"]);
    expect(edges).toHaveLength(0);
    const cn = rn.find((n) => n.id === "compact");
    expect(cn?.data.isFoldHost).toBe(true);
    expect(cn?.data.foldedCount).toBe(3);
  });

  it("fork child survives and re-routes to fold host when its parent is hidden", () => {
    //                a → packA (range=[a])
    // root → a
    //                ↘ sib (fork child of a, not in pack range)
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      packA: makeTurn("packA", ["a"], {
        pack_snapshot: {
          summary: "summary",
          packed_range: ["a"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
      sib: makeTurn("sib", ["a"]),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["packA"]);
    const { nodes: rn, edges } = buildGraph(cf, null, {}, {}, folded);
    const visibleIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    expect(visibleIds).toEqual(["packA", "root", "sib"]);
    const edgeKeys = edges.map((e) => `${e.source}->${e.target}`).sort();
    // root → a becomes root → packA; a → sib becomes packA → sib.
    expect(edgeKeys).toEqual(["packA->sib", "root->packA"]);
  });

  it("nested packs: outer fold absorbs inner range, inner host is hidden", () => {
    // root → m1 → m2 → packB (range=[m1, m2]) → n3 → packA (range=[m2, packB, n3])
    const nodes = {
      root: makeTurn("root", []),
      m1: makeTurn("m1", ["root"]),
      m2: makeTurn("m2", ["m1"]),
      packB: makeTurn("packB", ["m2"], {
        pack_snapshot: {
          summary: "innerB",
          packed_range: ["m1", "m2"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
      n3: makeTurn("n3", ["packB"]),
      packA: makeTurn("packA", ["n3"], {
        pack_snapshot: {
          summary: "outerA",
          packed_range: ["m2", "packB", "n3"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    // Both folded; packA's range is larger so it wins attribution for
    // any overlap (m2). packB itself is inside packA's range → hidden.
    const folded = new Set<string>(["packA", "packB"]);
    const { nodes: rn, edges } = buildGraph(cf, null, {}, {}, folded);
    const visibleIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    // packB is hidden (it's in packA's range). m1 is only in packB's
    // range, but packB is swallowed — so m1 falls through to packB as
    // its host? No: packB is hidden, so it's not an effective fold.
    // After filtering out hidden folds, only packA remains active, and
    // m1 is NOT in packA's range — so m1 stays visible.
    expect(visibleIds).toEqual(["m1", "packA", "root"].sort());
    // Edges: root → m1 unchanged; m1 → m2 becomes m1 → packA (m2 hidden).
    const edgeKeys = edges.map((e) => `${e.source}->${e.target}`).sort();
    expect(edgeKeys).toEqual(["m1->packA", "root->m1"]);
  });

  it("fold does not affect brief node emission for visible sources", () => {
    // Verifies we skip briefs for hidden sources but keep them for visible ones.
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      pack: makeTurn("pack", ["a"], {
        pack_snapshot: {
          summary: "summary",
          packed_range: ["a"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const now = "2026-04-24T00:00:00Z";
    const boardItems: Record<string, BoardItem> = {
      a: {
        id: "bi-a",
        source_node_id: "a",
        scope: "chat",
        source_kind: "chat_turn",
        description: "a brief",
        chatflow_id: cf.id,
        workflow_id: null,
        created_at: now,
        fallback: false,
      },
      pack: {
        id: "bi-pack",
        source_node_id: "pack",
        scope: "chat",
        source_kind: "chat_pack",
        description: "pack brief",
        chatflow_id: cf.id,
        workflow_id: null,
        created_at: now,
        fallback: false,
        pack_inner_ids: ["a"],
      },
    };
    const folded = new Set<string>(["pack"]);
    const { nodes: rn } = buildGraph(cf, null, {}, boardItems, folded);
    const briefIds = rn
      .filter((n) => n.id.startsWith(CHAT_BRIEF_NODE_PREFIX))
      .map((n) => n.id.slice(CHAT_BRIEF_NODE_PREFIX.length))
      .sort();
    // Only pack's brief remains — a's brief is suppressed because a is hidden.
    expect(briefIds).toEqual(["pack"]);
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
          draft_model: null,
          default_judge_model: null,
          default_tool_call_model: null,
          brief_model: null,
          default_execution_mode: 'native_react',
          judge_retry_budget: 3,
    min_ground_ratio: null,
    ground_ratio_grace_nodes: 20,
      disabled_tool_names: [],
      compact_trigger_pct: 0.7,
      compact_target_pct: 0.5,
      compact_keep_recent_count: 3,
      compact_preserve_mode: "by_count",
      recalled_context_sticky_turns: 3,
      compact_model: null,
      compact_require_confirmation: true,
      chatnode_compact_trigger_pct: 0.6,
      chatnode_compact_target_pct: 0.4,
          root_ids: [],
          nodes: {},
          created_at: "2026-04-10T00:00:00Z",
        }}
      />,
    );
    expect(screen.getByTestId("chatflow-canvas-empty")).toBeInTheDocument();
  });
});
