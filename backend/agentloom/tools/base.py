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

import contextvars
import enum
import fnmatch
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from agentloom.schemas.common import ToolConstraints, ToolResult


class SideEffect(str, enum.Enum):
    """How a tool affects external state. Drives M7.5's capability
    model: cognitive nodes (judges) get only ``NONE`` / ``READ`` tools
    by default, ``WRITE`` is reserved for execution nodes.

    PR 1 only adds the metadata. Engine consumers (registry filter,
    capability model defaults) come in PR 2-3 — for now this is pure
    documentation that future passes will read.
    """

    #: No external resources touched; pure compute / in-memory query.
    #: Rare in practice — most "lookup" tools talk to the DB so they're
    #: ``READ`` not ``NONE``.
    NONE = "none"
    #: Read external state (filesystem read, HTTP GET, registry
    #: lookup). Idempotent, safe to call from cognitive nodes.
    READ = "read"
    #: Modify external state (filesystem write, HTTP POST, exec, DB
    #: mutation). Only ``execution`` (Layer-2 ``draft`` / ``tool_call``
    #: / ``delegate``) nodes should see these by default.
    WRITE = "write"


class ToolError(Exception):
    """Raised by a tool to signal a user-visible execution failure.

    Engine catches this, freezes the node as ``ToolResult(is_error=True)``,
    and lets the LLM see the error message on the next turn.
    """


# Task-local binding for the "nodes fetched this turn" signal. The
# engine opens a fresh scope around each ChatNode's inner-workflow
# execution (see :meth:`ChatFlowEngine._execute_node`) so concurrent
# sibling ChatNodes don't pour into the same set. Tools write into the
# currently-bound set via :func:`record_accessed_node_id` instead of
# mutating ``ctx.accessed_node_ids`` directly, which keeps the ctx
# field as a compat fallback for callers that don't open a scope
# (bare-unit tests, MCP tool runners, etc.).
_accessed_var: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar(
    "agentloom.tools.accessed_node_ids", default=None
)


@contextmanager
def accessed_scope() -> Iterator[set[str]]:
    """Bind a fresh ``accessed_node_ids`` set for the current asyncio task.

    Tools executing inside this block write to the yielded set via
    :func:`record_accessed_node_id`. contextvars are task-local, so
    concurrent sibling tasks opening their own scopes don't see each
    other's writes — the engine relies on this to route
    ``get_node_context`` hits back to the correct ChatNode.
    """
    scope: set[str] = set()
    token = _accessed_var.set(scope)
    try:
        yield scope
    finally:
        _accessed_var.reset(token)


def record_accessed_node_id(ctx: "ToolContext", node_id: str) -> None:
    """Record that *node_id* was just fetched by a tool.

    Writes to the task-local scope if one is bound (the normal engine
    path); otherwise falls back to ``ctx.accessed_node_ids`` so direct
    tool invocations outside the engine (tests, ad-hoc scripts) still
    have an inspectable trail.
    """
    scope = _accessed_var.get()
    if scope is not None:
        scope.add(node_id)
        return
    ctx.accessed_node_ids.add(node_id)


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
    #: ChatNode turn to update :attr:`ChatFlowNode.sticky_restored` —
    #: every hit becomes (or refreshes) a sticky entry with counter =
    #: recalled_context_sticky_turns; every turn that doesn't re-touch
    #: an entry decrements its counter. Empty set means "nothing was
    #: restored this turn". Concurrent sibling ChatNodes use the
    #: ``accessed_scope`` contextvar to keep their per-turn sets
    #: isolated even though the inner WorkFlowEngine's tool_ctx is
    #: shared; this field is the fallback path for bare-test usage.
    accessed_node_ids: set[str] = field(default_factory=set)


class Tool(ABC):
    """Abstract base for every built-in tool."""

    #: Identifier the LLM sees in ``tool_use.name``.
    name: str = ""
    #: One-paragraph description shown to the LLM.
    description: str = ""
    #: JSONSchema for the ``arguments`` object.
    parameters: dict[str, Any] = {}
    #: How this tool affects external state. Default ``WRITE`` is
    #: deliberately conservative: a new tool that forgot to opt in
    #: stays out of cognitive-node ``effective_tools`` (M7.5 default
    #: filter for judges = NONE/READ only). Override on the subclass:
    #: read-only tools set ``side_effect = SideEffect.READ``.
    #: PR 1 (commit pending) only stores the field; consumer
    #: filtering lands in PR 3.
    side_effect: SideEffect = SideEffect.WRITE

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

        .. note::
            Pre-M7.5 entry point. Engine callers should prefer
            :meth:`resolve_for_node`, which folds chatflow-disabled +
            ``effective_tools`` whitelist + side-effect filter into the
            same pass. This method survives for tests and for the legacy
            "no capability model" code path.
        """
        out: list[dict[str, Any]] = []
        for t in self._tools.values():
            if self._visible(t, constraints):
                out.append(t.definition())
        return out

    def resolve_for_node(
        self,
        *,
        node_effective: list[str] | None,
        chatflow_disabled: frozenset[str] = frozenset(),
        side_effect_filter: set[SideEffect] | None = None,
        legacy_constraints: ToolConstraints | None = None,
    ) -> list["Tool"]:
        """M7.5 unified pipeline: resolve which tools a WorkNode may call.

        Pipeline (each filter is independent and applied in order):

        1. **chatflow_disabled** — the workspace/chatflow toggled this
           tool off entirely. Drops the tool unconditionally.
        2. **node_effective** (whitelist) — when not ``None``, only
           tools whose name appears in the list survive. ``[]`` means
           "no tools". ``None`` means "fall through to legacy behavior"
           (= pre-M7.5 chatflow path; engine schedules without an
           effective_tools allocation).
        3. **side_effect_filter** — when set, drops tools whose
           ``side_effect`` is not in the set. Cognitive nodes (judges,
           planner) pass ``{NONE, READ}`` so they can't call WRITE tools
           even if a careless prompt asks for one.
        4. **legacy_constraints** — when set, the existing allow/deny
           ``ToolConstraints`` glob check (the same logic
           ``definitions_for_constraints`` uses). Kept for nodes that
           still attach explicit allow/deny lists alongside the M7.5
           whitelist; both gates apply (intersection).

        Returns surviving :class:`Tool` objects in registration order so
        the engine can render their ``.definition()`` for the provider.
        """
        whitelist: set[str] | None = (
            set(node_effective) if node_effective is not None else None
        )
        out: list[Tool] = []
        for tool in self._tools.values():
            if tool.name in chatflow_disabled:
                continue
            if whitelist is not None and tool.name not in whitelist:
                continue
            if side_effect_filter is not None and tool.side_effect not in side_effect_filter:
                continue
            if legacy_constraints is not None and not self._visible(tool, legacy_constraints):
                continue
            out.append(tool)
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
