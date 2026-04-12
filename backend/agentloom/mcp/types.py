"""MCP server configuration schemas.

The MVP supports two connection kinds:

* ``http`` — remote MCP server reached over streamable HTTP (this is how
  Tavily is exposed). Needs ``url`` + optional ``headers``.
* ``stdio`` — subprocess launched by us speaking MCP over stdio. Needs
  ``command`` + ``args`` + optional ``env``.

Server rows are workspace-scoped like every other user config; see
``agentloom.db.models.tenancy.DEFAULT_WORKSPACE_ID`` for the singleton
workspace we target in M7.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, model_validator

from agentloom.schemas.common import generate_node_id, utcnow


class MCPServerKind(str, Enum):
    HTTP = "http"
    STDIO = "stdio"


class MCPServerConfig(BaseModel):
    """One registered MCP server.

    ``server_id`` is a short stable identifier (``tavily``, ``github``) —
    it becomes part of every tool name we surface to the LLM so multiple
    servers can coexist without collision.
    """

    id: str = Field(default_factory=generate_node_id)
    server_id: str
    friendly_name: str
    kind: MCPServerKind

    # http kind
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)

    # stdio kind
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    enabled: bool = True
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> "MCPServerConfig":
        if self.kind == MCPServerKind.HTTP:
            if not self.url:
                raise ValueError("http MCP server requires 'url'")
            if self.command is not None:
                raise ValueError("http MCP server must not set 'command'")
        elif self.kind == MCPServerKind.STDIO:
            if not self.command:
                raise ValueError("stdio MCP server requires 'command'")
            if self.url is not None:
                raise ValueError("stdio MCP server must not set 'url'")
        # server_id must be a safe identifier — it lands inside a tool
        # name the LLM sees, which must match [A-Za-z_][A-Za-z0-9_]*.
        if not self.server_id.replace("_", "").isalnum():
            raise ValueError(
                f"server_id {self.server_id!r} must be alphanumeric/underscore only"
            )
        return self
