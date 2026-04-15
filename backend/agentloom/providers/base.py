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
        temperature: float = 0.0,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
        on_token: TokenCallback | None = None,
    ) -> ChatResponse:
        """Call the chat completions endpoint and return a typed response.

        When ``on_token`` is supplied the adapter SHOULD use the
        provider's streaming endpoint and invoke the callback as each
        text fragment arrives. The final return value is still the
        fully assembled ``ChatResponse``.
        """

    @abstractmethod
    async def list_models(self) -> list[str]:
        """Return model IDs the provider exposes (best effort)."""

    async def close(self) -> None:
        """Release any open network resources."""
