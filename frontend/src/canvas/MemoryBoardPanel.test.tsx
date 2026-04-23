/**
 * MemoryBoardPanel tests — cover render, click-to-jump, fallback badge,
 * empty-state, and collapse toggle.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import { MemoryBoardPanel } from "./MemoryBoardPanel";
import type { BoardItem } from "@/types/schema";

function makeItem(overrides: Partial<BoardItem> = {}): BoardItem {
  return {
    id: "bi-" + Math.random().toString(36).slice(2, 8),
    chatflow_id: "cf",
    workflow_id: null,
    source_node_id: "src-1",
    source_kind: "chat_turn",
    scope: "chat",
    description: "first line of the brief\nsecond line ignored",
    fallback: false,
    created_at: "2026-04-21T00:00:00Z",
    ...overrides,
  };
}

function renderPanel(items: BoardItem[], onClick = vi.fn()) {
  return {
    onClick,
    ...render(
      <MemoryBoardPanel
        testId="mbp"
        title="ChatBoard"
        emptyText="No briefs yet"
        fallbackLabel="fallback"
        items={items}
        onItemClick={onClick}
      />,
    ),
  };
}

describe("MemoryBoardPanel", () => {
  it("renders the count header and first-line description preview", () => {
    renderPanel([makeItem({ source_node_id: "cn-a" })]);
    expect(screen.getByTestId("mbp")).toHaveTextContent("ChatBoard (1)");
    expect(screen.getByText("first line of the brief")).toBeInTheDocument();
    expect(screen.queryByText(/second line/)).not.toBeInTheDocument();
  });

  it("invokes onItemClick with the matching BoardItem", () => {
    const item = makeItem({ source_node_id: "cn-b" });
    const { onClick } = renderPanel([item]);
    fireEvent.click(screen.getByTestId("mbp-item-cn-b"));
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(onClick).toHaveBeenCalledWith(item);
  });

  it("shows the fallback badge for items with fallback=true", () => {
    renderPanel([makeItem({ source_node_id: "cn-c", fallback: true })]);
    const row = screen.getByTestId("mbp-item-cn-c");
    expect(row).toHaveTextContent("fallback");
  });

  it("shows the empty-state message when there are no items", () => {
    renderPanel([]);
    expect(screen.getByText("No briefs yet")).toBeInTheDocument();
  });

  it("hides the list when collapsed via the header toggle", () => {
    renderPanel([makeItem({ source_node_id: "cn-d" })]);
    // The header bar now has two buttons (open/close + maximize) —
    // click the first (toggle) by role+name rather than the ambiguous
    // getByRole("button") which would fail on multiple matches.
    const toggle = screen.getAllByRole("button")[0];
    fireEvent.click(toggle);
    expect(screen.queryByTestId("mbp-item-cn-d")).not.toBeInTheDocument();
  });

  it("stops click propagation so the canvas pane does not deselect", () => {
    const parentClick = vi.fn();
    render(
      <div onClick={parentClick}>
        <MemoryBoardPanel
          testId="mbp"
          title="ChatBoard"
          emptyText="No briefs yet"
          fallbackLabel="fallback"
          items={[makeItem({ source_node_id: "cn-e" })]}
          onItemClick={vi.fn()}
        />
      </div>,
    );
    fireEvent.click(screen.getByTestId("mbp"));
    expect(parentClick).not.toHaveBeenCalled();
  });
});
