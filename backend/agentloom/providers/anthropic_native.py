"""Anthropic native provider adapter.

Why a dedicated adapter instead of squeezing Anthropic into the OpenAI
compat shape? Three reasons:

1. **cache_control**. Anthropic's prompt caching needs per-block markers
   (``{"cache_control": {"type": "ephemeral"}}``) on the exact content
   blocks you want cached. The OpenAI shape has nowhere to express this,
   and prefix caching is a ~5× cost reduction for long tool-use loops,
   so we can't afford to skip it.

2. **Content blocks**. Messages carry arrays of blocks
   (``text``, ``tool_use``, ``tool_result``), not flat strings. Tool
   results are emitted as ``user`` messages with ``tool_result`` blocks,
   not as a separate ``tool`` role — so we have to re-group our
   intermediate ``ToolMessage`` list into Anthropic-shaped user turns.

3. **System prompt**. Anthropic takes ``system`` as a top-level field,
   not as a message. A compat shim would have to split the list every
   call; doing it once, here, is cleaner.

ADR-013 still applies: message order is preserved exactly. We never
reorder — we only merge adjacent tool results (which is legal because
they belong to the same user turn conceptually).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from agentloom.providers.base import ProviderAdapter, ProviderError, TokenCallback
from agentloom.providers.types import (
    AssistantMessage,
    ChatResponse,
    FinishReason,
    Message,
    SystemMessage,
    TokenUsage,
    ToolDefinition,
    ToolMessage,
    ToolUse,
    UserMessage,
)

_MAX_RETRIES = 3

#: Sent on every request per Anthropic's API contract. Bump together
#: with the ``anthropic-beta`` header when adopting a new version.
_ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicNativeAdapter(ProviderAdapter):
    """HTTP adapter for ``POST https://api.anthropic.com/v1/messages``.

    ``cache_system`` and ``cache_last_user`` default to True — the two
    Anthropic-recommended cache breakpoints that get most of the savings
    without over-fragmenting the cache. Callers who want manual control
    can set them to False and supply their own markers via
    ``extra={"cache_control_overrides": ...}``.
    """

    provider_kind = "anthropic_native"

    def __init__(
        self,
        *,
        friendly_name: str = "anthropic",
        base_url: str = "https://api.anthropic.com",
        api_key: str | None,
        cache_system: bool = True,
        cache_last_user: bool = True,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(friendly_name=friendly_name, base_url=base_url, api_key=api_key)
        self._cache_system = cache_system
        self._cache_last_user = cache_last_user
        self._extra_headers = extra_headers or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ headers

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "content-type": "application/json",
            "anthropic-version": _ANTHROPIC_API_VERSION,
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key
        headers.update(self._extra_headers)
        return headers

    # ------------------------------------------------------------------ wire format

    def _split_system(
        self, messages: list[Message]
    ) -> tuple[list[dict[str, Any]] | None, list[Message]]:
        """Pull every SystemMessage out into an Anthropic-shape system block list.

        Multiple system messages are concatenated (preserving order) into
        a single list of text blocks so ``cache_control`` can be applied
        to the whole prefix in one shot.
        """
        systems: list[SystemMessage] = [m for m in messages if isinstance(m, SystemMessage)]
        rest: list[Message] = [m for m in messages if not isinstance(m, SystemMessage)]
        if not systems:
            return None, rest

        blocks: list[dict[str, Any]] = []
        for i, sm in enumerate(systems):
            block: dict[str, Any] = {"type": "text", "text": sm.content}
            # Mark the *last* system block as a cache breakpoint when
            # caching is enabled — Anthropic recommends one marker at
            # the end of the stable prefix.
            is_last = i == len(systems) - 1
            if (is_last and self._cache_system) or sm.cache_breakpoint:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        return blocks, rest

    def _to_wire_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert the intermediate message list into Anthropic's shape.

        Key transformation: any run of ``ToolMessage`` entries is merged
        into a single ``user`` message whose content is a list of
        ``tool_result`` blocks. A trailing ``UserMessage`` is appended
        as a ``text`` block to the same user turn (this is rare but
        valid per the API).
        """
        wire: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if isinstance(m, UserMessage):
                wire.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": m.content}],
                    }
                )
                i += 1
            elif isinstance(m, AssistantMessage):
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tu in m.tool_uses:
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tu.id,
                            "name": tu.name,
                            "input": dict(tu.arguments),
                        }
                    )
                if not blocks:
                    # Anthropic rejects empty assistant turns — keep at
                    # least a whitespace text block to preserve alternation.
                    blocks.append({"type": "text", "text": " "})
                wire.append({"role": "assistant", "content": blocks})
                i += 1
            elif isinstance(m, ToolMessage):
                # Merge consecutive tool messages into one user turn.
                tool_blocks: list[dict[str, Any]] = []
                while i < len(messages) and isinstance(messages[i], ToolMessage):
                    tm: ToolMessage = messages[i]  # type: ignore[assignment]
                    tool_blocks.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tm.tool_use_id,
                            "content": tm.content,
                            "is_error": tm.is_error,
                        }
                    )
                    i += 1
                wire.append({"role": "user", "content": tool_blocks})
            else:  # pragma: no cover — SystemMessage filtered out upstream
                raise ValueError(f"unexpected message in body: {m!r}")

        # Apply cache_control to the last user block if enabled. We
        # mark the *final* content block of the last user turn — that
        # captures the most recent tool_result or text, which is the
        # prefix boundary for the next generation.
        if self._cache_last_user and wire:
            for entry in reversed(wire):
                if entry["role"] == "user" and entry["content"]:
                    entry["content"][-1]["cache_control"] = {"type": "ephemeral"}
                    break

        return wire

    @staticmethod
    def _to_wire_tools(tools: list[ToolDefinition] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters or {"type": "object", "properties": {}},
            }
            for t in tools
        ]

    @staticmethod
    def _parse_response(raw: dict[str, Any], fallback_model: str) -> ChatResponse:
        content_blocks = raw.get("content") or []
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_uses: list[ToolUse] = []
        for block in content_blocks:
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text") or "")
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking") or "")
            elif btype == "tool_use":
                tool_uses.append(
                    ToolUse(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        arguments=dict(block.get("input") or {}),
                    )
                )
            # Unknown block types (image, ...) silently dropped for
            # MVP; adding them later is additive.

        stop_reason = raw.get("stop_reason") or "end_turn"
        finish_map: dict[str, FinishReason] = {
            "end_turn": "stop",
            "max_tokens": "length",
            "tool_use": "tool_use",
            "stop_sequence": "stop",
        }
        finish: FinishReason = finish_map.get(stop_reason, "unknown")

        usage_raw = raw.get("usage") or {}
        # Anthropic splits cached tokens into creation vs read buckets;
        # surface the sum as ``cached_tokens`` and count uncached input
        # as ``prompt_tokens`` for compatibility with the common field.
        cache_creation = int(usage_raw.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage_raw.get("cache_read_input_tokens") or 0)
        input_tokens = int(usage_raw.get("input_tokens") or 0)
        output_tokens = int(usage_raw.get("output_tokens") or 0)
        usage = TokenUsage(
            prompt_tokens=input_tokens + cache_creation + cache_read,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + cache_creation + cache_read + output_tokens,
            cached_tokens=cache_read,
        )

        extras: dict[str, Any] = {}
        if thinking_parts:
            extras["thinking"] = "\n\n".join(thinking_parts)

        return ChatResponse(
            model=raw.get("model") or fallback_model,
            message=AssistantMessage(
                content="".join(text_parts),
                tool_uses=tool_uses,
                extras=extras,
            ),
            usage=usage,
            finish_reason=finish,
            provider_raw=raw,
        )

    # ------------------------------------------------------------------ public

    async def chat(
        self,
        *,
        model: str,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        presence_penalty: float | None = None,  # noqa: ARG002 — not in Anthropic API
        frequency_penalty: float | None = None,  # noqa: ARG002 — not in Anthropic API
        repetition_penalty: float | None = None,  # noqa: ARG002 — not in Anthropic API
        num_ctx: int | None = None,  # noqa: ARG002 — Ollama-only
        thinking_budget_tokens: int | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
        on_token: TokenCallback | None = None,  # noqa: ARG002 — streaming TBD
        json_mode: str | None = None,  # noqa: ARG002 — Anthropic uses tool_use for structured output
        json_schema: dict[str, Any] | None = None,  # noqa: ARG002
        forced_tool_name: str | None = None,
    ) -> ChatResponse:
        system_blocks, body = self._split_system(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_wire_messages(body),
            # Anthropic requires max_tokens; default to a reasonable
            # ceiling if the caller omits it.
            "max_tokens": max_tokens if max_tokens is not None else 4096,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if top_k is not None:
            payload["top_k"] = top_k
        if thinking_budget_tokens is not None:
            # Extended thinking mode. Anthropic requires temperature=1
            # when thinking is enabled, so drop any caller-set value.
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget_tokens,
            }
            payload.pop("temperature", None)
        if system_blocks is not None:
            payload["system"] = system_blocks
        wire_tools = self._to_wire_tools(tools)
        if wire_tools is not None:
            payload["tools"] = wire_tools
        # Anthropic's tool_choice shape differs from OpenAI's. Spec:
        # ``{"type": "tool", "name": "<tool_name>"}``. As with the
        # OpenAI-compat adapter, we only emit it when the named tool is
        # actually exposed — otherwise the API would 400.
        if forced_tool_name is not None and wire_tools:
            tool_names = {t.get("name") for t in wire_tools}
            if forced_tool_name in tool_names:
                payload["tool_choice"] = {
                    "type": "tool",
                    "name": forced_tool_name,
                }
        if extra:
            payload.update(extra)

        url = f"{self.base_url}/v1/messages"

        last_error: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self._client.post(url, headers=self._headers(), json=payload)
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = ProviderError(
                        f"{resp.status_code} from {self.friendly_name}",
                        status_code=resp.status_code,
                        raw=resp.text,
                    )
                    await asyncio.sleep(2**attempt)
                    continue
                if resp.status_code >= 400:
                    raise ProviderError(
                        f"{resp.status_code}: {resp.text}",
                        status_code=resp.status_code,
                        raw=resp.text,
                    )
                data = resp.json()
                return self._parse_response(data, fallback_model=model)
            except httpx.RequestError as e:
                last_error = ProviderError(
                    f"network error: {type(e).__name__}: {e}".rstrip(": ")
                )
                await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    async def list_models(self) -> list[str]:
        """Anthropic doesn't expose a public ``/models`` endpoint at a
        stable URL for all accounts. Return the curated set of
        production model IDs that we actively support. Callers that
        need live discovery can use ``extra={"probe":True}`` later.
        """
        return [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
        ]
