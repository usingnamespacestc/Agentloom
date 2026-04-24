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

import {
  ChatFlowCanvas,
  buildGraph,
  CHAT_BRIEF_NODE_PREFIX,
  CHAT_FOLD_NODE_PREFIX,
} from "./ChatFlowCanvas";
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
  it("single pack fold: synthetic fold node appears, range hidden, boundary edge routes fold.right → pack", () => {
    // root → a → b → c → pack (packed_range=[a,b,c]).
    // c is the LAST range member so the edge into pack uses fold's
    // right handle; root's edge into the range uses fold's left (input).
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
    const chatIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    // pack itself stays visible; a,b,c hidden behind the fold.
    expect(chatIds).toEqual(["pack", "root"]);
    // A synthetic fold node appears for pack's range.
    const foldId = `${CHAT_FOLD_NODE_PREFIX}pack`;
    const foldNode = rn.find((n) => n.id === foldId);
    expect(foldNode).toBeDefined();
    expect(foldNode?.type).toBe("chatFold");
    expect(foldNode?.data).toMatchObject({
      hostId: "pack",
      hostKind: "pack",
      foldedCount: 3,
    });
    // Edges: root → fold (fold-input), fold → pack (fold-output-right).
    const edgeByKey = new Map(
      edges.map((e) => [`${e.source}->${e.target}`, e]),
    );
    expect([...edgeByKey.keys()].sort()).toEqual(
      [`root->${foldId}`, `${foldId}->pack`].sort(),
    );
    expect(edgeByKey.get(`root->${foldId}`)?.targetHandle).toBe("fold-input");
    expect(edgeByKey.get(`${foldId}->pack`)?.sourceHandle).toBe(
      "fold-output-right",
    );
  });

  it("single compact fold: ancestors hidden, fold takes over as chain link", () => {
    // root → x → y → compact
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
    const chatIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    // root also gets pulled into compact's range (compact walks all
    // primary-parent ancestors up to a merge boundary).
    expect(chatIds).toEqual(["compact"]);
    const foldId = `${CHAT_FOLD_NODE_PREFIX}compact`;
    expect(rn.find((n) => n.id === foldId)?.data).toMatchObject({
      hostId: "compact",
      hostKind: "compact",
      foldedCount: 3,
    });
    // Only edge: fold → compact via the boundary handle.
    expect(edges.map((e) => `${e.source}->${e.target}`)).toEqual([
      `${foldId}->compact`,
    ]);
    expect(edges[0].sourceHandle).toBe("fold-output-right");
  });

  it("fork from earlier member routes fold.top (interior), fork from boundary routes fold.right", () => {
    //   root → a → b → c → pack  (range=[a,b,c])
    //              ↘ interior_fork      (from b — interior)
    //                  c ↘ boundary_fork (from c — boundary)
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      b: makeTurn("b", ["a"]),
      c: makeTurn("c", ["b"]),
      interior_fork: makeTurn("interior_fork", ["b"]),
      boundary_fork: makeTurn("boundary_fork", ["c"]),
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
    const { edges } = buildGraph(cf, null, {}, {}, folded);
    const foldId = `${CHAT_FOLD_NODE_PREFIX}pack`;
    const byKey = new Map(edges.map((e) => [`${e.source}->${e.target}`, e]));

    // Interior fork (from b) routes fold-output-top.
    expect(byKey.get(`${foldId}->interior_fork`)?.sourceHandle).toBe(
      "fold-output-top",
    );
    // Boundary fork (from c, the last range member) routes fold-output-right.
    expect(byKey.get(`${foldId}->boundary_fork`)?.sourceHandle).toBe(
      "fold-output-right",
    );
    // Host edge (into pack from the last range member) also right.
    expect(byKey.get(`${foldId}->pack`)?.sourceHandle).toBe(
      "fold-output-right",
    );
  });

  it("pack under a folded range lands on fold.bottom (pack-below preserved)", () => {
    // root → a → b → c → pack1 (main chain pack, range=[a,b,c])
    //              ↘ innerPack    (pack hanging off b, range=[b])
    //              innerPack.parent = b
    // Fold pack1 only. innerPack should route via fold.bottom.
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      b: makeTurn("b", ["a"]),
      c: makeTurn("c", ["b"]),
      innerPack: makeTurn("innerPack", ["b"], {
        pack_snapshot: {
          summary: "inner",
          packed_range: ["b"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
      pack1: makeTurn("pack1", ["c"], {
        pack_snapshot: {
          summary: "outer",
          packed_range: ["a", "b", "c"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["pack1"]);
    const { edges } = buildGraph(cf, null, {}, {}, folded);
    const foldId = `${CHAT_FOLD_NODE_PREFIX}pack1`;
    const byKey = new Map(edges.map((e) => [`${e.source}->${e.target}`, e]));
    // Pack child (innerPack) dropped off a hidden range member (b)
    // should arrive via fold-output-bottom.
    expect(byKey.get(`${foldId}->innerPack`)?.sourceHandle).toBe(
      "fold-output-bottom",
    );
    expect(byKey.get(`${foldId}->innerPack`)?.targetHandle).toBe(
      "main-target-top",
    );
  });

  it("nested packs: outer fold absorbs inner, inner host is hidden along with its range", () => {
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
    const folded = new Set<string>(["packA", "packB"]);
    const { nodes: rn, edges } = buildGraph(cf, null, {}, {}, folded);
    const chatIds = rn
      .filter((n) => n.type === "chatflow")
      .map((n) => n.id)
      .sort();
    // packB sits inside packA's range → hidden. packA remains visible.
    // m1 is only in packB's range, but packB is swallowed so it's not
    // an effective fold; m1 stays visible.
    expect(chatIds).toEqual(["m1", "packA", "root"].sort());
    // Only effective fold: packA.
    const foldIds = rn
      .filter((n) => n.type === "chatFold")
      .map((n) => n.id);
    expect(foldIds).toEqual([`${CHAT_FOLD_NODE_PREFIX}packA`]);
    // Edges: root → m1 verbatim; m1 → fold (input); fold → packA (right).
    const byKey = new Set(edges.map((e) => `${e.source}->${e.target}`));
    const foldA = `${CHAT_FOLD_NODE_PREFIX}packA`;
    expect(byKey.has("root->m1")).toBe(true);
    expect(byKey.has(`m1->${foldA}`)).toBe(true);
    expect(byKey.has(`${foldA}->packA`)).toBe(true);
  });

  it("strict nested fold: inner pack inside compact, both visible, split attribution + containment edge", () => {
    // Mirror of the user-reported CF topology:
    //   root → early1 → early2 → compact (compact.range walks up,
    //                                     includes root/early1/early2)
    //   compact → W → X → Y (pack range = [W, X, Y], pack.parent = Y)
    //   Y → compact2 (not folded in this test)
    //   Y → pack (fork child via pack_snapshot)
    // Fold compact2 + pack together. pack.range ⊆ compact2.range,
    // pack.host not in compact2.range (it forks off Y), and pack's
    // range is at the head of compact2's walk = convex.
    const nodes = {
      root: makeTurn("root", []),
      early1: makeTurn("early1", ["root"]),
      early2: makeTurn("early2", ["early1"]),
      W: makeTurn("W", ["early2"]),
      X: makeTurn("X", ["W"]),
      Y: makeTurn("Y", ["X"]),
      compact2: makeTurn("compact2", ["Y"], {
        compact_snapshot: {
          summary: "c2",
          preserved_messages: [],
          preserved_before_summary: false,
        },
      }),
      pack: makeTurn("pack", ["Y"], {
        pack_snapshot: {
          summary: "pk",
          packed_range: ["W", "X", "Y"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["compact2", "pack"]);
    const { nodes: rn, edges } = buildGraph(cf, null, {}, {}, folded);

    // BOTH fold rfNodes should appear.
    const foldIds = rn
      .filter((n) => n.type === "chatFold")
      .map((n) => n.id)
      .sort();
    expect(foldIds).toEqual(
      [`${CHAT_FOLD_NODE_PREFIX}compact2`, `${CHAT_FOLD_NODE_PREFIX}pack`].sort(),
    );

    const compactFold = `${CHAT_FOLD_NODE_PREFIX}compact2`;
    const packFold = `${CHAT_FOLD_NODE_PREFIX}pack`;
    // Outer fold absorbs only outer-exclusive (root/early1/early2 = 3);
    // inner fold absorbs pack's own range (W/X/Y = 3). No double count.
    expect(rn.find((n) => n.id === compactFold)?.data.foldedCount).toBe(3);
    expect(rn.find((n) => n.id === packFold)?.data.foldedCount).toBe(3);

    const byKey = new Map(edges.map((e) => [`${e.source}->${e.target}`, e]));
    // Chain continuation crosses outer→inner via the inner fold's
    // input handle — this is the containment link.
    const containment = byKey.get(`${compactFold}->${packFold}`);
    expect(containment).toBeDefined();
    expect(containment?.targetHandle).toBe("fold-input");
    // Containment edge is dashed + slate-400 (muted) to visually
    // signal "entering a nested fold".
    expect(containment?.style?.strokeDasharray).toBe("6 4");
    expect(containment?.style?.stroke).toBe("#94a3b8");
    // Inner fold → compact2 (outer host) via right handle.
    expect(byKey.get(`${packFold}->compact2`)?.sourceHandle).toBe(
      "fold-output-right",
    );
    // Inner fold → pack (inner host) also via right handle.
    expect(byKey.get(`${packFold}->pack`)?.sourceHandle).toBe(
      "fold-output-right",
    );
  });

  it("inner in middle of outer walk falls back to largest-first (no split)", () => {
    // packOuter.range covers [a, b, c, d, e] (all on primary chain);
    // packInner.range = [c] is a single node in the MIDDLE of the walk.
    // Outer-exclusive = {a, b, d, e} is non-convex (c separates them),
    // so split would create a directed cycle. Attribution must stay
    // largest-first — inner's claim gets empty-filtered out.
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      b: makeTurn("b", ["a"]),
      c: makeTurn("c", ["b"]),
      d: makeTurn("d", ["c"]),
      e: makeTurn("e", ["d"]),
      packInner: makeTurn("packInner", ["c"], {
        pack_snapshot: {
          summary: "inner",
          packed_range: ["c"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
      packOuter: makeTurn("packOuter", ["e"], {
        pack_snapshot: {
          summary: "outer",
          packed_range: ["a", "b", "c", "d", "e"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["packOuter", "packInner"]);
    const { nodes: rn } = buildGraph(cf, null, {}, {}, folded);

    // Only the outer fold rfNode survives — packInner's claim is
    // absorbed by the outer, leaving it empty, so the orphan filter
    // drops it.
    const foldIds = rn
      .filter((n) => n.type === "chatFold")
      .map((n) => n.id);
    expect(foldIds).toEqual([`${CHAT_FOLD_NODE_PREFIX}packOuter`]);
  });

  it("partial-overlap fold with zero claim is filtered (no orphan fold card)", () => {
    // Two packs with overlapping but non-nested ranges. The smaller
    // one's range is fully contained in the larger one; largest-first
    // attribution leaves the smaller with zero claimed hidden nodes,
    // and the orphan filter drops its rfNode so it never renders
    // disconnected.
    const nodes = {
      root: makeTurn("root", []),
      a: makeTurn("a", ["root"]),
      b: makeTurn("b", ["a"]),
      c: makeTurn("c", ["b"]),
      // Large outer pack covers a,b,c as a suffix of its walk but also
      // extends somewhere else — we construct with only the chain
      // here, so outer fully covers inner. Inner is positioned in the
      // MIDDLE of outer's range to prevent split-attribution from
      // firing (this is the "orphan defense", not the nesting path).
      packOuter: makeTurn("packOuter", ["c"], {
        pack_snapshot: {
          summary: "outer",
          packed_range: ["a", "b", "c"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
      packMid: makeTurn("packMid", ["b"], {
        pack_snapshot: {
          summary: "mid",
          packed_range: ["b"],
          use_detailed_index: false,
          preserve_last_n: 0,
          preserved_messages: [],
        },
      }),
    };
    const cf = baseChatFlow(nodes);
    const folded = new Set<string>(["packOuter", "packMid"]);
    const { nodes: rn } = buildGraph(cf, null, {}, {}, folded);
    // Only outer fold survives (inner is mid-of-walk → no split → inner
    // empty claim → filtered).
    const foldIds = rn
      .filter((n) => n.type === "chatFold")
      .map((n) => n.id);
    expect(foldIds).toEqual([`${CHAT_FOLD_NODE_PREFIX}packOuter`]);
  });

  it("brief nodes skip hidden sources but stay for visible ones", () => {
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
