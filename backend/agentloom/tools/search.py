"""Filesystem search tools: Glob, Grep."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agentloom.schemas.common import ToolResult
from agentloom.tools.base import SideEffect, Tool, ToolContext, ToolError


class GlobTool(Tool):
    name = "Glob"
    side_effect = SideEffect.READ
    description = (
        "Find files matching a glob pattern (e.g. 'src/**/*.py'). "
        "Returns newline-separated absolute paths, sorted by "
        "modification time descending. Capped at 250 matches."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {
                "type": "string",
                "description": "Root directory to search under. Defaults to cwd.",
            },
        },
        "required": ["pattern"],
    }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("Glob: 'pattern' is required")

        root_str = args.get("path") or ctx.cwd
        root = Path(str(root_str)).expanduser()
        if not root.is_absolute():
            root = (Path(ctx.cwd) / root).resolve()
        if not root.exists():
            raise ToolError(f"Glob: root directory not found: {root}")

        matches = sorted(
            [p for p in root.glob(pattern) if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        capped = matches[:250]
        content = "\n".join(str(p) for p in capped)
        if len(matches) > 250:
            content += f"\n[... {len(matches) - 250} more match(es) truncated]"
        return ToolResult(content=content or "(no matches)")


class GrepTool(Tool):
    name = "Grep"
    side_effect = SideEffect.READ
    description = (
        "Search file contents for a regex pattern. Returns "
        "file:line:content triples, capped at 250 matches. Optionally "
        "filter by glob (e.g. '*.py'). The 'pattern' is a Python "
        "regular expression."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {
                "type": "string",
                "description": "Root directory to search under. Defaults to cwd.",
            },
            "glob": {
                "type": "string",
                "description": "Glob filter applied to file names (e.g. '*.py').",
            },
            "case_insensitive": {"type": "boolean", "default": False},
        },
        "required": ["pattern"],
    }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        return str(args.get("pattern", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("Grep: 'pattern' is required")

        flags = re.IGNORECASE if args.get("case_insensitive") else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            raise ToolError(f"Grep: invalid regex: {exc}") from exc

        root_str = args.get("path") or ctx.cwd
        root = Path(str(root_str)).expanduser()
        if not root.is_absolute():
            root = (Path(ctx.cwd) / root).resolve()
        if not root.exists():
            raise ToolError(f"Grep: root directory not found: {root}")

        glob = args.get("glob") or "**/*"
        candidates = [p for p in root.glob(glob) if p.is_file()]

        hits: list[str] = []
        total = 0
        for file_path in candidates:
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    total += 1
                    if len(hits) < 250:
                        hits.append(f"{file_path}:{lineno}:{line}")
            if len(hits) >= 250 and total > 250:
                break

        if not hits:
            return ToolResult(content="(no matches)")
        content = "\n".join(hits)
        if total > 250:
            content += f"\n[... {total - 250} more match(es) truncated]"
        return ToolResult(content=content)
