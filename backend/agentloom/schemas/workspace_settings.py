"""Workspace-scoped settings — currently only tool states.

Stored in ``workspaces.payload`` as a plain JSON bag so new settings
can land without schema churn. :class:`WorkspaceSettings` is the
Pydantic shape over that bag.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ToolState(str, Enum):
    """Per-tool default exposure policy at the workspace level.

    - ``default_allow`` — tool is registered AND auto-visible to every
      new ChatFlow (not pre-listed in ``disabled_tool_names``).
    - ``available`` — tool is registered but new ChatFlows pre-list it
      as disabled; the user must explicitly enable it per-chatflow.
    - ``disabled`` — tool is blocked everywhere in the workspace. The
      engine refuses to expose or execute it regardless of the
      per-chatflow setting.
    """

    DEFAULT_ALLOW = "default_allow"
    AVAILABLE = "available"
    DISABLED = "disabled"


#: Default state for each built-in tool. Bash is the one surface that
#: warrants explicit opt-in — everything else is benign read/search.
BUILTIN_DEFAULT_STATES: dict[str, ToolState] = {
    "Bash": ToolState.AVAILABLE,
    "Read": ToolState.DEFAULT_ALLOW,
    "Write": ToolState.DEFAULT_ALLOW,
    "Edit": ToolState.DEFAULT_ALLOW,
    "Glob": ToolState.DEFAULT_ALLOW,
    "Grep": ToolState.DEFAULT_ALLOW,
}


#: Language tags the backend ships prompt translations for. Add a new
#: ``fixtures/<tag>/`` subdirectory to extend this list.
WorkspaceLanguage = Literal["en-US", "zh-CN"]


class CanvasPrefs(BaseModel):
    """Per-workspace canvas display toggles, persisted to DB so they
    follow the account rather than the browser. Frontend store mirrors
    this shape; ``usePreferencesStore`` rehydrates from
    ``GET /api/workspace/settings`` on boot and pushes changes back via
    PATCH (write-through). ``composerModels`` and other purely-session
    state stay client-side in localStorage — those are per-tab picks,
    not workspace preferences.
    """

    show_node_id: bool = False
    show_chatflow_id: bool = False
    show_tokens: bool = False
    show_gen_time: bool = False
    show_gen_speed: bool = False
    show_worknode_model: bool = False


class WorkspaceSettings(BaseModel):
    """Settings payload stored in ``workspaces.payload``."""

    tool_states: dict[str, ToolState] = Field(default_factory=dict)
    #: UI + built-in prompt language for this workspace. Drives which
    #: ``fixtures/<lang>/`` variant the engine picks when resolving
    #: planner / judge / worker / compact templates. The frontend
    #: mirrors this to its i18n runtime on boot and pushes changes
    #: back via PATCH.
    language: WorkspaceLanguage = "en-US"
    #: Canvas / display toggles. Default everything off so a fresh
    #: workspace renders the minimum-clutter view; the user opts into
    #: each overlay (node id, chatflow id, token counts, generation
    #: timing/speed, WorkNode model badges) via the global Settings →
    #: Canvas tab.
    canvas_prefs: CanvasPrefs = Field(default_factory=CanvasPrefs)
    #: Workspace-wide trust toggle for the M7.5 PR 8 cross-chatflow
    #: read scope. ``get_node_context(scope='cross_chatflow')`` is
    #: gated by the virtual capability ``get_node_context.cross_chatflow``
    #: on the calling WorkNode's effective_tools — but no production
    #: path writes that cap. This setting is the production grant
    #: path: when ``True`` the engine adds the virtual cap to every
    #: tool call's caller context within the workspace, so
    #: ``get_node_context`` honors cross-chatflow lookups. Default
    #: ``False`` keeps the pre-PR-8 boundary (a chatflow's agents
    #: can only read inside their own chatflow). The toggle is
    #: workspace-scoped because the trust boundary is the tenant —
    #: per-task gating belongs to the not-yet-activated effective_tools
    #: whitelist instead.
    allow_cross_chatflow_lookup: bool = False

    def state_for(self, tool_name: str) -> ToolState:
        """Return the stored state, falling back to the built-in
        default, or :attr:`ToolState.DEFAULT_ALLOW` for unknown tools
        (MCP tools default to visible until explicitly configured)."""
        if tool_name in self.tool_states:
            return self.tool_states[tool_name]
        return BUILTIN_DEFAULT_STATES.get(tool_name, ToolState.DEFAULT_ALLOW)

    def globally_disabled(self) -> set[str]:
        """Tool names explicitly set to ``disabled``."""
        return {name for name, st in self.tool_states.items() if st == ToolState.DISABLED}

    def pre_disabled_for_new_chatflow(self, known_tool_names: list[str]) -> list[str]:
        """Tool names a fresh ChatFlow's ``disabled_tool_names`` should
        pre-list. Every tool whose effective state is ``available`` or
        ``disabled`` lands in here; ``default_allow`` ones don't.
        """
        out: list[str] = []
        for name in known_tool_names:
            st = self.state_for(name)
            if st in (ToolState.AVAILABLE, ToolState.DISABLED):
                out.append(name)
        return out
