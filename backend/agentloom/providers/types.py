"""Provider-agnostic message and response types.

These mirror the OpenAI Chat Completions shape as the lowest common denominator,
with added fields for Anthropic-native adapters (cache_control markers, etc.)
carried as provider-specific extras on ``Message.extras``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

MessageRole = Literal["system", "user", "assistant", "tool"]


class ToolDefinition(BaseModel):
    """A tool the model may invoke.

    ``parameters`` must be a JSON Schema object. The name/description map
    cleanly to both OpenAI ``function`` and Anthropic ``tool`` shapes.
    """

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolUse(BaseModel):
    """An assistant's request to call a tool."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class SystemMessage(BaseModel):
    role: Literal["system"] = "system"
    content: str
    cache_breakpoint: bool = False  # Anthropic-only hint
    extras: dict[str, Any] = Field(default_factory=dict)


class UserMessage(BaseModel):
    role: Literal["user"] = "user"
    content: str
    extras: dict[str, Any] = Field(default_factory=dict)


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str = ""
    tool_uses: list[ToolUse] = Field(default_factory=list)
    extras: dict[str, Any] = Field(default_factory=dict)


class ToolMessage(BaseModel):
    role: Literal["tool"] = "tool"
    tool_use_id: str
    content: str
    is_error: bool = False
    extras: dict[str, Any] = Field(default_factory=dict)


Message = SystemMessage | UserMessage | AssistantMessage | ToolMessage


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        if self.prompt_tokens == 0:
            return 0.0
        return self.cached_tokens / self.prompt_tokens


FinishReason = Literal["stop", "length", "tool_use", "content_filter", "error", "unknown"]


class ChatResponse(BaseModel):
    """A single completion from a provider."""

    model: str
    message: AssistantMessage
    usage: TokenUsage
    finish_reason: FinishReason = "stop"
    provider_raw: dict[str, Any] = Field(default_factory=dict)
