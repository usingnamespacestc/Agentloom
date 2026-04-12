/**
 * User preferences store — canvas / display toggles persisted to
 * ``localStorage``. These are per-browser client prefs, not per-workspace
 * settings (those live in the DB). Keep it small: one zustand store,
 * one localStorage key, write-through on every change.
 */

import { create } from "zustand";

const STORAGE_KEY = "agentloom_prefs_v1";

export interface Preferences {
  /** Render the raw DB node id in the top-right corner of each node card. */
  showNodeId: boolean;
  /** Show per-turn token totals (prompt+completion+cached). */
  showTokens: boolean;
  /** Show wall-clock generation time (finished_at − started_at). */
  showGenTime: boolean;
  /** Show generation speed (completion_tokens / seconds). */
  showGenSpeed: boolean;
}

const DEFAULTS: Preferences = {
  showNodeId: false,
  showTokens: false,
  showGenTime: false,
  showGenSpeed: false,
};

function load(): Preferences {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<Preferences>;
    return { ...DEFAULTS, ...parsed };
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
  setShowTokens: (value: boolean) => void;
  setShowGenTime: (value: boolean) => void;
  setShowGenSpeed: (value: boolean) => void;
}

export const usePreferencesStore = create<PreferencesStore>((set, get) => ({
  ...load(),
  setShowNodeId(value) {
    set({ showNodeId: value });
    save({ ...get(), showNodeId: value });
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
}));
