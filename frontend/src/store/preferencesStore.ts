/**
 * User preferences store.
 *
 * Two storage tiers:
 * - **Canvas display toggles** (showNodeId, showTokens, etc.) live on
 *   ``WorkspaceSettings.canvas_prefs`` server-side so they follow the
 *   account, not the browser. ``hydrateFromServer`` runs once at app
 *   boot to load them; toggle setters write through ``PATCH``.
 * - **Composer model picks** (``composerModels``) stay in
 *   ``localStorage`` — they're per-tab session state (which model
 *   should this composer default to right now), not workspace
 *   preferences. Surviving across browsers/devices is desirable for
 *   the canvas toggles but actively wrong for the composer (different
 *   tabs may want different model picks; different machines may have
 *   different preferred providers).
 */

import { create } from "zustand";

import { api, type CanvasPrefs } from "@/lib/api";
import type { ProviderModelRef } from "@/types/schema";

const COMPOSER_STORAGE_KEY = "agentloom_composer_models_v1";

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

const LEGACY_COMBINED_KEY = "agentloom_prefs_v1";

function loadComposerModels(): ComposerModelMap {
  try {
    const raw = localStorage.getItem(COMPOSER_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<ComposerModelMap>;
      return {
        llm: sanitizeRef(parsed.llm),
        judge: sanitizeRef(parsed.judge),
        tool_call: sanitizeRef(parsed.tool_call),
      };
    }
  } catch {
    // fall through to legacy migration
  }
  // Fallback: migrate from the pre-2026-04-25 combined key. The old
  // schema bundled canvas toggles + composerModels under one
  // localStorage entry; we now split — canvas to server, composer
  // to its own key. Lift composerModels here so the user's existing
  // sticky picks survive the storage rework.
  try {
    const old = localStorage.getItem(LEGACY_COMBINED_KEY);
    if (!old) return EMPTY_COMPOSER_MODELS;
    const parsed = JSON.parse(old) as {
      composerModels?: Partial<ComposerModelMap>;
      composerModel?: ProviderModelRef | null;  // even older single-pick
    };
    const oldComposer = parsed.composerModels ?? {};
    const migrated: ComposerModelMap = {
      llm: sanitizeRef(oldComposer.llm),
      judge: sanitizeRef(oldComposer.judge),
      tool_call: sanitizeRef(oldComposer.tool_call),
    };
    if (parsed.composerModel && !parsed.composerModels) {
      migrated.llm = sanitizeRef(parsed.composerModel);
    }
    saveComposerModels(migrated);
    return migrated;
  } catch {
    return EMPTY_COMPOSER_MODELS;
  }
}

/** One-shot migration of legacy canvas toggles. If the server's
 * stored ``canvas_prefs`` are all-false (the freshly-defaulted state
 * a user hits the first time after this rework deploys) AND the
 * legacy localStorage entry has toggles set, push them to the server
 * and seed the store. Also clears the legacy key on success so we
 * don't re-migrate every boot. Best-effort: any parse / network
 * failure leaves both sides untouched and the user just re-toggles.
 */
export function maybeMigrateLegacyCanvasPrefs(
  serverPrefs: CanvasPrefs,
): CanvasPrefs | null {
  const serverEmpty =
    !serverPrefs.show_node_id &&
    !serverPrefs.show_chatflow_id &&
    !serverPrefs.show_tokens &&
    !serverPrefs.show_gen_time &&
    !serverPrefs.show_gen_speed &&
    !serverPrefs.show_worknode_model;
  if (!serverEmpty) return null;
  let parsed: Record<string, unknown>;
  try {
    const raw = localStorage.getItem(LEGACY_COMBINED_KEY);
    if (!raw) return null;
    parsed = JSON.parse(raw) as Record<string, unknown>;
  } catch {
    return null;
  }
  const lsCanvas: CanvasPrefs = {
    show_node_id: !!parsed.showNodeId,
    show_chatflow_id: !!parsed.showChatflowId,
    show_tokens: !!parsed.showTokens,
    show_gen_time: !!parsed.showGenTime,
    show_gen_speed: !!parsed.showGenSpeed,
    show_worknode_model: !!parsed.showWorkNodeModel,
  };
  const anyOn =
    lsCanvas.show_node_id ||
    lsCanvas.show_chatflow_id ||
    lsCanvas.show_tokens ||
    lsCanvas.show_gen_time ||
    lsCanvas.show_gen_speed ||
    lsCanvas.show_worknode_model;
  if (!anyOn) return null;
  // Drop the legacy key so subsequent boots don't re-migrate (the
  // user might toggle things off after this — we don't want to
  // resurrect their original choices each time).
  try {
    localStorage.removeItem(LEGACY_COMBINED_KEY);
  } catch {
    // best-effort
  }
  return lsCanvas;
}

