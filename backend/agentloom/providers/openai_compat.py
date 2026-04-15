"""OpenAI-compatible provider adapter.

Covers DeepSeek, Moonshot, GLM, Qwen, Volcengine ark, Ollama, LM Studio,
OpenRouter, and any other endpoint that speaks the OpenAI Chat Completions
shape.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from agentloom.providers.base import ProviderAdapter, ProviderError
from agentloom.providers.types import (
    AssistantMessage,
    ChatResponse,
    FinishReason,
    Message,
    TokenUsage,
    ToolDefinition,
    ToolUse,
)

_MAX_RETRIES = 3


class OpenAICompatAdapter(ProviderAdapter):
    """HTTP adapter that speaks ``POST /chat/completions`` (OpenAI shape)."""

    provider_kind = "openai_compat"

    def __init__(
        self,
        *,
        friendly_name: str,
        base_url: str,
        api_key: str | None,
        extra_headers: dict[str, str] | None = None,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(friendly_name=friendly_name, base_url=base_url, api_key=api_key)
        self._extra_headers = extra_headers or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ helpers

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self._extra_headers)
        return headers

    @staticmethod
    def _to_wire_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert our typed messages to the raw wire shape.

        **Order is preserved exactly.** Index `i` in the input maps to
        index `i` in the output. This is the KV-cache contract.
        """
        wire: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                wire.append({"role": "system", "content": m.content})
            elif m.role == "user":
                wire.append({"role": "user", "content": m.content})
            elif m.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.content or ""}
                if m.tool_uses:
                    entry["tool_calls"] = [
                        {
                            "id": tu.id,
                            "type": "function",
                            "function": {
                                "name": tu.name,
                                "arguments": json.dumps(tu.arguments, ensure_ascii=False),
                            },
                        }
                        for tu in m.tool_uses
                    ]
                wire.append(entry)
            elif m.role == "tool":
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": m.tool_use_id,
                        "content": m.content,
                    }
                )
            else:  # pragma: no cover
                raise ValueError(f"Unknown message role: {m!r}")
        return wire

    @staticmethod
    def _to_wire_tools(tools: list[ToolDefinition] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]

    @staticmethod
    def _parse_response(raw: dict[str, Any], fallback_model: str) -> ChatResponse:
        choices = raw.get("choices") or []
        if not choices:
            raise ProviderError("Response has no choices", raw=raw)
        choice = choices[0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content") or ""

        # DeepSeek-style reasoning_content / Volcengine thinking_content
        extras: dict[str, Any] = {}
        reasoning_content = (
            msg.get("reasoning_content")
            or msg.get("thinking_content")
            or ""
        )
        if reasoning_content:
            extras["thinking"] = reasoning_content

        tool_uses: list[ToolUse] = []
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {}) or {}
            args_raw = func.get("arguments")
            args: dict[str, Any]
            if isinstance(args_raw, dict):
                args = args_raw
            elif isinstance(args_raw, str) and args_raw:
                try:
                    args = json.loads(args_raw)
                except json.JSONDecodeError:
                    args = {"_raw": args_raw}
            else:
                args = {}
            tool_uses.append(
                ToolUse(id=tc.get("id") or "", name=func.get("name") or "", arguments=args)
            )

        raw_finish = choice.get("finish_reason") or "stop"
        finish_map: dict[str, FinishReason] = {
            "stop": "stop",
            "length": "length",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "content_filter",
        }
        finish: FinishReason = finish_map.get(raw_finish, "unknown")

        usage_raw = raw.get("usage") or {}
        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        completion_details = usage_raw.get("completion_tokens_details") or {}
        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
        )

        return ChatResponse(
            model=raw.get("model") or fallback_model,
            message=AssistantMessage(content=content, tool_uses=tool_uses, extras=extras),
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
        temperature: float = 0.0,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_wire_messages(messages),
            "temperature": temperature,
        }
        wire_tools = self._to_wire_tools(tools)
        if wire_tools is not None:
            payload["tools"] = wire_tools
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if extra:
            payload.update(extra)

        url = f"{self.base_url}/chat/completions"

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
                # ``str(e)`` is empty for many httpx exceptions
                # (ReadTimeout, ConnectTimeout, ...), which strips the
                # only signal you have for diagnosing a "network error: "
                # in the UI. Keep the exception class name.
                last_error = ProviderError(
                    f"network error: {type(e).__name__}: {e}".rstrip(": ")
                )
                await asyncio.sleep(2**attempt)
        assert last_error is not None
        raise last_error

    async def list_models(self) -> list[str]:
        try:
            resp = await self._client.get(f"{self.base_url}/models", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or data.get("models") or []
            return [x.get("id") for x in items if isinstance(x, dict) and x.get("id")]
        except Exception:
            return []
