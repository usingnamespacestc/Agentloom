/**
 * ChatBriefNodeCard tests.
 *
 * The card subscribes to the ``boardItems`` store slice directly, so
 * seed a BoardItem into the store and render the card with the
 * matching ``sourceNodeId``.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, afterEach } from "vitest";
import { ReactFlowProvider } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";

import {
  ChatBriefNodeCard,
  type ChatBriefNodeData,
} from "./ChatBriefNodeCard";
import { useChatFlowStore } from "@/store/chatflowStore";
import type { BoardItem } from "@/types/schema";

function renderCard(sourceNodeId: string) {
  const data: ChatBriefNodeData = { sourceNodeId };
  return render(
    <ReactFlowProvider>
      <ChatBriefNodeCard {...({ data } as unknown as NodeProps)} />
    </ReactFlowProvider>,
  );
}

describe("ChatBriefNodeCard", () => {
  afterEach(() => {
    useChatFlowStore.setState({ boardItems: {} });
  });

  it("renders the BoardItem description when a scope='chat' row exists", () => {
    const item: BoardItem = {
      id: "bi-1",
      chatflow_id: "cf",
      workflow_id: null,
      source_node_id: "cn-1",
      source_kind: "chat_turn",
      scope: "chat",
      description: "user asked about tokens; agent explained the budget",
      fallback: false,
      created_at: "2026-04-21T00:00:00Z",
    };
    useChatFlowStore.setState({ boardItems: { "cn-1": item } });

    renderCard("cn-1");
    const card = screen.getByTestId("chat-brief-node-cn-1");
    expect(card.getAttribute("data-source-kind")).toBe("chat_turn");
    expect(card.getAttribute("data-fallback")).toBe("false");
    expect(card).toHaveTextContent("user asked about tokens");
  });

  it("renders the fallback badge when the row was produced by the code template", () => {
    const item: BoardItem = {
      id: "bi-2",
      chatflow_id: "cf",
      workflow_id: null,
      source_node_id: "cn-2",
      source_kind: "chat_compact",
      scope: "chat",
      description: "compact summary",
      fallback: true,
      created_at: "2026-04-21T00:00:00Z",
    };
    useChatFlowStore.setState({ boardItems: { "cn-2": item } });

    renderCard("cn-2");
    const card = screen.getByTestId("chat-brief-node-cn-2");
    expect(card.getAttribute("data-fallback")).toBe("true");
  });

  it("renders null when no BoardItem is seeded for the source id", () => {
    const { container } = renderCard("cn-missing");
    expect(container.firstChild).toBeNull();
  });

  it("ignores non-chat scope BoardItems at the same id", () => {
    const wrongScope: BoardItem = {
      id: "bi-3",
      chatflow_id: "cf",
      workflow_id: "wf",
      source_node_id: "cn-3",
      source_kind: "draft",
      scope: "node",
      description: "wrong scope",
      fallback: false,
      created_at: "2026-04-21T00:00:00Z",
    };
    useChatFlowStore.setState({ boardItems: { "cn-3": wrongScope } });
    const { container } = renderCard("cn-3");
    expect(container.firstChild).toBeNull();
  });
});