function saveComposerModels(models: ComposerModelMap): void {
  try {
    localStorage.setItem(COMPOSER_STORAGE_KEY, JSON.stringify(models));
  } catch {
    // Quota errors ignored — composer state isn't load-bearing.
  }
}

/** Push canvas toggle changes to the server. Best-effort: a failed
 * PATCH leaves the in-memory store updated so the UI reflects the
 * user's click immediately, but the value won't survive a reload.
 * Network failures are swallowed (offline / backend down). */
function pushCanvasPrefs(prefs: CanvasPrefs): void {
  void api
    .patchWorkspaceSettings({ canvas_prefs: prefs })
    .catch(() => {
      // Best-effort write-through: don't surface errors mid-toggle.
    });
}

interface PreferencesStore extends Preferences {
  setShowNodeId: (value: boolean) => void;
  setShowChatflowId: (value: boolean) => void;
  setShowTokens: (value: boolean) => void;
  setShowGenTime: (value: boolean) => void;
  setShowGenSpeed: (value: boolean) => void;
  setShowWorkNodeModel: (value: boolean) => void;
  setComposerModel: (kind: keyof ComposerModelMap, value: ProviderModelRef | null) => void;
  /** Replace canvas toggles from a server response. Used by the
   * boot-time hydration in ``App.tsx`` after fetching workspace
   * settings — keeps the user's stored prefs visible from the very
   * first render rather than briefly flashing the defaults. */
  hydrateCanvasPrefsFromServer: (prefs: CanvasPrefs) => void;
}

/** Build the canvas_prefs PATCH body from the current store state plus
 * a single override. Centralized so each setter doesn't have to spell
 * out all six field names. */
function snapshotCanvasPrefs(state: Preferences): CanvasPrefs {
  return {
    show_node_id: state.showNodeId,
    show_chatflow_id: state.showChatflowId,
    show_tokens: state.showTokens,
    show_gen_time: state.showGenTime,
    show_gen_speed: state.showGenSpeed,
    show_worknode_model: state.showWorkNodeModel,
  };
}

export const usePreferencesStore = create<PreferencesStore>((set, get) => ({
  // Canvas toggles start at defaults; ``hydrateCanvasPrefsFromServer``
  // overlays the persisted state once the boot fetch completes.
  ...DEFAULTS,
  composerModels: loadComposerModels(),
  setShowNodeId(value) {
    set({ showNodeId: value });
    pushCanvasPrefs(snapshotCanvasPrefs({ ...get(), showNodeId: value }));
  },
  setShowChatflowId(value) {
    set({ showChatflowId: value });
    pushCanvasPrefs(snapshotCanvasPrefs({ ...get(), showChatflowId: value }));
  },
  setShowTokens(value) {
    set({ showTokens: value });
    pushCanvasPrefs(snapshotCanvasPrefs({ ...get(), showTokens: value }));
  },
  setShowGenTime(value) {
    set({ showGenTime: value });
    pushCanvasPrefs(snapshotCanvasPrefs({ ...get(), showGenTime: value }));
  },
  setShowGenSpeed(value) {
    set({ showGenSpeed: value });
    pushCanvasPrefs(snapshotCanvasPrefs({ ...get(), showGenSpeed: value }));
  },
  setShowWorkNodeModel(value) {
    set({ showWorkNodeModel: value });
    pushCanvasPrefs(snapshotCanvasPrefs({ ...get(), showWorkNodeModel: value }));
  },
  setComposerModel(kind, value) {
    const next = { ...get().composerModels, [kind]: value };
    set({ composerModels: next });
    saveComposerModels(next);
  },
  hydrateCanvasPrefsFromServer(prefs) {
    set({
      showNodeId: prefs.show_node_id,
      showChatflowId: prefs.show_chatflow_id,
      showTokens: prefs.show_tokens,
      showGenTime: prefs.show_gen_time,
      showGenSpeed: prefs.show_gen_speed,
      showWorkNodeModel: prefs.show_worknode_model,
    });
  },
}));
