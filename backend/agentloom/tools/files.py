"""File tools: Read, Write, Edit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentloom.schemas.common import ToolResult
from agentloom.tools.base import SideEffect, Tool, ToolContext, ToolError


def _resolve(ctx: ToolContext, path_str: str) -> Path:
    """Resolve a tool-supplied path against the context cwd.

    Not a security boundary — the MVP runs tools in-process on trusted
    workspaces. A future hardening pass will add a chroot/jail layer.
    """
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = (Path(ctx.cwd) / path).resolve()
    return path


class ReadTool(Tool):
    name = "Read"
    side_effect = SideEffect.READ
    description = (
        "Read a text file and return its contents. Supports an optional "
        "line-range window via 'offset' (1-based start line) and 'limit' "
        "(max lines returned). Line numbers are prefixed in the output."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 1, "default": 1},
            "limit": {"type": "integer", "minimum": 1, "default": 2000},
        },
        "required": ["file_path"],
    }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        return str(args.get("file_path", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise ToolError("Read: 'file_path' is required")

        path = _resolve(ctx, file_path)
        if not path.exists():
            raise ToolError(f"Read: file not found: {path}")
        if not path.is_file():
            raise ToolError(f"Read: not a regular file: {path}")

        offset = max(1, int(args.get("offset", 1)))
        limit = max(1, int(args.get("limit", 2000)))

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Read: cannot read {path}: {exc}") from exc

        lines = text.splitlines()
        window = lines[offset - 1 : offset - 1 + limit]
        numbered = "\n".join(f"{i + offset}\t{line}" for i, line in enumerate(window))
        return ToolResult(content=numbered)


class WriteTool(Tool):
    name = "Write"
    side_effect = SideEffect.WRITE  # explicit (matches default; documents intent)
    description = (
        "Write text to a file, creating it (and parent directories) if "
        "needed. Overwrites any existing content — prefer Edit for "
        "in-place modifications."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
    }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        return str(args.get("file_path", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args.get("file_path")
        content = args.get("content")
        if not isinstance(file_path, str) or not file_path:
            raise ToolError("Write: 'file_path' is required")
        if not isinstance(content, str):
            raise ToolError("Write: 'content' must be a string")

        path = _resolve(ctx, file_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Write: cannot write {path}: {exc}") from exc

        return ToolResult(content=f"wrote {len(content)} bytes to {path}")


class EditTool(Tool):
    name = "Edit"
    side_effect = SideEffect.WRITE  # explicit (matches default)
    description = (
        "Replace the first occurrence of 'old_string' with 'new_string' "
        "in a file. Set 'replace_all' to true to replace every "
        "occurrence. Errors if the old string is not found or is not "
        "unique (when replace_all is false)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
        "required": ["file_path", "old_string", "new_string"],
    }

    def detail_for_constraints(self, args: dict[str, Any]) -> str:
        return str(args.get("file_path", ""))

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file_path = args.get("file_path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))

        if not isinstance(file_path, str) or not file_path:
            raise ToolError("Edit: 'file_path' is required")
        if not isinstance(old_string, str):
            raise ToolError("Edit: 'old_string' must be a string")
        if not isinstance(new_string, str):
            raise ToolError("Edit: 'new_string' must be a string")
        if old_string == new_string:
            raise ToolError("Edit: old_string and new_string must differ")

        path = _resolve(ctx, file_path)
        if not path.exists():
            raise ToolError(f"Edit: file not found: {path}")

        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Edit: cannot read {path}: {exc}") from exc

        count = text.count(old_string)
        if count == 0:
            raise ToolError(f"Edit: old_string not found in {path}")
        if count > 1 and not replace_all:
            raise ToolError(
                f"Edit: old_string appears {count} times in {path}; "
                "provide a more specific string or set replace_all=true"
            )

        if replace_all:
            new_text = text.replace(old_string, new_string)
        else:
            new_text = text.replace(old_string, new_string, 1)

        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Edit: cannot write {path}: {exc}") from exc

        return ToolResult(
            content=f"edited {path} ({count} replacement{'s' if count != 1 else ''})"
        )
