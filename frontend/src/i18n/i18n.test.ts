/**
 * i18n smoke tests — make sure every key referenced by the M8 canvas
 * exists in both locales, and that switching languages actually
 * changes the rendered string.
 *
 * If the canvas adds a new ``t('foo.bar')`` call, extend the key list
 * below; that's the contract that keeps zh-CN and en-US in lockstep.
 */

import { describe, it, expect, beforeAll } from "vitest";
import i18n from "./index";

const REQUIRED_KEYS = [
  "app.title",
  "app.tagline",
  "app.switch_language",
  "node.status.planned",
  "node.status.running",
  "node.status.succeeded",
  "node.status.failed",
  "node.kind.llm_call",
  "node.kind.tool_call",
  "node.kind.sub_agent_delegation",
  "chatflow.user",
  "chatflow.agent",
  "chatflow.empty",
  "chatflow.loading",
  "chatflow.load_failed",
  "chatflow.select_chatflow",
  "chatflow.open_workflow",
  "workflow.panel_title",
  "workflow.close_panel",
  "workflow.empty",
  "workflow.tool_result",
  "workflow.tool_error",
  "workflow.prompt_tokens",
  "workflow.completion_tokens",
  "workflow.cached_tokens",
  "workflow.model",
  "workflow.no_selection",
  "workflow.back_to_chatflow",
  "workflow.io_title",
  "workflow.detail_title",
  "conversation.panel_title",
  "conversation.input_placeholder",
  "conversation.input_placeholder_active",
  "conversation.send",
  "conversation.retry",
  "conversation.delete_failed",
  "conversation.branch_label",
];

describe("i18n", () => {
  beforeAll(async () => {
    await i18n.changeLanguage("zh-CN");
  });

  it("every required key resolves in zh-CN", async () => {
    await i18n.changeLanguage("zh-CN");
    for (const key of REQUIRED_KEYS) {
      const value = i18n.t(key);
      expect(value, `missing zh-CN key: ${key}`).not.toBe(key);
      expect(value.trim().length).toBeGreaterThan(0);
    }
  });

  it("every required key resolves in en-US", async () => {
    await i18n.changeLanguage("en-US");
    for (const key of REQUIRED_KEYS) {
      const value = i18n.t(key);
      expect(value, `missing en-US key: ${key}`).not.toBe(key);
      expect(value.trim().length).toBeGreaterThan(0);
    }
  });

  it("switching language returns different strings", async () => {
    await i18n.changeLanguage("zh-CN");
    const zh = i18n.t("chatflow.user");
    await i18n.changeLanguage("en-US");
    const en = i18n.t("chatflow.user");
    expect(zh).not.toBe(en);
  });
});
