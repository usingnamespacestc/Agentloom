/**
 * App shell smoke test.
 *
 * With M8 the app is canvas-driven, so without a chatflow id in the
 * URL we should see the localized empty-state placeholder. We also
 * still assert the title/tagline for a smooth continuation from M0.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, beforeEach } from "vitest";

import App from "./App";
import { useChatFlowStore } from "@/store/chatflowStore";
import i18n from "@/i18n";

describe("App", () => {
  beforeEach(async () => {
    useChatFlowStore.getState().reset();
    await i18n.changeLanguage("zh-CN");
  });

  it("renders title and tagline", () => {
    render(<App />);
    expect(
      screen.getByRole("heading", { name: /agentloom/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/每一次对话都是一张可分支的 DAG/),
    ).toBeInTheDocument();
  });

  it("shows the select-chatflow empty state when no chatflow is loaded", () => {
    render(<App />);
    expect(screen.getByTestId("chatflow-canvas-empty")).toBeInTheDocument();
  });
});
