"""Tool ABC + registry + constraint matching.

Every tool subclasses ``Tool`` and provides:
- ``name`` — the identifier the LLM uses in ``tool_use.name``
- ``description`` — plain English, inserted into the JSONSchema for the LLM
- ``parameters`` — JSONSchema dict for the arguments
- ``execute(args, context)`` — the async implementation

The ``ToolRegistry`` owns a set of tools and knows how to filter them
for an agent given a ``ToolConstraints``.

Constraint syntax (see requirements §4.6 and
``schemas.common.ToolConstraints``):

    "Bash"               -> all bash commands
    "Bash(git *)"        -> any bash command whose first word is "git"
    "McpTool(tavily, *)" -> any tavily mcp tool (handled in M7)

The parenthesized tail is a glob matched against a *tool-specific*
detail string that the tool reports via ``detail_for_constraints``.
For Bash that's the full command string; for Read/Write/Edit it's the
target path; for Glob/Grep it's the pattern.
"""

from __future__ import annotations

import fnmatch
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agentloom.schemas.common import ToolConstraints, ToolResult


class ToolError(Exception):
    """Raised by a tool to signal a user-visible execution failure.

    Engine catches this, freezes the node as ``ToolResult(is_error=True)``,
    and lets the LLM see the error message on the next turn.
    """


@dataclass
class ToolContext:
    """Ambient state passed to every ``Tool.execute`` call.

    Most tools don't need any of this, but Bash wants the working
    directory, and M7's MCP tools will want the workspace id for
    resource isolation.
    """

    workspace_id: str = "default"
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    #: Node ids that tools have pulled into the current ChatNode's view
    #: via ``get_node_context``. The engine reads this after each
    #: ChatNode turn to update :attr:`CompactSnapshot.sticky_restored` —
    #: every hit becomes (or refreshes) a sticky entry with counter =
    #: compact_preserve_recent_turns; every turn that doesn't re-touch
    #: an entry decrements its counter. Empty set means "nothing was
    #: restored this turn". Managed by the engine per-ChatNode: reset
    #: to a fresh set before each turn, drained after.
    accessed_node_ids: set[str] = field(default_factory=set)


class Tool(ABC):
    """Abstract base for every built-in tool."""

    #: Identifier the LLM sees in ``tool_use.name``.
    name: str = ""
    #: One-paragraph description shown to the LLM.
    description: str = ""
    #: JSONSchema for the ``arguments`` object.
    parameters: dict[str, Any] = {}

    @abstractmethod
    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Run the tool and return a serializable result.

        Raise ``ToolError`` for expected failures (non-zero exit, file
        not found, pattern mismatch). Unexpected exceptions propagate
        up to the engine and are translated into ``is_error=True``
        results there.
        """

    def definition(self) -> dict[str, Any]:
        """Return the OpenAI/Anthropic-shaped tool definition."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        """Return the substring used for ``Name(...)`` glob matching.

        Default: empty string — only the bare name is checked. Tools
        that care about argument-level filtering (Bash, Read, etc)
        override this.
        """
        return ""


# -------------------------------------------------------------------- Registry


_CONSTRAINT_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\((?P<detail>.*)\))?$")


def _parse_constraint(expr: str) -> tuple[str, str | None]:
    m = _CONSTRAINT_RE.match(expr.strip())
    if not m:
        raise ValueError(f"invalid tool constraint expression {expr!r}")
    return m.group("name"), m.group("detail")


def _matches(expr: str, tool_name: str, detail: str) -> bool:
    """Does ``expr`` match the tool+detail pair?"""
    expr_name, expr_detail = _parse_constraint(expr)
    if expr_name != tool_name:
        return False
    if expr_detail is None:
        return True
    return fnmatch.fnmatchcase(detail, expr_detail)


class ToolRegistry:
    """Owns a set of tools and enforces per-call allow/deny."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError(f"tool {type(tool).__name__} has empty name")
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if removed, False if not
        present. Used by MCP runtime when a server is disconnected so
        its tools no longer appear in LLM prompts."""
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool {name!r}") from exc

    def has(self, name: str) -> bool:
        """Return True iff *name* is a registered tool. Ignores
        visibility constraints — callers that need constraint-aware
        checks should use :meth:`definitions_for_constraints` instead."""
        return name in self._tools

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def definitions_for_constraints(
        self, constraints: ToolConstraints | None
    ) -> list[dict[str, Any]]:
        """Return LLM-facing tool definitions visible under these constraints.

        This drives what the LLM *sees* — the execute-time allow/deny
        check in ``check_call`` is the enforcing gate.
        """
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            if self._visible(t, constraints):
                out.append(t.definition())
        return out

    def _visible(self, tool: Tool, constraints: ToolConstraints | None) -> bool:
        if constraints is None:
            return True
        if constraints.deny:
            for d in constraints.deny:
                expr_name, expr_detail = _parse_constraint(d)
                # A bare "Bash" in deny hides the tool entirely.
                if expr_name == tool.name and expr_detail is None:
                    return False
        if not constraints.allow:
            return True  # allow-list empty means "allow all"
        for a in constraints.allow:
            if _parse_constraint(a)[0] == tool.name:
                return True
        return False

    def check_call(
        self,
        tool_name: str,
        args: dict[str, Any],
        constraints: ToolConstraints | None,
    ) -> None:
        """Raise ``ToolError`` if the call is forbidden.

        This is the enforcing gate — called at execute time. Deny wins
        over allow; an empty allow list means "allow anything not
        explicitly denied".
        """
        tool = self.get(tool_name)
        detail = tool.detail_for_constraints(args)

        if constraints is None:
            return

        for d in constraints.deny:
            if _matches(d, tool_name, detail):
                raise ToolError(
                    f"tool call {tool_name}({detail!r}) denied by constraint {d!r}"
                )

        if not constraints.allow:
            return

        for a in constraints.allow:
            if _matches(a, tool_name, detail):
                return

        raise ToolError(
            f"tool call {tool_name}({detail!r}) not in allow list {constraints.allow}"
        )

    async def execute(
        self,
        tool_name: str,
        args: dict[str, Any],
        ctx: ToolContext,
        constraints: ToolConstraints | None = None,
    ) -> ToolResult:
        """Run a tool end-to-end with constraint check + error translation.

        Both constraint-check failures AND tool-runtime ``ToolError``
        are translated into ``ToolResult(is_error=True)`` so the caller
        (usually the engine) sees a uniform return shape — the LLM can
        then observe the error on its next turn and retry or apologize.
        """
        try:
            self.check_call(tool_name, args, constraints)
            tool = self.get(tool_name)
            return await tool.execute(args, ctx)
        except ToolError as exc:
            return ToolResult(content=str(exc), is_error=True)
