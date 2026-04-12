"""Shared types used by both ChatFlow and WorkFlow."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

import uuid_utils
from pydantic import BaseModel, Field

NodeId = str  # UUIDv7 string; sortable by creation time


def generate_node_id() -> NodeId:
    """Return a freshly minted UUIDv7 string."""
    return str(uuid_utils.uuid7())


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class NodeStatus(str, Enum):
    """Lifecycle of a node.

    Transitions are:

        planned → running → succeeded | failed | cancelled
                        ↘ waiting_for_rate_limit → running
                        ↘ retrying → running

    ``succeeded`` and ``failed`` are terminal (frozen). See §4.1 of
    ``docs/requirements.md``.
    """

    PLANNED = "planned"
    RUNNING = "running"
    WAITING_FOR_RATE_LIMIT = "waiting_for_rate_limit"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.CANCELLED}

    @property
    def is_frozen(self) -> bool:
        """Frozen nodes are immutable — see §4.1."""
        return self in {NodeStatus.SUCCEEDED, NodeStatus.FAILED}


class StepKind(str, Enum):
    """Kind of work a WorkFlowNode does."""

    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    SUB_AGENT_DELEGATION = "sub_agent_delegation"


class EditProvenance(str, Enum):
    """Who authored an editable field. See §4.4 of requirements."""

    PURE_USER = "pure_user"  # green
    PURE_AGENT = "pure_agent"  # blue
    MIXED = "mixed"  # purple
    UNSET = "unset"


class EditableText(BaseModel):
    """Rich text field with edit provenance.

    MVP stores attribution at field-level granularity (one tag per field).
    Character-level spans are a v2+ concern — see ADR-015 (implicit) in
    requirements §4.4.
    """

    text: str = ""
    provenance: EditProvenance = EditProvenance.UNSET
    updated_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def by_user(cls, text: str) -> "EditableText":
        return cls(text=text, provenance=EditProvenance.PURE_USER)

    @classmethod
    def by_agent(cls, text: str) -> "EditableText":
        return cls(text=text, provenance=EditProvenance.PURE_AGENT)

    def edited_by_user(self, new_text: str) -> "EditableText":
        """Return a new EditableText after a user edit.

        If we were pure_agent, we become mixed. If pure_user or unset, we
        remain pure_user. Mixed stays mixed.
        """
        new_prov: EditProvenance
        if self.provenance == EditProvenance.PURE_AGENT:
            new_prov = EditProvenance.MIXED
        else:
            new_prov = EditProvenance.PURE_USER
        return EditableText(text=new_text, provenance=new_prov, updated_at=utcnow())

    def edited_by_agent(self, new_text: str) -> "EditableText":
        """Return a new EditableText after an auto-planner edit."""
        new_prov: EditProvenance
        if self.provenance == EditProvenance.PURE_USER:
            new_prov = EditProvenance.MIXED
        else:
            new_prov = EditProvenance.PURE_AGENT
        return EditableText(text=new_text, provenance=new_prov, updated_at=utcnow())


class ProviderModelRef(BaseModel):
    """A reference to a specific model on a specific configured provider."""

    provider_id: str
    model_id: str


class ToolConstraints(BaseModel):
    """Per-node allow/deny list for tools.

    Wildcards use glob-like syntax handled at resolve time:

        "Bash"               -> all bash commands
        "Bash(git *)"        -> bash starting with "git "
        "McpTool(tavily, *)" -> any tavily mcp tool
    """

    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class NodeBase(BaseModel):
    """Fields common to both ChatFlowNode and WorkFlowNode."""

    id: NodeId = Field(default_factory=generate_node_id)
    parent_ids: list[NodeId] = Field(default_factory=list)
    description: EditableText = Field(default_factory=EditableText)
    expected_outcome: EditableText | None = None
    status: NodeStatus = NodeStatus.PLANNED
    model_override: ProviderModelRef | None = None
    locked: bool = False
    error: str | None = None

    # Canvas position — persisted so layouts survive page reload.
    position_x: float | None = None
    position_y: float | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_dashed(self) -> bool:
        """Pre-execution placeholder — opposite of frozen/solid."""
        return self.status in {NodeStatus.PLANNED, NodeStatus.WAITING_FOR_RATE_LIMIT}

    @property
    def is_frozen(self) -> bool:
        return self.status.is_frozen

    def require_mutable(self) -> None:
        """Raise if the node is frozen. Call at the top of any mutation path."""
        if self.is_frozen:
            raise FrozenNodeError(f"Node {self.id} is frozen ({self.status.value})")
        if self.locked:
            # Note: locked nodes CAN be user-edited; engine/auto-planner must
            # check this separately. This helper is the frozen-guard only.
            pass


class FrozenNodeError(Exception):
    """Raised when mutating a frozen node. See §4.1."""


class CycleError(Exception):
    """Raised when an edge would create a DAG cycle."""


class NodeHasReferencesError(Exception):
    """Raised when deleting a node that is still referenced by children."""


class ToolUse(BaseModel):
    """An assistant request to call a tool (stored on llm_call output)."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Result of a tool invocation (stored on tool_call output)."""

    content: str
    is_error: bool = False
    attachments: list[str] = Field(default_factory=list)  # blob refs


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
