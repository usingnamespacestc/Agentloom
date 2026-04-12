"""LLM provider adapters.

Contract: adapters MUST preserve the order of input messages. Reordering
breaks prefix-based KV cache on every provider we target. See ADR-013.
"""

from agentloom.providers.anthropic_native import AnthropicNativeAdapter
from agentloom.providers.base import ProviderAdapter, ProviderError
from agentloom.providers.openai_compat import OpenAICompatAdapter
from agentloom.providers.registry import build_adapter
from agentloom.providers.types import (
    AssistantMessage,
    ChatResponse,
    Message,
    MessageRole,
    SystemMessage,
    TokenUsage,
    ToolDefinition,
    ToolMessage,
    ToolUse,
    UserMessage,
)

__all__ = [
    "AnthropicNativeAdapter",
    "AssistantMessage",
    "ChatResponse",
    "Message",
    "MessageRole",
    "OpenAICompatAdapter",
    "ProviderAdapter",
    "ProviderError",
    "SystemMessage",
    "TokenUsage",
    "ToolDefinition",
    "ToolMessage",
    "ToolUse",
    "UserMessage",
    "build_adapter",
]
