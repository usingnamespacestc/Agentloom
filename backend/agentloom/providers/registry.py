"""Adapter factory.

Given a provider kind + config, returns a concrete adapter instance.
"""

from __future__ import annotations

from typing import Any

from agentloom.providers.anthropic_native import AnthropicNativeAdapter
from agentloom.providers.base import ProviderAdapter
from agentloom.providers.openai_compat import OpenAICompatAdapter

_KINDS: dict[str, type[ProviderAdapter]] = {
    "openai_compat": OpenAICompatAdapter,
    "anthropic_native": AnthropicNativeAdapter,
}


def build_adapter(
    *,
    kind: str,
    friendly_name: str,
    base_url: str,
    api_key: str | None,
    extra_headers: dict[str, str] | None = None,
    **kwargs: Any,
) -> ProviderAdapter:
    if kind not in _KINDS:
        raise ValueError(f"Unknown provider kind: {kind!r}. Known: {sorted(_KINDS)}")
    cls = _KINDS[kind]
    return cls(
        friendly_name=friendly_name,
        base_url=base_url,
        api_key=api_key,
        extra_headers=extra_headers,
        **kwargs,
    )
