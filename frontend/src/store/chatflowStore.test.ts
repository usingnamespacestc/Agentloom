/**
 * Store unit tests — focus on SSE event application and load-state
 * lifecycle. The API layer is mocked with ``vi.stubGlobal`` so we
 * never hit a real network.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";

import { useChatFlowStore } from "./chatflowStore";
import type { ChatFlow, ChatFlowNode, WorkFlowEvent } from "@/types/schema";

function stubChatNode(
  id: string,
  parents: string[],
  created: string = `2026-04-10T00:00:${id.padStart(2, "0")}Z`,
): ChatFlowNode {
  return {
    id,
    parent_ids: parents,
    description: { text: "", provenance: "unset", updated_at: created },
    expected_outcome: null,
    status: "succeeded",
    model_override: null,
    locked: false,
    error: null,
    position_x: null,
    position_y: null,
    created_at: created,
    updated_at: created,
    started_at: null,
    finished_at: null,
    user_message: { text: "", provenance: "pure_user", updated_at: created },
    agent_response: { text: "", provenance: "pure_agent", updated_at: created },
    workflow: { id: `wf-${id}`, root_ids: [], nodes: {} },
    pending_queue: [],
  };
}

function seedChatFlow(): ChatFlow {
  return {
    id: "chat-1",
    title: "demo",
    default_chat_model: null,
    default_work_model: null,
    root_ids: ["n1"],
    nodes: {
      n1: {
        id: "n1",
        parent_ids: [],
        description: { text: "", provenance: "unset", updated_at: "2026-04-10T00:00:00Z" },
        expected_outcome: null,
        status: "running",
        model_override: null,
        locked: false,
        error: null,
        position_x: null,
        position_y: null,
        created_at: "2026-04-10T00:00:00Z",
        updated_at: "2026-04-10T00:00:00Z",
        started_at: null,
        finished_at: null,
        user_message: { text: "hi", provenance: "pure_user", updated_at: "2026-04-10T00:00:00Z" },
        agent_response: { text: "", provenance: "unset", updated_at: "2026-04-10T00:00:00Z" },
        workflow: {
          id: "wf-1",
          root_ids: ["w1"],
          nodes: {
            w1: {
              id: "w1",
              parent_ids: [],
              description: { text: "", provenance: "unset", updated_at: "2026-04-10T00:00:00Z" },
              expected_outcome: null,
              status: "running",
              model_override: null,
              locked: false,
              error: null,
              position_x: null,
              position_y: null,
              created_at: "2026-04-10T00:00:00Z",
              updated_at: "2026-04-10T00:00:00Z",
              started_at: null,
              finished_at: null,
              step_kind: "llm_call",
              tool_constraints: null,
              input_messages: null,
              output_message: null,
              usage: null,
            },
          },
        },
        pending_queue: [],
      },
    },
  };
}

describe("chatflowStore", () => {
  beforeEach(() => {
    useChatFlowStore.getState().reset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("starts in idle state with no chatflow", () => {
    const s = useChatFlowStore.getState();
    expect(s.loadState).toBe("idle");
    expect(s.chatflow).toBeNull();
    expect(s.viewMode).toBe("chatflow");
    expect(s.rightPanelWidth).toBeGreaterThan(0);
  });

  it("setChatFlow flips to ready and auto-selects the latest leaf", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    const s = useChatFlowStore.getState();
    expect(s.loadState).toBe("ready");
    expect(s.chatflow?.id).toBe("chat-1");
    // The seed has a single leaf n1 → it should be auto-selected.
    expect(s.selectedNodeId).toBe("n1");
  });

  it("selectNode updates the selected id", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    useChatFlowStore.getState().selectNode("n1");
    expect(useChatFlowStore.getState().selectedNodeId).toBe("n1");
  });

  it("enterWorkflow switches view mode and auto-selects inner leaf", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    useChatFlowStore.getState().enterWorkflow("n1");
    const s = useChatFlowStore.getState();
    expect(s.viewMode).toBe("workflow");
    expect(s.drillDownChatNodeId).toBe("n1");
    expect(s.workflowSelectedNodeId).toBe("w1");
  });

  it("exitWorkflow returns to chatflow view and clears workflow state", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    useChatFlowStore.getState().enterWorkflow("n1");
    useChatFlowStore.getState().exitWorkflow();
    const s = useChatFlowStore.getState();
    expect(s.viewMode).toBe("chatflow");
    expect(s.drillDownChatNodeId).toBeNull();
    expect(s.workflowSelectedNodeId).toBeNull();
  });

  it("selectNode remembers the endpoint for branch-root ancestors", () => {
    // Build a small fork-in-fork chatflow in-memory for this test.
    const chat: ChatFlow = {
      id: "c",
      title: null,
      default_chat_model: null,
      default_work_model: null,
      root_ids: ["a"],
      nodes: {
        a: stubChatNode("a", []),
        b: stubChatNode("b", ["a"], "2026-04-10T00:00:01Z"),
        c: stubChatNode("c", ["a"], "2026-04-10T00:00:02Z"),
        d: stubChatNode("d", ["b"], "2026-04-10T00:00:03Z"),
        e: stubChatNode("e", ["b"], "2026-04-10T00:00:04Z"),
      },
    };
    useChatFlowStore.getState().setChatFlow(chat);
    // setChatFlow auto-selects latest leaf — that's c (the latest direct child of a).
    expect(useChatFlowStore.getState().selectedNodeId).toBe("c");
    // c is a branch root (sibling of b) → memory[c] = c.
    expect(useChatFlowStore.getState().branchMemory.c).toBe("c");

    // Picking branch b at fork a: no memory for b yet, so we land on b.
    useChatFlowStore.getState().pickBranch("a", "b");
    expect(useChatFlowStore.getState().selectedNodeId).toBe("b");

    // Now go deeper: select e inside branch b.
    useChatFlowStore.getState().selectNode("e");
    // Both b and e are branch roots, so memory remembers both.
    expect(useChatFlowStore.getState().branchMemory.b).toBe("e");
    expect(useChatFlowStore.getState().branchMemory.e).toBe("e");

    // Switch away to c, then back to b — b should resume at e, not at b.
    useChatFlowStore.getState().pickBranch("a", "c");
    expect(useChatFlowStore.getState().selectedNodeId).toBe("c");
    useChatFlowStore.getState().pickBranch("a", "b");
    expect(useChatFlowStore.getState().selectedNodeId).toBe("e");
  });

  it("setRightPanelWidth clamps out-of-range values", () => {
    useChatFlowStore.getState().setRightPanelWidth(50);
    expect(useChatFlowStore.getState().rightPanelWidth).toBeGreaterThanOrEqual(320);
    useChatFlowStore.getState().setRightPanelWidth(10_000);
    expect(useChatFlowStore.getState().rightPanelWidth).toBeLessThanOrEqual(900);
  });

  it("applyEvent patches outer chatflow node status", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    const event: WorkFlowEvent = {
      kind: "chat.node.status",
      workflow_id: "chat-1",
      node_id: "n1",
      data: { status: "succeeded" },
      at: "2026-04-10T00:00:00Z",
    };
    useChatFlowStore.getState().applyEvent(event);
    const s = useChatFlowStore.getState();
    expect(s.chatflow?.nodes.n1.status).toBe("succeeded");
  });

  it("applyEvent patches inner workflow node status", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    const event: WorkFlowEvent = {
      kind: "chat.node.status",
      workflow_id: "chat-1",
      node_id: "w1",
      data: { status: "succeeded" },
      at: "2026-04-10T00:00:00Z",
    };
    useChatFlowStore.getState().applyEvent(event);
    const s = useChatFlowStore.getState();
    expect(s.chatflow?.nodes.n1.workflow.nodes.w1.status).toBe("succeeded");
  });

  it("applyEvent ignores unknown node ids for status events", () => {
    useChatFlowStore.getState().setChatFlow(seedChatFlow());
    const before = useChatFlowStore.getState().chatflow;
    useChatFlowStore.getState().applyEvent({
      kind: "chat.node.status",
      workflow_id: "chat-1",
      node_id: "ghost",
      data: { status: "succeeded" },
      at: "2026-04-10T00:00:00Z",
    });
    const after = useChatFlowStore.getState().chatflow;
    // Same reference — no patch happened.
    expect(after).toBe(before);
  });

  it("loadChatFlow sets error state on API failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("boom", { status: 500, statusText: "Server Error" }),
      ),
    );
    await useChatFlowStore.getState().loadChatFlow("missing");
    const s = useChatFlowStore.getState();
    expect(s.loadState).toBe("error");
    expect(s.errorMessage).toContain("500");
  });

  it("loadChatFlow populates chatflow on success", async () => {
    const payload = seedChatFlow();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    await useChatFlowStore.getState().loadChatFlow("chat-1");
    const s = useChatFlowStore.getState();
    expect(s.loadState).toBe("ready");
    expect(s.chatflow?.id).toBe("chat-1");
  });
});
