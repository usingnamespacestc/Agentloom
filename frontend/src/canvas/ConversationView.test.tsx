/**
 * ConversationView smoke tests.
 *
 * Pure branch-resolution logic is covered in pathUtils.test.ts; here
 * we focus on: (a) the empty state, (b) rendering a 2-turn chatflow
 * with its messages, and (c) the inline branch selector actually
 * switching the active branch through the store.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, it, expect, beforeEach, vi } from "vitest";

import { ConversationView } from "./ConversationView";
import { useChatFlowStore } from "@/store/chatflowStore";
import { api } from "@/lib/api";
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
    compact_snapshot: null,
    entry_prompt_tokens: null,
    output_response_tokens: null,
  };
}

function twoBranchFlow(): ChatFlow {
  return {
    id: "c",
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
    cognitive_react_enabled: false,
    chatnode_compact_trigger_pct: 0.6,
    chatnode_compact_target_pct: 0.4,
    max_produced_tags: 10,
    max_consumed_tags: 8,
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
    // Tests that toggle view-state write to localStorage via the
    // store subscription; clear it so a prior test's saved
    // ``selectedNodeId`` doesn't surface as a "stale" hydration when
    // the next test's setChatFlow fires.
    try {
      localStorage.clear();
    } catch {
      /* happy-dom may not expose localStorage in some configs */
    }
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

  it("renders a pack_summary segment as a distinct pack bubble (segments-driven path)", async () => {
    // Three-turn chain a → b → p where p is a pack ChatNode whose
    // packed_range collapses [a, b]. With the backend feeding the
    // segments-driven render path, the panel renders only the pack
    // summary in p's place — the per-turn bubbles for a and b must
    // NOT appear because the LLM context replaces them with the pack
    // summary, and panel === LLM-prompt parity is the design goal.
    const cf: ChatFlow = {
      id: "cf-pack",
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
      compact_target_pct: 0.5,
      compact_keep_recent_count: 3,
      compact_preserve_mode: "by_count",
      recalled_context_sticky_turns: 3,
      compact_model: null,
      compact_require_confirmation: true,
      cognitive_react_enabled: false,
      chatnode_compact_trigger_pct: 0.6,
      chatnode_compact_target_pct: 0.4,
    max_produced_tags: 10,
    max_consumed_tags: 8,
      root_ids: ["a"],
      nodes: {
        a: node("a", [], "topic 1 user", "topic 1 agent", 0),
        b: node("b", ["a"], "topic 1 follow-up", "topic 1 reply", 1),
        p: {
          ...node("p", ["b"], "", "PACK_SUMMARY_TEXT", 2),
          pack_snapshot: {
            summary: "PACK_SUMMARY_TEXT",
            packed_range: ["a", "b"],
            use_detailed_index: true,
            preserve_last_n: 0,
            preserved_messages: [],
          },
        },
      },
      created_at: "2026-04-10T00:00:00Z",
    };

    // Stub the inbound_context endpoint to deliver one pack_summary
    // segment for ``p``. SegmentRenderer should pick this up and
    // render a single pack bubble for p — and only p.
    const stub = vi.spyOn(api, "getInboundContext").mockResolvedValue({
      segments: [
        {
          kind: "pack_summary",
          source_node_id: "p",
          synthetic: true,
          messages: [],
          cbi_entries: null,
        },
      ],
    });
    try {
      useChatFlowStore.getState().setChatFlow(cf);
      render(<ConversationView />);

      // The pack bubble appears (data-testid signals the pack variant).
      await waitFor(() =>
        expect(
          screen.getByTestId("conversation-node-p-pack"),
        ).toBeInTheDocument(),
      );
      // The packed range members must NOT render — segments-driven path
      // suppresses them in favor of the pack summary.
      expect(screen.queryByTestId("conversation-node-a")).not.toBeInTheDocument();
      expect(screen.queryByTestId("conversation-node-b")).not.toBeInTheDocument();
      // Pack summary text from agent_response is rendered.
      expect(screen.getByText(/PACK_SUMMARY_TEXT/)).toBeInTheDocument();
    } finally {
      stub.mockRestore();
    }
  });

  it("falls back to visiblePath rendering when /inbound_context returns nothing", async () => {
    // Backend may be unreachable or return an empty segments list
    // (e.g. tests, offline, fresh chatflow). The panel must still
    // render the chain via the local visiblePath path so the user
    // sees their conversation rather than a blank panel.
    const stub = vi.spyOn(api, "getInboundContext").mockResolvedValue({
      segments: [],
    });
    try {
      useChatFlowStore.getState().setChatFlow(twoBranchFlow());
      render(<ConversationView />);

      // visiblePath path renders the local node bubbles as before.
      expect(screen.getByTestId("conversation-node-a")).toBeInTheDocument();
      expect(screen.getByTestId("conversation-node-c")).toBeInTheDocument();
    } finally {
      stub.mockRestore();
    }
  });

  it("original view toggle shows pre-compact ancestors and hides compact node", () => {
    // a → b (compact) → c. Effective view shows b + c; original view
    // should show a + c (compact node b filtered out as an aggregator op).
    const cf: ChatFlow = {
      id: "cf-orig",
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
      compact_target_pct: 0.5,
      compact_keep_recent_count: 3,
      compact_preserve_mode: "by_count",
      recalled_context_sticky_turns: 3,
      compact_model: null,
      compact_require_confirmation: true,
      cognitive_react_enabled: false,
      chatnode_compact_trigger_pct: 0.6,
      chatnode_compact_target_pct: 0.4,
      max_produced_tags: 10,
      max_consumed_tags: 8,
      root_ids: ["a"],
      nodes: {
        a: node("a", [], "old user", "old agent", 0),
        b: {
          ...node("b", ["a"], "", "SUMMARY_BODY", 1),
          compact_snapshot: {
            summary: "SUMMARY_BODY",
            preserved_messages: [],
            preserved_before_summary: false,
          },
        },
        c: node("c", ["b"], "new user", "new agent", 2),
      },
      created_at: "2026-04-10T00:00:00Z",
    };
    useChatFlowStore.getState().setChatFlow(cf);
    render(<ConversationView />);

    // Default = effective: a hidden, b shown.
    expect(screen.queryByTestId("conversation-node-a")).not.toBeInTheDocument();
    expect(screen.getByTestId("conversation-node-b")).toBeInTheDocument();

    // Click "Original" toggle.
    fireEvent.click(screen.getByTestId("cv-view-original"));

    // Original view: a now visible, b (compact op) hidden, c still visible.
    expect(screen.getByTestId("conversation-node-a")).toBeInTheDocument();
    expect(screen.queryByTestId("conversation-node-b")).not.toBeInTheDocument();
    expect(screen.getByTestId("conversation-node-c")).toBeInTheDocument();
    // Truncation notice belongs to effective mode only.
    expect(screen.queryByTestId("compact-truncation-notice")).not.toBeInTheDocument();
  });

  it("truncates at a compact node and renders a distinct compact bubble", () => {
    // a → b (compact) → c — the panel should hide a and render b with
    // a compact marker. Matches _build_chat_context's upward-walk stop.
    const cf: ChatFlow = {
      id: "cf",
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
      compact_target_pct: 0.5,
      compact_keep_recent_count: 3,
      compact_preserve_mode: "by_count",
      recalled_context_sticky_turns: 3,
      compact_model: null,
      compact_require_confirmation: true,
      cognitive_react_enabled: false,
      chatnode_compact_trigger_pct: 0.6,
      chatnode_compact_target_pct: 0.4,
    max_produced_tags: 10,
    max_consumed_tags: 8,
      root_ids: ["a"],
      nodes: {
        a: node("a", [], "old user", "old agent", 0),
        b: {
          ...node("b", ["a"], "", "SUMMARY_BODY", 1),
          compact_snapshot: {
            summary: "SUMMARY_BODY",
            preserved_messages: [],
            preserved_before_summary: false,
          },
        },
        c: node("c", ["b"], "new user", "new agent", 2),
      },
      created_at: "2026-04-10T00:00:00Z",
    };
    useChatFlowStore.getState().setChatFlow(cf);
    render(<ConversationView />);

    // Default walk lands on c. Path should be b (compact) → c; a hidden.
    expect(screen.queryByTestId("conversation-node-a")).not.toBeInTheDocument();
    expect(screen.getByTestId("conversation-node-b")).toBeInTheDocument();
    expect(screen.getByTestId("conversation-node-c")).toBeInTheDocument();
    // Compact bubble variant is in use.
    expect(screen.getByTestId("conversation-node-b-compact")).toBeInTheDocument();
    // Truncation notice tells the user some history was compacted away.
    expect(screen.getByTestId("compact-truncation-notice")).toBeInTheDocument();
  });
});
