"""Default ToolRegistry factory — assembles the built-in tools."""

from __future__ import annotations

from agentloom.tools.base import ToolRegistry
from agentloom.tools.bash import BashTool
from agentloom.tools.files import EditTool, ReadTool, WriteTool
from agentloom.tools.memoryboard_lookup import MemoryBoardLookupTool
from agentloom.tools.node_context import GetNodeContextTool
from agentloom.tools.search import GlobTool, GrepTool


def default_registry() -> ToolRegistry:
    """Return a fresh registry populated with Bash, Read, Write, Edit,
    Glob, Grep, get_node_context, and memoryboard_lookup. Each call
    returns a new instance so tests and production can have isolated
    state.

    ``get_node_context`` and ``memoryboard_lookup`` coexist during the
    PR 2 migration window — the former returns raw node bodies, the
    latter returns the short description distilled by the brief
    WorkNode. PR 4+ will retire the raw-body path.
    """
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadTool())
    reg.register(WriteTool())
    reg.register(EditTool())
    reg.register(GlobTool())
    reg.register(GrepTool())
    reg.register(GetNodeContextTool())
    reg.register(MemoryBoardLookupTool())
    return reg
