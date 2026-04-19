"""Default ToolRegistry factory — assembles the built-in tools."""

from __future__ import annotations

from agentloom.tools.base import ToolRegistry
from agentloom.tools.bash import BashTool
from agentloom.tools.files import EditTool, ReadTool, WriteTool
from agentloom.tools.node_context import GetNodeContextTool
from agentloom.tools.search import GlobTool, GrepTool


def default_registry() -> ToolRegistry:
    """Return a fresh registry populated with Bash, Read, Write, Edit,
    Glob, Grep, and get_node_context. Each call returns a new instance
    so tests and production can have isolated state.
    """
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadTool())
    reg.register(WriteTool())
    reg.register(EditTool())
    reg.register(GlobTool())
    reg.register(GrepTool())
    reg.register(GetNodeContextTool())
    return reg
