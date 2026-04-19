"""Provider adapter base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from agentloom.providers.types import ChatResponse, Message, ToolDefinition

#: Optional per-token callback. Invoked with each text fragment as it
#: arrives from the provider's SSE stream so the engine can republish
#: a live preview to the frontend. ``None`` means non-streaming —
#: adapters that don't support streaming must still accept the
#: parameter but may ignore it.
TokenCallback = Callable[[str], Awaitable[None]]


class ProviderError(Exception):
    """Raised when a provider call fails after all retries."""

    def __init__(self, message: str, *, status_code: int | None = None, raw: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.raw = raw


class ProviderAdapter(ABC):
    """Abstract base for all LLM provider adapters.

    **Contract.** Implementations MUST NOT reorder the input messages. KV
    cache prefixes on every supported provider are order-sensitive; a reorder
    here costs the user money and latency. See ADR-013.
    """

    provider_kind: str

    def __init__(self, *, friendly_name: str, base_url: str, api_key: str | None) -> None:
        self.friendly_name = friendly_name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    @abstractmethod
    async def chat(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        repetition_penalty: float | None = None,
        num_ctx: int | None = None,
        thinking_budget_tokens: int | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
        on_token: TokenCallback | None = None,
        json_mode: str | None = None,
        json_schema: dict[str, Any] | None = None,
        forced_tool_name: str | None = None,
    ) -> ChatResponse:
        """Call the chat completions endpoint and return a typed response.

        When ``on_token`` is supplied the adapter SHOULD use the
        provider's streaming endpoint and invoke the callback as each
        text fragment arrives. The final return value is still the
        fully assembled ``ChatResponse``.

        ``json_mode`` selects structured-output discipline:
        ``"schema"`` (full JSON Schema, requires ``json_schema``),
        ``"object"`` (free-form JSON object), or ``"none"`` / ``None``
        (prompt-only). The adapter translates this to whatever its wire
        protocol expects (OpenAI-compat: ``response_format``).

        ``forced_tool_name`` pins the model to a specific tool via the
        provider's tool_choice mechanism. When set, the model MUST call
        that tool rather than reply in free text. Used by the judge path
        to guarantee the verdict tool is invoked instead of being
        silently skipped in favor of an unstructured content reply.
        Adapters translate this to the provider-specific payload shape:
        OpenAI-compat uses ``tool_choice={"type":"function","function":{"name":...}}``;
        Anthropic uses ``tool_choice={"type":"tool","name":...}``.
        """

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return model IDs the provider exposes (best effort)."""

    async def close(self) -> None:
        """Release any open network resources."""
