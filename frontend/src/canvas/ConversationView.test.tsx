/**
 * ConversationView smoke tests.
 *
 * Pure branch-resolution logic is covered in pathUtils.test.ts; here
 * we focus on: (a) the empty state, (b) rendering a 2-turn chatflow
 * with its messages, and (c) the inline branch selector actually
 * switching the active branch through the store.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, beforeEach } from "vitest";

import { ConversationView } from "./ConversationView";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { ChatFlow, ChatFlowNode } from "@/types/schema";

function node(
  id: string,
  parents: string[] = [],
  userText = "",
  agentText = "",
  createdSeconds = Number(id.replace(/[^\d]/g, "") || "0"),
): ChatFlowNode {
  const iso = `2026-04-10T00:00:${String(createdSeconds).padStart(2, "0")}Z`;
  return {
    id,
    parent_ids: parents,
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
    user_message: { text: userText, provenance: "pure_user", updated_at: iso },
    agent_response: { text: agentText, provenance: "pure_agent", updated_at: iso },
    workflow: { id: `wf-${id}`, root_ids: [], nodes: {} },
    pending_queue: [],
  };
}

function twoBranchFlow(): ChatFlow {
  return {
    id: "c",
    title: null,
    description: null,
    tags: [],
    default_model: null,
    default_execution_mode: 'direct',
    root_ids: ["a"],
    nodes: {
      a: node("a", [], "hello", "hi there", 0),
      b: node("b", ["a"], "path b", "response b", 1),
      c: node("c", ["a"], "path c", "response c", 2),
    },
    created_at: "2026-04-10T00:00:00Z",
  };
}

describe("ConversationView", () => {
  beforeEach(() => {
    useChatFlowStore.getState().reset();
  });

  it("shows the select-chatflow empty state when no chatflow is loaded", () => {
    render(<ConversationView />);
    expect(screen.getByTestId("conversation-empty")).toBeInTheDocument();
  });

  it("renders the default path (root + latest child) and the branch selector at the fork", () => {
    useChatFlowStore.getState().setChatFlow(twoBranchFlow());
    render(<ConversationView />);

    // Default walk: a → c (c has the latest created_at).
    expect(screen.getByTestId("conversation-node-a")).toBeInTheDocument();
    expect(screen.getByTestId("conversation-node-c")).toBeInTheDocument();
    expect(screen.queryByTestId("conversation-node-b")).not.toBeInTheDocument();

    // Branch selector shows both options.
    expect(screen.getByTestId("branch-selector-a")).toBeInTheDocument();
    expect(screen.getByTestId("branch-option-b")).toBeInTheDocument();
    expect(screen.getByTestId("branch-option-c")).toBeInTheDocument();
  });

  it("switches branches when the user picks a different option at a fork", () => {
    useChatFlowStore.getState().setChatFlow(twoBranchFlow());
    render(<ConversationView />);

    fireEvent.click(screen.getByTestId("branch-option-b"));

    // After picking b, the path is a → b (c should disappear).
    expect(screen.getByTestId("conversation-node-b")).toBeInTheDocument();
    expect(screen.queryByTestId("conversation-node-c")).not.toBeInTheDocument();

    // The store moved the selection onto b.
    expect(useChatFlowStore.getState().selectedNodeId).toBe("b");
    // And remembered b as the endpoint for its own branch.
    expect(useChatFlowStore.getState().branchMemory.b).toBe("b");
  });

  it("terminates the path strictly at the selected node (no extension past it)", () => {
    useChatFlowStore.getState().setChatFlow(twoBranchFlow());
    // Select the root a explicitly — path must be just [a], no b or c.
    useChatFlowStore.getState().selectNode("a");
    render(<ConversationView />);

    expect(screen.getByTestId("conversation-node-a")).toBeInTheDocument();
    expect(screen.queryByTestId("conversation-node-b")).not.toBeInTheDocument();
    expect(screen.queryByTestId("conversation-node-c")).not.toBeInTheDocument();
    // The branch selector at the terminal fork is still rendered,
    // so the user can click into a branch.
    expect(screen.getByTestId("branch-selector-a")).toBeInTheDocument();
  });
});
