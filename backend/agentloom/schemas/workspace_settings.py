"""Workspace-scoped settings — currently only tool states.

Stored in ``workspaces.payload`` as a plain JSON bag so new settings
can land without schema churn. :class:`WorkspaceSettings` is the
Pydantic shape over that bag.
"""

from __future__ import annotations

from enum import Enum

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


class WorkspaceSettings(BaseModel):
    """Settings payload stored in ``workspaces.payload``."""

    tool_states: dict[str, ToolState] = Field(default_factory=dict)

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
