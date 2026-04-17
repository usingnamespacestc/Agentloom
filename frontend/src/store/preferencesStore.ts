/**
 * User preferences store — canvas / display toggles persisted to
 * ``localStorage``. These are per-browser client prefs, not per-workspace
 * settings (those live in the DB). Keep it small: one zustand store,
 * one localStorage key, write-through on every change.
 */

import { create } from "zustand";

import type { ProviderModelRef } from "@/types/schema";

const STORAGE_KEY = "agentloom_prefs_v1";

/** Composer model picks, keyed by the ModelKind that consumes them.
 * Each kind is independently sticky: switching the judge model leaves
 * llm/tool_call alone. ``null`` for a kind = "inherit" (engine falls
 * back to the chatflow default for that kind, then to the main turn
 * model). */
export interface ComposerModelMap {
  llm: ProviderModelRef | null;
  judge: ProviderModelRef | null;
  tool_call: ProviderModelRef | null;
}

const EMPTY_COMPOSER_MODELS: ComposerModelMap = {
  llm: null,
  judge: null,
  tool_call: null,
};

/** Accept only refs that carry both provider_id and model_id as non-empty
 * strings. Guards against stale localStorage shapes (pre-migration partial
 * refs, or refs whose model_id was lost in an older build), which would
 * otherwise serialize to a body missing ``model_id`` and 422 the backend. */
function sanitizeRef(ref: unknown): ProviderModelRef | null {
  if (!ref || typeof ref !== "object") return null;
  const r = ref as Partial<ProviderModelRef>;
  if (typeof r.provider_id !== "string" || !r.provider_id) return null;
  if (typeof r.model_id !== "string" || !r.model_id) return null;
  return { provider_id: r.provider_id, model_id: r.model_id };
}

export interface Preferences {
  /** Render the raw DB node id in the top-right corner of each node card. */
  showNodeId: boolean;
  /** Append the ChatFlow's own id after its title in the top header. */
  showChatflowId: boolean;
  /** Show per-turn token totals (prompt+completion+cached). */
  showTokens: boolean;
  /** Show wall-clock generation time (finished_at − started_at). */
  showGenTime: boolean;
  /** Show generation speed (completion_tokens / seconds). */
  showGenSpeed: boolean;
  /** Show the resolved model_override on every WorkNode card (llm_call
   * already shows it; this also enables it for judge_call / tool_call). */
  showWorkNodeModel: boolean;
  /** Per-kind sticky composer picks (llm / judge / tool_call). Each
   * new turn defaults to these until the user changes them. */
  composerModels: ComposerModelMap;
}

const DEFAULTS: Preferences = {
  showNodeId: false,
  showChatflowId: false,
  showTokens: false,
  showGenTime: false,
  showGenSpeed: false,
  showWorkNodeModel: false,
  composerModels: EMPTY_COMPOSER_MODELS,
};

function load(): Preferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<Preferences> & {
      composerModel?: ProviderModelRef | null;
    };
    // Migration: pre-per-kind builds stored a single composerModel.
    // Promote it to composerModels.llm so existing users don't lose
    // their pick.
    const rawComposer = (parsed.composerModels ?? {}) as Partial<ComposerModelMap>;
    const composerModels: ComposerModelMap = {
      llm: sanitizeRef(rawComposer.llm),
      judge: sanitizeRef(rawComposer.judge),
      tool_call: sanitizeRef(rawComposer.tool_call),
    };
    if (parsed.composerModel && !parsed.composerModels) {
      composerModels.llm = sanitizeRef(parsed.composerModel);
    }
    return { ...DEFAULTS, ...parsed, composerModels };
  } catch {
    return DEFAULTS;
  }
}

function save(prefs: Preferences): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Quota errors are ignored — worst case prefs don't persist.
  }
}

interface PreferencesStore extends Preferences {
  setShowNodeId: (value: boolean) => void;
  setShowChatflowId: (value: boolean) => void;
  setShowTokens: (value: boolean) => void;
  setShowGenTime: (value: boolean) => void;
  setShowGenSpeed: (value: boolean) => void;
  setShowWorkNodeModel: (value: boolean) => void;
  setComposerModel: (kind: keyof ComposerModelMap, value: ProviderModelRef | null) => void;
}

export const usePreferencesStore = create<PreferencesStore>((set, get) => ({
  ...load(),
  setShowNodeId(value) {
    set({ showNodeId: value });
    save({ ...get(), showNodeId: value });
  },
  setShowChatflowId(value) {
    set({ showChatflowId: value });
    save({ ...get(), showChatflowId: value });
  },
  setShowTokens(value) {
    set({ showTokens: value });
    save({ ...get(), showTokens: value });
  },
  setShowGenTime(value) {
    set({ showGenTime: value });
    save({ ...get(), showGenTime: value });
  },
  setShowGenSpeed(value) {
    set({ showGenSpeed: value });
    save({ ...get(), showGenSpeed: value });
  },
  setShowWorkNodeModel(value) {
    set({ showWorkNodeModel: value });
    save({ ...get(), showWorkNodeModel: value });
  },
  setComposerModel(kind, value) {
    const next = { ...get().composerModels, [kind]: value };
    set({ composerModels: next });
    save({ ...get(), composerModels: next });
  },
}));
