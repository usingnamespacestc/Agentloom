"""``Bash`` tool — run a shell command with timeout + captured output."""

from __future__ import annotations

import asyncio
from typing import Any

from agentloom.schemas.common import ToolResult
from agentloom.tools.base import SideEffect, Tool, ToolContext, ToolError


class BashTool(Tool):
    name = "Bash"
    # Conservative: arbitrary shell commands can mutate FS / network /
    # processes. M7.5 ShellSkill split (deferred) will hand-pick a
    # read-only subset; for now Bash stays WRITE.
    side_effect = SideEffect.WRITE
    description = (
        "Execute a single shell command. On Linux/macOS the host shell "
        "is /bin/sh (POSIX commands: ls, cat, grep, ...); on Windows "
        "native it falls back to cmd.exe (dir, type, findstr, ...). "
        "Adapt your command to the OS reported in the runtime "
        "environment system message. Captures stdout, stderr, and exit "
        "code. Use for one-off commands; do not pipe interactive "
        "programs. The working directory is fixed for the session and "
        "cannot be changed with cd (use absolute paths)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Kill the process after this many seconds (default 30, max 600)",
                "default": 30,
                "minimum": 1,
                "maximum": 600,
            },
        },
        "required": ["command"],
    }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        return str(args.get("command", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = args.get("command")
        if not command or not isinstance(command, str):
            raise ToolError("Bash: 'command' must be a non-empty string")

        timeout = int(args.get("timeout_seconds", 30))
        timeout = max(1, min(timeout, 600))

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=ctx.cwd,
            env={**ctx.env} if ctx.env else None,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise ToolError(
                f"Bash: command timed out after {timeout}s: {command[:80]}"
            ) from None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode or 0

        pieces = []
        if stdout:
            pieces.append(stdout)
        if stderr:
            pieces.append(f"[stderr]\n{stderr}")
        pieces.append(f"[exit code {exit_code}]")
        return ToolResult(content="\n".join(pieces), is_error=exit_code != 0)
