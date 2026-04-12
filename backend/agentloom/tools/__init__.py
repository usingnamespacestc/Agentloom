"""Built-in tool library — Bash, Read, Write, Edit, Glob, Grep.

M6: an in-process tool executor invoked from the engine whenever an
llm_call emits ``tool_uses``. Each tool is a subclass of ``Tool`` with
a JSONSchema surface the LLM sees and an async ``execute`` that runs
server-side and returns a ``ToolResult``.

Security posture for MVP:
- Tools run in the API process (no sandbox); only enable on trusted
  workspaces.
- ``ToolConstraints`` (allow/deny globs) are enforced by the registry
  before ``execute`` is called — a denied tool can never run, even if
  the LLM asks.
- ``Bash`` is the only tool that shells out; it accepts a single string
  and runs it via ``asyncio.create_subprocess_shell`` with a timeout.
  Tests must not invoke arbitrary bash — use the per-tool unit tests.
"""

from agentloom.tools.base import Tool, ToolContext, ToolError, ToolRegistry
from agentloom.tools.registry import default_registry

__all__ = [
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "default_registry",
]
