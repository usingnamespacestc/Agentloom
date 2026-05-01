/**
 * Bug fix verify (2026-05-01): TokenBar showing 44% on a 14k / 128k
 * doubao node was caused by ``contextWindowMap`` only keying entries
 * by the provider's UUID — when a WorkNode's ``model_override.provider_id``
 * happens to hold the friendly_name (some legacy paths wrote
 * ``"volcengine"`` instead of the UUID), the lookup missed and
 * ``TokenBar`` fell back to ``DEFAULT_MAX_CONTEXT_TOKENS`` (32k),
 * inflating the displayed percentage.
 *
 * The fix adds a parallel ``"<friendly_name>:<model_id>"`` key per
 * model so both shapes resolve.
 */
import { describe, expect, it } from "vitest";

import { contextWindowMap } from "./ChatFlowCanvas";
import type { ProviderSummary } from "@/lib/api";

function provider(
  overrides: Partial<ProviderSummary> & {
    id: string;
    friendly_name: string;
  },
): ProviderSummary {
  return {
    id: overrides.id,
    friendly_name: overrides.friendly_name,
    provider_kind: "openai_compat",
    provider_sub_kind: null,
    base_url: "https://example.com",
    api_key_source: "env_var",
    api_key_env_var: "EXAMPLE_KEY",
    api_key_inline_set: false,
    json_mode: "object",
    extra_headers: {},
    rate_limit_bucket: null,
    available_models: [],
    created_at: "2026-04-01T00:00:00Z",
    updated_at: "2026-04-01T00:00:00Z",
    ...overrides,
  } as ProviderSummary;
}

describe("contextWindowMap", () => {
  it("keys by both UUID and friendly_name so legacy nodes resolve", () => {
    const map = contextWindowMap([
      provider({
        id: "019d83a5-cd69-7103-aced-e2707cb2008a",
        friendly_name: "volcengine",
        available_models: [
          {
            id: "doubao-seed-2-0-pro-260215",
            context_window: 128_000,
            max_output_tokens: null,
            supports_tools: true,
            supports_streaming: true,
            pinned: true,
            json_mode: null,
            temperature: null,
            top_p: null,
            top_k: null,
            presence_penalty: null,
            frequency_penalty: null,
            repetition_penalty: null,
            num_ctx: null,
            thinking_budget_tokens: null,
            thinking_enabled: null,
          },
        ],
      }),
    ]);
    expect(
      map["019d83a5-cd69-7103-aced-e2707cb2008a:doubao-seed-2-0-pro-260215"],
    ).toBe(128_000);
    expect(map["volcengine:doubao-seed-2-0-pro-260215"]).toBe(128_000);
  });

  it("skips models without a context_window", () => {
    const map = contextWindowMap([
      provider({
        id: "p1",
        friendly_name: "p1-name",
        available_models: [
          {
            id: "m1",
            context_window: null,
            max_output_tokens: null,
            supports_tools: true,
            supports_streaming: true,
            pinned: true,
            json_mode: null,
            temperature: null,
            top_p: null,
            top_k: null,
            presence_penalty: null,
            frequency_penalty: null,
            repetition_penalty: null,
            num_ctx: null,
            thinking_budget_tokens: null,
            thinking_enabled: null,
          },
        ],
      }),
    ]);
    expect(map).toEqual({});
  });

  it("handles multiple providers + models without cross-talk", () => {
    const map = contextWindowMap([
      provider({
        id: "uuid-a",
        friendly_name: "alpha",
        available_models: [
          {
            id: "m1",
            context_window: 32_000,
            max_output_tokens: null,
            supports_tools: true,
            supports_streaming: true,
            pinned: false,
            json_mode: null,
            temperature: null,
            top_p: null,
            top_k: null,
            presence_penalty: null,
            frequency_penalty: null,
            repetition_penalty: null,
            num_ctx: null,
            thinking_budget_tokens: null,
            thinking_enabled: null,
          },
        ],
      }),
      provider({
        id: "uuid-b",
        friendly_name: "beta",
        available_models: [
          {
            id: "m2",
            context_window: 200_000,
            max_output_tokens: null,
            supports_tools: true,
            supports_streaming: true,
            pinned: false,
            json_mode: null,
            temperature: null,
            top_p: null,
            top_k: null,
            presence_penalty: null,
            frequency_penalty: null,
            repetition_penalty: null,
            num_ctx: null,
            thinking_budget_tokens: null,
            thinking_enabled: null,
          },
        ],
      }),
    ]);
    expect(map["uuid-a:m1"]).toBe(32_000);
    expect(map["alpha:m1"]).toBe(32_000);
    expect(map["uuid-b:m2"]).toBe(200_000);
    expect(map["beta:m2"]).toBe(200_000);
  });
});
